#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterable, Optional


PLUGIN_ID = "local.input-method-keeper"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLUGIN_PATH = ROOT
REQUIRED_ACTIONS = {
    "toggle-enabled",
    "status",
    "set-default-input-source",
    "set-default-action-keep",
    "set-default-action-reset",
    "set-default-action-ignore",
    "debug-on",
    "debug-off",
    "set-backend-helper",
    "set-backend-macism",
    "doctor",
    "doctor-gc-all",
}


class SmokeFailure(Exception):
    pass


@dataclass
class Command:
    argv: list[str]
    stdout: str
    stderr: str
    returncode: int


@dataclass
class StateBackup:
    state_dir: Path
    files: dict[Path, bytes]


def log(message: str) -> None:
    print(message, flush=True)


def run(
    argv: Iterable[str],
    env: Optional[dict[str, str]] = None,
    check: bool = True,
    echo: bool = True,
) -> Command:
    argv_list = [str(arg) for arg in argv]
    if echo:
        log("$ " + " ".join(argv_list))
    completed = subprocess.run(
        argv_list,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if echo and completed.stdout.strip():
        log(completed.stdout.rstrip())
    if echo and completed.stderr.strip():
        log(completed.stderr.rstrip())
    command = Command(argv_list, completed.stdout, completed.stderr, completed.returncode)
    if check and completed.returncode != 0:
        raise SmokeFailure(f"command failed with exit {completed.returncode}: {' '.join(argv_list)}")
    return command


def parse_json(command: Command) -> dict[str, Any]:
    try:
        data = json.loads(command.stdout)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"expected JSON from {' '.join(command.argv)}: {exc}") from exc
    if not isinstance(data, dict):
        raise SmokeFailure(f"expected JSON object from {' '.join(command.argv)}")
    return data


def result_object(command: Command) -> dict[str, Any]:
    data = parse_json(command)
    result = data.get("result")
    if not isinstance(result, dict):
        raise SmokeFailure(f"missing result object from {' '.join(command.argv)}")
    return result


def herdr(
    args: Iterable[str],
    session: Optional[str] = None,
    check: bool = True,
    echo: bool = True,
) -> Command:
    argv = ["herdr"]
    if session:
        argv += ["--session", session]
    argv += list(args)
    return run(argv, check=check, echo=echo)


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise SmokeFailure(f"missing required tool on PATH: {name}")
    return path


def assert_local_files(plugin_path: Path) -> None:
    required = [
        plugin_path / "herdr-plugin.toml",
        plugin_path / "bin" / "ime-keeper",
        plugin_path / "bin" / "herdr-ime-helper",
        plugin_path / "helpers" / "herdr-ime-helper.swift",
        plugin_path / "src" / "ime_keeper.py",
    ]
    for path in required:
        if not path.exists():
            raise SmokeFailure(f"missing plugin file: {path}")
    if not os.access(plugin_path / "bin" / "ime-keeper", os.X_OK):
        raise SmokeFailure("bin/ime-keeper must be executable")
    if not os.access(plugin_path / "bin" / "herdr-ime-helper", os.X_OK):
        raise SmokeFailure("bin/herdr-ime-helper must be executable")


def plugin_list(plugin_id: str, session: Optional[str]) -> list[dict[str, Any]]:
    result = result_object(herdr(["plugin", "list", "--json"], session=session, echo=False))
    plugins = result.get("plugins")
    if not isinstance(plugins, list):
        raise SmokeFailure("plugin list did not return a plugins array")
    return [
        plugin
        for plugin in plugins
        if isinstance(plugin, dict) and plugin.get("plugin_id", plugin.get("id")) == plugin_id
    ]


def ensure_plugin_linked(plugin_id: str, plugin_path: Path, session: Optional[str], should_link: bool) -> None:
    if plugin_list(plugin_id, session):
        log(f"plugin already linked: {plugin_id}")
        return
    if not should_link:
        raise SmokeFailure(f"plugin is not linked: {plugin_id}; rerun with --link")
    herdr(["plugin", "link", str(plugin_path)], session=session)
    herdr(["plugin", "enable", plugin_id], session=session)
    if not plugin_list(plugin_id, session):
        raise SmokeFailure(f"plugin did not appear after link: {plugin_id}")


def action_ids(actions: list[Any]) -> set[str]:
    ids = set()
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_id = action.get("action_id", action.get("id"))
        if isinstance(action_id, str):
            ids.add(action_id)
    return ids


def assert_actions(plugin_id: str, session: Optional[str]) -> None:
    result = result_object(
        herdr(["plugin", "action", "list", "--plugin", plugin_id], session=session, echo=False)
    )
    actions = result.get("actions")
    if not isinstance(actions, list):
        raise SmokeFailure("plugin action list did not return an actions array")
    missing = sorted(REQUIRED_ACTIONS - action_ids(actions))
    if missing:
        raise SmokeFailure(f"missing plugin actions: {', '.join(missing)}")


def plugin_logs(plugin_id: str, session: Optional[str], limit: int = 50) -> list[dict[str, Any]]:
    result = result_object(
        herdr(
            ["plugin", "log", "list", "--plugin", plugin_id, "--limit", str(limit)],
            session=session,
            echo=False,
        )
    )
    logs = result.get("logs")
    if not isinstance(logs, list):
        raise SmokeFailure("plugin log list did not return a logs array")
    return [log for log in logs if isinstance(log, dict)]


def latest_log_start(plugin_id: str, session: Optional[str]) -> int:
    starts = [
        int(log_entry.get("started_unix_ms", 0))
        for log_entry in plugin_logs(plugin_id, session, limit=100)
        if isinstance(log_entry.get("started_unix_ms"), int)
    ]
    return max(starts) if starts else 0


def wait_for_event_after(
    plugin_id: str,
    event_name: str,
    after_unix_ms: int,
    session: Optional[str],
    timeout: float = 5.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    newest = None
    while time.monotonic() < deadline:
        candidates = [
            log_entry
            for log_entry in plugin_logs(plugin_id, session, limit=100)
            if log_entry.get("event") == event_name
            and int(log_entry.get("started_unix_ms", 0)) > after_unix_ms
        ]
        if candidates:
            newest = max(candidates, key=lambda item: int(item.get("started_unix_ms", 0)))
            if newest.get("status") != "running":
                if newest.get("exit_code") != 0:
                    raise SmokeFailure(
                        f"plugin event {event_name} failed: exit={newest.get('exit_code')} "
                        f"stdout={newest.get('stdout')!r} stderr={newest.get('stderr')!r}"
                    )
                return newest
        time.sleep(0.1)
    raise SmokeFailure(f"plugin event did not finish: {event_name}; last={newest}")


def wait_for_plugin_idle(
    plugin_id: str,
    session: Optional[str],
    timeout: float = 5.0,
    quiet_seconds: float = 0.15,
) -> None:
    deadline = time.monotonic() + timeout
    idle_since: Optional[float] = None
    while time.monotonic() < deadline:
        running = [
            log_entry
            for log_entry in plugin_logs(plugin_id, session, limit=100)
            if log_entry.get("status") == "running"
        ]
        now = time.monotonic()
        if running:
            idle_since = None
        else:
            if idle_since is None:
                idle_since = now
            if now - idle_since >= quiet_seconds:
                return
        time.sleep(0.05)
    raise SmokeFailure("plugin commands did not become idle")


def wait_for_action_log(plugin_id: str, log_id: str, session: Optional[str], timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_log = None
    while time.monotonic() < deadline:
        for log_entry in plugin_logs(plugin_id, session):
            if log_entry.get("log_id") == log_id:
                last_log = log_entry
                if log_entry.get("status") != "running":
                    if log_entry.get("exit_code") != 0:
                        raise SmokeFailure(
                            f"plugin action {log_entry.get('action_id')} failed: "
                            f"exit={log_entry.get('exit_code')} stdout={log_entry.get('stdout')!r} "
                            f"stderr={log_entry.get('stderr')!r}"
                        )
                    return log_entry
        time.sleep(0.1)
    raise SmokeFailure(f"plugin action log did not finish: {log_id}; last={last_log}")


def invoke_action(plugin_id: str, action_id: str, session: Optional[str]) -> dict[str, Any]:
    command = herdr(
        ["plugin", "action", "invoke", action_id, "--plugin", plugin_id],
        session=session,
        echo=False,
    )
    result = result_object(command)
    log_entry = result.get("log")
    if not isinstance(log_entry, dict) or not isinstance(log_entry.get("log_id"), str):
        raise SmokeFailure(f"plugin action invoke did not return a log id for {action_id}")
    finished = wait_for_action_log(plugin_id, log_entry["log_id"], session)
    log(f"action passed: {action_id}")
    return finished


def action_stdout_json(log_entry: dict[str, Any], action_id: str) -> dict[str, Any]:
    stdout = log_entry.get("stdout")
    if not isinstance(stdout, str):
        raise SmokeFailure(f"plugin action {action_id} did not return stdout")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"plugin action {action_id} did not return JSON stdout: {exc}") from exc
    if not isinstance(data, dict):
        raise SmokeFailure(f"plugin action {action_id} stdout was not a JSON object")
    return data


def state_dir_from_status(status: dict[str, Any]) -> Path:
    focus_log_path = status.get("focus_log_path")
    if isinstance(focus_log_path, str) and focus_log_path:
        path = Path(focus_log_path)
        if len(path.parents) >= 3:
            return path.parents[2]
    raise SmokeFailure("status output did not include a usable focus_log_path")


def tracked_state_files(state_dir: Path) -> list[Path]:
    sessions_dir = state_dir / "sessions"
    if not sessions_dir.exists():
        return []
    paths = []
    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir():
            continue
        for name in ("state.json", "focus.dirty"):
            path = session_dir / name
            if path.exists():
                paths.append(path)
    return paths


def backup_state(state_dir: Path) -> StateBackup:
    files = {
        path.relative_to(state_dir): path.read_bytes()
        for path in tracked_state_files(state_dir)
        if path.is_file()
    }
    log(f"state backup captured: {state_dir} ({len(files)} files)")
    return StateBackup(state_dir=state_dir, files=files)


def write_state_backup_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".restore-tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    except PermissionError:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        path.write_bytes(data)


def restore_state(backup: StateBackup) -> None:
    current_files = tracked_state_files(backup.state_dir)
    for path in current_files:
        relative = path.relative_to(backup.state_dir)
        if relative not in backup.files:
            path.unlink()
    for relative, data in backup.files.items():
        path = backup.state_dir / relative
        write_state_backup_file(path, data)
    log(f"state backup restored: {backup.state_dir} ({len(backup.files)} files)")


def assert_state_restore_writable(backup: StateBackup) -> None:
    failures = []
    for relative, data in backup.files.items():
        path = backup.state_dir / relative
        try:
            write_state_backup_file(path, data)
        except Exception as exc:
            failures.append(f"{relative}: {exc}")
    probe_dir = backup.state_dir / "sessions" / f".smoke-restore-probe-{os.getpid()}-{int(time.time() * 1000)}"
    probe_path = probe_dir / "state.json"
    try:
        write_state_backup_file(probe_path, b"{}\n")
        probe_path.unlink()
        probe_dir.rmdir()
    except Exception as exc:
        failures.append(f"{probe_path.relative_to(backup.state_dir)}: {exc}")
        try:
            probe_path.unlink()
        except FileNotFoundError:
            pass
        try:
            probe_dir.rmdir()
        except OSError:
            pass
    if failures:
        raise SmokeFailure("state restore preflight failed: " + "; ".join(failures))


def action_smoke(plugin_id: str, session: Optional[str]) -> StateBackup:
    assert_actions(plugin_id, session)
    status_log = invoke_action(plugin_id, "status", session)
    state_backup = backup_state(state_dir_from_status(action_stdout_json(status_log, "status")))
    assert_state_restore_writable(state_backup)
    invoke_action(plugin_id, "doctor", session)
    return state_backup


def plugin_config_dir(plugin_id: str, session: Optional[str]) -> Path:
    command = herdr(["plugin", "config-dir", plugin_id], session=session, echo=False)
    text = command.stdout.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return Path(text)
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, dict):
        for key in ("path", "config_dir", "dir"):
            if isinstance(result.get(key), str):
                return Path(result[key])
    if isinstance(result, str):
        return Path(result)
    raise SmokeFailure("could not determine plugin config dir")


def parse_input_source_output(output: str, command_name: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise SmokeFailure(f"{command_name} returned an empty current input source")
    return lines[-1]


def read_current_input_source() -> str:
    command = run(["macism"])
    return parse_input_source_output(command.stdout, "macism")


def select_input_source(input_source: str) -> None:
    command = run(["macism", input_source])
    current = read_current_input_source()
    if current != input_source:
        stderr_tail = "\n".join(command.stderr.splitlines()[-5:])
        raise SmokeFailure(
            f"macism selected {input_source}, but current is {current}; "
            f"stdout={command.stdout.strip()!r} stderr_tail={stderr_tail!r}"
        )


def pane_shell_capture(
    pane_id: str,
    session: Optional[str],
    shell_fragment: str,
    timeout_ms: int = 5000,
) -> Command:
    token = f"IME_KEEPER_CAPTURE_{os.getpid()}_{int(time.time() * 1000)}"
    output_path = Path("/tmp") / f"ime-keeper-capture-{token}.txt"
    wrapped = (
        f"{{ {shell_fragment}; }} > {shlex.quote(str(output_path))} 2>&1; "
        "rc=$?; "
        f"echo {token}:$rc"
    )
    herdr(["pane", "run", pane_id, wrapped], session=session, echo=False)
    wait_command = herdr(
        [
            "wait",
            "output",
            pane_id,
            "--match",
            f"^{token}:[0-9]+$",
            "--regex",
            "--timeout",
            str(timeout_ms),
        ],
        session=session,
        echo=False,
    )
    result = result_object(wait_command)
    matched = result.get("matched_line")
    if not isinstance(matched, str) or not matched.startswith(f"{token}:"):
        raise SmokeFailure(f"pane command did not report exit status: {matched!r}")
    try:
        returncode = int(matched.rsplit(":", 1)[1])
    except ValueError as exc:
        raise SmokeFailure(f"pane command reported invalid exit status: {matched!r}") from exc
    try:
        stdout = output_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        stdout = ""
    finally:
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
    return Command(["pane-shell", pane_id, shell_fragment], stdout, "", returncode)


def pane_read_current_input_source(pane_id: str, session: Optional[str]) -> str:
    command = pane_shell_capture(pane_id, session, "macism")
    if command.returncode != 0:
        raise SmokeFailure(
            f"pane macism current failed with exit {command.returncode}: "
            f"{command.stdout.strip()!r}"
        )
    return parse_input_source_output(command.stdout, "pane macism")


def pane_select_input_source(pane_id: str, session: Optional[str], input_source: str) -> None:
    command = pane_shell_capture(pane_id, session, f"macism {shlex.quote(input_source)}")
    current = pane_read_current_input_source(pane_id, session)
    if current != input_source:
        output_tail = "\n".join(command.stdout.splitlines()[-5:])
        raise SmokeFailure(
            f"pane macism selected {input_source}, but current is {current}; "
            f"exit={command.returncode} output_tail={output_tail!r}"
        )


def hitoolbox_candidates() -> list[str]:
    path = Path.home() / "Library/Preferences/com.apple.HIToolbox.plist"
    if not path.exists():
        return []
    try:
        data = plistlib.loads(path.read_bytes())
    except Exception:
        return []
    sources = data.get("AppleEnabledInputSources")
    if not isinstance(sources, list):
        return []
    candidates = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("Input Mode", "Bundle ID"):
            value = source.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)
        layout_name = source.get("KeyboardLayout Name")
        if isinstance(layout_name, str) and layout_name:
            candidates.append(f"com.apple.keylayout.{layout_name}")
    return candidates


def find_test_sources(
    source_a: Optional[str],
    source_b: Optional[str],
    read_source: Callable[[], str] = read_current_input_source,
    select_source: Callable[[str], None] = select_input_source,
) -> tuple[str, str, str]:
    original = read_source()
    first = source_a or original
    candidates = []
    if source_b:
        candidates.append(source_b)
    candidates += [
        "com.apple.keylayout.ABC",
        "com.apple.keylayout.US",
        "com.apple.inputmethod.SCIM.ITABC",
        "com.apple.inputmethod.SCIM.Shuangpin",
    ]
    candidates += hitoolbox_candidates()
    seen = set()
    failures = []
    for candidate in candidates:
        if not candidate or candidate == first or candidate in seen:
            continue
        seen.add(candidate)
        try:
            select_source(candidate)
        except SmokeFailure as exc:
            failures.append(f"{candidate}: {exc}")
            continue
        finally:
            try:
                select_source(original)
            except SmokeFailure:
                pass
        return first, candidate, original
    raise SmokeFailure(
        "could not find a second switchable input source; set "
        "HERDR_IME_KEEPER_TEST_SOURCE_B to an id that `macism <id>` can select. "
        "Tried:\n- " + "\n- ".join(failures)
    )


def workspace_id_from(command: Command) -> str:
    result = result_object(command)
    workspace = result.get("workspace")
    if isinstance(workspace, dict) and isinstance(workspace.get("workspace_id"), str):
        return workspace["workspace_id"]
    if isinstance(result.get("workspace_id"), str):
        return result["workspace_id"]
    raise SmokeFailure("could not parse workspace id")


def panes_for_workspace(workspace_id: str, session: Optional[str]) -> list[dict[str, Any]]:
    result = result_object(
        herdr(["pane", "list", "--workspace", workspace_id], session=session, echo=False)
    )
    panes = result.get("panes")
    if not isinstance(panes, list):
        raise SmokeFailure("pane list did not return a panes array")
    return [pane for pane in panes if isinstance(pane, dict)]


def current_pane(session: Optional[str]) -> dict[str, Any]:
    result = result_object(herdr(["pane", "current"], session=session, echo=False))
    pane = result.get("pane")
    if not isinstance(pane, dict):
        raise SmokeFailure("pane current did not return a pane")
    return pane


def focus_neighbor(reference_pane_id: str, direction: str, session: Optional[str]) -> None:
    herdr(
        ["pane", "focus", "--direction", direction, "--pane", reference_pane_id],
        session=session,
        echo=False,
    )


def focus_neighbor_and_wait(
    plugin_id: str,
    reference_pane_id: str,
    direction: str,
    target_pane_id: str,
    session: Optional[str],
) -> None:
    marker = latest_log_start(plugin_id, session)
    focus_neighbor(reference_pane_id, direction, session)
    wait_for_focus(target_pane_id, session)
    wait_for_event_after(plugin_id, "pane.focused", marker, session)
    wait_for_plugin_idle(plugin_id, session)


def wait_for_focus(pane_id: str, session: Optional[str], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pane = current_pane(session)
        if pane.get("pane_id") == pane_id:
            return
        time.sleep(0.1)
    raise SmokeFailure(f"pane did not become focused: {pane_id}")


def wait_for_input_source(input_source: str, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_current_input_source() == input_source:
            return
        time.sleep(0.1)
    raise SmokeFailure(f"input source did not become {input_source}; current={read_current_input_source()}")


def wait_for_pane_input_source(
    pane_id: str,
    session: Optional[str],
    input_source: str,
    timeout: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pane_read_current_input_source(pane_id, session) == input_source:
            return
        time.sleep(0.1)
    current = pane_read_current_input_source(pane_id, session)
    raise SmokeFailure(f"pane input source did not become {input_source}; current={current}")


def backup_config(config_dir: Path) -> tuple[Path, Optional[bytes]]:
    path = config_dir / "config.json"
    return path, path.read_bytes() if path.exists() else None


def restore_config(path: Path, data: Optional[bytes]) -> None:
    if data is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_config_from_pane(
    pane_id: str,
    session: Optional[str],
    config_path: Path,
    config: Optional[dict[str, Any]],
    raw_backup: Optional[bytes],
) -> None:
    token = f"IME_KEEPER_CONFIG_DONE_{int(time.time() * 1000)}"
    payload_path = Path("/tmp") / f"ime-keeper-config-{os.getpid()}-{token}.json"
    script_path = Path("/tmp") / f"ime-keeper-config-writer-{os.getpid()}.py"
    if config is None:
        if raw_backup is not None:
            payload_path.write_bytes(raw_backup)
        mode = "restore-bytes"
    else:
        payload_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        mode = "write-json"
    script = (
        "from pathlib import Path; import shutil; "
        f"target=Path({str(config_path)!r}); target.parent.mkdir(parents=True, exist_ok=True); "
    )
    if mode == "restore-bytes" or mode == "write-json":
        script += f"shutil.copyfile({str(payload_path)!r}, target); "
    else:
        script += "target.unlink(missing_ok=True); "
    if config is None and raw_backup is None:
        script = (
            "from pathlib import Path; "
            f"target=Path({str(config_path)!r}); target.unlink(missing_ok=True); "
        )
    script += f"print({token!r})"
    script_path.write_text(script, encoding="utf-8")
    command = f"python3 {shlex.quote(str(script_path))}"
    herdr(["pane", "run", pane_id, command], session=session, echo=False)
    herdr(
        ["wait", "output", pane_id, "--match", f"^{token}$", "--regex", "--timeout", "5000"],
        session=session,
        echo=False,
    )
    try:
        payload_path.unlink()
    except FileNotFoundError:
        pass
    try:
        script_path.unlink()
    except FileNotFoundError:
        pass


def write_config_for_smoke(
    config_path: Path,
    config: dict[str, Any],
    pane_id: Optional[str],
    session: Optional[str],
) -> None:
    try:
        write_config(config_path, config)
    except PermissionError:
        if not pane_id:
            raise
        write_config_from_pane(pane_id, session, config_path, config, None)


def restore_config_for_smoke(
    config_path: Path,
    raw_backup: Optional[bytes],
    pane_id: Optional[str],
    session: Optional[str],
) -> None:
    try:
        restore_config(config_path, raw_backup)
    except PermissionError:
        if not pane_id:
            raise
        write_config_from_pane(pane_id, session, config_path, None, raw_backup)


def fake_backend_script(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
from pathlib import Path
import sys

cmd = sys.argv[1]
state = Path(sys.argv[2])
if cmd == "current":
    print(state.read_text(encoding="utf-8").strip() if state.exists() else "")
elif cmd == "select":
    state.write_text(sys.argv[3], encoding="utf-8")
else:
    raise SystemExit(f"unknown fake backend command: {cmd}")
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def fake_backend_config(backend_path: Path, source_path: Path, default_source: str) -> dict[str, Any]:
    return {
        "enabled": True,
        "debug": True,
        "session_name": "auto",
        "default_action": "keep",
        "default_input_source": default_source,
        "notify_on_focus": False,
        "pane_status_on_focus": False,
        "focus_log": False,
        "backend": {
            "name": "fake-smoke",
            "executable_candidates": [str(backend_path)],
            "current_args": ["current", str(source_path)],
            "select_args": ["select", str(source_path), "{id}"],
        },
    }


def split_pane(pane_id: str, direction: str, session: Optional[str]) -> str:
    split = herdr(
        ["pane", "split", pane_id, "--direction", direction, "--cwd", "/tmp", "--focus"],
        session=session,
        echo=False,
    )
    result = result_object(split)
    pane = result.get("pane")
    if isinstance(pane, dict) and isinstance(pane.get("pane_id"), str):
        return pane["pane_id"]
    raise SmokeFailure("could not parse split pane id")


def pane_focus_e2e(
    plugin_id: str,
    session: Optional[str],
    source_a: str,
    source_b: str,
    select_source: Any,
    wait_source: Any,
    workspace_id: Optional[str] = None,
    pane_a: Optional[str] = None,
    close_workspace: bool = True,
) -> None:
    created_workspace_id: Optional[str] = None
    try:
        select_source(source_a)
        invoke_action(plugin_id, "set-default-input-source", session)
        invoke_action(plugin_id, "set-default-action-reset", session)
        invoke_action(plugin_id, "set-default-action-keep", session)

        if workspace_id is None:
            label = f"ime-keeper-smoke-{int(time.time())}"
            workspace_id = workspace_id_from(
                herdr(
                    ["workspace", "create", "--cwd", "/tmp", "--label", label, "--focus"],
                    session=session,
                    echo=False,
                )
            )
            created_workspace_id = workspace_id
        if pane_a is None:
            panes = panes_for_workspace(workspace_id, session)
            if not panes:
                raise SmokeFailure("new workspace has no pane")
            pane_a = panes[0]["pane_id"]
        pane_b = split_pane(pane_a, "right", session)
        wait_for_plugin_idle(plugin_id, session)

        focus_neighbor_and_wait(plugin_id, pane_b, "left", pane_a, session)
        wait_source(source_a)
        select_source(source_a)
        time.sleep(0.3)

        focus_neighbor_and_wait(plugin_id, pane_a, "right", pane_b, session)
        wait_source(source_a)
        select_source(source_b)
        time.sleep(0.3)

        focus_neighbor_and_wait(plugin_id, pane_b, "left", pane_a, session)
        wait_source(source_a)

        focus_neighbor_and_wait(plugin_id, pane_a, "right", pane_b, session)
        wait_source(source_b)

        log("pane focus E2E passed")
    finally:
        if close_workspace and (created_workspace_id or workspace_id):
            herdr(
                ["workspace", "close", created_workspace_id or workspace_id],
                session=session,
                check=False,
                echo=False,
            )


def real_action_e2e(
    plugin_id: str,
    session: Optional[str],
    source_a: str,
    source_b: str,
    select_source: Callable[[str], None],
    wait_source: Callable[[str], None],
    pane_a: str,
    pane_b: str,
) -> None:
    focus_neighbor_and_wait(plugin_id, pane_b, "left", pane_a, session)
    invoke_action(plugin_id, "set-default-action-reset", session)
    select_source(source_b)
    focus_neighbor_and_wait(plugin_id, pane_a, "right", pane_b, session)
    wait_source(source_a)

    invoke_action(plugin_id, "set-default-action-ignore", session)
    focus_neighbor_and_wait(plugin_id, pane_b, "left", pane_a, session)
    select_source(source_b)
    focus_neighbor_and_wait(plugin_id, pane_a, "right", pane_b, session)
    wait_source(source_b)
    log("real action E2E passed")


def full_ime_e2e(
    plugin_id: str,
    session: Optional[str],
    source_a: Optional[str],
    source_b: Optional[str],
    real_actions: bool = False,
) -> None:
    require_tool("macism")
    config_path: Optional[Path] = None
    config_backup: Optional[bytes] = None
    workspace_id: Optional[str] = None
    pane_a: Optional[str] = None
    original_source: Optional[str] = None
    try:
        label = f"ime-keeper-full-{int(time.time())}"
        workspace_id = workspace_id_from(
            herdr(
                ["workspace", "create", "--cwd", "/tmp", "--label", label, "--focus"],
                session=session,
                echo=False,
            )
        )
        panes = panes_for_workspace(workspace_id, session)
        if not panes:
            raise SmokeFailure("new workspace has no pane")
        pane_a = panes[0]["pane_id"]

        def read_pane_source() -> str:
            return pane_read_current_input_source(pane_a, session)

        def select_pane_source(input_source: str) -> None:
            pane_select_input_source(pane_a, session, input_source)

        def wait_pane_source(input_source: str) -> None:
            wait_for_pane_input_source(pane_a, session, input_source)

        source_a, source_b, original_source = find_test_sources(
            source_a,
            source_b,
            read_source=read_pane_source,
            select_source=select_pane_source,
        )
        log(f"test input sources: A={source_a} B={source_b} original={original_source}")

        config_dir = plugin_config_dir(plugin_id, session)
        config_path, config_backup = backup_config(config_dir)

        pane_focus_e2e(
            plugin_id,
            session,
            source_a,
            source_b,
            select_pane_source,
            wait_pane_source,
            workspace_id=workspace_id,
            pane_a=pane_a,
            close_workspace=False,
        )
        if real_actions:
            panes = panes_for_workspace(workspace_id, session)
            other_panes = [pane for pane in panes if pane.get("pane_id") != pane_a]
            if len(other_panes) != 1 or not isinstance(other_panes[0].get("pane_id"), str):
                raise SmokeFailure("real action E2E requires exactly two panes")
            real_action_e2e(
                plugin_id,
                session,
                source_a,
                source_b,
                select_pane_source,
                wait_pane_source,
                pane_a,
                other_panes[0]["pane_id"],
            )
        log("full input-method E2E passed")
    finally:
        if original_source and pane_a:
            try:
                pane_select_input_source(pane_a, session, original_source)
            except Exception as exc:
                log(f"warning: failed to restore original input source {original_source}: {exc}")
        if config_path:
            restore_config_for_smoke(config_path, config_backup, pane_a, session)
        if workspace_id:
            herdr(["workspace", "close", workspace_id], session=session, check=False, echo=False)


def fake_backend_e2e(plugin_id: str, session: Optional[str]) -> None:
    config_path: Optional[Path] = None
    config_backup: Optional[bytes] = None
    workspace_id: Optional[str] = None
    pane_a: Optional[str] = None
    with TemporaryDirectory(prefix="ime-keeper-fake-backend-") as temp_dir:
        temp = Path(temp_dir)
        backend_path = temp / "fake_backend.py"
        source_path = temp / "current-source.txt"
        fake_backend_script(backend_path)

        label = f"ime-keeper-fake-{int(time.time())}"
        workspace_id = workspace_id_from(
            herdr(
                ["workspace", "create", "--cwd", "/tmp", "--label", label, "--focus"],
                session=session,
                echo=False,
            )
        )
        panes = panes_for_workspace(workspace_id, session)
        if not panes:
            raise SmokeFailure("new workspace has no pane")
        pane_a = panes[0]["pane_id"]

        config_dir = plugin_config_dir(plugin_id, session)
        config_path, config_backup = backup_config(config_dir)
        source_a = "fake.source.A"
        source_b = "fake.source.B"
        write_config_for_smoke(
            config_path,
            fake_backend_config(backend_path, source_path, source_a),
            pane_a,
            session,
        )

        def select_fake(input_source: str) -> None:
            source_path.write_text(input_source, encoding="utf-8")

        def wait_fake(input_source: str) -> None:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if source_path.exists() and source_path.read_text(encoding="utf-8") == input_source:
                    return
                time.sleep(0.1)
            current = source_path.read_text(encoding="utf-8") if source_path.exists() else "<missing>"
            raise SmokeFailure(f"fake backend source did not become {input_source}; current={current}")

        try:
            pane_focus_e2e(
                plugin_id,
                session,
                source_a,
                source_b,
                select_fake,
                wait_fake,
                workspace_id=workspace_id,
                pane_a=pane_a,
                close_workspace=False,
            )
            log("fake backend Herdr E2E passed")
        finally:
            if config_path:
                restore_config_for_smoke(config_path, config_backup, pane_a, session)
            if workspace_id:
                herdr(["workspace", "close", workspace_id], session=session, check=False, echo=False)


def complex_fake_backend_e2e(plugin_id: str, session: Optional[str]) -> None:
    config_path: Optional[Path] = None
    config_backup: Optional[bytes] = None
    workspace_id: Optional[str] = None
    pane_a: Optional[str] = None
    with TemporaryDirectory(prefix="ime-keeper-complex-fake-") as temp_dir:
        temp = Path(temp_dir)
        backend_path = temp / "fake_backend.py"
        source_path = temp / "current-source.txt"
        fake_backend_script(backend_path)

        label = f"ime-keeper-complex-{int(time.time())}"
        workspace_id = workspace_id_from(
            herdr(
                ["workspace", "create", "--cwd", "/tmp", "--label", label, "--focus"],
                session=session,
                echo=False,
            )
        )
        panes = panes_for_workspace(workspace_id, session)
        if not panes:
            raise SmokeFailure("new workspace has no pane")
        pane_a = panes[0]["pane_id"]

        config_dir = plugin_config_dir(plugin_id, session)
        config_path, config_backup = backup_config(config_dir)
        source_a = "fake.source.A"
        source_b = "fake.source.B"
        source_c = "fake.source.C"
        write_config_for_smoke(
            config_path,
            fake_backend_config(backend_path, source_path, source_a),
            pane_a,
            session,
        )

        def read_fake() -> str:
            return source_path.read_text(encoding="utf-8") if source_path.exists() else ""

        def select_fake(input_source: str) -> None:
            source_path.write_text(input_source, encoding="utf-8")

        def wait_fake(input_source: str) -> None:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if read_fake() == input_source:
                    return
                time.sleep(0.1)
            raise SmokeFailure(f"fake backend source did not become {input_source}; current={read_fake()}")

        def assert_fake_stays(input_source: str, duration: float = 0.5) -> None:
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                current = read_fake()
                if current != input_source:
                    raise SmokeFailure(
                        f"fake backend source changed under ignore/disabled mode: "
                        f"expected={input_source} current={current}"
                    )
                time.sleep(0.05)

        try:
            select_fake(source_a)
            invoke_action(plugin_id, "set-default-input-source", session)
            invoke_action(plugin_id, "set-default-action-reset", session)
            invoke_action(plugin_id, "set-default-action-keep", session)

            pane_b = split_pane(pane_a, "right", session)
            pane_c = split_pane(pane_b, "right", session)
            wait_for_plugin_idle(plugin_id, session)

            focus_neighbor_and_wait(plugin_id, pane_c, "left", pane_b, session)
            focus_neighbor_and_wait(plugin_id, pane_b, "left", pane_a, session)
            wait_fake(source_a)
            select_fake(source_a)

            focus_neighbor_and_wait(plugin_id, pane_a, "right", pane_b, session)
            wait_fake(source_a)
            select_fake(source_b)

            focus_neighbor_and_wait(plugin_id, pane_b, "right", pane_c, session)
            wait_fake(source_a)
            select_fake(source_c)

            focus_neighbor_and_wait(plugin_id, pane_c, "left", pane_b, session)
            wait_fake(source_b)
            focus_neighbor_and_wait(plugin_id, pane_b, "left", pane_a, session)
            wait_fake(source_a)
            focus_neighbor_and_wait(plugin_id, pane_a, "right", pane_b, session)
            wait_fake(source_b)
            focus_neighbor_and_wait(plugin_id, pane_b, "right", pane_c, session)
            wait_fake(source_c)
            log("complex fake: three-pane keep memory passed")

            marker = latest_log_start(plugin_id, session)
            focus_neighbor(pane_c, "left", session)
            focus_neighbor(pane_b, "left", session)
            focus_neighbor(pane_a, "right", session)
            focus_neighbor(pane_b, "right", session)
            wait_for_focus(pane_c, session)
            wait_for_event_after(plugin_id, "pane.focused", marker, session)
            wait_fake(source_c)
            log("complex fake: focus storm passed")

            invoke_action(plugin_id, "set-default-action-reset", session)
            select_fake(source_c)
            focus_neighbor_and_wait(plugin_id, pane_c, "left", pane_b, session)
            wait_fake(source_a)
            focus_neighbor_and_wait(plugin_id, pane_b, "right", pane_c, session)
            wait_fake(source_a)
            log("complex fake: reset action passed")

            invoke_action(plugin_id, "set-default-action-ignore", session)
            select_fake(source_b)
            focus_neighbor_and_wait(plugin_id, pane_c, "left", pane_b, session)
            assert_fake_stays(source_b)
            focus_neighbor_and_wait(plugin_id, pane_b, "left", pane_a, session)
            assert_fake_stays(source_b)
            log("complex fake: ignore action passed")

            invoke_action(plugin_id, "set-default-action-keep", session)
            invoke_action(plugin_id, "toggle-enabled", session)
            select_fake(source_c)
            focus_neighbor_and_wait(plugin_id, pane_a, "right", pane_b, session)
            assert_fake_stays(source_c)
            invoke_action(plugin_id, "toggle-enabled", session)
            focus_neighbor_and_wait(plugin_id, pane_b, "right", pane_c, session)
            wait_fake(source_a)
            log("complex fake: disabled/enabled action passed")

            log("complex fake Herdr E2E passed")
        finally:
            if config_path:
                restore_config_for_smoke(config_path, config_backup, pane_a, session)
            if workspace_id:
                herdr(["workspace", "close", workspace_id], session=session, check=False, echo=False)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run Herdr smoke tests for input-method-keeper.")
    parser.add_argument("--plugin-id", default=PLUGIN_ID)
    parser.add_argument("--plugin-path", type=Path, default=DEFAULT_PLUGIN_PATH)
    parser.add_argument("--session", help="Run Herdr commands against a named session.")
    parser.add_argument("--link", action="store_true", help="Link and enable the local plugin if needed.")
    parser.add_argument("--full-ime", action="store_true", help="Run live pane focus/input-source E2E.")
    parser.add_argument("--fake-backend", action="store_true", help="Run live Herdr E2E with a fake input-source backend.")
    parser.add_argument("--complex-fake", action="store_true", help="Run complex live Herdr E2E with a fake backend.")
    parser.add_argument("--real-actions", action="store_true", help="With --full-ime, also test reset/ignore against real macism.")
    parser.add_argument("--source-a", default=os.environ.get("HERDR_IME_KEEPER_TEST_SOURCE_A"))
    parser.add_argument("--source-b", default=os.environ.get("HERDR_IME_KEEPER_TEST_SOURCE_B"))
    args = parser.parse_args(argv)

    state_backup: Optional[StateBackup] = None
    exit_code = 0
    try:
        if args.real_actions and not args.full_ime:
            raise SmokeFailure("--real-actions requires --full-ime")
        require_tool("herdr")
        assert_local_files(args.plugin_path)
        herdr(["--version"], session=args.session)
        ensure_plugin_linked(args.plugin_id, args.plugin_path, args.session, args.link)
        state_backup = action_smoke(args.plugin_id, args.session)
        if args.fake_backend:
            fake_backend_e2e(args.plugin_id, args.session)
        if args.complex_fake:
            complex_fake_backend_e2e(args.plugin_id, args.session)
        if args.full_ime:
            full_ime_e2e(args.plugin_id, args.session, args.source_a, args.source_b, args.real_actions)
    except SmokeFailure as exc:
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        if state_backup:
            try:
                wait_for_plugin_idle(args.plugin_id, args.session)
            except SmokeFailure as exc:
                log(f"warning: restoring state while plugin may still be active: {exc}")
            try:
                restore_state(state_backup)
            except Exception as exc:
                log(f"warning: failed to restore state backup: {exc}")
                exit_code = 1
    if exit_code:
        return exit_code
    print("SMOKE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
