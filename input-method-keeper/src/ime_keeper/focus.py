from __future__ import annotations

import json
import sys
import time
import dataclasses
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from ._files import FileLock, run_lock_path
from .config import load_config
from .herdr import pane_parts
from .input_source import CommandResult, ensure_input_source_details, short_input_source
from .logs import append_focus_line, log_debug
from .records import (
    PaneRecords,
    RecordsUnavailable,
    StateStore,
    local_now_for_log,
    reconcile_state_policy,
)

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
        return append_focus_line(
            store,
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
                ),
        )
    except OSError as exc:
        return f"focus_log_failed: {exc}"


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
    current_tokens: Optional[Mapping[str, Any]] = None,
) -> Dict[str, float]:
    status = short_input_source(input_source_id)
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
    publication_ms = {"metadata_ms": 0.0, "notification_ms": 0.0}
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
    token_unchanged = isinstance(current_tokens, Mapping) and current_tokens.get("ime") == status
    if (
        bool(config.get("pane_status_on_focus", True))
        and not token_unchanged
        and hasattr(herdr, "report_pane_status")
    ):
        started = time.monotonic()
        try:
            result = herdr.report_pane_status(pane_id, status, int(config.get("status_ttl_ms", 600000)))
            if isinstance(result, CommandResult) and not result.ok:
                failures.append(f"pane_status_failed: {result.stderr or result.stdout}")
        except Exception as exc:
            failures.append(f"pane_status_failed: {exc}")
        publication_ms["metadata_ms"] = (time.monotonic() - started) * 1000
    if bool(config.get("notify_on_focus", True)) and hasattr(herdr, "show_notification"):
        started = time.monotonic()
        try:
            result = herdr.show_notification(title, body)
            if isinstance(result, CommandResult) and not result.ok:
                failures.append(f"notification_failed: {result.stderr or result.stdout}")
        except Exception as exc:
            failures.append(f"notification_failed: {exc}")
        publication_ms["notification_ms"] = (time.monotonic() - started) * 1000
    if failures:
        warning = {
            "event": "focus-status",
            "pane_id": pane_id,
            "input_source_id": input_source_id,
            "status": status,
            "title": title,
            "errors": failures,
        }
        log_debug(store, config, warning)
        print(json.dumps({"level": "warning", **warning}, ensure_ascii=False), file=sys.stderr)
    return publication_ms


def log_focus_timings(
    store: StateStore,
    config: Mapping[str, Any],
    pane_id: str,
    timings: Mapping[str, Any],
    started: float,
) -> None:
    log_debug(
        store,
        config,
        {
            "event": "focus-timings",
            "pane_id": pane_id,
            **dict(timings),
            "total_ms": (time.monotonic() - started) * 1000,
        },
    )


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
    context: Any,
    store: StateStore,
    backend: Any,
    herdr: Any,
    parsed: Mapping[str, Any],
    debounce_seconds: float,
) -> int:
    focus_started = time.monotonic()
    timings: Dict[str, Any] = {
        "lock_wait_ms": 0.0,
        "stabilize_ms": 0.0,
        "backend_current_ms": 0.0,
        "backend_select_ms": 0.0,
        "metadata_ms": 0.0,
        "notification_ms": 0.0,
        "coalesced_events": 0,
    }
    records = PaneRecords(store)
    with FileLock(store.focus_lock_path, blocking=False) as focus_lock:
        if not focus_lock.acquired:
            payload = {"pane_id": parsed.get("pane_id")} if parsed.get("pane_id") else {}
            store.mark_dirty(payload)
            return 0
        lock_started = time.monotonic()
        with FileLock(run_lock_path(context.state_dir), blocking=True):
            timings["lock_wait_ms"] += (time.monotonic() - lock_started) * 1000
            config = load_config(context.config_dir, readonly=True)
            mode = reconcile_state_policy(config, store, "pane-focused")
            if mode in {"disabled", "ignore"}:
                store.clear_dirty()
                return 0
        deadline = time.monotonic() + 1.0
        while True:
            stabilize_started = time.monotonic()
            pane = stable_current_pane(herdr, debounce_seconds)
            timings["stabilize_ms"] += (time.monotonic() - stabilize_started) * 1000
            if not pane:
                return 0
            stable_pane_id = pane.get("pane_id")
            if not stable_pane_id:
                return 0
            lock_started = time.monotonic()
            with FileLock(run_lock_path(context.state_dir), blocking=True) as decision_lock:
                timings["lock_wait_ms"] += (time.monotonic() - lock_started) * 1000
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
                    decision_lock.release()
                    publication_ms = publish_focus_status(
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
                        current_tokens=pane.get("tokens") if isinstance(pane.get("tokens"), Mapping) else None,
                    )
                    timings.update(publication_ms)
                    timings["backend_current_ms"] += float(ensure_result.get("current_ms", 0.0))
                    timings["backend_select_ms"] += float(ensure_result.get("select_ms", 0.0))
                    log_focus_timings(store, config, str(stable_pane_id), timings, focus_started)
                    if should_loop_again(store, herdr, stable_pane_id, deadline):
                        timings["coalesced_events"] += 1
                        continue
                    return 0
                try:
                    memory = records.focus_memory(str(stable_pane_id))
                except RecordsUnavailable as exc:
                    log_debug(
                        store,
                        config,
                        {
                            **focus_debug_base(config, mode, stable_pane_id),
                            "reason": "state-load-failed",
                            "error": records.last_diagnostic or str(exc),
                        },
                    )
                    return 0
                if memory.previous_pane_id == stable_pane_id:
                    stored_source = memory.target_stored_source
                    status_input_source = stored_source
                    records.touch()
                    store.clear_dirty()
                    log_debug(
                        store,
                        config,
                        {
                            **focus_debug_base(
                                config,
                                "keep",
                                stable_pane_id,
                                memory.previous_pane_id,
                            ),
                            "backend_current_before_select": None,
                            "stored_target_input_source": stored_source,
                            "reason": "same-pane",
                        },
                    )
                    decision_lock.release()
                    publication_ms = publish_focus_status(
                        store,
                        config,
                        herdr,
                        stable_pane_id,
                        status_input_source,
                        "keep",
                        stored_source,
                        backend_current_before_select=None,
                        reason="same-pane",
                        current_tokens=pane.get("tokens") if isinstance(pane.get("tokens"), Mapping) else None,
                    )
                    timings.update(publication_ms)
                    log_focus_timings(store, config, str(stable_pane_id), timings, focus_started)
                    if should_loop_again(store, herdr, stable_pane_id, deadline):
                        timings["coalesced_events"] += 1
                        continue
                    attempt_due_reconciliation(context, store, herdr, config)
                    if should_loop_again(store, herdr, stable_pane_id, deadline):
                        continue
                    return 0
                previous_pane_id = memory.previous_pane_id
                pending_observation = None
                previous_stored_source = memory.previous_stored_source
                if previous_pane_id and previous_pane_id != stable_pane_id:
                    try:
                        backend_current_started = time.monotonic()
                        pending_observation = backend.current()
                        timings["backend_current_ms"] += (
                            time.monotonic() - backend_current_started
                        ) * 1000
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
                target = memory.target_stored_source or config.get("default_input_source", "")
                current_before_select = herdr.current_pane()
                if not current_before_select:
                    log_debug(
                        store,
                        config,
                        {
                            **focus_debug_base(config, "keep", stable_pane_id, previous_pane_id),
                            "observed_previous_input_source": pending_observation,
                            "target_input_source": target,
                            "stored_target_input_source": memory.target_stored_source,
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
                            "stored_target_input_source": memory.target_stored_source,
                            "current_pane_id": current_before_select.get("pane_id"),
                            "reason": "focus-changed-before-select",
                        },
                    )
                    continue
                try:
                    if pending_observation is not None:
                        ensure_result = ensure_input_source_details(
                            backend,
                            str(target),
                            known_current=pending_observation,
                        )
                    else:
                        ensure_result = ensure_input_source_details(backend, str(target))
                except Exception as exc:
                    log_debug(
                        store,
                        config,
                        {
                            **focus_debug_base(config, "keep", stable_pane_id, previous_pane_id),
                            "observed_previous_input_source": pending_observation,
                            "target_input_source": target,
                            "stored_target_input_source": memory.target_stored_source,
                            "reason": "backend-select-failed",
                            "error": f"backend_select_failed: {exc}",
                        },
                    )
                    return 0
                records.commit_focus(
                    memory,
                    pane,
                    observed_previous_source=pending_observation,
                    selected_source=str(target) if target else None,
                )
                store.clear_dirty()
                log_debug(
                    store,
                    config,
                    {
                        **focus_debug_base(config, "keep", stable_pane_id, previous_pane_id),
                        "target_input_source": target,
                        "stored_target_input_source": memory.target_stored_source,
                        "previous_stored_input_source": previous_stored_source,
                        "previous_updated_input_source": pending_observation,
                        "observed_previous_input_source": pending_observation,
                        "backend_current_before_select": ensure_result.get("current"),
                        "select_action": ensure_result.get("action"),
                        "select_exit_code": ensure_result.get("select_exit_code"),
                        "reason": "restored-target",
                    },
                )
                decision_lock.release()
                publication_ms = publish_focus_status(
                    store,
                    config,
                    herdr,
                    stable_pane_id,
                    str(target) if target else ensure_result.get("current"),
                    "keep",
                    memory.target_stored_source,
                    previous_pane_id=str(previous_pane_id) if previous_pane_id else None,
                    previous_stored_input_source=previous_stored_source,
                    previous_observed_input_source=pending_observation,
                    backend_current_before_select=ensure_result.get("current"),
                    select_action=ensure_result.get("action"),
                    reason="restored-target",
                    current_tokens=pane.get("tokens") if isinstance(pane.get("tokens"), Mapping) else None,
                )
                timings.update(publication_ms)
                timings["backend_current_ms"] += float(ensure_result.get("current_ms", 0.0))
                timings["backend_select_ms"] += float(ensure_result.get("select_ms", 0.0))
                log_focus_timings(store, config, str(stable_pane_id), timings, focus_started)
                if should_loop_again(store, herdr, stable_pane_id, deadline):
                    timings["coalesced_events"] += 1
                    continue
                attempt_due_reconciliation(context, store, herdr, config)
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


def attempt_due_reconciliation(
    context: Any,
    store: StateStore,
    herdr: Any,
    config: Mapping[str, Any],
) -> None:
    records = PaneRecords(store)
    if not records.audit_live_panes([]).maintenance_due:
        return
    with FileLock(run_lock_path(context.state_dir), blocking=False) as maintenance_lock:
        if not maintenance_lock.acquired:
            return
        latest_config = load_config(context.config_dir, readonly=True)
        if reconcile_state_policy(latest_config, store, "automatic-reconciliation") != "keep":
            return
        result = records.reconcile_with_herdr(herdr, budget_seconds=0.25)
    log_debug(
        store,
        latest_config,
        {
            "event": "pane-reconciliation",
            **dataclasses.asdict(result),
        },
    )
