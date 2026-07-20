from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .config import ConfigError, config_path, default_config, load_config
from .herdr import HerdrClient, pane_parts
from .input_source import short_input_source
from .records import PaneRecords, StateStore, session_identity

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
    tokens = live.get("tokens") if isinstance(live, Mapping) else None
    live_status = tokens.get("ime") if isinstance(tokens, Mapping) else None
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
    state, state_diag = PaneRecords(store).snapshot()
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
    tab_diag_start = len(diagnostics)
    tabs = call_herdr_list(diagnostics, "tab_list", herdr.list_tabs)
    if not tabs and len(diagnostics) > tab_diag_start and workspaces:
        diagnostics.append("tab_list_fallback: per-workspace")
        tabs = []
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

    return {
        "config": config,
        "identity": identity,
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
