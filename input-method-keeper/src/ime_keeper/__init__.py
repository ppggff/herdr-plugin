"""Input Method Keeper modules and compatibility exports."""

from . import records
from ._files import FileLock, run_lock_path
from .cli import HerdrContext, doctor, handle_event, main, mutate_config, parse_event, print_status
from .config import (
    apply_config_mutation,
    ConfigError,
    DEFAULT_CONFIG,
    VALID_ACTIONS,
    default_config,
    ensure_config,
    load_config,
    merge_config,
    write_config,
)
from .dashboard import collect_dashboard_data, render_dashboard, run_dashboard
from .focus import handle_pane_focused
from .herdr import HerdrClient
from .input_source import (
    BackendExecutor,
    CommandResult,
    ensure_input_source,
    ensure_input_source_details,
)
from .records import (
    DEBUG_LOG_MAX_BYTES,
    PaneRecords,
    SessionIdentity,
    StateStore,
    empty_state,
    log_debug,
    reconcile_state_policy,
    session_identity,
)

__all__ = [
    "BackendExecutor",
    "CommandResult",
    "ConfigError",
    "DEBUG_LOG_MAX_BYTES",
    "DEFAULT_CONFIG",
    "FileLock",
    "HerdrClient",
    "HerdrContext",
    "PaneRecords",
    "SessionIdentity",
    "StateStore",
    "VALID_ACTIONS",
    "collect_dashboard_data",
    "apply_config_mutation",
    "default_config",
    "doctor",
    "empty_state",
    "ensure_config",
    "ensure_input_source",
    "ensure_input_source_details",
    "handle_event",
    "handle_pane_focused",
    "load_config",
    "log_debug",
    "main",
    "merge_config",
    "mutate_config",
    "parse_event",
    "print_status",
    "reconcile_state_policy",
    "render_dashboard",
    "run_dashboard",
    "run_lock_path",
    "session_identity",
    "write_config",
]
