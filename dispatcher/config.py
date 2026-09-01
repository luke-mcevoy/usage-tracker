"""Configuration for the `ai` dispatcher.

Ships with sane defaults; users may override any subset of them in
~/.config/usage-tracker/dispatcher.json. Missing or corrupt config files are
ignored so the dispatcher always starts.
"""

import copy
import json
import os

CONFIG_PATH = os.path.expanduser("~/.config/usage-tracker/dispatcher.json")

DEFAULT_CONFIG = {
    "priority": ["claude", "codex", "gemini", "cursor"],
    "thresholds": {
        "claude": {"max_window_percent": 85},
        "codex": {"max_window_percent": 85},
        "gemini": {"max_window_percent": 85},
        "cursor": {"min_remaining_usd": 25.0},
    },
    # Agents of last resort: only dispatched to when nothing else is usable
    # and waiting for a rate-limit window reset is not worth it.
    "reserve": ["cursor"],
    "wait": {
        "enabled": True,
        "max_wait_minutes": 360,
        "poll_seconds": 120,
        "grace_minutes": 10,
    },
    "tracker_url": "http://127.0.0.1:8899/api/usage",
    # HTTP chat-completions gateway (OpenAI-compatible). A backend is a
    # config dict, not a new module: kind gemini | openai. ``quota`` names
    # the usage-tracker service whose remaining window/dollars gate this
    # backend. Omit quota for pay-as-you-go APIs (tried last).
    "gateway": {
        "enabled": True,
        "backends": [
            {
                "id": "gemini",
                "kind": "gemini",
                "quota": "gemini",
                "model": "gemini-3.6-flash",
                "api_key_env": "GEMINI_API_KEY",
                "api_key_keychain": "gemini-cli-api-key",
            },
        ],
    },
}


def _deep_merge(base, override):
    """Merge `override` into `base` in place; nested dicts merge recursively."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config():
    """Return DEFAULT_CONFIG merged with the user's config file, if any."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, ValueError):
        return config
    if isinstance(user, dict):
        _deep_merge(config, user)
    return config
