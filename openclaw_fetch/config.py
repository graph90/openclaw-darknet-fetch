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


def resolve(opts, env=None):
    """Merge a dict of explicit options over env-derived defaults."""
    merged = env if env is not None else env_values()
    for key, value in opts.items():
        if value is not None:
            merged[key] = value
    merged.setdefault("cache_dir", default_cache_dir())
    return merged