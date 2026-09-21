"""Application configuration package.

The settings object lives in `config/settings.py`; this re-export keeps
`from config import get_settings` working, which is how every module in the app
reaches configuration. Nothing about how settings are loaded changed when
`config.py` became this package - same env var names, same defaults, same
`.env` file, same `@lru_cache` instance.
"""

from config.settings import (
    PLACEHOLDER_MARKERS,
    SERVER_DIR,
    Settings,
    get_settings,
    placeholder_env_values,
    unknown_env_keys,
)

__all__ = [
    "PLACEHOLDER_MARKERS",
    "SERVER_DIR",
    "Settings",
    "get_settings",
    "placeholder_env_values",
    "unknown_env_keys",
]
