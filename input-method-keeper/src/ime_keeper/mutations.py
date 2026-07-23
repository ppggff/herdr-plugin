from __future__ import annotations

import dataclasses
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Set, Tuple

from ._files import FileLock, backup_path, run_lock_path
from .config import (
    ConfigError,
    apply_config_mutation,
    config_path,
    default_config,
    ensure_config,
    load_config,
    write_config,
)
from .records import PaneRecords, StateStore, session_identity


Fingerprint = Tuple[str, str]


def file_fingerprint(path: Path) -> Fingerprint:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return ("missing", "")
    except OSError as exc:
        return ("unreadable", f"{type(exc).__name__}:{exc}")
    return ("present", hashlib.sha256(payload).hexdigest())


@dataclasses.dataclass(frozen=True)
class ConfirmationToken:
    nonce: str
    mutation: str
    value: Optional[str]
    target_config_json: str
    clear_records: bool
    repair_config: bool
    displayed_input_source: Optional[str]
    record_count: Optional[int]
    state_unknown: bool
    config_fingerprint: Fingerprint
    state_fingerprint: Fingerprint
    dirty_fingerprint: Fingerprint

    @property
    def target_config(self) -> Dict[str, Any]:
        return json.loads(self.target_config_json)


@dataclasses.dataclass(frozen=True)
class MutationResult:
    status: str
    mutation: str
    config: Optional[Dict[str, Any]] = None
    token: Optional[ConfirmationToken] = None
    clear_records: bool = False
    record_count: Optional[int] = 0
    state_unknown: bool = False
    displayed_input_source: Optional[str] = None
    message: str = ""


class MutationService:
    """Shared application seam for the finite set of user config mutations."""

    def __init__(self, env: Mapping[str, str]):
        self.env = dict(env)
        self.config_dir = Path(self.env.get("HERDR_PLUGIN_CONFIG_DIR", "."))
        self.state_dir = Path(self.env.get("HERDR_PLUGIN_STATE_DIR", "."))
        self._consumed_tokens: Set[str] = set()

    def _readonly_config(self) -> Tuple[Dict[str, Any], bool]:
        try:
            return load_config(self.config_dir, readonly=True), False
        except (ConfigError, OSError):
            return default_config(), True

    def apply(
        self,
        mutation: str,
        value: Optional[str] = None,
        backend: Optional[Any] = None,
        interactive: bool = False,
    ) -> MutationResult:
        if not interactive:
            return self._apply_noninteractive(mutation, value, backend)

        with FileLock(run_lock_path(self.state_dir), blocking=True):
            config, repair_config = self._readonly_config()
            store = StateStore(self.state_dir, session_identity(config, self.env))
            displayed_source = None
            if mutation == "set-default-input-source":
                if backend is None:
                    raise ConfigError("input source backend is unavailable")
                displayed_source = backend.current()
            target, clear_records = apply_config_mutation(
                config,
                mutation,
                value=value,
                current_input_source=displayed_source,
            )
            with store.dirty_guard() as guard:
                presence = PaneRecords(store).record_presence()
                record_count, state_unknown = presence.count, presence.unknown
                config_fp = file_fingerprint(config_path(self.config_dir))
                state_fp = file_fingerprint(store.state_path)
                dirty_fp = file_fingerprint(store.dirty_path)
                confirmation_required = repair_config or (
                    clear_records and (state_unknown or bool(record_count))
                )
                if confirmation_required:
                    token = ConfirmationToken(
                        nonce=secrets.token_urlsafe(18),
                        mutation=mutation,
                        value=value,
                        target_config_json=json.dumps(target, sort_keys=True),
                        clear_records=clear_records,
                        repair_config=repair_config,
                        displayed_input_source=displayed_source,
                        record_count=record_count,
                        state_unknown=state_unknown,
                        config_fingerprint=config_fp,
                        state_fingerprint=state_fp,
                        dirty_fingerprint=dirty_fp,
                    )
                    return MutationResult(
                        "confirmation_required",
                        mutation,
                        token=token,
                        clear_records=clear_records,
                        record_count=record_count,
                        state_unknown=state_unknown,
                        displayed_input_source=displayed_source,
                        message=self._confirmation_message(token),
                    )
                write_config(self.config_dir, target)
                if clear_records:
                    store.clear_locked(guard)
                return MutationResult(
                    "applied",
                    mutation,
                    config=target,
                    clear_records=clear_records,
                    record_count=record_count,
                    displayed_input_source=displayed_source,
                    message=self._updated_message(mutation, target, displayed_source),
                )

    def confirm(self, token: Optional[ConfirmationToken]) -> MutationResult:
        if token is None or token.nonce in self._consumed_tokens:
            return MutationResult("preview_stale", "", message="Changed while waiting: review the setting again")
        self._consumed_tokens.add(token.nonce)
        with FileLock(run_lock_path(self.state_dir), blocking=True):
            config, _repair = self._readonly_config()
            store = StateStore(self.state_dir, session_identity(config, self.env))
            with store.dirty_guard() as guard:
                current = (
                    file_fingerprint(config_path(self.config_dir)),
                    file_fingerprint(store.state_path),
                    file_fingerprint(store.dirty_path),
                )
                expected = (
                    token.config_fingerprint,
                    token.state_fingerprint,
                    token.dirty_fingerprint,
                )
                if current != expected:
                    return MutationResult(
                        "preview_stale",
                        token.mutation,
                        message="Changed while waiting: review the setting again",
                    )
                path = config_path(self.config_dir)
                if token.repair_config and path.exists():
                    path.rename(backup_path(path))
                target = token.target_config
                write_config(self.config_dir, target)
                if token.clear_records:
                    store.clear_locked(guard)
                return MutationResult(
                    "applied",
                    token.mutation,
                    config=target,
                    clear_records=token.clear_records,
                    record_count=token.record_count,
                    state_unknown=token.state_unknown,
                    displayed_input_source=token.displayed_input_source,
                    message=self._updated_message(
                        token.mutation, target, token.displayed_input_source
                    ),
                )

    def _apply_noninteractive(
        self, mutation: str, value: Optional[str], backend: Optional[Any]
    ) -> MutationResult:
        with FileLock(run_lock_path(self.state_dir), blocking=True):
            config = ensure_config(self.config_dir)
            store = StateStore(self.state_dir, session_identity(config, self.env))
            displayed_source = None
            if mutation == "set-default-input-source":
                if backend is None:
                    raise ConfigError("input source backend is unavailable")
                displayed_source = backend.current()
            target, clear_records = apply_config_mutation(
                config,
                mutation,
                value=value,
                current_input_source=displayed_source,
            )
            if clear_records:
                with store.dirty_guard() as guard:
                    write_config(self.config_dir, target)
                    store.clear_locked(guard)
            else:
                write_config(self.config_dir, target)
            return MutationResult(
                "applied",
                mutation,
                config=target,
                clear_records=clear_records,
                displayed_input_source=displayed_source,
                message=self._updated_message(mutation, target, displayed_source),
            )

    @staticmethod
    def _confirmation_message(token: ConfirmationToken) -> str:
        effects = [MutationService.target_effect(token)]
        if token.repair_config:
            effects.append("repair invalid config")
        if token.clear_records:
            if token.state_unknown:
                effects.append("remove unreadable pane memory and pending focus marker")
            else:
                effects.append(f"clear {token.record_count or 0} pane record(s)")
        return "Confirm: " + "; ".join(effects)

    @staticmethod
    def target_effect(token: ConfirmationToken) -> str:
        target = token.target_config
        if token.mutation == "toggle-enabled":
            return f"set enabled to {'on' if target.get('enabled') else 'off'}"
        if token.mutation == "set-default-action":
            return f"set default action to {target.get('default_action')}"
        if token.mutation == "set-default-input-source":
            return f"set default source to {token.displayed_input_source or '-'}"
        if token.mutation.startswith("set-backend-"):
            return f"set backend to {'helper' if token.mutation.endswith('helper') else 'macism'}"
        if token.mutation.startswith("debug-"):
            return f"set debug logging to {'on' if target.get('debug') else 'off'}"
        return token.mutation

    @staticmethod
    def _updated_message(
        mutation: str, config: Mapping[str, Any], displayed_source: Optional[str]
    ) -> str:
        if mutation == "toggle-enabled":
            return f"Updated: enabled is {'on' if config.get('enabled') else 'off'}"
        if mutation == "set-default-action":
            return f"Updated: default action is {config.get('default_action')}"
        if mutation == "set-default-input-source":
            return f"Updated: default source is {displayed_source or '-'}"
        if mutation.startswith("set-backend-"):
            return f"Updated: backend is {'helper' if mutation.endswith('helper') else 'macism'}"
        if mutation.startswith("debug-"):
            return f"Updated: debug logging is {'on' if config.get('debug') else 'off'}"
        return "Updated"
