#!/usr/bin/env python3
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


VALID_ACTIONS = {"keep", "reset", "ignore"}
DEBUG_LOG_MAX_BYTES = 100 * 1024 * 1024
DEBUG_LOG_NAME_RE = re.compile(r"^debug\.\d{8}T\d{12}Z\.log$")
DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "debug": False,
    "session_name": "auto",
    "default_action": "keep",
    "default_input_source": "com.apple.keylayout.ABC",
    "notify_on_focus": True,
    "pane_status_on_focus": True,
    "focus_log": True,
    "status_ttl_ms": 600000,
    "backend": {
        "name": "macism",
        "executable_candidates": [
            "/opt/homebrew/bin/macism",
            "/usr/local/bin/macism",
            "macism",
        ],
        "current_args": [],
        "select_args": ["{id}"],
    },
}


class ConfigError(Exception):
    pass


def coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


@dataclasses.dataclass(frozen=True)
class CommandResult:
    ok: bool
    stdout: str
    stderr: str
    exit_code: Optional[int] = None


@dataclasses.dataclass(frozen=True)
class SessionIdentity:
    label: str
    key: str
    socket_path_hash: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_now_for_log() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def default_config() -> Dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def macism_backend_config() -> Dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG["backend"]))


def helper_backend_config() -> Dict[str, Any]:
    return {
        "name": "herdr-ime-helper",
        "executable_candidates": [str(plugin_root() / "bin" / "herdr-ime-helper")],
        "current_args": ["current"],
        "select_args": ["select", "{id}", "--refresh", "--wait-ms", "150"],
    }


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return path.with_name(f"{path.name}.broken.{stamp}")


def timestamp_for_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def config_path(config_dir: Path) -> Path:
    return config_dir / "config.json"


def load_config(config_dir: Path, readonly: bool = True) -> Dict[str, Any]:
    path = config_path(Path(config_dir))
    if not path.exists():
        return default_config()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        if readonly:
            raise ConfigError(f"config_invalid: {exc}") from exc
        repaired = backup_path(path)
        path.rename(repaired)
        config = default_config()
        atomic_write_json(path, config)
        return config
    if not isinstance(value, dict):
        if readonly:
            raise ConfigError("config_invalid: top-level value must be an object")
        repaired = backup_path(path)
        path.rename(repaired)
        config = default_config()
        atomic_write_json(path, config)
        return config
    return merge_config(value)


def merge_config(value: Mapping[str, Any]) -> Dict[str, Any]:
    config = default_config()
    for key, item in value.items():
        if key == "backend" and isinstance(item, dict):
            backend = dict(config["backend"])
            backend.update(item)
            config["backend"] = backend
        else:
            config[key] = item
    action = str(config.get("default_action", "keep"))
    if action not in VALID_ACTIONS:
        config["default_action"] = "keep"
    config["enabled"] = coerce_bool(config.get("enabled"), bool(DEFAULT_CONFIG["enabled"]))
    config["debug"] = coerce_bool(config.get("debug"), bool(DEFAULT_CONFIG["debug"]))
    config["notify_on_focus"] = coerce_bool(
        config.get("notify_on_focus"), bool(DEFAULT_CONFIG["notify_on_focus"])
    )
    config["pane_status_on_focus"] = coerce_bool(
        config.get("pane_status_on_focus"), bool(DEFAULT_CONFIG["pane_status_on_focus"])
    )
    config["focus_log"] = coerce_bool(config.get("focus_log"), bool(DEFAULT_CONFIG["focus_log"]))
    try:
        config["status_ttl_ms"] = max(1000, int(config.get("status_ttl_ms", 600000)))
    except (TypeError, ValueError):
        config["status_ttl_ms"] = 600000
    return config


def ensure_config(config_dir: Path) -> Dict[str, Any]:
    path = config_path(Path(config_dir))
    config = load_config(Path(config_dir), readonly=False)
    if not path.exists():
        atomic_write_json(path, config)
    return config


def write_config(config_dir: Path, config: Mapping[str, Any]) -> None:
    atomic_write_json(config_path(Path(config_dir)), config)


def socket_hash(socket_path: str) -> str:
    digest = hashlib.sha256(socket_path.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-").lower()
    return cleaned or "session"


def derive_session_label(socket_path_value: str) -> str:
    if not socket_path_value:
        return "default"
    parts = Path(socket_path_value).parts
    for index, part in enumerate(parts[:-1]):
        if part == "sessions" and index + 1 < len(parts):
            candidate = parts[index + 1].strip()
            if candidate:
                return candidate
    if Path(socket_path_value).name == "herdr.sock" and "sessions" not in parts:
        return "default"
    return "socket"


def session_identity(config: Mapping[str, Any], env: Mapping[str, str]) -> SessionIdentity:
    raw_name = str(config.get("session_name", "auto")).strip()
    socket_path_value = env.get("HERDR_SOCKET_PATH", "").strip()
    if raw_name and raw_name != "auto":
        label = raw_name
    else:
        label = derive_session_label(socket_path_value)
    hash_value = socket_hash(socket_path_value) if socket_path_value else ""
    short_hash = hash_value.split(":", 1)[1][:12] if hash_value else ""
    key = slug(label)
    if short_hash:
        key = f"{key}-{short_hash}"
    return SessionIdentity(label=label, key=key, socket_path_hash=hash_value)


def empty_state(identity: SessionIdentity) -> Dict[str, Any]:
    return {
        "version": 1,
        "session_label": identity.label,
        "socket_path_hash": identity.socket_path_hash,
        "last_seen_at": utc_now(),
        "last_focused_pane_id": None,
        "panes": {},
    }


class StateStore:
    def __init__(self, state_dir: Path, identity: SessionIdentity):
        self.state_dir = Path(state_dir)
        self.identity = identity
        self.session_dir = self.state_dir / "sessions" / identity.key
        self.state_path = self.session_dir / "state.json"
        self.dirty_path = self.session_dir / "focus.dirty"
        self.focus_log_path = self.session_dir / "focus.log"
        self.debug_path = self.session_dir / "debug.log"
        self.debug_current_path = self.session_dir / "debug.current"
        self.focus_lock_path = self.session_dir / "focus.lock"

    def load(self, readonly: bool = True) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        if not self.state_path.exists():
            return empty_state(self.identity), None
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._validate_state(state)
            return state, None
        except Exception as exc:
            if readonly:
                return None, f"invalid_state: {exc}"
            try:
                self.session_dir.mkdir(parents=True, exist_ok=True)
                self.state_path.rename(backup_path(self.state_path))
            except OSError as rename_exc:
                return None, f"state_repair_failed: {rename_exc}"
            return empty_state(self.identity), f"repaired_invalid_state: {exc}"

    def _validate_state(self, state: Any) -> None:
        if not isinstance(state, dict):
            raise ValueError("top-level value must be an object")
        if state.get("version") != 1:
            raise ValueError("unsupported version")
        if not isinstance(state.get("panes"), dict):
            raise ValueError("panes must be an object")
        last_focused = state.get("last_focused_pane_id")
        if last_focused is not None and not isinstance(last_focused, str):
            raise ValueError("last_focused_pane_id must be a string or null")
        for pane_id, pane_state in state["panes"].items():
            if not isinstance(pane_id, str):
                raise ValueError("pane id must be a string")
            if not isinstance(pane_state, dict):
                raise ValueError(f"pane entry must be an object: {pane_id}")
            for field in ("input_source_id", "workspace_id", "tab_id", "agent", "cwd"):
                value = pane_state.get(field)
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"pane entry {field} must be a string or null: {pane_id}")

    def save(self, state: Mapping[str, Any]) -> None:
        data = dict(state)
        data["version"] = 1
        data["session_label"] = self.identity.label
        data["socket_path_hash"] = self.identity.socket_path_hash
        data["last_seen_at"] = utc_now()
        atomic_write_json(self.state_path, data)

    def clear(self) -> None:
        with contextlib_suppress_file_not_found():
            self.state_path.unlink()
        with contextlib_suppress_file_not_found():
            self.dirty_path.unlink()

    def mark_dirty(self, payload: Mapping[str, Any]) -> None:
        data = dict(payload)
        data["marked_at"] = utc_now()
        atomic_write_json(self.dirty_path, data)

    def read_dirty_mtime(self) -> Optional[float]:
        try:
            return self.dirty_path.stat().st_mtime
        except FileNotFoundError:
            return None

    def clear_dirty(self) -> None:
        with contextlib_suppress_file_not_found():
            self.dirty_path.unlink()


class contextlib_suppress_file_not_found:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return exc_type is FileNotFoundError


class FileLock:
    def __init__(self, path: Path, blocking: bool = True):
        self.path = Path(path)
        self.blocking = blocking
        self.handle = None
        self.acquired = False

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        flags = fcntl.LOCK_EX
        if not self.blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(self.handle.fileno(), flags)
            self.acquired = True
        except BlockingIOError:
            self.acquired = False
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is not None:
            if self.acquired:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        return False


def run_lock_path(state_dir: Path) -> Path:
    return Path(state_dir) / "run.lock"


class BackendExecutor:
    def __init__(self, config: Mapping[str, Any]):
        backend = config.get("backend", {})
        if not isinstance(backend, dict):
            backend = {}
        self.executable = self._resolve_executable(
            backend.get("executable_candidates", ["macism"])
        )
        self.current_args = self._coerce_args(backend.get("current_args", []), [])
        self.select_args = self._coerce_args(backend.get("select_args", ["{id}"]), ["{id}"])

    def _coerce_args(self, value: Any, default: List[str]) -> List[str]:
        if value is None:
            return list(default)
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return list(default)

    def _resolve_executable(self, candidates: Any) -> str:
        if isinstance(candidates, str):
            candidates = [candidates]
        for candidate in candidates or ["macism"]:
            candidate = str(candidate)
            if "/" in candidate:
                if Path(candidate).exists():
                    return candidate
            else:
                resolved = shutil.which(candidate)
                if resolved:
                    return resolved
        return str((candidates or ["macism"])[0])

    def _run(self, args: List[str], timeout: float = 2.0) -> CommandResult:
        try:
            completed = subprocess.run(
                [self.executable] + [str(arg) for arg in args],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            return CommandResult(
                completed.returncode == 0,
                completed.stdout.strip(),
                completed.stderr.strip(),
                completed.returncode,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(False, "", str(exc), None)

    def current(self) -> str:
        result = self._run(self.current_args)
        if not result.ok:
            raise RuntimeError(result.stderr or "backend current failed")
        return result.stdout.strip()

    def select(self, input_source_id: str) -> CommandResult:
        args = [str(arg).replace("{id}", input_source_id) for arg in self.select_args]
        return self._run(args)

    def doctor(self) -> CommandResult:
        return self._run(self.current_args)


def ensure_input_source_details(backend: Any, target: str) -> Dict[str, Any]:
    details: Dict[str, Any] = {
        "target": target,
        "current": None,
        "action": "no-target",
    }
    if not target:
        return details
    current = backend.current()
    details["current"] = current
    if current == target:
        details["action"] = "already-current"
        return details
    result = backend.select(target)
    if isinstance(result, CommandResult) and not result.ok:
        raise RuntimeError(result.stderr or "backend select failed")
    details["action"] = "selected"
    if isinstance(result, CommandResult):
        details["select_exit_code"] = result.exit_code
        details["select_stdout"] = result.stdout
        details["select_stderr"] = result.stderr
    return details


def ensure_input_source(backend: Any, target: str) -> str:
    return str(ensure_input_source_details(backend, target)["action"])


class HerdrClient:
    def __init__(self, env: Mapping[str, str]):
        self.env = dict(env)

    def current_pane(self) -> Optional[Dict[str, Any]]:
        socket_path_value = self.env.get("HERDR_SOCKET_PATH", "")
        if socket_path_value:
            pane = self._current_pane_socket(socket_path_value)
            if pane:
                return pane
        return self._current_pane_cli()

    def _current_pane_socket(self, socket_path_value: str) -> Optional[Dict[str, Any]]:
        request = {
            "id": f"ime-keeper-{os.getpid()}-{int(time.time() * 1000)}",
            "method": "pane.current",
            "params": {},
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(socket_path_value)
                sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
                chunks = []
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
            line = b"".join(chunks).splitlines()[0]
            response = json.loads(line.decode("utf-8"))
        except Exception:
            return None
        result = response.get("result", {})
        if isinstance(result, dict):
            pane = result.get("pane")
            if isinstance(pane, dict):
                return pane
        return None

    def _current_pane_cli(self) -> Optional[Dict[str, Any]]:
        herdr_bin = self._herdr_bin()
        if not herdr_bin:
            return None
        child_env = dict(os.environ)
        child_env.update(self.env)
        child_env.pop("HERDR_PANE_ID", None)
        try:
            completed = subprocess.run(
                [herdr_bin, "pane", "current"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=1.0,
                check=False,
                env=child_env,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return None
        result = response.get("result", {})
        pane = result.get("pane") if isinstance(result, dict) else None
        return pane if isinstance(pane, dict) else None

    def _herdr_bin(self) -> Optional[str]:
        return self.env.get("HERDR_BIN_PATH") or shutil.which("herdr")

    def _run_herdr(self, args: List[str], timeout: float = 1.0) -> CommandResult:
        herdr_bin = self._herdr_bin()
        if not herdr_bin:
            return CommandResult(False, "", "herdr executable not found", None)
        child_env = dict(os.environ)
        child_env.update(self.env)
        try:
            completed = subprocess.run(
                [herdr_bin] + args,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                env=child_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(False, "", str(exc), None)
        return CommandResult(
            completed.returncode == 0,
            completed.stdout.strip(),
            completed.stderr.strip(),
            completed.returncode,
        )

    def _run_herdr_json(self, args: List[str], timeout: float = 2.0) -> Dict[str, Any]:
        result = self._run_herdr(args, timeout=timeout)
        if not result.ok:
            raise RuntimeError(result.stderr or result.stdout or "herdr command failed")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"herdr returned invalid json: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("herdr returned non-object json")
        error = payload.get("error")
        if error:
            raise RuntimeError(str(error))
        return payload

    def list_workspaces(self) -> List[Dict[str, Any]]:
        payload = self._run_herdr_json(["workspace", "list"], timeout=2.0)
        result = payload.get("result")
        if not isinstance(result, dict):
            return []
        workspaces = result.get("workspaces", [])
        return [item for item in workspaces if isinstance(item, dict)]

    def list_tabs(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        args = ["tab", "list"]
        if workspace_id:
            args += ["--workspace", workspace_id]
        payload = self._run_herdr_json(args, timeout=2.0)
        result = payload.get("result")
        if not isinstance(result, dict):
            return []
        tabs = result.get("tabs", [])
        return [item for item in tabs if isinstance(item, dict)]

    def list_panes(self, workspace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        args = ["pane", "list"]
        if workspace_id:
            args += ["--workspace", workspace_id]
        payload = self._run_herdr_json(args, timeout=2.0)
        result = payload.get("result")
        if not isinstance(result, dict):
            return []
        panes = result.get("panes", [])
        return [item for item in panes if isinstance(item, dict)]

    def show_notification(self, title: str, body: str) -> CommandResult:
        return self._run_herdr(
            [
                "notification",
                "show",
                title,
                "--body",
                body,
                "--position",
                "top-right",
                "--sound",
                "none",
            ],
            timeout=1.5,
        )

    def report_pane_status(self, pane_id: str, status: str, ttl_ms: int) -> CommandResult:
        return self._run_herdr(
            [
                "pane",
                "report-metadata",
                pane_id,
                "--source",
                "local.input-method-keeper",
                "--custom-status",
                status,
                "--ttl-ms",
                str(ttl_ms),
            ],
            timeout=1.5,
        )

    def doctor(self) -> CommandResult:
        return CommandResult(True, "", "")


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


def context_from_env(env: Mapping[str, str]) -> Dict[str, Any]:
    raw = env.get("HERDR_PLUGIN_CONTEXT_JSON", "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def apply_context_fallback(
    event_name: str,
    parsed: Dict[str, Any],
    env: Mapping[str, str],
) -> Dict[str, Any]:
    if event_name != "pane.focused" or parsed.get("pane_id"):
        return parsed
    context = context_from_env(env)
    focused_pane_id = context.get("focused_pane_id")
    if isinstance(focused_pane_id, str) and focused_pane_id:
        parsed["pane_id"] = focused_pane_id
    workspace_id = context.get("workspace_id")
    if not parsed.get("workspace_id") and isinstance(workspace_id, str) and workspace_id:
        parsed["workspace_id"] = workspace_id
    return parsed


def reconcile_state_policy(config: Mapping[str, Any], store: StateStore, cause: str) -> str:
    if not bool(config.get("enabled", True)):
        store.clear()
        return "disabled"
    action = str(config.get("default_action", "keep"))
    if action == "ignore":
        store.clear()
        return "ignore"
    if action == "reset":
        store.clear()
        return "reset"
    return "keep"


def timestamped_debug_log_path(store: StateStore) -> Path:
    return store.session_dir / f"debug.{timestamp_for_filename()}.log"


def read_current_debug_log_path(store: StateStore) -> Optional[Path]:
    try:
        name = store.debug_current_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not DEBUG_LOG_NAME_RE.fullmatch(name):
        return None
    return store.session_dir / name


def write_current_debug_log_path(store: StateStore, path: Path) -> None:
    atomic_write_text(store.debug_current_path, path.name + "\n")


def migrate_legacy_debug_log(store: StateStore) -> Optional[Path]:
    if not store.debug_path.exists():
        return None
    target = timestamped_debug_log_path(store)
    store.debug_path.rename(target)
    return target


def resolve_debug_log_path(store: StateStore) -> Path:
    store.session_dir.mkdir(parents=True, exist_ok=True)
    migrated_path = migrate_legacy_debug_log(store)
    current_path = read_current_debug_log_path(store)
    if current_path is None:
        current_path = migrated_path or timestamped_debug_log_path(store)
        write_current_debug_log_path(store, current_path)
    if current_path.exists() and current_path.stat().st_size > DEBUG_LOG_MAX_BYTES:
        current_path = timestamped_debug_log_path(store)
        write_current_debug_log_path(store, current_path)
    return current_path


def log_debug(store: StateStore, config: Mapping[str, Any], message: Mapping[str, Any]) -> None:
    if not bool(config.get("debug", False)):
        return
    debug_path = resolve_debug_log_path(store)
    line = json.dumps(
        {
            "timestamp": utc_now(),
            "session_label": store.identity.label,
            "session_key": store.identity.key,
            **dict(message),
        },
        ensure_ascii=False,
    )
    with debug_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def focus_debug_base(
    config: Mapping[str, Any],
    mode: str,
    pane_id: Optional[str] = None,
    previous_pane_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "event": "pane-focused",
        "mode": mode,
        "pane_id": pane_id,
        "previous_pane_id": previous_pane_id,
        "default_action": config.get("default_action"),
        "default_input_source": config.get("default_input_source"),
    }


def pane_metadata(pane: Mapping[str, Any]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    for field in ("workspace_id", "tab_id", "agent", "cwd"):
        value = pane.get(field)
        if isinstance(value, str) and value:
            metadata[field] = value
    return metadata


def short_input_source(input_source_id: Optional[str]) -> str:
    if not input_source_id:
        return "unknown"
    return str(input_source_id).rsplit(".", 1)[-1]


def previous_change_kind(
    previous_pane_id: Optional[str],
    previous_stored_input_source: Optional[str],
    previous_observed_input_source: Optional[str],
) -> str:
    if not previous_pane_id or not previous_observed_input_source:
        return "none"
    if not previous_stored_input_source:
        return "set"
    if previous_stored_input_source != previous_observed_input_source:
        return "changed"
    return "same"


def new_change_kind(select_action: Optional[str]) -> str:
    if select_action == "selected":
        return "switched"
    if select_action == "already-current":
        return "same"
    if select_action == "no-target":
        return "no-target"
    return "unknown"


def pane_marker(pane_id: Optional[str]) -> str:
    if not pane_id:
        return ""
    value = str(pane_id)
    if ":" in value:
        workspace_id, local_pane_id = value.split(":", 1)
        return f" ({local_pane_id} {workspace_id})"
    return f" ({value})"


def pane_parts(pane_id: Optional[str]) -> Tuple[str, str]:
    if not pane_id:
        return "-", "-"
    value = str(pane_id)
    if ":" in value:
        workspace_id, local_pane_id = value.split(":", 1)
        return local_pane_id or "-", workspace_id or "-"
    return value, "-"


def status_line(side: str, action: str, detail: str, pane_id: Optional[str]) -> str:
    return f"{side:<4} {action:<4}: {detail}{pane_marker(pane_id)}"


def focus_log_field(name: str, value: Optional[str]) -> str:
    text = str(value) if value else "-"
    return f"{name}={text}"


def source_transition(before: Optional[str], after: Optional[str]) -> str:
    before_short = short_input_source(before)
    after_short = short_input_source(after)
    if before_short == after_short:
        return after_short
    return f"{before_short}->{after_short}"


def previous_action_code(
    previous_pane_id: Optional[str],
    previous_stored_input_source: Optional[str],
    previous_observed_input_source: Optional[str],
) -> str:
    kind = previous_change_kind(
        previous_pane_id,
        previous_stored_input_source,
        previous_observed_input_source,
    )
    if kind == "set":
        return "INIT"
    if kind == "changed":
        return "CHNG"
    if kind == "same":
        return "SAME"
    if previous_pane_id and not previous_observed_input_source:
        return "MISS"
    return "NONE"


def current_action_code(select_action: Optional[str], reason: Optional[str]) -> str:
    if reason == "same-pane":
        return "SAME"
    kind = new_change_kind(select_action)
    if kind == "switched":
        return "SWCH"
    if kind == "same":
        return "SAME"
    if kind == "no-target":
        return "NONE"
    return "UNKN"


def focus_status_title(
    previous_pane_id: Optional[str],
    previous_stored_input_source: Optional[str],
    previous_observed_input_source: Optional[str],
) -> str:
    return previous_status_text(
        previous_pane_id,
        previous_stored_input_source,
        previous_observed_input_source,
    )


def previous_status_text(
    previous_pane_id: Optional[str],
    previous_stored_input_source: Optional[str],
    previous_observed_input_source: Optional[str],
) -> str:
    kind = previous_change_kind(
        previous_pane_id, previous_stored_input_source, previous_observed_input_source
    )
    if not previous_pane_id:
        return status_line("OLD", "NONE", "no previous pane", None)
    if not previous_observed_input_source:
        return status_line("OLD", "MISS", "not read", previous_pane_id)
    if kind == "set":
        return status_line(
            "OLD",
            "INIT",
            f"unknown -> {short_input_source(previous_observed_input_source)}",
            previous_pane_id,
        )
    if kind == "changed":
        return status_line(
            "OLD",
            "CHNG",
            (
                f"{short_input_source(previous_stored_input_source)} -> "
                f"{short_input_source(previous_observed_input_source)}"
            ),
            previous_pane_id,
        )
    return status_line(
        "OLD",
        "SAME",
        short_input_source(previous_observed_input_source),
        previous_pane_id,
    )


def current_status_text(
    pane_id: str,
    input_source_id: Optional[str],
    backend_current_before_select: Optional[str],
    select_action: Optional[str],
    reason: Optional[str],
) -> str:
    if reason == "same-pane":
        return status_line("NEW", "SAME", short_input_source(input_source_id), pane_id)
    kind = new_change_kind(select_action)
    if kind == "switched":
        return status_line(
            "NEW",
            "SWCH",
            (
                f"{short_input_source(backend_current_before_select)} -> "
                f"{short_input_source(input_source_id)}"
            ),
            pane_id,
        )
    if kind == "same":
        return status_line("NEW", "SAME", short_input_source(input_source_id), pane_id)
    if kind == "no-target":
        return status_line(
            "NEW",
            "NONE",
            short_input_source(backend_current_before_select),
            pane_id,
        )
    return status_line(
        "NEW",
        "UNKN",
        (
            f"{short_input_source(backend_current_before_select)} -> "
            f"{short_input_source(input_source_id)}"
        ),
        pane_id,
    )


def focus_status_body(
    pane_id: str,
    input_source_id: Optional[str],
    mode: str,
    default_input_source: Optional[str],
    stored_input_source: Optional[str],
    debug_enabled: bool,
    previous_pane_id: Optional[str],
    previous_stored_input_source: Optional[str],
    previous_observed_input_source: Optional[str],
    backend_current_before_select: Optional[str],
    select_action: Optional[str],
    reason: Optional[str],
) -> str:
    return (
        f"{current_status_text(pane_id, input_source_id, backend_current_before_select, select_action, reason)}"
        f" | default {short_input_source(default_input_source)}"
    )


def previous_focus_log_detail(
    previous_pane_id: Optional[str],
    previous_stored_input_source: Optional[str],
    previous_observed_input_source: Optional[str],
) -> str:
    if not previous_pane_id:
        return "no-previous"
    if not previous_observed_input_source:
        return "not-read"
    return source_transition(previous_stored_input_source, previous_observed_input_source)


def current_focus_log_detail(
    input_source_id: Optional[str],
    backend_current_before_select: Optional[str],
    select_action: Optional[str],
    reason: Optional[str],
) -> str:
    if reason == "same-pane" or select_action == "already-current":
        return short_input_source(input_source_id)
    if select_action == "no-target":
        return short_input_source(backend_current_before_select)
    return source_transition(backend_current_before_select, input_source_id)


def focus_log_line(
    store: StateStore,
    pane_id: str,
    input_source_id: Optional[str],
    mode: str,
    default_input_source: Optional[str],
    stored_input_source: Optional[str],
    previous_pane_id: Optional[str],
    previous_stored_input_source: Optional[str],
    previous_observed_input_source: Optional[str],
    backend_current_before_select: Optional[str],
    select_action: Optional[str],
    reason: Optional[str],
) -> str:
    old_pane, old_workspace = pane_parts(previous_pane_id)
    new_pane, new_workspace = pane_parts(pane_id)
    old_action = previous_action_code(
        previous_pane_id,
        previous_stored_input_source,
        previous_observed_input_source,
    )
    old_detail = previous_focus_log_detail(
        previous_pane_id,
        previous_stored_input_source,
        previous_observed_input_source,
    )
    new_action = current_action_code(select_action, reason)
    new_detail = current_focus_log_detail(
        input_source_id,
        backend_current_before_select,
        select_action,
        reason,
    )
    fields = [
        local_now_for_log(),
        focus_log_field("OLD", old_action),
        focus_log_field("OLD_IME", old_detail),
        focus_log_field("OLD_P", old_pane),
        focus_log_field("OLD_W", old_workspace),
        focus_log_field("NEW", new_action),
        focus_log_field("NEW_IME", new_detail),
        focus_log_field("NEW_P", new_pane),
        focus_log_field("NEW_W", new_workspace),
        focus_log_field("DEFAULT", short_input_source(default_input_source)),
        focus_log_field("TARGET", short_input_source(input_source_id)),
        focus_log_field("BEFORE", short_input_source(backend_current_before_select)),
        focus_log_field("STORED", short_input_source(stored_input_source)),
        focus_log_field("MODE", mode),
        focus_log_field("ACTION", select_action),
        focus_log_field("REASON", reason),
        focus_log_field("SESSION", store.identity.label),
    ]
    return " ".join(fields).rstrip()


def append_focus_log(
    store: StateStore,
    config: Mapping[str, Any],
    pane_id: str,
    input_source_id: Optional[str],
    mode: str,
    default_input_source: Optional[str],
    stored_input_source: Optional[str],
    previous_pane_id: Optional[str],
    previous_stored_input_source: Optional[str],
    previous_observed_input_source: Optional[str],
    backend_current_before_select: Optional[str],
    select_action: Optional[str],
    reason: Optional[str],
) -> Optional[str]:
    if not bool(config.get("focus_log", True)):
        return None
    try:
        store.session_dir.mkdir(parents=True, exist_ok=True)
        with store.focus_log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                focus_log_line(
                    store,
                    pane_id,
                    input_source_id,
                    mode,
                    default_input_source,
                    stored_input_source,
                    previous_pane_id,
                    previous_stored_input_source,
                    previous_observed_input_source,
                    backend_current_before_select,
                    select_action,
                    reason,
                )
                + "\n"
            )
    except OSError as exc:
        return f"focus_log_failed: {exc}"
    return None


def publish_focus_status(
    store: StateStore,
    config: Mapping[str, Any],
    herdr: Any,
    pane_id: str,
    input_source_id: Optional[str],
    mode: str,
    stored_input_source: Optional[str] = None,
    previous_pane_id: Optional[str] = None,
    previous_stored_input_source: Optional[str] = None,
    previous_observed_input_source: Optional[str] = None,
    backend_current_before_select: Optional[str] = None,
    select_action: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    status = f"IME {short_input_source(input_source_id)}"
    title = focus_status_title(
        previous_pane_id,
        previous_stored_input_source,
        previous_observed_input_source,
    )
    body = focus_status_body(
        pane_id,
        input_source_id,
        mode,
        str(config.get("default_input_source", "")),
        stored_input_source,
        bool(config.get("debug", False)),
        previous_pane_id,
        previous_stored_input_source,
        previous_observed_input_source,
        backend_current_before_select,
        select_action,
        reason,
    )
    failures = []
    focus_log_error = append_focus_log(
        store,
        config,
        pane_id,
        input_source_id,
        mode,
        str(config.get("default_input_source", "")),
        stored_input_source,
        previous_pane_id,
        previous_stored_input_source,
        previous_observed_input_source,
        backend_current_before_select,
        select_action,
        reason,
    )
    if focus_log_error:
        failures.append(focus_log_error)
    if bool(config.get("pane_status_on_focus", True)) and hasattr(herdr, "report_pane_status"):
        try:
            result = herdr.report_pane_status(pane_id, status, int(config.get("status_ttl_ms", 600000)))
            if isinstance(result, CommandResult) and not result.ok:
                failures.append(f"pane_status_failed: {result.stderr or result.stdout}")
        except Exception as exc:
            failures.append(f"pane_status_failed: {exc}")
    if bool(config.get("notify_on_focus", True)) and hasattr(herdr, "show_notification"):
        try:
            result = herdr.show_notification(title, body)
            if isinstance(result, CommandResult) and not result.ok:
                failures.append(f"notification_failed: {result.stderr or result.stdout}")
        except Exception as exc:
            failures.append(f"notification_failed: {exc}")
    if failures:
        log_debug(
            store,
            config,
            {
                "event": "focus-status",
                "pane_id": pane_id,
                "input_source_id": input_source_id,
                "status": status,
                "title": title,
                "errors": failures,
            },
        )


def current_pane_stored_source(state: Optional[Mapping[str, Any]], pane_id: str) -> Optional[str]:
    if not isinstance(state, Mapping):
        return None
    panes = state.get("panes")
    if not isinstance(panes, Mapping):
        return None
    entry = panes.get(pane_id)
    if not isinstance(entry, Mapping):
        return None
    value = entry.get("input_source_id")
    return str(value) if value else None


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
    parsed = apply_context_fallback(event_name, parse_event(event_name, event_payload), actual_env)
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


def stable_current_pane(herdr: Any, debounce_seconds: float) -> Optional[Dict[str, Any]]:
    first = herdr.current_pane()
    if not first:
        return None
    if debounce_seconds > 0:
        time.sleep(debounce_seconds)
    second = herdr.current_pane()
    if not second:
        return None
    if second.get("pane_id") != first.get("pane_id"):
        return second
    return first


def handle_pane_focused(
    context: HerdrContext,
    store: StateStore,
    backend: Any,
    herdr: Any,
    parsed: Mapping[str, Any],
    debounce_seconds: float,
) -> int:
    with FileLock(store.focus_lock_path, blocking=False) as focus_lock:
        if not focus_lock.acquired:
            payload = {"pane_id": parsed.get("pane_id")} if parsed.get("pane_id") else {}
            store.mark_dirty(payload)
            return 0
        with FileLock(run_lock_path(context.state_dir), blocking=True):
            config = load_config(context.config_dir, readonly=True)
            mode = reconcile_state_policy(config, store, "pane-focused")
            if mode in {"disabled", "ignore"}:
                store.clear_dirty()
                return 0
        deadline = time.monotonic() + 1.0
        while True:
            pane = stable_current_pane(herdr, debounce_seconds)
            if not pane:
                return 0
            stable_pane_id = pane.get("pane_id")
            if not stable_pane_id:
                return 0
            with FileLock(run_lock_path(context.state_dir), blocking=True):
                config = load_config(context.config_dir, readonly=True)
                mode = reconcile_state_policy(config, store, "pane-focused")
                if mode in {"disabled", "ignore"}:
                    log_debug(
                        store,
                        config,
                        {
                            **focus_debug_base(config, mode, stable_pane_id),
                            "reason": "policy-stop",
                        },
                    )
                    store.clear_dirty()
                    return 0
                current_again = herdr.current_pane()
                if not current_again:
                    log_debug(
                        store,
                        config,
                        {
                            **focus_debug_base(config, mode, stable_pane_id),
                            "reason": "current-pane-unavailable-before-decision",
                        },
                    )
                    store.clear_dirty()
                    return 0
                if current_again.get("pane_id") != stable_pane_id:
                    log_debug(
                        store,
                        config,
                        {
                            **focus_debug_base(config, mode, stable_pane_id),
                            "current_pane_id": current_again.get("pane_id"),
                            "reason": "focus-changed-before-decision",
                        },
                    )
                    continue
                if mode == "reset":
                    target = str(config.get("default_input_source", ""))
                    try:
                        ensure_result = ensure_input_source_details(backend, target)
                    except Exception as exc:
                        log_debug(
                            store,
                            config,
                            {
                                **focus_debug_base(config, "reset", stable_pane_id),
                                "target_input_source": target,
                                "reason": "backend-failed",
                                "error": f"backend_failed: {exc}",
                            },
                        )
                        return 0
                    log_debug(
                        store,
                        config,
                        {
                            **focus_debug_base(config, "reset", stable_pane_id),
                            "target_input_source": target,
                            "backend_current_before_select": ensure_result.get("current"),
                            "select_action": ensure_result.get("action"),
                            "select_exit_code": ensure_result.get("select_exit_code"),
                            "reason": "reset-default",
                        },
                    )
                    store.clear_dirty()
                    publish_focus_status(
                        store,
                        config,
                        herdr,
                        stable_pane_id,
                        target or ensure_result.get("current"),
                        "reset",
                        None,
                        backend_current_before_select=ensure_result.get("current"),
                        select_action=ensure_result.get("action"),
                        reason="reset-default",
                    )
                    if should_loop_again(store, herdr, stable_pane_id, deadline):
                        continue
                    return 0
                state, diagnostic = store.load(readonly=False)
                if state is None:
                    log_debug(
                        store,
                        config,
                        {
                            **focus_debug_base(config, mode, stable_pane_id),
                            "reason": "state-load-failed",
                            "error": diagnostic,
                        },
                    )
                    return 0
                if state.get("last_focused_pane_id") == stable_pane_id:
                    stored_source = current_pane_stored_source(state, stable_pane_id)
                    status_input_source = stored_source
                    try:
                        status_input_source = backend.current()
                    except Exception as exc:
                        log_debug(
                            store,
                            config,
                            {
                                **focus_debug_base(config, "keep", stable_pane_id, stable_pane_id),
                                "stored_target_input_source": stored_source,
                                "reason": "same-pane-status-current-failed",
                                "error": f"backend_current_failed: {exc}",
                            },
                        )
                    store.save(state)
                    store.clear_dirty()
                    log_debug(
                        store,
                        config,
                        {
                            **focus_debug_base(
                                config,
                                "keep",
                                stable_pane_id,
                                state.get("last_focused_pane_id"),
                            ),
                            "backend_current_before_select": status_input_source,
                            "stored_target_input_source": stored_source,
                            "reason": "same-pane",
                        },
                    )
                    publish_focus_status(
                        store,
                        config,
                        herdr,
                        stable_pane_id,
                        status_input_source,
                        "keep",
                        stored_source,
                        backend_current_before_select=status_input_source,
                        reason="same-pane",
                    )
                    if should_loop_again(store, herdr, stable_pane_id, deadline):
                        continue
                    return 0
                previous_pane_id = state.get("last_focused_pane_id")
                pending_observation = None
                previous_stored_source = None
                if previous_pane_id and previous_pane_id != stable_pane_id:
                    previous_stored_source = current_pane_stored_source(state, str(previous_pane_id))
                    try:
                        pending_observation = backend.current()
                    except Exception as exc:
                        log_debug(
                            store,
                            config,
                            {
                                **focus_debug_base(config, "keep", stable_pane_id, previous_pane_id),
                                "reason": "backend-current-failed",
                                "error": f"backend_current_failed: {exc}",
                            },
                        )
                        return 0
                    current_after_backend = herdr.current_pane()
                    if not current_after_backend:
                        log_debug(
                            store,
                            config,
                            {
                                **focus_debug_base(config, "keep", stable_pane_id, previous_pane_id),
                                "observed_previous_input_source": pending_observation,
                                "reason": "current-pane-unavailable-after-observation",
                            },
                        )
                        store.clear_dirty()
                        return 0
                    if current_after_backend.get("pane_id") != stable_pane_id:
                        log_debug(
                            store,
                            config,
                            {
                                **focus_debug_base(config, "keep", stable_pane_id, previous_pane_id),
                                "observed_previous_input_source": pending_observation,
                                "current_pane_id": current_after_backend.get("pane_id"),
                                "reason": "focus-changed-after-observation",
                            },
                        )
                        continue
                if pending_observation and previous_pane_id:
                    panes = state.setdefault("panes", {})
                    previous_entry = dict(panes.get(previous_pane_id, {}))
                    previous_entry.update(
                        {
                            "input_source_id": pending_observation,
                            "source": "observed_before_plugin_switch",
                            "updated_at": utc_now(),
                        }
                    )
                    panes[previous_pane_id] = previous_entry
                panes = state.setdefault("panes", {})
                target_entry = panes.get(stable_pane_id, {})
                target = target_entry.get("input_source_id") or config.get("default_input_source", "")
                current_before_select = herdr.current_pane()
                if not current_before_select:
                    log_debug(
                        store,
                        config,
                        {
                            **focus_debug_base(config, "keep", stable_pane_id, previous_pane_id),
                            "observed_previous_input_source": pending_observation,
                            "target_input_source": target,
                            "stored_target_input_source": target_entry.get("input_source_id"),
                            "reason": "current-pane-unavailable-before-select",
                        },
                    )
                    store.clear_dirty()
                    return 0
                if current_before_select.get("pane_id") != stable_pane_id:
                    log_debug(
                        store,
                        config,
                        {
                            **focus_debug_base(config, "keep", stable_pane_id, previous_pane_id),
                            "observed_previous_input_source": pending_observation,
                            "target_input_source": target,
                            "stored_target_input_source": target_entry.get("input_source_id"),
                            "current_pane_id": current_before_select.get("pane_id"),
                            "reason": "focus-changed-before-select",
                        },
                    )
                    continue
                try:
                    ensure_result = ensure_input_source_details(backend, str(target))
                except Exception as exc:
                    log_debug(
                        store,
                        config,
                        {
                            **focus_debug_base(config, "keep", stable_pane_id, previous_pane_id),
                            "observed_previous_input_source": pending_observation,
                            "target_input_source": target,
                            "stored_target_input_source": target_entry.get("input_source_id"),
                            "reason": "backend-select-failed",
                            "error": f"backend_select_failed: {exc}",
                        },
                    )
                    return 0
                target_state = dict(panes.get(stable_pane_id, {}))
                target_state.update(pane_metadata(pane))
                if target:
                    target_state.setdefault("input_source_id", target)
                panes[stable_pane_id] = target_state
                state["last_focused_pane_id"] = stable_pane_id
                store.save(state)
                store.clear_dirty()
                log_debug(
                    store,
                    config,
                    {
                        **focus_debug_base(config, "keep", stable_pane_id, previous_pane_id),
                        "target_input_source": target,
                        "stored_target_input_source": target_entry.get("input_source_id"),
                        "previous_stored_input_source": previous_stored_source,
                        "previous_updated_input_source": pending_observation,
                        "observed_previous_input_source": pending_observation,
                        "backend_current_before_select": ensure_result.get("current"),
                        "select_action": ensure_result.get("action"),
                        "select_exit_code": ensure_result.get("select_exit_code"),
                        "reason": "restored-target",
                    },
                )
                publish_focus_status(
                    store,
                    config,
                    herdr,
                    stable_pane_id,
                    str(target) if target else ensure_result.get("current"),
                    "keep",
                    target_entry.get("input_source_id"),
                    previous_pane_id=str(previous_pane_id) if previous_pane_id else None,
                    previous_stored_input_source=previous_stored_source,
                    previous_observed_input_source=pending_observation,
                    backend_current_before_select=ensure_result.get("current"),
                    select_action=ensure_result.get("action"),
                    reason="restored-target",
                )
                if should_loop_again(store, herdr, stable_pane_id, deadline):
                    continue
                return 0


def should_loop_again(store: StateStore, herdr: Any, stable_pane_id: str, deadline: float) -> bool:
    if time.monotonic() >= deadline:
        return False
    dirty = store.read_dirty_mtime() is not None
    pane = herdr.current_pane()
    changed = bool(pane and pane.get("pane_id") != stable_pane_id)
    return dirty or changed


def handle_cleanup_event(
    context: HerdrContext,
    store: StateStore,
    parsed: Mapping[str, Any],
    cleanup_kind: str,
) -> int:
    with FileLock(run_lock_path(context.state_dir), blocking=True):
        config = load_config(context.config_dir, readonly=True)
        mode = reconcile_state_policy(config, store, f"{cleanup_kind}-closed")
        if mode != "keep":
            return 0
        state, diagnostic = store.load(readonly=False)
        if state is None:
            log_debug(store, config, {"event": f"{cleanup_kind}-closed", "error": diagnostic})
            return 0
        panes = state.setdefault("panes", {})
        removed_ids: List[str] = []
        cleared_last_focused = False
        if cleanup_kind == "pane":
            pane_id = parsed.get("pane_id")
            if not pane_id:
                log_debug(
                    store,
                    config,
                    {
                        "event": f"{cleanup_kind}-closed",
                        "mode": mode,
                        "reason": "missing-pane-id",
                    },
                )
                return 0
            if panes.pop(str(pane_id), None) is not None:
                removed_ids.append(str(pane_id))
            if state.get("last_focused_pane_id") == pane_id:
                state["last_focused_pane_id"] = None
                cleared_last_focused = True
        elif cleanup_kind == "tab":
            tab_id = parsed.get("tab_id")
            if not tab_id:
                log_debug(
                    store,
                    config,
                    {
                        "event": f"{cleanup_kind}-closed",
                        "mode": mode,
                        "reason": "missing-tab-id",
                    },
                )
                return 0
            remove_ids = [
                pane_id
                for pane_id, pane_state in panes.items()
                if isinstance(pane_state, dict) and pane_state.get("tab_id") == tab_id
            ]
            last_entry = panes.get(state.get("last_focused_pane_id"))
            for pane_id in remove_ids:
                panes.pop(pane_id, None)
            removed_ids = [str(pane_id) for pane_id in remove_ids]
            if isinstance(last_entry, dict) and last_entry.get("tab_id") == tab_id:
                state["last_focused_pane_id"] = None
                cleared_last_focused = True
        elif cleanup_kind == "workspace":
            workspace_id = parsed.get("workspace_id")
            if not workspace_id:
                log_debug(
                    store,
                    config,
                    {
                        "event": f"{cleanup_kind}-closed",
                        "mode": mode,
                        "reason": "missing-workspace-id",
                    },
                )
                return 0
            remove_ids = [
                pane_id
                for pane_id, pane_state in panes.items()
                if isinstance(pane_state, dict) and pane_state.get("workspace_id") == workspace_id
            ]
            last_entry = panes.get(state.get("last_focused_pane_id"))
            for pane_id in remove_ids:
                panes.pop(pane_id, None)
            removed_ids = [str(pane_id) for pane_id in remove_ids]
            if isinstance(last_entry, dict) and last_entry.get("workspace_id") == workspace_id:
                state["last_focused_pane_id"] = None
                cleared_last_focused = True
        store.save(state)
        log_debug(
            store,
            config,
            {
                "event": f"{cleanup_kind}-closed",
                "mode": mode,
                "pane_id": parsed.get("pane_id"),
                "tab_id": parsed.get("tab_id"),
                "workspace_id": parsed.get("workspace_id"),
                "removed_pane_ids": removed_ids,
                "cleared_last_focused": cleared_last_focused,
                "reason": "cleanup",
            },
        )
        return 0


def handle_pane_moved(context: HerdrContext, store: StateStore, parsed: Mapping[str, Any]) -> int:
    with FileLock(run_lock_path(context.state_dir), blocking=True):
        config = load_config(context.config_dir, readonly=True)
        mode = reconcile_state_policy(config, store, "pane-moved")
        if mode != "keep":
            return 0
        state, diagnostic = store.load(readonly=False)
        if state is None:
            log_debug(store, config, {"event": "pane-moved", "error": diagnostic})
            return 0
        old_id = parsed.get("previous_pane_id")
        previous_workspace_id = parsed.get("previous_workspace_id")
        previous_tab_id = parsed.get("previous_tab_id")
        pane = parsed.get("pane") if isinstance(parsed.get("pane"), dict) else {}
        new_id = pane.get("pane_id")
        new_workspace_id = pane.get("workspace_id")
        new_tab_id = pane.get("tab_id")
        if not all(
            isinstance(value, str) and value
            for value in (
                old_id,
                previous_workspace_id,
                previous_tab_id,
                new_id,
                new_workspace_id,
                new_tab_id,
            )
        ):
            log_debug(
                store,
                config,
                {
                    "event": "pane-moved",
                    "mode": mode,
                    "old": old_id,
                    "new": new_id,
                    "previous_workspace_id": previous_workspace_id,
                    "previous_tab_id": previous_tab_id,
                    "new_workspace_id": new_workspace_id,
                    "new_tab_id": new_tab_id,
                    "reason": "missing-move-metadata",
                },
            )
            return 0
        panes = state.setdefault("panes", {})
        migrated = False
        if old_id in panes:
            entry = dict(panes.pop(old_id))
            entry.update(pane_metadata(pane))
            panes[str(new_id)] = entry
            migrated = True
        updated_last_focused = False
        if state.get("last_focused_pane_id") == old_id:
            state["last_focused_pane_id"] = new_id
            updated_last_focused = True
        store.save(state)
        log_debug(
            store,
            config,
            {
                "event": "pane-moved",
                "mode": mode,
                "old": old_id,
                "new": new_id,
                "migrated": migrated,
                "updated_last_focused": updated_last_focused,
                "pane_metadata": pane_metadata(pane),
                "reason": "moved",
            },
        )
        return 0


def backend_status(config: Mapping[str, Any], backend: Optional[Any]) -> Dict[str, Any]:
    backend_config = config.get("backend")
    if not isinstance(backend_config, dict):
        backend_config = {}
    return {
        "name": backend_config.get("name"),
        "executable": getattr(backend, "executable", None),
        "current_args": backend_config.get("current_args"),
        "select_args": backend_config.get("select_args"),
    }


def call_herdr_list(
    diagnostics: List[str],
    label: str,
    func: Any,
    *args: Any,
) -> List[Dict[str, Any]]:
    try:
        value = func(*args)
    except Exception as exc:
        diagnostics.append(f"{label}_failed: {exc}")
        return []
    if not isinstance(value, list):
        diagnostics.append(f"{label}_invalid")
        return []
    return [item for item in value if isinstance(item, dict)]


def display_source(input_source_id: Optional[str]) -> str:
    return short_input_source(input_source_id)


def display_bool(value: Any) -> str:
    return "on" if bool(value) else "off"


class DashboardStyle:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, text: str, code: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def stored(self, text: str) -> str:
        return self.muted(text)

    def marker(self, text: str) -> str:
        return self._wrap(text, "1;34")

    def live(self, text: str) -> str:
        return self._wrap(text, "32")

    def muted(self, text: str) -> str:
        return self._wrap(text, "2")


def dashboard_color_enabled(env: Mapping[str, str], mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    if env.get("NO_COLOR"):
        return False
    if env.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


def dashboard_plain_setting(label: str, value: str) -> str:
    return f"{label}={value}"


def dashboard_marker(active: bool, style: DashboardStyle) -> str:
    return style.marker(">") if active else " "


def dashboard_pane_label(value: Any, active: bool, style: DashboardStyle) -> str:
    text = str(value)
    if not active:
        return text
    return f"{style.marker('[')}{text}{style.marker(']')}"


def dashboard_paren_label(value: Any) -> str:
    return f"({value})"


def tab_sort_key(tab_id: str, tab_by_id: Mapping[str, Mapping[str, Any]]) -> Tuple[int, str]:
    tab = tab_by_id.get(tab_id, {})
    number = tab.get("number")
    return (int(number) if isinstance(number, int) else 999999, tab_id)


def workspace_sort_key(
    workspace_id: str,
    workspace_by_id: Mapping[str, Mapping[str, Any]],
) -> Tuple[int, str]:
    workspace = workspace_by_id.get(workspace_id, {})
    number = workspace.get("number")
    return (int(number) if isinstance(number, int) else 999999, workspace_id)


def pane_sort_key(pane_id: str) -> Tuple[str, str]:
    local_pane_id, workspace_id = pane_parts(pane_id)
    return (workspace_id, local_pane_id)


def dashboard_pane_status(
    live: Optional[Mapping[str, Any]],
    pane_state: Mapping[str, Any],
    style: DashboardStyle,
) -> str:
    live_status = live.get("custom_status") if isinstance(live, Mapping) else None
    if live_status:
        return style.live(str(live_status))
    stored = pane_state.get("input_source_id")
    if stored:
        return style.stored(f"stored {display_source(stored)}")
    return style.muted("-")


def dashboard_pane_token(
    pane_id: str,
    live: Optional[Mapping[str, Any]],
    pane_state: Mapping[str, Any],
    style: DashboardStyle,
) -> str:
    local_pane_id, _workspace_id = pane_parts(pane_id)
    focused = bool(isinstance(live, Mapping) and live.get("focused"))
    pane_label = dashboard_pane_label(local_pane_id, focused, style)
    text = f"{pane_label}={dashboard_pane_status(live, pane_state, style)}"
    if focused:
        return f"{dashboard_marker(True, style)}{text}"
    return text


def collect_dashboard_data(
    env: Mapping[str, str],
    backend: Optional[Any],
    herdr: Any,
) -> Dict[str, Any]:
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
    state, state_diag = store.load(readonly=True)
    if state_diag:
        diagnostics.append(state_diag)
    current_input_source = None
    if backend is not None:
        try:
            current_input_source = backend.current()
        except Exception as exc:
            diagnostics.append(f"backend_current_failed: {exc}")

    workspaces = call_herdr_list(diagnostics, "workspace_list", herdr.list_workspaces)
    panes = call_herdr_list(diagnostics, "pane_list", herdr.list_panes)
    tabs: List[Dict[str, Any]] = []
    if workspaces:
        for workspace in workspaces:
            workspace_id = workspace.get("workspace_id")
            if workspace_id:
                tabs.extend(
                    call_herdr_list(
                        diagnostics,
                        f"tab_list:{workspace_id}",
                        herdr.list_tabs,
                        str(workspace_id),
                    )
                )
    else:
        tabs = call_herdr_list(diagnostics, "tab_list", herdr.list_tabs)

    return {
        "config": config,
        "identity": identity,
        "store": store,
        "state": state if isinstance(state, dict) else None,
        "current_input_source": current_input_source,
        "backend": backend_status(config, backend),
        "workspaces": workspaces,
        "tabs": tabs,
        "panes": panes,
        "diagnostics": diagnostics,
    }


def render_dashboard(data: Mapping[str, Any], color_enabled: bool = False) -> str:
    style = DashboardStyle(color_enabled)
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    state = data.get("state") if isinstance(data.get("state"), dict) else None
    identity = data.get("identity")
    backend = data.get("backend") if isinstance(data.get("backend"), dict) else {}
    diagnostics = data.get("diagnostics") if isinstance(data.get("diagnostics"), list) else []
    workspaces = data.get("workspaces") if isinstance(data.get("workspaces"), list) else []
    tabs = data.get("tabs") if isinstance(data.get("tabs"), list) else []
    panes = data.get("panes") if isinstance(data.get("panes"), list) else []
    state_panes = state.get("panes", {}) if isinstance(state, dict) and isinstance(state.get("panes"), dict) else {}

    workspace_by_id = {
        str(workspace.get("workspace_id")): workspace
        for workspace in workspaces
        if workspace.get("workspace_id")
    }
    tab_by_id = {
        str(tab.get("tab_id")): tab
        for tab in tabs
        if tab.get("tab_id")
    }
    pane_by_id = {
        str(pane.get("pane_id")): pane
        for pane in panes
        if pane.get("pane_id")
    }

    workspace_ids = set(workspace_by_id)
    tab_ids_by_workspace: Dict[str, set] = {}
    pane_ids_by_tab: Dict[str, set] = {}
    for tab_id, tab in tab_by_id.items():
        workspace_id = str(tab.get("workspace_id") or tab_id.split(":", 1)[0])
        workspace_ids.add(workspace_id)
        tab_ids_by_workspace.setdefault(workspace_id, set()).add(tab_id)
    for pane_id, pane in pane_by_id.items():
        workspace_id = str(pane.get("workspace_id") or pane_id.split(":", 1)[0])
        tab_id = str(pane.get("tab_id") or f"{workspace_id}:unknown")
        workspace_ids.add(workspace_id)
        tab_ids_by_workspace.setdefault(workspace_id, set()).add(tab_id)
        pane_ids_by_tab.setdefault(tab_id, set()).add(pane_id)
    for pane_id, pane_state in state_panes.items():
        if not isinstance(pane_state, dict):
            continue
        workspace_id = str(pane_state.get("workspace_id") or str(pane_id).split(":", 1)[0])
        tab_id = str(pane_state.get("tab_id") or f"{workspace_id}:state")
        workspace_ids.add(workspace_id)
        tab_ids_by_workspace.setdefault(workspace_id, set()).add(tab_id)
        pane_ids_by_tab.setdefault(tab_id, set()).add(str(pane_id))

    now = datetime.now().astimezone().strftime("%H:%M:%S")
    session_label = getattr(identity, "label", "-")
    live_count = len(panes)
    state_count = len(state_panes)
    lines = [
        (
            f"IME Keeper {now} "
            f"{dashboard_plain_setting('session', session_label)} "
            f"{dashboard_plain_setting('enabled', display_bool(config.get('enabled')))} "
            f"{dashboard_plain_setting('debug', display_bool(config.get('debug')))} "
            f"{dashboard_plain_setting('action', str(config.get('default_action', '-')))}"
        ),
        (
            f"{dashboard_plain_setting('default', display_source(config.get('default_input_source')))} "
            f"{dashboard_plain_setting('current', display_source(data.get('current_input_source')))} "
            f"{dashboard_plain_setting('backend', str(backend.get('name') or '-'))} "
            f"{dashboard_plain_setting('panes', f'live:{live_count}/state:{state_count}')}"
        ),
    ]
    if diagnostics:
        lines.append("diagnostics=" + " | ".join(str(item) for item in diagnostics))
    lines.append("")

    if not workspace_ids:
        lines.append("  (no live or stored panes)")
    for workspace_id in sorted(workspace_ids, key=lambda item: workspace_sort_key(item, workspace_by_id)):
        workspace = workspace_by_id.get(workspace_id, {})
        workspace_label = workspace.get("label") or workspace_id
        workspace_focused = bool(workspace.get("focused"))
        workspace_marker = dashboard_marker(workspace_focused, style)
        workspace_number = workspace.get("number", "-")
        active_tab_id = workspace.get("active_tab_id", "-")
        lines.append(
            f"{workspace_marker} workspace {workspace_number} {dashboard_paren_label(workspace_label)}"
        )
        tab_ids = tab_ids_by_workspace.get(workspace_id, set())
        if not tab_ids:
            lines.append("    (no panes)")
            continue
        for tab_id in sorted(tab_ids, key=lambda item: tab_sort_key(item, tab_by_id)):
            tab = tab_by_id.get(tab_id, {})
            tab_focused = bool(tab.get("focused") or active_tab_id == tab_id)
            tab_marker = dashboard_marker(tab_focused, style)
            tab_label = tab.get("label") or tab_id.rsplit(":", 1)[-1]
            tab_number = tab.get("number", "-")
            pane_ids = pane_ids_by_tab.get(tab_id, set())
            if not pane_ids:
                continue
            pane_tokens: List[str] = []
            for pane_id in sorted(pane_ids, key=pane_sort_key):
                live = pane_by_id.get(pane_id)
                pane_state = state_panes.get(pane_id)
                if not isinstance(pane_state, Mapping):
                    pane_state = {}
                pane_tokens.append(dashboard_pane_token(pane_id, live, pane_state, style))
            tab_label_text = dashboard_paren_label(tab_label)
            tab_head = f"  {tab_marker} tab {tab_number} {tab_label_text}: "
            lines.append(tab_head + ", ".join(pane_tokens))
    lines.append("")
    lines.append(style.muted("Ctrl-C to exit"))
    return "\n".join(lines)


def run_dashboard(
    env: Mapping[str, str],
    backend: Optional[Any],
    herdr: Optional[Any] = None,
    interval_seconds: float = 1.0,
    once: bool = False,
    color_mode: str = "auto",
) -> int:
    herdr = herdr if herdr is not None else HerdrClient(env)
    color_enabled = dashboard_color_enabled(env, color_mode)
    while True:
        data = collect_dashboard_data(env, backend, herdr)
        output = render_dashboard(data, color_enabled=color_enabled)
        if once:
            print(output)
            return 0
        print("\033[3J\033[2J\033[H" + output, flush=True)
        time.sleep(max(0.2, interval_seconds))


def print_status(env: Mapping[str, str], backend: Optional[Any] = None) -> int:
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
    state, state_diag = store.load(readonly=True)
    if state_diag:
        diagnostics.append(state_diag)
    current_input_source = None
    if backend is not None:
        try:
            current_input_source = backend.current()
        except Exception as exc:
            diagnostics.append(f"backend_current_failed: {exc}")
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
        "status_ttl_ms": config.get("status_ttl_ms"),
        "backend": backend_status(config, backend),
        "current_input_source": current_input_source,
        "state": state,
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
    context = HerdrContext.from_env(env, readonly_config=False)
    store = StateStore(context.state_dir, context.identity)
    herdr = herdr if herdr is not None else HerdrClient(env)
    with FileLock(run_lock_path(context.state_dir), blocking=True):
        config = ensure_config(context.config_dir)
        state, diagnostic = store.load(readonly=False)
        mode = reconcile_state_policy(config, store, "doctor")
        result = {
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "script_path": str(Path(__file__).resolve()),
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
        }
        try:
            result["backend_current"] = backend.current()
        except Exception as exc:
            result["backend_error"] = str(exc)
        try:
            result["current_pane"] = herdr.current_pane()
        except Exception as exc:
            result["herdr_error"] = str(exc)
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
            result["gc_deleted"] = gc_sessions(context.state_dir, context.identity.key)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def gc_sessions(state_dir: Path, current_key: str, days: int = 30) -> List[str]:
    sessions_dir = Path(state_dir) / "sessions"
    if not sessions_dir.exists():
        return []
    cutoff = time.time() - days * 86400
    deleted = []
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir() or session_dir.name == current_key:
            continue
        state_path = session_dir / "state.json"
        marker_time = None
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                value = state.get("last_seen_at") if isinstance(state, dict) else None
                if isinstance(value, str):
                    marker_time = datetime.fromisoformat(value).timestamp()
            except Exception:
                marker_time = None
        if marker_time is None:
            marker_time = session_dir.stat().st_mtime
        if marker_time < cutoff:
            shutil.rmtree(session_dir)
            deleted.append(session_dir.name)
    return deleted


def mutate_config(env: Mapping[str, str], mutation: str, value: Optional[str], backend: Any) -> int:
    context = HerdrContext.from_env(env, readonly_config=False)
    store = StateStore(context.state_dir, context.identity)
    with FileLock(run_lock_path(context.state_dir), blocking=True):
        config = ensure_config(context.config_dir)
        reconcile_state_policy(config, store, mutation)
        if mutation == "toggle-enabled":
            config["enabled"] = not bool(config.get("enabled", True))
            write_config(context.config_dir, config)
            store.clear()
        elif mutation == "debug-on":
            config["debug"] = True
            write_config(context.config_dir, config)
        elif mutation == "debug-off":
            config["debug"] = False
            write_config(context.config_dir, config)
        elif mutation == "set-default-action":
            if value not in VALID_ACTIONS:
                print(f"invalid default action: {value}", file=sys.stderr)
                return 2
            config["default_action"] = value
            write_config(context.config_dir, config)
            reconcile_state_policy(config, store, mutation)
        elif mutation == "set-default-input-source":
            config["default_input_source"] = backend.current()
            write_config(context.config_dir, config)
        elif mutation == "set-backend-helper":
            config["backend"] = helper_backend_config()
            write_config(context.config_dir, config)
        elif mutation == "set-backend-macism":
            config["backend"] = macism_backend_config()
            write_config(context.config_dir, config)
        else:
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
    backend = backend if backend is not None else BackendExecutor(config_for_backend)
    if not argv:
        print("usage: ime-keeper <command>", file=sys.stderr)
        return 2
    command = argv[0]
    if command == "status":
        return print_status(actual_env, backend=backend)
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
