"""Configuration storage for phantombox."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from phantombox.env import get_data_dir

CONFIG_FILE = get_data_dir() / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "user_name": "User",
    "assistant_name": "Assistant",
    "theme_color": "#f97316",
    "user_color": "#58a6ff",
    "assistant_color": "#f97316",
    # Tool permissions & scope
    # permission_mode: 'restricted' | 'custom' | 'full_access'
    "permission_mode": "restricted",
    # Path scopes within phantombox-dir: ['*'] means all files and folders in phantombox-dir
    # Or specific subfolder(s) e.g. ['projects', 'notes']
    "allowed_read_paths": ["*"],
    "allowed_write_paths": ["*"],
    # For 'custom' mode: which tools can run without confirmation
    # Tools in auto_approve_tools run without asking; others ask for confirmation
    "custom_auto_approve_tools": ["list_dir", "read_file", "create_file", "create_folder", "internet_search"],
    # Online internet search capability toggle
    "enable_internet_search": True,
    # Model thinking / internal deep reasoning directive toggle
    "enable_thinking": False,
    # External API / MCP providers: provider_id -> {"api_key": "...", "enabled": True}
    "providers": {},
    # PhantomBox Server configuration
    "server": {
        "host": "0.0.0.0",
        "port": 8443,
        "discoverable": True,
        "users": {},
        "remote_servers": {},
    },
    # Subagents configuration
    "subagents": {
        "agents": {},  # nickname -> {"nickname": str, "model": str, "role": str, "system_prompt": str, "scripts": [...]}
        "global_auto": False,  # Automatically assign a subagent when chat starts
        "global_auto_nickname": "",
    },
    # PhantomBox Smart Router configuration
    "router": {
        "enabled": True,
        "max_exploratory_attempts": 10,  # Max random attempts before falling back to known healthy model
        "timeout_per_attempt": 6.0,      # Seconds timeout per provider probe
        "strategy": "adaptive",          # 'adaptive' | 'fastest' | 'random'
        "successful_models": [],         # List of {"model_id": "...", "provider": "...", "last_latency": 1.2, "success_count": 5}
    },
}


def load_config() -> Dict[str, Any]:
    """Load persistent global configuration, falling back to defaults."""
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(DEFAULT_CONFIG)
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        return merged
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(config: Dict[str, Any]) -> None:
    """Save persistent global configuration to disk."""
    try:
        CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_config_val(key: str, default: Any = None) -> Any:
    cfg = load_config()
    return cfg.get(key, default)


def set_config_val(key: str, value: Any) -> None:
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)


def reset_config_val(key: str) -> Any:
    """Reset a specific key to its default value and return the new value."""
    cfg = load_config()
    default_val = DEFAULT_CONFIG.get(key)
    cfg[key] = default_val
    save_config(cfg)
    return default_val
