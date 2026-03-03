"""Build environment dict with credential vars stripped (for subprocess/shell tools)."""

import os

_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
    "TMPDIR", "TEMP", "TMP",
    "PYTHONPATH", "PYTHONHOME", "PYTHONDONTWRITEBYTECODE",
    "PYTHONIOENCODING", "PYTHONUNBUFFERED",
    "VIRTUAL_ENV",
    "TZ",
})

_CREDENTIAL_PATTERNS = (
    "KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "PASS",
    "AUTH", "CREDENTIAL", "CERT", "PRIVATE", "API_",
    "ACCESS_", "NANOBOT_", "DATABASE_", "DB_", "REDIS_",
    "MONGO_", "POSTGRES_", "MYSQL_", "WEBHOOK_",
)


def build_safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return env dict with credential-like vars stripped. Optional extra k/v added."""
    safe: dict[str, str] = {}
    for k, v in os.environ.items():
        k_upper = k.upper()
        if any(pat in k_upper for pat in _CREDENTIAL_PATTERNS):
            continue
        if k in _SAFE_ENV_KEYS:
            safe[k] = v
    if extra:
        safe.update(extra)
    return safe
