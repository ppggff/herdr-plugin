from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from ._files import FileLock, atomic_write_text, timestamp_for_filename

FOCUS_LOG_MAX_BYTES = 5 * 1024 * 1024
DEBUG_LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_SEGMENTS = 3
DEBUG_LOG_NAME_RE = re.compile(r"^debug\.\d{8}T\d{12}Z\.log$")
FOCUS_ROTATED_NAME_RE = re.compile(r"^focus\.\d{8}T\d{12}Z\.log$")


def _retained(paths: list[Path], maximum: int) -> None:
    for path in sorted(paths, key=lambda item: item.name, reverse=True)[maximum:]:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _would_overflow(path: Path, line: str, maximum: int) -> bool:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    return size + len((line + "\n").encode("utf-8")) > maximum


def append_focus_line(store: Any, line: str) -> Optional[str]:
    try:
        with FileLock(_log_lock_path(store), blocking=True):
            store.session_dir.mkdir(parents=True, exist_ok=True)
            if _would_overflow(store.focus_log_path, line, FOCUS_LOG_MAX_BYTES):
                rotated = store.session_dir / f"focus.{timestamp_for_filename()}.log"
                store.focus_log_path.rename(rotated)
            with store.focus_log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            rotated_paths = [
                path
                for path in store.session_dir.glob("focus.*.log")
                if FOCUS_ROTATED_NAME_RE.fullmatch(path.name)
            ]
            _retained(rotated_paths, LOG_SEGMENTS - 1)
    except OSError as exc:
        return f"focus_log_failed: {exc}"
    return None


def append_focus_fields(
    store: Any, config: Mapping[str, Any], fields: Mapping[str, Optional[str]]
) -> Optional[str]:
    if not bool(config.get("focus_log", True)):
        return None
    parts = [_local_now()]
    parts.extend(_focus_log_field(name, value) for name, value in fields.items())
    parts.append(_focus_log_field("SESSION", store.identity.label))
    return append_focus_line(store, " ".join(parts).rstrip())


def timestamped_debug_log_path(store: Any) -> Path:
    return store.session_dir / f"debug.{timestamp_for_filename()}.log"


def read_current_debug_log_path(store: Any) -> Optional[Path]:
    try:
        name = store.debug_current_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not DEBUG_LOG_NAME_RE.fullmatch(name):
        return None
    return store.session_dir / name


def _write_current_debug_log_path(store: Any, path: Path) -> None:
    atomic_write_text(store.debug_current_path, path.name + "\n")


def resolve_debug_log_path(store: Any, next_line: str = "") -> Path:
    store.session_dir.mkdir(parents=True, exist_ok=True)
    migrated_path = None
    if store.debug_path.exists():
        migrated_path = timestamped_debug_log_path(store)
        store.debug_path.rename(migrated_path)
    current_path = read_current_debug_log_path(store)
    if current_path is None:
        current_path = migrated_path or timestamped_debug_log_path(store)
        _write_current_debug_log_path(store, current_path)
    if _would_overflow(current_path, next_line, DEBUG_LOG_MAX_BYTES):
        current_path = timestamped_debug_log_path(store)
        _write_current_debug_log_path(store, current_path)
    debug_paths = [
        path
        for path in store.session_dir.glob("debug.*.log")
        if DEBUG_LOG_NAME_RE.fullmatch(path.name)
    ]
    _retained(debug_paths, LOG_SEGMENTS)
    return current_path


def log_debug(
    store: Any, config: Mapping[str, Any], message: Mapping[str, Any]
) -> Optional[str]:
    if not bool(config.get("debug", False)):
        return None
    payload = {
        "timestamp": _utc_now(),
        "session_label": store.identity.label,
        "session_key": store.identity.key,
        **dict(message),
    }
    line = json.dumps(payload, ensure_ascii=False)
    try:
        with FileLock(_log_lock_path(store), blocking=True):
            debug_path = resolve_debug_log_path(store, line)
            with debug_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            debug_paths = [
                path
                for path in store.session_dir.glob("debug.*.log")
                if DEBUG_LOG_NAME_RE.fullmatch(path.name)
            ]
            _retained(debug_paths, LOG_SEGMENTS)
    except OSError as exc:
        return f"debug_log_failed: {exc}"
    return None


def log_health(store: Any) -> Dict[str, Dict[str, int]]:
    focus_paths = [store.focus_log_path] + [
        path
        for path in store.session_dir.glob("focus.*.log")
        if FOCUS_ROTATED_NAME_RE.fullmatch(path.name)
    ]
    debug_paths = [
        path
        for path in store.session_dir.glob("debug.*.log")
        if DEBUG_LOG_NAME_RE.fullmatch(path.name)
    ]
    return {
        "focus": _path_health(focus_paths),
        "debug": _path_health(debug_paths),
    }


def _path_health(paths: list[Path]) -> Dict[str, int]:
    sizes = []
    for path in paths:
        try:
            sizes.append(path.stat().st_size)
        except FileNotFoundError:
            pass
    return {
        "bytes": sum(sizes),
        "segments": len(sizes),
    }


def _log_lock_path(store: Any) -> Path:
    return store.session_dir / "logs.lock"


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _local_now() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def _focus_log_field(name: str, value: Optional[str]) -> str:
    return f"{name}={str(value) if value else '-'}"
