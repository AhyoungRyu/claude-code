from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional


DEFAULT_CONFIG = {
    "poll_interval_seconds": "120",
    "busy_policy": "run_if_idle_queue_if_busy",
    "default_delivery": "confirm_first",
    "include_drafts": "false",
    "notification_mode": "none",
}


def default_state_dir() -> Path:
    return Path.home() / ".pr-watch"


def state_db_path(state_dir: Optional[str] = None) -> Path:
    return Path(state_dir).expanduser() / "state.sqlite" if state_dir else default_state_dir() / "state.sqlite"


def config_path(state_dir: Optional[str] = None) -> Path:
    return Path(state_dir).expanduser() / "config.toml" if state_dir else default_state_dir() / "config.toml"


def load_config(state_dir: Optional[str] = None) -> Dict[str, str]:
    path = config_path(state_dir)
    config = dict(DEFAULT_CONFIG)
    if not path.exists():
        return config
    for raw_line in path.read_text(encoding="utf-8").splitlines():
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
    config[key] = value
    lines = [f'{name} = "{config[name]}"' for name in sorted(config)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def config_bool(config: Dict[str, str], key: str, default: bool = False) -> bool:
    value = config.get(key)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
