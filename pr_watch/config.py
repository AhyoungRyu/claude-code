from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional


DEFAULT_CONFIG = {
    "poll_interval_seconds": "120",
    "busy_policy": "run_if_idle_queue_if_busy",
    "default_delivery": "confirm_first",
    "include_drafts": "false",
    "notification_mode": "auto",
    "notify_event_types": ["*"],
}

LIST_CONFIG_KEYS = {"notify_event_types"}


def default_state_dir() -> Path:
    return Path.home() / ".pr-watch"


def state_db_path(state_dir: Optional[str] = None) -> Path:
    return Path(state_dir).expanduser() / "state.sqlite" if state_dir else default_state_dir() / "state.sqlite"


def config_path(state_dir: Optional[str] = None) -> Path:
    return Path(state_dir).expanduser() / "config.toml" if state_dir else default_state_dir() / "config.toml"


def load_config(state_dir: Optional[str] = None) -> Dict[str, Any]:
    path = config_path(state_dir)
    config = dict(DEFAULT_CONFIG)
    if not path.exists():
        return config
    text = path.read_text(encoding="utf-8")
    try:
        loaded = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        loaded = _load_legacy_config(text)
    for key, value in loaded.items():
        config[str(key)] = value
    return config


def _load_legacy_config(text: str) -> Dict[str, str]:
    config: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line or line.startswith("["):
            continue
        key, value = line.split("=", 1)
        config[key.strip()] = value.strip().strip('"')
    return config


def set_config_value(key: str, value: str, state_dir: Optional[str] = None) -> Path:
    path = config_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    config = load_config(state_dir)
    config[key] = parse_config_value(key, value)
    lines = [f"{name} = {_format_config_value(config[name])}" for name in sorted(config)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def parse_config_value(key: str, value: str) -> Any:
    if key in LIST_CONFIG_KEYS:
        raw = value.strip()
        if raw.startswith("["):
            parsed = tomllib.loads(f"value = {raw}\n").get("value", [])
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in raw.split(",") if item.strip()]
    return value


def _format_config_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(str(item)) for item in value) + "]"
    return json.dumps(str(value))


def config_bool(config: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = config.get(key)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def config_list(config: Dict[str, Any], key: str, default: Optional[list[str]] = None) -> list[str]:
    value = config.get(key)
    if value is None:
        return list(default or [])
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value).strip()
    if not raw:
        return []
    if raw.startswith("["):
        parsed = tomllib.loads(f"value = {raw}\n").get("value", [])
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in raw.split(",") if item.strip()]
