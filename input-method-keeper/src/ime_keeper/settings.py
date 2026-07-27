from __future__ import annotations

import io
import os
import select
import shutil
import signal
import sys
import termios
import time
import textwrap
import traceback
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, TextIO, Tuple

from .config import ConfigError, default_config, load_config
from .dashboard import collect_dashboard_data, dashboard_color_enabled, display_source
from .herdr import HerdrClient
from .input_source import BackendExecutor
from .mutations import ConfirmationToken, MutationResult, MutationService


ROWS = ("Enabled", "Default action", "Default source", "Backend", "Debug logging")
ESCAPE_SEQUENCE_TIMEOUT_SECONDS = 0.1


@dataclass(frozen=True)
class ChoiceState:
    label: str
    values: Tuple[str, ...]
    index: int
    operation: str


def _log_summary(log: Any) -> str:
    if not isinstance(log, Mapping):
        return "0B/0"
    size = int(log.get("bytes", 0) or 0)
    if size >= 1024 * 1024:
        text = f"{size / (1024 * 1024):.1f}MiB"
    elif size >= 1024:
        text = f"{size / 1024:.1f}KiB"
    else:
        text = f"{size}B"
    return f"{text}/{int(log.get('segments', 0) or 0)}"


def render_settings(
    data: Mapping[str, Any],
    selected: int = 0,
    choice: Optional[ChoiceState] = None,
    confirmation: Optional[ConfirmationToken] = None,
    result_line: str = "Ready",
    color_enabled: bool = False,
    width: int = 80,
) -> str:
    del color_enabled  # v0.4 keeps the compact surface legible without color semantics.
    safe_width = max(20, width)
    config = data.get("config") if isinstance(data.get("config"), Mapping) else {}
    identity = data.get("identity")
    backend = data.get("backend") if isinstance(data.get("backend"), Mapping) else {}
    health = data.get("pane_health") if isinstance(data.get("pane_health"), Mapping) else {}
    logs = data.get("logs") if isinstance(data.get("logs"), Mapping) else {}
    diagnostics = data.get("diagnostics") if isinstance(data.get("diagnostics"), list) else []
    state = data.get("state") if isinstance(data.get("state"), Mapping) else {}
    panes = state.get("panes") if isinstance(state.get("panes"), Mapping) else {}
    values = (
        "on" if config.get("enabled") else "off",
        str(config.get("default_action", "-")),
        f"{display_source(config.get('default_input_source'))}  current={display_source(data.get('current_input_source'))}",
        "helper" if backend.get("name") == "herdr-ime-helper" else str(backend.get("name") or "-"),
        "on" if config.get("debug") else "off",
    )
    backend_health = "ok" if backend.get("healthy") else "error"
    reconcile = "due" if health.get("maintenance_due", True) else "ok"
    lines = [
        f"Input Method Keeper                         session={getattr(identity, 'label', '-')}",
        (
            f"enabled={values[0]}  action={values[1]}  backend={values[3]}"
            f"     health={backend_health}"
        ),
        "",
        "Settings",
    ]
    for index, (label, value) in enumerate(zip(ROWS, values)):
        marker = ">" if index == selected else " "
        if choice and choice.label == label:
            options = [
                f"[{item}]" if i == choice.index else item
                for i, item in enumerate(choice.values)
            ]
            value = "  ".join(options)
        lines.append(f"{marker} {label:<16}{value}")

    def append_wrapped(text: str) -> None:
        lines.extend(
            textwrap.wrap(
                text,
                width=safe_width,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [""]
        )

    lines.append("")
    append_wrapped(
        f"Pane memory  live={health.get('live', 0)} stored={health.get('stored', len(panes))} "
        f"unmatched={len(health.get('unmatched_ids', []))} "
        f"missing={len(health.get('missing_ids', []))} reconcile={reconcile}"
    )
    append_wrapped(
        f"Logs         focus={_log_summary(logs.get('focus'))} "
        f"debug={_log_summary(logs.get('debug'))}"
    )
    lines.append("")
    if confirmation is not None:
        effects = [MutationService.target_effect(confirmation)]
        if confirmation.repair_config:
            effects.append("repair invalid config")
        if confirmation.clear_records:
            effects.append(
                "remove unreadable state and focus.dirty"
                if confirmation.state_unknown
                else f"clear {confirmation.record_count or 0} pane record(s)"
            )
        append_wrapped("Confirm: " + "; ".join(effects))
        append_wrapped("Enter confirm   Esc cancel")
    elif choice:
        append_wrapped("Left/Right choose   Enter apply   Esc cancel")
    else:
        append_wrapped("Up/Down or j/k move   Enter change   r refresh   q/Esc close")
    if diagnostics:
        append_wrapped("Health: " + " | ".join(str(item) for item in diagnostics))
    append_wrapped(result_line)
    return "\n".join(line[:safe_width] for line in lines)


@dataclass
class SettingsController:
    env: Mapping[str, str]
    herdr: Any
    fixed_backend: Optional[Any] = None
    backend_factory: Callable[[Mapping[str, Any]], Any] = BackendExecutor

    def __post_init__(self) -> None:
        self.selected = 0
        self.choice: Optional[ChoiceState] = None
        self.confirmation: Optional[ConfirmationToken] = None
        self.result_line = "Ready"
        self.service = MutationService(self.env)
        self.data: Dict[str, Any] = {}
        self.backend: Optional[Any] = None

    def refresh(self) -> None:
        config_dir = Path(self.env.get("HERDR_PLUGIN_CONFIG_DIR", "."))
        try:
            config = load_config(config_dir, readonly=True)
        except ConfigError:
            config = default_config()
        self.backend = self.fixed_backend or self.backend_factory(config)
        self.data = collect_dashboard_data(self.env, self.backend, self.herdr)

    def _choice_for_row(self) -> Optional[ChoiceState]:
        config = self.data.get("config", {})
        if self.selected == 1:
            values = ("keep", "reset", "ignore")
            current = str(config.get("default_action", "keep"))
            return ChoiceState(
                ROWS[self.selected],
                values,
                values.index(current) if current in values else 0,
                "set-default-action",
            )
        if self.selected == 3:
            values = ("helper", "macism")
            backend = config.get("backend", {})
            current = "helper" if isinstance(backend, Mapping) and backend.get("name") == "herdr-ime-helper" else "macism"
            return ChoiceState(
                ROWS[self.selected], values, values.index(current), "set-backend"
            )
        return None

    def _operation(self) -> Tuple[str, Optional[str]]:
        config = self.data.get("config", {})
        if self.selected == 0:
            return "toggle-enabled", None
        if self.selected == 2:
            return "set-default-input-source", None
        if self.selected == 4:
            return ("debug-off" if config.get("debug") else "debug-on"), None
        raise RuntimeError("row requires a choice")

    def _apply(self, mutation: str, value: Optional[str]) -> None:
        try:
            result = self.service.apply(
                mutation, value=value, backend=self.backend, interactive=True
            )
            if result.status == "confirmation_required":
                self.confirmation = result.token
                self.result_line = result.message
                return
            self.result_line = result.message
            self.refresh()
            if mutation.startswith("set-backend-") and not self.data.get("backend", {}).get("healthy"):
                self.result_line += "; health check failed"
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            self.result_line = f"Error: {exc}"

    def handle_key(self, key: str) -> bool:
        if self.confirmation is not None:
            if key == "enter":
                try:
                    result = self.service.confirm(self.confirmation)
                    self.confirmation = None
                    self.result_line = result.message
                    self.refresh()
                    if result.mutation.startswith("set-backend-") and not self.data.get("backend", {}).get("healthy"):
                        self.result_line += "; health check failed"
                except Exception as exc:
                    traceback.print_exc(file=sys.stderr)
                    self.result_line = f"Error: {exc}"
            elif key == "escape":
                self.confirmation = None
                self.result_line = "Cancelled: no changes were made"
            return True
        if self.choice is not None:
            choice = self.choice
            if key == "left":
                self.choice = ChoiceState(
                    choice.label,
                    choice.values,
                    (choice.index - 1) % len(choice.values),
                    choice.operation,
                )
            elif key == "right":
                self.choice = ChoiceState(
                    choice.label,
                    choice.values,
                    (choice.index + 1) % len(choice.values),
                    choice.operation,
                )
            elif key == "escape":
                self.choice = None
                self.result_line = "Cancelled: no changes were made"
            elif key == "enter":
                chosen = choice.values[choice.index]
                self.choice = None
                mutation = (
                    choice.operation
                    if choice.operation == "set-default-action"
                    else f"set-backend-{chosen}"
                )
                self._apply(mutation, chosen if mutation == "set-default-action" else None)
            return True
        if key in {"up", "k"}:
            self.selected = (self.selected - 1) % len(ROWS)
        elif key in {"down", "j"}:
            self.selected = (self.selected + 1) % len(ROWS)
        elif key == "enter":
            self.choice = self._choice_for_row()
            if self.choice is None:
                mutation, value = self._operation()
                self._apply(mutation, value)
        elif key == "r":
            self.refresh()
            self.result_line = "Refreshed"
        elif key in {"q", "escape", "eof"}:
            return False
        return True


def _read_key(stream: TextIO) -> str:
    try:
        fd: Optional[int] = stream.fileno()
    except (AttributeError, io.UnsupportedOperation, OSError):
        fd = None
    if fd is None:
        char = stream.read(1)
    else:
        char = os.read(fd, 1).decode("utf-8", errors="ignore")
    if char == "":
        return "eof"
    if char in {"\r", "\n"}:
        return "enter"
    if char == "\x03":
        raise KeyboardInterrupt
    if char != "\x1b":
        return char
    try:
        readable, _, _ = select.select(
            [fd if fd is not None else stream],
            [],
            [],
            ESCAPE_SEQUENCE_TIMEOUT_SECONDS,
        )
    except (TypeError, ValueError, OSError):
        readable = []
    if not readable:
        return "escape"
    if fd is None:
        suffix = stream.read(2)
    else:
        chunks = []
        remaining = 2
        deadline = time.monotonic() + ESCAPE_SEQUENCE_TIMEOUT_SECONDS
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
            if remaining:
                wait = max(0.0, deadline - time.monotonic())
                readable, _, _ = select.select([fd], [], [], wait)
                if not readable:
                    break
        suffix = b"".join(chunks).decode("utf-8", errors="ignore")
    return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(suffix, "unknown")


def run_settings(
    env: Mapping[str, str],
    backend: Optional[Any] = None,
    herdr: Optional[Any] = None,
    once: bool = False,
    color_mode: str = "auto",
    input_stream: Optional[TextIO] = None,
    output: Optional[TextIO] = None,
) -> int:
    input_stream = input_stream or sys.stdin
    output = output or sys.stdout
    controller = SettingsController(env, herdr or HerdrClient(env), fixed_backend=backend)
    controller.refresh()  # Must succeed before terminal mode changes.
    width = shutil.get_terminal_size((80, 22)).columns
    color_enabled = dashboard_color_enabled(env, color_mode)
    if once:
        print(render_settings(controller.data, width=width, color_enabled=color_enabled), file=output)
        return 0
    if not input_stream.isatty():
        print("settings requires a terminal (use --once for plain output)", file=sys.stderr)
        return 2

    fd = input_stream.fileno()
    original = termios.tcgetattr(fd)
    previous_handlers = {}

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, stop)
        tty.setcbreak(fd)
        output.write("\x1b[?25l")
        while True:
            screen = render_settings(
                controller.data,
                selected=controller.selected,
                choice=controller.choice,
                confirmation=controller.confirmation,
                result_line=controller.result_line,
                color_enabled=color_enabled,
                width=width,
            )
            output.write("\x1b[2J\x1b[H" + screen)
            output.flush()
            if not controller.handle_key(_read_key(input_stream)):
                break
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)
        output.write("\x1b[?25h\n")
        output.flush()
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return 0
