from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ._files import FileLock, run_lock_path
from .config import (
    apply_config_mutation,
    ConfigError,
    config_path,
    default_config,
    ensure_config,
    load_config,
    write_config,
)
from .dashboard import backend_status, run_dashboard
from .focus import handle_pane_focused
from .herdr import HerdrClient
from .input_source import BackendExecutor, CommandResult
from .logs import log_health
from .mutations import MutationService
from .records import (
    PaneRecords,
    RecordsUnavailable,
    SessionIdentity,
    StateStore,
    gc_sessions,
    handle_cleanup_event,
    handle_pane_moved,
    reconcile_state_policy,
    session_identity,
)
from .settings import run_settings

@dataclasses.dataclass(frozen=True)
class HerdrContext:
    env: Mapping[str, str]
    config_dir: Path
    state_dir: Path
    config: Dict[str, Any]
    identity: SessionIdentity

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, readonly_config: bool = True) -> "HerdrContext":
        actual_env = dict(os.environ if env is None else env)
        config_dir = Path(actual_env.get("HERDR_PLUGIN_CONFIG_DIR", "."))
        state_dir = Path(actual_env.get("HERDR_PLUGIN_STATE_DIR", "."))
        config = load_config(config_dir, readonly=readonly_config)
        identity = session_identity(config, actual_env)
        return cls(actual_env, config_dir, state_dir, config, identity)


def parse_event(event_name: str, event: Mapping[str, Any]) -> Dict[str, Any]:
    data = event.get("data") if isinstance(event, Mapping) else None
    if not isinstance(data, dict):
        data = {}
    if event_name == "pane.focused":
        return {
            "pane_id": data.get("pane_id"),
            "workspace_id": data.get("workspace_id"),
        }
    if event_name == "pane.closed":
        return {
            "pane_id": data.get("pane_id"),
            "workspace_id": data.get("workspace_id"),
        }
    if event_name == "tab.closed":
        return {
            "tab_id": data.get("tab_id"),
            "workspace_id": data.get("workspace_id"),
        }
    if event_name == "pane.moved":
        pane = data.get("pane") if isinstance(data.get("pane"), dict) else {}
        return {
            "previous_pane_id": data.get("previous_pane_id"),
            "previous_workspace_id": data.get("previous_workspace_id"),
            "previous_tab_id": data.get("previous_tab_id"),
            "pane": pane,
        }
    if event_name == "workspace.closed":
        return {
            "workspace_id": data.get("workspace_id"),
            "workspace": data.get("workspace") if isinstance(data.get("workspace"), dict) else None,
        }
    return {}


def event_dot_name(command_event: str) -> str:
    return command_event.replace("-", ".")


def event_from_env(env: Mapping[str, str]) -> Optional[Dict[str, Any]]:
    raw = env.get("HERDR_PLUGIN_EVENT_JSON", "")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None



def handle_event(
    command_event: str,
    env: Optional[Mapping[str, str]] = None,
    backend: Optional[Any] = None,
    herdr: Optional[Any] = None,
    event: Optional[Mapping[str, Any]] = None,
    debounce_seconds: float = 0.1,
) -> int:
    actual_env = dict(os.environ if env is None else env)
    try:
        context = HerdrContext.from_env(actual_env, readonly_config=True)
    except ConfigError:
        return 0
    store = StateStore(context.state_dir, context.identity)
    backend = backend if backend is not None else BackendExecutor(context.config)
    herdr = herdr if herdr is not None else HerdrClient(actual_env)
    event_name = event_dot_name(command_event)
    event_payload = dict(event or event_from_env(actual_env) or {})
    parsed = parse_event(event_name, event_payload)
    if command_event == "pane-focused":
        return handle_pane_focused(context, store, backend, herdr, parsed, debounce_seconds)
    if command_event == "pane-closed":
        return handle_cleanup_event(context, store, parsed, "pane")
    if command_event == "tab-closed":
        return handle_cleanup_event(context, store, parsed, "tab")
    if command_event == "pane-moved":
        return handle_pane_moved(context, store, parsed)
    if command_event == "workspace-closed":
        return handle_cleanup_event(context, store, parsed, "workspace")
    return 0

def print_status(
    env: Mapping[str, str], backend: Optional[Any] = None, herdr: Optional[Any] = None
) -> int:
    config_dir = Path(env.get("HERDR_PLUGIN_CONFIG_DIR", "."))
    state_dir = Path(env.get("HERDR_PLUGIN_STATE_DIR", "."))
    diagnostics: List[str] = []
    try:
        config = load_config(config_dir, readonly=True)
        if not config_path(config_dir).exists():
            diagnostics.append("config_missing")
    except ConfigError as exc:
        diagnostics.append(str(exc))
        config = default_config()
    identity = session_identity(config, env)
    store = StateStore(state_dir, identity)
    state, state_diag = PaneRecords(store).snapshot()
    if state_diag:
        diagnostics.append(state_diag)
    current_input_source = None
    if backend is not None:
        try:
            current_input_source = backend.current()
        except Exception as exc:
            diagnostics.append(f"backend_current_failed: {exc}")
    pane_health = None
    herdr = herdr if herdr is not None else HerdrClient(env)
    try:
        live_panes = herdr.list_panes()
        if not isinstance(live_panes, list) or any(
            not isinstance(pane, dict) or not isinstance(pane.get("pane_id"), str)
            for pane in live_panes
        ):
            raise ValueError("invalid pane list")
        audit = PaneRecords(store).audit_live_panes(
            [str(pane["pane_id"]) for pane in live_panes]
        )
        pane_health = dataclasses.asdict(audit)
    except Exception as exc:
        diagnostics.append(f"pane_list_failed: {exc}")
    backend_info = backend_status(config, backend)
    backend_info["healthy"] = current_input_source is not None
    output = {
        "enabled": config.get("enabled"),
        "debug": config.get("debug"),
        "session_label": identity.label,
        "session_key": identity.key,
        "default_action": config.get("default_action"),
        "default_input_source": config.get("default_input_source"),
        "notify_on_focus": config.get("notify_on_focus"),
        "pane_status_on_focus": config.get("pane_status_on_focus"),
        "focus_log": config.get("focus_log"),
        "focus_log_path": str(store.focus_log_path),
        "logs": log_health(store),
        "status_ttl_ms": config.get("status_ttl_ms"),
        "backend": backend_info,
        "current_input_source": current_input_source,
        "state": state,
        "pane_health": pane_health,
        "diagnostics": diagnostics,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def command_result_payload(result: Any) -> Dict[str, Any]:
    if isinstance(result, CommandResult):
        return {
            "ok": result.ok,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
    return {"ok": bool(result)}


def doctor(
    env: Mapping[str, str],
    backend: Any,
    herdr: Optional[Any] = None,
    gc_all: bool = False,
    select_self_test: bool = False,
) -> int:
    actual_env = dict(env)
    config_dir = Path(actual_env.get("HERDR_PLUGIN_CONFIG_DIR", "."))
    state_dir = Path(actual_env.get("HERDR_PLUGIN_STATE_DIR", "."))
    herdr = herdr if herdr is not None else HerdrClient(actual_env)
    with FileLock(run_lock_path(state_dir), blocking=True):
        config = ensure_config(config_dir)
        identity = session_identity(config, actual_env)
        context = HerdrContext(actual_env, config_dir, state_dir, config, identity)
        store = StateStore(state_dir, identity)
        try:
            _state, diagnostic = PaneRecords(store).repair_and_snapshot()
        except RecordsUnavailable as exc:
            diagnostic = str(exc)
        mode = reconcile_state_policy(config, store, "doctor")
        result = {
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "script_path": str(Path(__file__).resolve().parents[1] / "ime_keeper.py"),
            "config_dir": str(context.config_dir),
            "state_dir": str(context.state_dir),
            "herdr_bin_path": env.get("HERDR_BIN_PATH"),
            "herdr_socket_path": env.get("HERDR_SOCKET_PATH"),
            "herdr_pane_id": env.get("HERDR_PANE_ID"),
            "session_key": context.identity.key,
            "session_label": context.identity.label,
            "backend_executable": getattr(backend, "executable", None),
            "state_diagnostic": diagnostic,
            "policy": mode,
            "backend_current": None,
            "current_pane": None,
            "logs": log_health(store),
        }
        try:
            result["backend_current"] = backend.current()
        except Exception as exc:
            result["backend_error"] = str(exc)
        try:
            result["current_pane"] = herdr.current_pane()
        except Exception as exc:
            result["herdr_error"] = str(exc)
        try:
            doctor_live_panes = herdr.list_panes()
            if not isinstance(doctor_live_panes, list) or any(
                not isinstance(pane, dict) or not isinstance(pane.get("pane_id"), str)
                for pane in doctor_live_panes
            ):
                raise ValueError("invalid pane list")
            doctor_audit = PaneRecords(store).audit_live_panes(
                [str(pane["pane_id"]) for pane in doctor_live_panes]
            )
            result["pane_health"] = dataclasses.asdict(doctor_audit)
        except Exception as exc:
            result["pane_health_error"] = str(exc)
        if select_self_test:
            target = result.get("backend_current")
            if target:
                try:
                    select_result = backend.select(str(target))
                    result["backend_select_self_test"] = {
                        "target": target,
                        **command_result_payload(select_result),
                    }
                except Exception as exc:
                    result["backend_select_self_test"] = {
                        "target": target,
                        "ok": False,
                        "error": str(exc),
                    }
            else:
                result["backend_select_self_test"] = {
                    "target": None,
                    "ok": False,
                    "error": "skipped: backend current failed",
                }
        if gc_all:
            if mode == "keep":
                reconciliation = PaneRecords(store).reconcile_with_herdr(
                    herdr, budget_seconds=2.0
                )
                result["reconciliation"] = dataclasses.asdict(reconciliation)
            else:
                result["reconciliation"] = {
                    "completed": False,
                    "pruned_ids": [],
                    "unknown_ids": [],
                    "reason": f"policy-{mode}",
                }
            result["gc_deleted"] = gc_sessions(context.state_dir, context.identity.key)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def mutate_config(env: Mapping[str, str], mutation: str, value: Optional[str], backend: Any) -> int:
    try:
        MutationService(env).apply(mutation, value=value, backend=backend, interactive=False)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def main(
    argv: Optional[List[str]] = None,
    env: Optional[Mapping[str, str]] = None,
    backend: Optional[Any] = None,
    herdr: Optional[Any] = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    actual_env = dict(os.environ if env is None else env)
    config_for_backend = default_config()
    try:
        config_for_backend = load_config(Path(actual_env.get("HERDR_PLUGIN_CONFIG_DIR", ".")), readonly=True)
    except ConfigError:
        pass
    injected_backend = backend
    backend = backend if backend is not None else BackendExecutor(config_for_backend)
    if not argv:
        print("usage: ime-keeper <command>", file=sys.stderr)
        return 2
    command = argv[0]
    if command == "status":
        return print_status(actual_env, backend=backend, herdr=herdr)
    if command == "dashboard":
        once = False
        interval_seconds = 1.0
        color_mode = "auto"
        index = 1
        while index < len(argv):
            if argv[index] == "--once":
                once = True
                index += 1
            elif argv[index] == "--interval":
                if index + 1 >= len(argv):
                    print("usage: ime-keeper dashboard [--once] [--interval seconds]", file=sys.stderr)
                    return 2
                try:
                    interval_seconds = float(argv[index + 1])
                except ValueError:
                    print("dashboard interval must be a number", file=sys.stderr)
                    return 2
                index += 2
            elif argv[index] == "--color":
                if index + 1 >= len(argv):
                    print(
                        "usage: ime-keeper dashboard [--once] [--interval seconds] [--color auto|always|never]",
                        file=sys.stderr,
                    )
                    return 2
                color_mode = argv[index + 1]
                if color_mode not in {"auto", "always", "never"}:
                    print("dashboard color must be auto, always, or never", file=sys.stderr)
                    return 2
                index += 2
            else:
                print(f"unknown dashboard option: {argv[index]}", file=sys.stderr)
                return 2
        return run_dashboard(
            actual_env,
            backend=backend,
            herdr=herdr,
            interval_seconds=interval_seconds,
            once=once,
            color_mode=color_mode,
        )
    if command == "settings":
        once = False
        color_mode = "auto"
        index = 1
        while index < len(argv):
            if argv[index] == "--once":
                once = True
                index += 1
            elif argv[index] == "--color" and index + 1 < len(argv):
                color_mode = argv[index + 1]
                if color_mode not in {"auto", "always", "never"}:
                    print("settings color must be auto, always, or never", file=sys.stderr)
                    return 2
                index += 2
            else:
                print(f"unknown settings option: {argv[index]}", file=sys.stderr)
                return 2
        return run_settings(
            actual_env,
            backend=injected_backend,
            herdr=herdr,
            once=once,
            color_mode=color_mode,
        )
    if command == "doctor":
        flags = set(argv[1:])
        unknown_flags = sorted(flags - {"--gc-all", "--select-self-test"})
        if unknown_flags:
            print(f"unknown doctor flag: {unknown_flags[0]}", file=sys.stderr)
            return 2
        return doctor(
            actual_env,
            backend,
            herdr=herdr,
            gc_all="--gc-all" in flags,
            select_self_test="--select-self-test" in flags,
        )
    if command == "event":
        if len(argv) < 2:
            print("usage: ime-keeper event <event-name>", file=sys.stderr)
            return 2
        return handle_event(argv[1], actual_env, backend=backend, herdr=herdr)
    if command == "set-default-action":
        if len(argv) != 2:
            print("usage: ime-keeper set-default-action <keep|reset|ignore>", file=sys.stderr)
            return 2
        return mutate_config(actual_env, command, argv[1], backend)
    if command in {
        "toggle-enabled",
        "debug-on",
        "debug-off",
        "set-default-input-source",
        "set-backend-helper",
        "set-backend-macism",
    }:
        return mutate_config(actual_env, command, None, backend)
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
