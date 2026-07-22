from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ._files import (
    FileLock,
    atomic_write_json,
    backup_path,
    contextlib_suppress_file_not_found,
    run_lock_path,
)
from .config import load_config, record_policy
from .logs import DEBUG_LOG_MAX_BYTES, log_debug

RECONCILE_INTERVAL = timedelta(hours=24)

@dataclasses.dataclass(frozen=True)
class SessionIdentity:
    label: str
    key: str
    socket_path_hash: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_now_for_log() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")



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
        last_reconciled = state.get("last_reconciled_at")
        if last_reconciled is not None and not isinstance(last_reconciled, str):
            raise ValueError("last_reconciled_at must be a string or null")
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


class RecordsUnavailable(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class FocusMemory:
    target_pane_id: str
    previous_pane_id: Optional[str]
    previous_stored_source: Optional[str]
    target_stored_source: Optional[str]


@dataclasses.dataclass(frozen=True)
class PaneAudit:
    live: int
    stored: int
    unmatched_ids: Tuple[str, ...]
    missing_ids: Tuple[str, ...]
    last_reconciled_at: Optional[str]
    maintenance_due: bool


@dataclasses.dataclass(frozen=True)
class ReconcileResult:
    completed: bool
    pruned_ids: Tuple[str, ...] = ()
    unknown_ids: Tuple[str, ...] = ()
    reason: Optional[str] = None


class PaneRecords:
    """Own pane-memory reads and atomic record updates for one Herdr session."""

    def __init__(self, store: StateStore):
        self.store = store
        self.last_diagnostic: Optional[str] = None

    def _load_for_update(self) -> Dict[str, Any]:
        state, diagnostic = self.store.load(readonly=False)
        self.last_diagnostic = diagnostic
        if state is None:
            raise RecordsUnavailable(diagnostic or "state unavailable")
        return state

    def snapshot(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        return self.store.load(readonly=True)

    def clear(self) -> None:
        self.store.clear()

    def repair_and_snapshot(self) -> Tuple[Dict[str, Any], Optional[str]]:
        state = self._load_for_update()
        return state, self.last_diagnostic

    def audit_live_panes(
        self,
        live_pane_ids: List[str],
        now: Optional[datetime] = None,
    ) -> PaneAudit:
        state, diagnostic = self.snapshot()
        self.last_diagnostic = diagnostic
        live_ids = {str(pane_id) for pane_id in live_pane_ids if str(pane_id)}
        panes = state.get("panes", {}) if isinstance(state, dict) else {}
        stored_ids = {str(pane_id) for pane_id in panes} if isinstance(panes, dict) else set()
        last_reconciled_at = state.get("last_reconciled_at") if isinstance(state, dict) else None
        if not isinstance(last_reconciled_at, str):
            last_reconciled_at = None
        return PaneAudit(
            live=len(live_ids),
            stored=len(stored_ids),
            unmatched_ids=tuple(sorted(stored_ids - live_ids)),
            missing_ids=tuple(sorted(live_ids - stored_ids)),
            last_reconciled_at=last_reconciled_at,
            maintenance_due=reconciliation_due(last_reconciled_at, now=now),
        )

    def reconcile_live_panes(
        self,
        live_pane_ids: List[str],
        presence: Any,
        now: Optional[datetime] = None,
        can_commit: Optional[Any] = None,
    ) -> ReconcileResult:
        live_ids = {str(pane_id) for pane_id in live_pane_ids if str(pane_id)}
        state = self._load_for_update()
        panes = state.setdefault("panes", {})
        focused_id = state.get("last_focused_pane_id")
        if not live_ids:
            return ReconcileResult(False, reason="empty-live-pane-list")
        if isinstance(focused_id, str) and focused_id and focused_id not in live_ids:
            return ReconcileResult(False, reason="focused-pane-missing")
        pruned: List[str] = []
        unknown: List[str] = []
        for pane_id in sorted(set(panes) - live_ids):
            result = presence(pane_id)
            if result == "absent":
                panes.pop(pane_id, None)
                pruned.append(pane_id)
            elif result != "present":
                unknown.append(pane_id)
        completed = not unknown
        if completed:
            state["last_reconciled_at"] = (now or datetime.now(timezone.utc)).isoformat()
        if can_commit is not None and not can_commit():
            return ReconcileResult(False, reason="budget-expired")
        if pruned or completed:
            self.store.save(state)
        return ReconcileResult(
            completed,
            pruned_ids=tuple(pruned),
            unknown_ids=tuple(unknown),
            reason=None if completed else "pane-presence-unknown",
        )

    def reconcile_with_herdr(self, herdr: Any, budget_seconds: float) -> ReconcileResult:
        deadline = time.monotonic() + max(0.0, budget_seconds)

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        timeout = remaining()
        if timeout <= 0:
            return ReconcileResult(False, reason="budget-expired")
        try:
            live_panes = herdr.list_panes_socket(timeout)
        except Exception as exc:
            return ReconcileResult(False, reason=f"pane-list-failed: {exc}")
        if live_panes is None:
            return ReconcileResult(False, reason="pane-list-unavailable-or-invalid")
        live_ids = [str(pane.get("pane_id")) for pane in live_panes]

        def presence(pane_id: str) -> str:
            timeout = remaining()
            if timeout <= 0:
                return "unknown"
            try:
                return str(herdr.pane_presence_socket(pane_id, timeout))
            except Exception:
                return "unknown"

        try:
            return self.reconcile_live_panes(
                live_ids,
                presence,
                can_commit=lambda: remaining() > 0,
            )
        except (OSError, RecordsUnavailable) as exc:
            return ReconcileResult(False, reason=f"state-update-failed: {exc}")

    def focus_memory(self, target_pane_id: str) -> FocusMemory:
        state = self._load_for_update()
        previous_pane_id = state.get("last_focused_pane_id")
        previous_id = str(previous_pane_id) if previous_pane_id else None
        return FocusMemory(
            target_pane_id=target_pane_id,
            previous_pane_id=previous_id,
            previous_stored_source=current_pane_stored_source(state, previous_id or ""),
            target_stored_source=current_pane_stored_source(state, target_pane_id),
        )

    def touch(self) -> None:
        self.store.save(self._load_for_update())

    def commit_focus(
        self,
        memory: FocusMemory,
        pane: Mapping[str, Any],
        observed_previous_source: Optional[str],
        selected_source: Optional[str],
    ) -> None:
        state = self._load_for_update()
        panes = state.setdefault("panes", {})
        if observed_previous_source and memory.previous_pane_id:
            previous_entry = dict(panes.get(memory.previous_pane_id, {}))
            previous_entry.update(
                {
                    "input_source_id": observed_previous_source,
                    "source": "observed_before_plugin_switch",
                    "updated_at": utc_now(),
                }
            )
            panes[memory.previous_pane_id] = previous_entry
        target_state = dict(panes.get(memory.target_pane_id, {}))
        target_state.update(pane_metadata(pane))
        if selected_source:
            target_state.setdefault("input_source_id", selected_source)
        panes[memory.target_pane_id] = target_state
        state["last_focused_pane_id"] = memory.target_pane_id
        self.store.save(state)

    def remove_pane(self, pane_id: str) -> Tuple[List[str], bool]:
        state = self._load_for_update()
        panes = state.setdefault("panes", {})
        removed_ids = [pane_id] if panes.pop(pane_id, None) is not None else []
        cleared_last_focused = state.get("last_focused_pane_id") == pane_id
        if cleared_last_focused:
            state["last_focused_pane_id"] = None
        self.store.save(state)
        return removed_ids, cleared_last_focused

    def remove_tab(self, tab_id: str) -> Tuple[List[str], bool]:
        return self._remove_matching("tab_id", tab_id)

    def remove_workspace(self, workspace_id: str) -> Tuple[List[str], bool]:
        return self._remove_matching("workspace_id", workspace_id)

    def _remove_matching(self, field: str, value: str) -> Tuple[List[str], bool]:
        state = self._load_for_update()
        panes = state.setdefault("panes", {})
        removed_ids = [
            str(pane_id)
            for pane_id, pane_state in panes.items()
            if isinstance(pane_state, dict) and pane_state.get(field) == value
        ]
        last_entry = panes.get(state.get("last_focused_pane_id"))
        for pane_id in removed_ids:
            panes.pop(pane_id, None)
        cleared_last_focused = isinstance(last_entry, dict) and last_entry.get(field) == value
        if cleared_last_focused:
            state["last_focused_pane_id"] = None
        self.store.save(state)
        return removed_ids, cleared_last_focused

    def move_pane(self, old_id: str, new_id: str, pane: Mapping[str, Any]) -> Tuple[bool, bool]:
        state = self._load_for_update()
        panes = state.setdefault("panes", {})
        migrated = old_id in panes
        if migrated:
            entry = dict(panes.pop(old_id))
            entry.update(pane_metadata(pane))
            panes[new_id] = entry
        updated_last_focused = state.get("last_focused_pane_id") == old_id
        if updated_last_focused:
            state["last_focused_pane_id"] = new_id
        self.store.save(state)
        return migrated, updated_last_focused



def reconcile_state_policy(config: Mapping[str, Any], store: StateStore, cause: str) -> str:
    mode = record_policy(config)
    if mode != "keep":
        PaneRecords(store).clear()
    return mode


def reconciliation_due(value: Optional[str], now: Optional[datetime] = None) -> bool:
    if not value:
        return True
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return True
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - timestamp >= RECONCILE_INTERVAL


def pane_metadata(pane: Mapping[str, Any]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    for field in ("workspace_id", "tab_id", "agent", "cwd"):
        value = pane.get(field)
        if isinstance(value, str) and value:
            metadata[field] = value
    return metadata

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

def handle_cleanup_event(
    context: Any,
    store: StateStore,
    parsed: Mapping[str, Any],
    cleanup_kind: str,
) -> int:
    with FileLock(run_lock_path(context.state_dir), blocking=True):
        config = load_config(context.config_dir, readonly=True)
        mode = reconcile_state_policy(config, store, f"{cleanup_kind}-closed")
        if mode != "keep":
            return 0
        records = PaneRecords(store)
        if cleanup_kind == "pane":
            pane_id_raw = parsed.get("pane_id")
            if pane_id_raw is None or pane_id_raw == "":
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
            pane_id = str(pane_id_raw)
            operation = lambda: records.remove_pane(pane_id)
        elif cleanup_kind == "tab":
            tab_id_raw = parsed.get("tab_id")
            if tab_id_raw is None or tab_id_raw == "":
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
            tab_id = str(tab_id_raw)
            operation = lambda: records.remove_tab(tab_id)
        elif cleanup_kind == "workspace":
            workspace_id_raw = parsed.get("workspace_id")
            if workspace_id_raw is None or workspace_id_raw == "":
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
            workspace_id = str(workspace_id_raw)
            operation = lambda: records.remove_workspace(workspace_id)
        else:
            return 0
        try:
            removed_ids, cleared_last_focused = operation()
        except RecordsUnavailable as exc:
            log_debug(store, config, {"event": f"{cleanup_kind}-closed", "error": str(exc)})
            return 0
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


def handle_pane_moved(context: Any, store: StateStore, parsed: Mapping[str, Any]) -> int:
    with FileLock(run_lock_path(context.state_dir), blocking=True):
        config = load_config(context.config_dir, readonly=True)
        mode = reconcile_state_policy(config, store, "pane-moved")
        if mode != "keep":
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
        try:
            migrated, updated_last_focused = PaneRecords(store).move_pane(
                str(old_id), str(new_id), pane
            )
        except RecordsUnavailable as exc:
            log_debug(store, config, {"event": "pane-moved", "error": str(exc)})
            return 0
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
