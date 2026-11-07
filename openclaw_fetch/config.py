"""Configuration resolution: env overrides + optional config file + defaults.

Precedence (lowest -> highest): built-in default, ``config.toml``,
environment variable, explicit CLI/library argument.
"""

import os

from .version import __version__

DEFAULT_UA = (
    "Mozilla/5.0 "
    "(compatible; OpenClawDarknetFetch/%s)"
    % __version__
)

TOR_PROXY_DEFAULT = "socks5h://127.0.0.1:9050"
I2P_PROXY_DEFAULT = "http://127.0.0.1:4444"

DEFAULT_TIMEOUT = 30
DEFAULT_CONNECT_TIMEOUT = 10
#: Onion/I2P targets must fetch service descriptors and build a tunnel before
#: the first byte flows, which regularly exceeds the clearnet connect timeout.
DARKNET_CONNECT_TIMEOUT = 45
#: Onion services are frequently slow to answer; a plain read timeout of 30s
#: produces spurious failures on a fresh circuit.
DARKNET_TIMEOUT = 90
DEFAULT_RETRIES = 2
DEFAULT_RETRY_BACKOFF = 1.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024      # hard body cap (decompression-bomb guard)
DEFAULT_MAX_CHARS = 12000
DEFAULT_MAX_LINKS = 50
DEFAULT_CACHE_TTL = 3600
DEFAULT_CACHE_DIR = os.path.join("~", ".cache", "openclaw-fetch")
DEFAULT_CONCURRENCY = 4
DEFAULT_MAX_REDIRECTS = 30
#: Route each Tor request over its own circuit (Tor's IsolateSOCKSAuth).
DEFAULT_ISOLATE = True

_ENV_PREFIX = "OPENCLAW_"

# env var name -> (type, default)
_ENV_BINDINGS = {
    "TOR_PROXY": ("str", TOR_PROXY_DEFAULT),
    "I2P_PROXY": ("str", I2P_PROXY_DEFAULT),
    "UA": ("str", None),
    "TIMEOUT": ("int", DEFAULT_TIMEOUT),
    "CONNECT_TIMEOUT": ("int", DEFAULT_CONNECT_TIMEOUT),
    "RETRIES": ("int", DEFAULT_RETRIES),
    "RETRY_BACKOFF": ("float", DEFAULT_RETRY_BACKOFF),
    "MAX_BYTES": ("int", DEFAULT_MAX_BYTES),
    "MAX_CHARS": ("int", DEFAULT_MAX_CHARS),
    "MAX_LINKS": ("int", DEFAULT_MAX_LINKS),
    "CACHE_TTL": ("int", DEFAULT_CACHE_TTL),
    "CACHE_DIR": ("str", DEFAULT_CACHE_DIR),
    "CONCURRENCY": ("int", DEFAULT_CONCURRENCY),
    "MAX_REDIRECTS": ("int", DEFAULT_MAX_REDIRECTS),
    "ISOLATE": ("bool", DEFAULT_ISOLATE),
}


def _coerce(kind, raw):
    if kind == "str":
        return raw
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "bool":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return raw


def env_values():
    """Read all recognized ``OPENCLAW_*`` variables into an options dict."""
    opts = {}
    for key, (kind, default) in _ENV_BINDINGS.items():
        raw = os.environ.get(_ENV_PREFIX + key)
        if raw is None:
            if default is not None:
                opts[key.lower()] = default
            continue
        try:
            opts[key.lower()] = _coerce(kind, raw)
        except ValueError:
            opts[key.lower()] = default
    return opts


def default_cache_dir():
    """Absolute path to ``~/.cache/openclaw-fetch``."""
    return os.path.expanduser(env_values().get("cache_dir", DEFAULT_CACHE_DIR))


# -- config.toml (v0.5) -------------------------------------------------------
# Flat keys plus [tor]/[i2p]/[cache] tables, flattened to the env key names.

_FILE_KEYS = {
    "tor_proxy": "str",
    "i2p_proxy": "str",
    "ua": "str",
    "timeout": "int",
    "connect_timeout": "int",
    "retries": "int",
    "retry_backoff": "float",
    "max_bytes": "int",
    "max_chars": "int",
    "max_links": "int",
    "cache_ttl": "int",
    "cache_dir": "str",
    "concurrency": "int",
    "max_redirects": "int",
    "isolate": "bool",
}


def _parse_toml_minimal(text):
    """Tiny TOML reader for our subset (strings, numbers, bools, tables).

    Used only when stdlib ``tomllib`` is unavailable (Python < 3.11). Handles
    ``key = value``, ``[table]`` headers, comments, quoted strings.
    """
    data = {}
    current = data
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip() if not raw_line.strip().startswith(("'", '"')) else raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            current = data.setdefault(name, {})
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().strip('"').strip("'")
        value = value.strip().split("#", 1)[0].strip()
        if value.startswith(('"', "'")) and value.endswith(('"', "'")) and len(value) >= 2:
            parsed = value[1:-1]
        elif value.lower() in ("true", "false"):
            parsed = value.lower() == "true"
        else:
            try:
                parsed = int(value)
            except ValueError:
                try:
                    parsed = float(value)
                except ValueError:
                    parsed = value.strip('"').strip("'")
        current[key] = parsed
    return data


def load_config(path):
    """Load ``config.toml`` at ``path`` into a flat options dict ({} on failure)."""
    if not path:
        return {}
    try:
        with open(os.path.expanduser(path), "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    try:
        import tomllib

        parsed = tomllib.loads(raw.decode("utf-8"))
    except ImportError:
        parsed = _parse_toml_minimal(raw.decode("utf-8", errors="replace"))
    except Exception:
        parsed = _parse_toml_minimal(raw.decode("utf-8", errors="replace"))
    flat = {}
    for key, value in parsed.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                flat[str(sub_key)] = sub_value
        else:
            flat[str(key)] = value
    out = {}
    for key, kind in _FILE_KEYS.items():
        if key not in flat:
            continue
        value = flat[key]
        try:
            if kind == "bool":
                if isinstance(value, bool):
                    out[key] = value
                else:
                    out[key] = _coerce("bool", str(value))
            elif isinstance(value, bool):
                continue
            else:
                out[key] = _coerce(kind, str(value))
        except ValueError:
            continue
    return out


def resolve(opts, env=None, config_path=None):
    """Merge: built-in defaults < config file < env < explicit ``opts``.

    ``opts`` entries with ``None`` are ignored. ``config_path`` loads extra
    TOML defaults underneath the env layer. When ``env`` is passed it replaces
    the env layer (tests); otherwise the process environment is used.
    """
    merged = {}
    for env_key, (_, default) in _ENV_BINDINGS.items():
        if default is not None:
            merged[env_key.lower()] = default
    if config_path:
        merged.update(load_config(config_path))
    if env is not None:
        merged.update(env)
    else:
        merged.update(_explicit_env_values())
    for key, value in opts.items():
        if value is not None:
            merged[key] = value
    if config_path:
        merged["config_source"] = os.path.expanduser(config_path)
    merged.setdefault("cache_dir", default_cache_dir())
    return merged


def _explicit_env_values():
    """Only the OPENCLAW_* variables actually present in os.environ."""
    opts = {}
    for key, (kind, _default) in _ENV_BINDINGS.items():
        raw = os.environ.get(_ENV_PREFIX + key)
        if raw is None:
            continue
        try:
            opts[key.lower()] = _coerce(kind, raw)
        except ValueError:
            continue
    return opts