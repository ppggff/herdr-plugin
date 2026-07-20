from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from ._files import atomic_write_json, backup_path

VALID_ACTIONS = {"keep", "reset", "ignore"}
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



def base_config() -> Dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]


def native_helper_available() -> bool:
    root = plugin_root()
    helper = root / "bin" / "herdr-ime-helper-native"
    source = root / "helpers" / "herdr-ime-helper.swift"
    if not helper.is_file() or not os.access(helper, os.X_OK):
        return False
    try:
        return not source.exists() or helper.stat().st_mtime >= source.stat().st_mtime
    except OSError:
        return False


def default_config() -> Dict[str, Any]:
    config = base_config()
    if native_helper_available():
        config["backend"] = helper_backend_config()
    return config


def macism_backend_config() -> Dict[str, Any]:
    return base_config()["backend"]


def helper_backend_config() -> Dict[str, Any]:
    return {
        "name": "herdr-ime-helper",
        "executable_candidates": [str(plugin_root() / "bin" / "herdr-ime-helper")],
        "current_args": ["current"],
        "select_args": ["select", "{id}", "--refresh", "--wait-ms", "150"],
    }


def rebind_plugin_helper_backend(config: Dict[str, Any]) -> None:
    """Point a canonical helper backend at the plugin checkout currently running it."""
    backend = config.get("backend")
    if not isinstance(backend, dict):
        return
    expected = helper_backend_config()
    candidates = backend.get("executable_candidates")
    if (
        backend.get("name") != expected["name"]
        or backend.get("current_args") != expected["current_args"]
        or backend.get("select_args") != expected["select_args"]
        or not isinstance(candidates, list)
        or len(candidates) != 1
        or not isinstance(candidates[0], str)
    ):
        return
    candidate = Path(candidates[0])
    if candidate.name != "herdr-ime-helper" or candidate.parent.name != "bin":
        return
    backend["executable_candidates"] = expected["executable_candidates"]



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
    config = base_config()
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
    rebind_plugin_helper_backend(config)
    return config


def ensure_config(config_dir: Path) -> Dict[str, Any]:
    path = config_path(Path(config_dir))
    config = load_config(Path(config_dir), readonly=False)
    if not path.exists():
        atomic_write_json(path, config)
    return config


def write_config(config_dir: Path, config: Mapping[str, Any]) -> None:
    atomic_write_json(config_path(Path(config_dir)), config)


def record_policy(config: Mapping[str, Any]) -> str:
    if not bool(config.get("enabled", True)):
        return "disabled"
    action = str(config.get("default_action", "keep"))
    return action if action in VALID_ACTIONS else "keep"


def apply_config_mutation(
    config: Mapping[str, Any],
    mutation: str,
    value: Optional[str] = None,
    current_input_source: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Apply one supported action and report whether pane records must be cleared."""
    updated = dict(config)
    clear_records = record_policy(config) != "keep"
    if mutation == "toggle-enabled":
        updated["enabled"] = not bool(updated.get("enabled", True))
        clear_records = True
    elif mutation == "debug-on":
        updated["debug"] = True
    elif mutation == "debug-off":
        updated["debug"] = False
    elif mutation == "set-default-action":
        if value not in VALID_ACTIONS:
            raise ConfigError(f"invalid default action: {value}")
        updated["default_action"] = value
    elif mutation == "set-default-input-source":
        updated["default_input_source"] = current_input_source
    elif mutation == "set-backend-helper":
        updated["backend"] = helper_backend_config()
    elif mutation == "set-backend-macism":
        updated["backend"] = macism_backend_config()
    else:
        raise ConfigError(f"unknown config mutation: {mutation}")
    if record_policy(updated) != "keep":
        clear_records = True
    return updated, clear_records
