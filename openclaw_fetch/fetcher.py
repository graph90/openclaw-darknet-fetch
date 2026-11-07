"""Network layer: per-network sessions, retries, cookies, redirects, limits.

The public surface is ``Fetcher`` plus the module-level convenience
functions ``fetch`` and ``fetch_many`` (re-exported from __init__).

Runtime failures are returned as ``ok: false`` result dicts, never raised.
``FetchError`` is reserved for usage-level violations (bad scheme, wrong
network for a target, non-string URL).
"""

import hashlib
import json
import os
import random
import re
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import requests

from . import config
from .cache import TtlCache, has_cookies
from .errors import FetchError, error_result, exit_code_for
from .parse import parse_document
from .result import Result
from .util import (RawBody, estimate_tokens, host_of, public_ip_addresses,
                   raw_text, redact_url)

NETWORKS = ("normal", "tor", "i2p", "auto")
NETWORK_LABEL = {"normal": "NORMAL", "tor": "TOR", "i2p": "I2P", "auto": "AUTO"}

# Retriable statuses: rate-limited (429) and server errors (5xx).
def _retriable_status(status):
    return status == 429 or 500 <= status < 600


def _normalize_network(network):
    net = (network or "normal").lower()
    if net in NETWORKS:
        return net
    if net.upper() in NETWORK_LABEL.values():
        return net.lower()
    raise FetchError("USAGE", "unknown network %r (expected normal/tor/i2p/auto)" % (network,))


def _target_tld(url):
    host = host_of(url)
    if not host:
        return "clearnet"
    if host.endswith(".onion"):
        return "onion"
    if host.endswith(".i2p"):
        return "i2p"
    return "clearnet"


def _now_iso():
    """Current UTC time as ISO-8601 ``...Z`` (provenance timestamp)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def onion_info(host):
    """Describe an onion host: ``{"is_onion", "valid", "version"}``.

    v3 addresses are 56 base32 chars encoding pubkey(32) + checksum(2) +
    version(1=0x03); the checksum is the first two bytes of
    ``SHA3-256(".onion checksum" || pubkey || version)``. Shorter/fake names
    report ``valid=False`` so typo'd addresses can be flagged without breaking
    legacy short names when strict mode is off.
    """
    if not isinstance(host, str):
        return {"is_onion": False, "valid": False, "version": None}
    h = host.strip().lower()
    if not h.endswith(".onion"):
        return {"is_onion": False, "valid": False, "version": None}
    label = h[:-6]
    version = None
    valid = False
    try:
        if len(label) == 56 and all(c in "abcdefghijklmnopqrstuvwxyz234567" for c in label):
            import base64

            raw = base64.b32decode(label.upper() + "=" * ((8 - len(label) % 8) % 8))
            if len(raw) == 35:
                pubkey, checksum, ver = raw[:32], raw[32:34], raw[34]
                version = ver
                want = hashlib.sha3_256(b".onion checksum" + pubkey + bytes([ver])).digest()[:2]
                valid = ver == 3 and want == checksum
            else:
                version = raw[-1] if len(raw) >= 1 else None
                valid = False
    except Exception:
        valid = False
    return {"is_onion": True, "valid": valid, "version": version}


def is_valid_onion(host):
    """True when ``host`` is a valid v3 onion hostname (checksum-verified)."""
    info = onion_info(host)
    return bool(info.get("is_onion") and info.get("valid"))


def _response_headers(resp_headers):
    """Lower-cased snapshot of response headers (safe for results/logs)."""
    try:
        return {str(k).lower(): str(v) for k, v in dict(resp_headers or {}).items()}
    except Exception:
        return {}


def _harvest_mirrors(headers_lower):
    """Extract onion/I2P mirror hints from lower-cased response headers."""
    onion = None
    for key in ("onion-location", "x-onion-location"):
        if headers_lower.get(key):
            onion = headers_lower[key].strip()
            break
    i2p = []
    for key, value in headers_lower.items():
        if key.startswith("x-i2p-") and value and str(value).strip():
            i2p.append(str(value).strip())
    return onion, i2p


def _is_http_scheme(url):
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https")


def _idna_host(host):
    """Punycode-normalize an IDN hostname; pass through as-is on failure."""
    import codecs

    if ":" in host:
        return host
    try:
        return codecs.encode(host, "idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return host


def validate_url(url, network=None):
    """Raise ``FetchError`` on usage violations; return normalized URL."""
    if not isinstance(url, str) or not url.strip():
        raise FetchError("USAGE", "URL must be a non-empty string")
    url = url.strip()
    if url.startswith(("//", "/")):
        raise FetchError("USAGE", "URL must be absolute (got %r)" % url)
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise FetchError("USAGE", "invalid URL") from exc
    if not _is_http_scheme(url):
        raise FetchError(
            "SCHEME_BLOCKED",
            "only http/https are allowed (got %r)" % url.split(":", 1)[0],
        )
    clean = parsed._replace(
        netloc=parsed.netloc.rsplit("@", 1)[-1],
    )
    host = _idna_host(clean.hostname or "")
    if not host:
        raise FetchError("USAGE", "URL must include a hostname")
    try:
        port = clean.port
    except ValueError as exc:
        raise FetchError("USAGE", "URL has an invalid port") from exc
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    netloc = host
    if port is not None:
        netloc += ":%d" % port
    clean = clean._replace(netloc=netloc)
    return clean.geturl()


def route_error(url):
    """Return (error_code, message) if the target needs a different network."""
    tld = _target_tld(url)
    if tld == "onion":
        return "NETWORK_MISMATCH", "target is a .onion address: use network='tor' (-t)"
    if tld == "i2p":
        return "NETWORK_MISMATCH", "target is a .i2p address: use network='i2p' (-i)"
    return None, None


def resolve_network(network, url):
    """Concrete network for ``url``: ``auto`` follows the target's suffix."""
    net = _normalize_network(network)
    if net != "auto":
        return net
    tld = _target_tld(url)
    if tld == "onion":
        return "tor"
    if tld == "i2p":
        return "i2p"
    return "normal"


def check_route(network, url):
    """Return (error_code, message) when ``network`` cannot reach ``url``."""
    net = resolve_network(network, url)
    tld = _target_tld(url)
    if net == "normal":
        return route_error(url)
    if net == "tor" and tld == "i2p":
        return "NETWORK_MISMATCH", "I2P eepsites cannot be reached over Tor: use network='i2p' (-i)"
    if net == "i2p" and tld == "onion":
        return "NETWORK_MISMATCH", "Tor onion services cannot be reached over I2P: use network='tor' (-t)"
    return None, None


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_SENSITIVE_REDIRECT_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "host",
    "x-api-key",
    "x-auth-token",
    "x-access-token",
}


def _origin(url):
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


def _redirect_headers(headers, source, target):
    result = dict(headers or {})
    if _origin(source) != _origin(target):
        for key in list(result):
            if key.lower() in _SENSITIVE_REDIRECT_HEADERS:
                result.pop(key, None)
    return result


def _is_redirect_response(resp):
    return resp.status_code in _REDIRECT_STATUSES and bool(resp.headers.get("Location"))


class _CookieStore:
    """Persistent JSON cookie jar with atomic writes and a thread lock."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self._loaded = False

    def load(self, session):
        if not self.path or self._loaded:
            return False
        self._loaded = True
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        for c in data.get("cookies", []):
            try:
                session.cookies.set(**c)
            except TypeError:
                try:
                    session.cookies.set(c["name"], c["value"], domain=c.get("domain"))
                except Exception:
                    pass
        return bool(data.get("cookies"))

    def save(self, session):
        if not self.path:
            return
        cookies = []
        for c in session.cookies:
            cookies.append(
                {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path,
                    "secure": c.secure,
                    "expires": c.expires,
                    "discard": getattr(c, "discard", False),
                }
            )
        payload = {"cookies": cookies, "saved_at": time.time()}
        with self.lock:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = _mkstemp(directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise


class _RateLimiter:
    """Token-bucket rate limiter (per Fetcher; shared across its workers)."""

    def __init__(self, rps, max_burst=None):
        self.rps = max(0.0, float(rps))
        self.max_burst = max_burst or max(1, int(self.rps) if self.rps else 1)
        self._tokens = float(self.max_burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def wait(self):
        if self.rps <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.max_burst,
                    self._tokens + (now - self._updated) * self.rps,
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rps
            time.sleep(wait)


def _mkstemp(directory):
    import tempfile

    return tempfile.mkstemp(prefix=".ocf-cookies-", suffix=".tmp", dir=directory)


class _RobotsGuard:
    """Minimal robots.txt checker with per-host caching (single pass)."""

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self._cache = {}
        self._lock = threading.Lock()

    def allowed(self, url):
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        origin = "%s://%s" % (parts.scheme, parts.netloc)
        with self._lock:
            rules = self._cache.get(origin)
        if rules is None:
            rules = self._fetch(origin, parts.scheme, parts.netloc)
            with self._lock:
                self._cache[origin] = rules
        path = parts.path or "/"
        if not rules or rules == "*":
            return True
        for disallowed in rules:
            if disallowed == "*" or path.startswith(disallowed):
                return False
        return True

    def _fetch(self, origin, scheme, netloc):
        resp = self.fetcher.fetch(
            "%s://%s/robots.txt" % (scheme, netloc),
            allow_errors=True,
            keep_raw=True,
            _skip_guards=True,
        )
        raw = raw_text(resp)
        if not resp.ok and resp.get("status") not in (404, 401, 403):
            return "*"  # robots unreachable -> allow (never hard-block)
        return _parse_robots(raw)


def _parse_robots(text):
    disallowed = []
    in_ours = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        if low.startswith("user-agent"):
            agent = line.split(":", 1)[1].strip() if ":" in line else ""
            in_ours = agent == "*" or agent.lower() == "openclawdarknetfetch"
        elif low.startswith("disallow") and in_ours:
            val = line.split(":", 1)[1].strip() if ":" in line else ""
            if val:
                disallowed.append(val)
    return [d for d in disallowed if d != ""]


class Fetcher:
    """Reusable, configured multi-network fetcher."""

    def __init__(
        self,
        *,
        network="normal",
        tor_proxy=None,
        i2p_proxy=None,
        timeout=None,
        connect_timeout=None,
        retries=None,
        retry_backoff=None,
        cookie_jar=None,
        cache_dir=None,
        cache_ttl=None,
        use_cache=True,
        cache_max_mb=None,
        max_bytes=None,
        max_links=None,
        max_chars=None,
        headers=None,
        ua=None,
        allow_errors=False,
        max_redirects=None,
        respect_robots=False,
        rate_limit=None,
        no_private_ip=False,
        expect=None,
        isolate=None,
        strict_onion=False,
        metadata_only=False,
        budget_seconds=None,
        budget_bytes=None,
        budget_requests=None,
        budget_tokens=None,
        log_jsonl=None,
        watch=False,
        tor_verify=False,
        config_path=None,
        selector=None,
        interstitial_retry=True,
        interstitial_wait=1.0,
        dead_ledger=None,
        dead_threshold=3,
        newnym_on_fail=0,
        tor_control=None,
        i2p_jump=None,
        via_archive=False,
    ):
        opts = config.resolve({}, config_path=config_path)
        self.config_path = os.path.expanduser(config_path) if config_path else None
        self.network = _normalize_network(network)
        self.tor_proxy = tor_proxy or os.environ.get("OPENCLAW_TOR_PROXY") or opts.get("tor_proxy", config.TOR_PROXY_DEFAULT)
        self.i2p_proxy = i2p_proxy or os.environ.get("OPENCLAW_I2P_PROXY") or opts.get("i2p_proxy", config.I2P_PROXY_DEFAULT)
        self._timeout_explicit = timeout is not None
        self._connect_timeout_explicit = connect_timeout is not None
        self.timeout = timeout if timeout is not None else opts.get("timeout", config.DEFAULT_TIMEOUT)
        self.connect_timeout = connect_timeout if connect_timeout is not None else opts.get("connect_timeout", config.DEFAULT_CONNECT_TIMEOUT)
        self.isolate = bool(opts.get("isolate", config.DEFAULT_ISOLATE)) if isolate is None else bool(isolate)
        self.strict_onion = bool(strict_onion)
        self.metadata_only = bool(metadata_only)
        self.watch = bool(watch)
        self.tor_verify = bool(tor_verify)
        self.selector = selector
        self.interstitial_retry = bool(interstitial_retry)
        self.interstitial_wait = float(interstitial_wait)
        self.dead_ledger = dead_ledger
        self.dead_threshold = int(dead_threshold)
        self.newnym_on_fail = int(newnym_on_fail or 0)
        self.tor_control = tor_control or "127.0.0.1:9051"
        self.i2p_jump = i2p_jump
        self.via_archive = bool(via_archive)
        self._tor_consecutive_fails = 0
        self._exit_cache = {}
        self._dead_lock = threading.Lock()
        self.log_jsonl = log_jsonl
        self._log_lock = threading.Lock()
        self.budget_seconds = budget_seconds
        self.budget_bytes = budget_bytes
        self.budget_requests = budget_requests
        self.budget_tokens = budget_tokens
        self._budget_started = time.monotonic()
        self._budget_lock = threading.Lock()
        self._budget_counts = {"requests": 0, "bytes": 0, "tokens": 0}
        self._validators = {}
        self._validators_lock = threading.Lock()

        self.retries = retries if retries is not None else opts.get("retries", config.DEFAULT_RETRIES)
        self.retry_backoff = retry_backoff if retry_backoff is not None else opts.get("retry_backoff", config.DEFAULT_RETRY_BACKOFF)
        self.cookie_jar = cookie_jar
        self.max_bytes = max_bytes if max_bytes is not None else opts.get("max_bytes", config.DEFAULT_MAX_BYTES)
        self.max_links = max_links if max_links is not None else opts.get("max_links", config.DEFAULT_MAX_LINKS)
        self.max_chars = max_chars if max_chars is not None else opts.get("max_chars", config.DEFAULT_MAX_CHARS)
        self.max_redirects = max_redirects if max_redirects is not None else opts.get("max_redirects", config.DEFAULT_MAX_REDIRECTS)
        self.max_redirects = max(0, int(self.max_redirects))
        self.allow_errors = allow_errors
        self.respect_robots = respect_robots
        self.rate_limit = rate_limit
        self.no_private_ip = bool(no_private_ip)
        self.expect = expect
        self.cache_max_mb = cache_max_mb
        self._rate = _RateLimiter(rate_limit) if rate_limit else None
        self._robots = _RobotsGuard(self) if respect_robots else None
        self.ua = ua or opts.get("ua") or config.DEFAULT_UA
        self.headers = dict(headers or {})
        self.headers.setdefault("User-Agent", self.ua)
        self._extra_headers = dict(headers or {})
        self._local = threading.local()
        self._jar = _CookieStore(cookie_jar)
        ttl = cache_ttl if cache_ttl is not None else opts.get("cache_ttl", config.DEFAULT_CACHE_TTL)
        self.cache = TtlCache(cache_dir=cache_dir, ttl=ttl, enabled=use_cache)
        self._cache_safe = bool(use_cache)

    def clone(self, **overrides):
        """A new Fetcher with the same configuration, minus per-call state.

        Used when one logical operation needs two different output budgets (an
        untruncated search-engine page plus truncated article pages) while
        keeping identical network, proxy, cookie and header behaviour.
        """
        options = {
            "network": self.network,
            "tor_proxy": self.tor_proxy,
            "i2p_proxy": self.i2p_proxy,
            "timeout": self.timeout,
            "connect_timeout": self.connect_timeout,
            "retries": self.retries,
            "retry_backoff": self.retry_backoff,
            "cookie_jar": self.cookie_jar,
            "cache_dir": self.cache.cache_dir,
            "cache_ttl": self.cache.ttl,
            "use_cache": self.cache.enabled,
            "cache_max_mb": self.cache_max_mb,
            "max_bytes": self.max_bytes,
            "max_links": self.max_links,
            "max_chars": self.max_chars,
            "max_redirects": self.max_redirects,
            "respect_robots": self.respect_robots,
            "rate_limit": self.rate_limit,
            "no_private_ip": self.no_private_ip,
            "expect": self.expect,
            "isolate": self.isolate,
            "strict_onion": self.strict_onion,
            "metadata_only": self.metadata_only,
            "watch": self.watch,
            "tor_verify": self.tor_verify,
            "config_path": self.config_path,
            "selector": self.selector,
            "interstitial_retry": self.interstitial_retry,
            "interstitial_wait": self.interstitial_wait,
            "dead_ledger": self.dead_ledger,
            "dead_threshold": self.dead_threshold,
            "newnym_on_fail": self.newnym_on_fail,
            "tor_control": self.tor_control,
            "i2p_jump": self.i2p_jump,
            "via_archive": self.via_archive,
            "log_jsonl": self.log_jsonl,
            "budget_seconds": self.budget_seconds,
            "budget_bytes": self.budget_bytes,
            "budget_requests": self.budget_requests,
            "budget_tokens": self.budget_tokens,
            "headers": dict(self._extra_headers),
            "ua": self.ua,
            "allow_errors": self.allow_errors,
        }
        options.update(overrides)
        return Fetcher(**options)

    # -- sessions ---------------------------------------------------------
    def _session(self, network=None):
        net = network or self.network
        sessions = getattr(self._local, "sessions", None)
        if sessions is None:
            sessions = {}
            self._local.sessions = sessions
        session = sessions.get(net)
        if session is None:
            session = self._new_session(net)
            sessions[net] = session
        return session

    def _new_session(self, network=None):
        net = network or self.network
        session = requests.Session()
        session.max_redirects = self.max_redirects
        if net != "normal":
            session.trust_env = False
        proxy = self.proxy_url(net)
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        self._jar.load(session)
        return session

    def proxy_url(self, network=None):
        """Configured proxy for ``network``, without isolation credentials."""
        net = network or self.network
        if net == "tor":
            return self.tor_proxy
        if net == "i2p":
            return self.i2p_proxy
        return None

    def _isolated_proxy(self, network=None, tag=""):
        """Return the proxy URL to dial for one request.

        Tor isolates streams by SOCKS credentials (``IsolateSOCKSAuth``), so a
        unique user/password per request puts every request on its own circuit.
        ``tag`` lets a retry ask for a different circuit.
        """
        net = network or self.network
        proxy = self.proxy_url(net)
        if not proxy or net != "tor" or not self.isolate:
            return proxy
        parts = urlsplit(proxy)
        if not parts.scheme or not parts.hostname:
            return proxy
        user = "isolate-%s" % uuid.uuid4().hex[:12]
        credentials = "%s:%s" % (user, uuid.uuid4().hex[:8])
        if tag:
            credentials = "%s-%s" % (credentials, tag)
        host = parts.hostname
        if ":" in host and not host.startswith("["):
            host = "[%s]" % host
        netloc = "%s@%s" % (credentials, host)
        if parts.port:
            netloc += ":%d" % parts.port
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    def _has_cookies(self):
        if has_cookies(self.cookie_jar):
            return True
        try:
            return bool(self._session().cookies)
        except Exception:
            return True

    def _cache_allowed(self, headers=None):
        if not self._cache_safe or self._has_cookies():
            return False
        for key in headers or {}:
            if key.lower() in _SENSITIVE_REDIRECT_HEADERS:
                return False
        return True

    def _cache_context(self, method, headers, allow_errors, network=None):
        payload = {
            "method": str(method).upper(),
            "headers": sorted(
                (str(key).lower(), str(value))
                for key, value in (headers or {}).items()
            ),
            "proxy": self.proxy_url(network) or "",
            "expect": self.expect or "",
            "allow_errors": bool(allow_errors),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _network_for(self, url):
        """Concrete network for ``url`` (``auto`` follows the target suffix)."""
        return resolve_network(self.network, url)

    def _validate_target(self, url):
        clean_url = validate_url(url, self.network)
        net = resolve_network(self.network, clean_url)
        code, message = check_route(net, clean_url)
        if code:
            raise FetchError(code, message)
        if self.strict_onion:
            info = onion_info(host_of(clean_url) or "")
            if info.get("is_onion") and not info.get("valid"):
                raise FetchError(
                    "ONION_INVALID",
                    "invalid v3 onion address %r (checksum/version check failed)" % host_of(clean_url),
                )
        if self.no_private_ip and net == "normal":
            host = host_of(clean_url)
            if not host:
                raise FetchError("HOST_BLOCKED", "target has no hostname")
            _, is_private, err = public_ip_addresses(host)
            if err:
                raise FetchError("HOST_BLOCKED", "cannot resolve %r: %s" % (host, err))
            if is_private:
                raise FetchError(
                    "HOST_BLOCKED",
                    "target %r resolves to a private/loopback address" % host,
                )
        return clean_url

    # -- budgets / logging / validators ------------------------------------
    def _budget_reason_and_reserve(self):
        """Atomically check budgets and consume one request slot.

        Returns the exceeded budget name, or None when the caller reserved a slot.
        """
        with self._budget_lock:
            counts = dict(self._budget_counts)
            reason = None
            if self.budget_requests is not None and counts["requests"] >= int(self.budget_requests):
                reason = "requests"
            elif self.budget_bytes is not None and counts["bytes"] >= int(self.budget_bytes):
                reason = "bytes"
            elif self.budget_tokens is not None and counts["tokens"] >= int(self.budget_tokens):
                reason = "tokens"
            elif self.budget_seconds is not None and (time.monotonic() - self._budget_started) > float(self.budget_seconds):
                reason = "seconds"
            if reason is None:
                self._budget_counts["requests"] += 1
            return reason

    def _budget_reason(self):
        """Return the exceeded budget name, or None when budget remains (no reserve)."""
        with self._budget_lock:
            counts = dict(self._budget_counts)
        if self.budget_requests is not None and counts["requests"] >= int(self.budget_requests):
            return "requests"
        if self.budget_bytes is not None and counts["bytes"] >= int(self.budget_bytes):
            return "bytes"
        if self.budget_tokens is not None and counts["tokens"] >= int(self.budget_tokens):
            return "tokens"
        if self.budget_seconds is not None:
            if (time.monotonic() - self._budget_started) > float(self.budget_seconds):
                return "seconds"
        return None

    def _budget_spend(self, content_length=0, tokens=0):
        with self._budget_lock:
            self._budget_counts["bytes"] += int(content_length or 0)
            self._budget_counts["tokens"] += int(tokens or 0)

    def _log_event(self, result):
        if not self.log_jsonl:
            return
        try:
            event = {
                "ts": _now_iso(),
                "network": result.get("network"),
                "url": redact_url(result.get("requested_url") or ""),
                "status": result.get("status"),
                "took_ms": result.get("took_ms"),
                "cached": bool(result.get("cached")),
                "ok": bool(result.get("ok")),
                "error_code": result.get("error_code"),
                "event": "fetch",
            }
            line = json.dumps(event) + "\n"
            with self._log_lock:
                directory = os.path.dirname(str(self.log_jsonl))
                if directory:
                    os.makedirs(directory, exist_ok=True)
                with open(str(self.log_jsonl), "a", encoding="utf-8") as fh:
                    fh.write(line)
        except OSError as exc:
            import sys

            sys.stderr.write("WARN: cannot write log-jsonl: %s\n" % exc)

    def _validators_for(self, url):
        if not self.watch:
            return {}
        with self._validators_lock:
            return dict(self._validators.get(url) or {})

    def _store_validators(self, url, resp_headers):
        if not self.watch:
            return
        etag = resp_headers.get("etag") or resp_headers.get("ETag") or ""
        last = resp_headers.get("last-modified") or resp_headers.get("Last-Modified") or ""
        if etag or last:
            with self._validators_lock:
                self._validators[url] = {"etag": etag, "last_modified": last}

    # -- exit verification / dead ledger -----------------------------------
    def _verify_exit(self):
        """Return (exit_ip, is_tor) for this Fetcher, probing at most once."""
        if "exit_ip" in self._exit_cache:
            return self._exit_cache["exit_ip"], self._exit_cache["is_tor"]
        probe = Fetcher(
            network="tor", tor_proxy=self.tor_proxy, timeout=max(8, self.timeout),
            retries=0, use_cache=False, max_bytes=65536, isolate=self.isolate,
        )
        # Avoid recursion when the probe fetcher itself has tor_verify on.
        probe.tor_verify = False
        result = probe._fetch_impl(
            "http://check.torproject.org/api/ip", _skip_guards=True,
        )
        exit_ip, is_tor = None, None
        if result.get("ok") and isinstance(result.get("text"), str):
            try:
                payload = json.loads(result["text"])
                exit_ip = payload.get("IP")
                is_tor = bool(payload.get("IsTor"))
            except (ValueError, AttributeError):
                pass
        self._exit_cache["exit_ip"] = exit_ip
        self._exit_cache["is_tor"] = is_tor
        return exit_ip, is_tor

    def _apply_exit_verification(self, result, net):
        """Stamp exit fields when tor_verify is on; fail closed on mismatch."""
        if not self.tor_verify or net != "tor":
            return result
        exit_ip, is_tor = self._verify_exit()
        result["exit_ip"] = exit_ip
        result["is_tor"] = is_tor
        result["exit_verified"] = bool(is_tor)
        if not is_tor and result.get("ok"):
            result["ok"] = False
            result["error"] = "exit verification failed (is_tor=%r, exit_ip=%r)" % (is_tor, exit_ip)
            result["error_code"] = "ANONYMITY_UNVERIFIED"
        return result

    def _load_dead(self):
        if not self.dead_ledger:
            return {}
        try:
            with open(os.path.expanduser(self.dead_ledger), "r", encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_dead(self, ledger):
        try:
            path = os.path.expanduser(self.dead_ledger)
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            fd, tmp = _mkstemp(directory or ".")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(ledger, fh, indent=2)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            pass

    def _dead_entry(self, host):
        if not self.dead_ledger or not host:
            return None
        with self._dead_lock:
            return self._load_dead().get(host)

    def _record_dead(self, host, ok):
        if not self.dead_ledger or not host:
            return
        with self._dead_lock:
            ledger = self._load_dead()
            entry = dict(ledger.get(host) or {})
            if ok:
                entry["consecutive_fails"] = 0
                entry["suspected_dead"] = False
                entry["last_ok_at"] = _now_iso()
            else:
                entry["consecutive_fails"] = int(entry.get("consecutive_fails") or 0) + 1
                entry["last_fail_at"] = _now_iso()
                entry["suspected_dead"] = entry["consecutive_fails"] >= max(1, self.dead_threshold)
            ledger[host] = entry
            self._save_dead(ledger)

    # -- circuit provenance / NEWNYM (v0.6) --------------------------------
    def _control(self):
        from .tor_control import TorControl

        host, _, port = (self.tor_control or "127.0.0.1:9051").partition(":")
        return TorControl(host=host or "127.0.0.1", port=int(port or 9051))

    def _maybe_newnym(self, ok):
        """Send NEWNYM after N consecutive Tor failures. Returns True when sent."""
        if self.newnym_on_fail <= 0:
            return False
        if ok:
            self._tor_consecutive_fails = 0
            return False
        self._tor_consecutive_fails += 1
        if self._tor_consecutive_fails < self.newnym_on_fail:
            return False
        self._tor_consecutive_fails = 0
        try:
            return bool(self._control().newnym())
        except Exception:
            return False

    def _stamp_circuit(self, result, net):
        """Stamp circuit fields (None when control is unavailable)."""
        result["circuit_id"] = result.get("circuit_id")
        result["guard"] = result.get("guard")
        result["exit_node"] = result.get("exit_node")
        result["exit_country"] = result.get("exit_country")
        if net != "tor":
            return result
        try:
            info = self._control().getinfo("circuit-status")
        except Exception:
            return result
        # circuit-status is multiline; take the first line as the id hint.
        result["circuit_id"] = info.splitlines()[0][:120] if info else None
        return result

    # -- I2P jump + archive fallbacks (v0.6) --------------------------------
    def _try_i2p_jump(self, host):
        """Try the jump service for ``host``; return the resolved host or None."""
        if not self.i2p_jump or not host:
            return None
        import hashlib

        jumps = [j.strip() for j in str(self.i2p_jump).split(",") if j.strip()]
        digest = hashlib.md5(host.encode("utf-8", errors="ignore")).hexdigest()[:8]
        for jump in jumps:
            jump_host = jump.split("://", 1)[-1].rstrip("/")
            jump_url = "http://%s/jump/%s/%s/" % (jump_host, digest, host)
            probe = self.clone(use_cache=False)
            res = probe._fetch_impl(jump_url, _skip_guards=True)
            if res.get("ok") or res.get("status") in (301, 302, 307, 308):
                final = (res.get("final_url") or "").strip()
                if final:
                    return final
                return res.get("requested_url")
        return None

    def _via_archive(self, clean_url):
        """Fetch the closest Wayback snapshot of ``clean_url`` (or None)."""
        probe = Fetcher(
            network="normal", timeout=max(8, self.timeout), retries=0,
            use_cache=False, max_bytes=65536, isolate=False,
        )
        res = probe._fetch_impl(
            "https://archive.org/wayback/available?url=" + clean_url,
            _skip_guards=True,
        )
        if not res.get("ok") or not res.get("text"):
            return None
        try:
            payload = json.loads(res["text"])
            snap = (payload.get("archived_snapshots") or {}).get("closest") or {}
            url = snap.get("url")
        except (ValueError, AttributeError):
            return None
        if not url:
            return None
        probe2 = Fetcher(
            network="normal", timeout=self.timeout, retries=1,
            use_cache=False, isolate=False, max_bytes=self.max_bytes,
        )
        return probe2._fetch_impl(url, _skip_guards=True)

    # -- request loop -----------------------------------------------------
    def _request(self, method, url, network=None, **request_kw):
        """Run a request with bounded retries and validated redirects."""
        current_method = str(method).upper()
        current_url = url
        current_net = network or self.network
        current_kw = dict(request_kw)
        history = []
        for redirect_count in range(self.max_redirects + 1):
            try:
                current_url = self._validate_target(current_url)
                current_net = self._network_for(current_url)
            except FetchError as exc:
                return None, (exc.error_code, str(exc))
            resp, req_error = self._request_once(
                current_method, current_url, network=current_net, **current_kw
            )
            if req_error:
                return None, req_error
            if not _is_redirect_response(resp):
                resp.history = history
                return resp, None
            location = resp.headers.get("Location")
            if redirect_count >= self.max_redirects:
                resp.close()
                return None, ("REQUEST", "too many redirects")
            try:
                next_url = self._validate_target(urljoin(current_url, location))
            except FetchError as exc:
                resp.close()
                return None, (exc.error_code, str(exc))
            redirect_status = resp.status_code
            if (
                redirect_status in (307, 308)
                and _origin(current_url) != _origin(next_url)
                and (
                    current_kw.get("data") is not None
                    or current_kw.get("json") is not None
                )
            ):
                resp.close()
                return None, (
                    "REQUEST",
                    "cross-origin redirect with a request body was blocked",
                )
            history.append(resp)
            resp.close()
            current_kw["headers"] = _redirect_headers(
                current_kw.get("headers"), current_url, next_url
            )
            if (redirect_status == 303 and current_method != "HEAD") or (
                redirect_status in (301, 302)
                and current_method not in ("GET", "HEAD")
            ):
                current_method = "GET"
                current_kw.pop("data", None)
                current_kw.pop("json", None)
                current_kw["headers"] = {
                    key: value
                    for key, value in current_kw["headers"].items()
                    if key.lower() not in ("content-length", "content-type", "transfer-encoding")
                }
            current_url = next_url
        return None, ("REQUEST", "too many redirects")

    def _timeouts(self, url):
        """Per-request ``(connect, read)`` timeouts, widened for darknet hosts.

        Onion and I2P targets pay for service-descriptor fetches and tunnel
        setup before the first byte, which the clearnet connect timeout treats
        as a failure. Explicit constructor values always win.
        """
        connect, read = self.connect_timeout, self.timeout
        if _target_tld(url) not in ("onion", "i2p"):
            return connect, read
        if not self._connect_timeout_explicit:
            connect = max(connect, config.DARKNET_CONNECT_TIMEOUT)
        if not self._timeout_explicit:
            read = max(read, config.DARKNET_TIMEOUT)
        return connect, read

    def _request_once(self, method, url, network=None, **request_kw):
        net = network or self.network
        session = self._session(net)
        if self._rate is not None:
            self._rate.wait()
        attempts = self.retries + 1
        can_retry = method in ("GET", "HEAD", "OPTIONS")
        for attempt in range(attempts):
            try:
                request_options = dict(request_kw)
                if net == "tor" and self.isolate:
                    isolated = self._isolated_proxy(net, tag="r%d" % attempt)
                    request_options["proxies"] = {"http": isolated, "https": isolated}
                resp = session.request(
                    method,
                    url,
                    timeout=self._timeouts(url),
                    stream=True,
                    allow_redirects=False,
                    **request_options,
                )

                if not _retriable_status(resp.status_code):
                    return resp, None
                if not can_retry or attempt == attempts - 1:
                    return resp, None
                delay = self._backoff(attempt)
                retry_after = _retry_after_seconds(resp)
                if retry_after is not None:
                    delay = max(delay, min(retry_after, 30))
                resp.close()
                time.sleep(delay)
            except requests.exceptions.ProxyError:
                if not can_retry or attempt == attempts - 1:
                    return None, (
                        "PROXY_DOWN",
                        "could not connect to %s proxy at %s"
                        % (net, redact_url(self.proxy_url(net))),
                    )
                time.sleep(self._backoff(attempt))
            except requests.exceptions.Timeout:
                if not can_retry or attempt == attempts - 1:
                    return None, ("TIMEOUT", "request timed out")
                time.sleep(self._backoff(attempt))
            except requests.exceptions.TooManyRedirects:
                return None, ("REQUEST", "too many redirects")
            except requests.exceptions.ConnectionError:
                if not can_retry or attempt == attempts - 1:
                    return None, ("CONNECTION", "connection failed")
                time.sleep(self._backoff(attempt))
            except requests.exceptions.RequestException as exc:
                return None, ("REQUEST", str(exc))
        return None, ("REQUEST", "request failed")

    def _backoff(self, attempt):
        base = self.retry_backoff * (2 ** attempt)
        return base * (0.5 + random.random())

    # -- body / decode ----------------------------------------------------
    def _read_and_decode(self, resp, requested_url, network=None):
        label = NETWORK_LABEL[network] if network in NETWORK_LABEL else NETWORK_LABEL[
            resolve_network(self.network, requested_url)
        ]
        try:
            headers = resp.headers
            content_length = headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > self.max_bytes:
                        return None, None, error_result(
                            "BODY_TOO_LARGE",
                            "Content-Length %s exceeds max-bytes %d" % (content_length, self.max_bytes),
                            requested_url=requested_url,
                            network=label,
                            status=resp.status_code,
                        )
                except (TypeError, ValueError):
                    pass
            chunks = []
            total = 0
            try:
                for chunk in resp.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if total > self.max_bytes:
                        return None, None, error_result(
                            "BODY_TOO_LARGE",
                            "body exceeds max-bytes %d during streaming" % self.max_bytes,
                            requested_url=requested_url,
                            network=label,
                            status=resp.status_code,
                        )
                    chunks.append(chunk)
            except requests.exceptions.RequestException:
                return None, None, error_result(
                    "CONNECTION",
                    "connection lost while reading body",
                    requested_url=requested_url,
                    network=label,
                    status=resp.status_code,
                )
            body = b"".join(chunks)
            encoding = requests.utils.get_encoding_from_headers(headers)
            if not encoding:
                encoding = _apparent_encoding(body)
            try:
                text = body.decode(encoding, errors="replace")
            except (LookupError, TypeError):
                text = body.decode("utf-8", errors="replace")
            return body, text, None
        finally:
            resp.close()

    # -- public API -------------------------------------------------------
    def fetch(self, url, method="GET", *, data=None, form=True, headers=None, allow_errors=None, keep_raw=False,
              selector=None, _skip_guards=False):
        """Fetch one URL and return a result dict (never raises for runtime errors)."""
        # Atomic check+reserve so parallel batches stop at the cap.
        reason = self._budget_reason_and_reserve()
        if reason is not None:
            # Label follows the resolved network for the requested URL.
            try:
                clean_tmp = self._validate_target(url)
                label_tmp = NETWORK_LABEL[self._network_for(clean_tmp)]
                req_url = clean_tmp
            except FetchError:
                safe_tmp = redact_url(url) if isinstance(url, str) else url
                label_tmp = NETWORK_LABEL.get(self.network, self.network)
                req_url = safe_tmp
            result = error_result(
                "BUDGET", "budget exhausted (%s)" % reason,
                requested_url=req_url, network=label_tmp,
            )
            result["budget_exhausted"] = True
            self._log_event(result)
            return result
        result = self._fetch_impl(
            url, method=method, data=data, form=form, headers=headers,
            allow_errors=allow_errors, keep_raw=keep_raw, selector=selector, _skip_guards=_skip_guards,
        )
        result = self._post_process_v06(result)
        self._budget_spend(
            content_length=result.get("content_length") or 0,
            tokens=result.get("estimated_tokens") or 0,
        )
        self._log_event(result)
        return result

    def _post_process_v06(self, result):
        """v0.6 stacking: outproxy/b32, language, circuits, NEWNYM, fallbacks."""
        try:
            host = host_of(result.get("requested_url") or "") or ""
        except Exception:
            host = ""
        try:
            net_label = (result.get("network") or "").upper()
        except Exception:
            net_label = ""
        # Legacy helper text also exposes language via metadata when present.
        try:
            text = result.get("text") or ""
            if text and not (result.get("metadata") or {}).get("language"):
                from .util import detect_language

                lang = detect_language(text)
                if lang.get("lang") != "unknown":
                    result.setdefault("metadata", {})["language"] = lang["lang"]
                    result["metadata"]["language_confidence"] = lang["confidence"]
        except Exception:
            pass
        # I2P honesty + b32.
        if result.get("metadata") is None:
            result["metadata"] = {}
        try:
            if net_label == "I2P" and result.get("target_tld") == "clearnet":
                result["metadata"]["i2p_outproxy_used"] = True
            elif net_label == "I2P":
                result["metadata"]["i2p_outproxy_used"] = False
            if host.endswith(".b32.i2p"):
                result["metadata"]["b32_host"] = True
        except Exception:
            pass
        # Circuit provenance + NEWNYM accounting.
        try:
            resolved_net = resolve_network(self.network, result.get("requested_url") or "")
        except Exception:
            resolved_net = self.network
        result = self._stamp_circuit(result, resolved_net)
        if resolved_net == "tor":
            sent = self._maybe_newnym(bool(result.get("ok")))
            if sent:
                result["metadata"]["newnym_sent"] = True
        # I2P jump fallback on failure.
        if (
            not result.get("ok")
            and self.i2p_jump
            and host.endswith(".i2p")
            and not result.get("metadata", {}).get("jump_via")
        ):
            jumped = self._try_i2p_jump(host)
            if jumped:
                retry = self._fetch_impl(
                    jumped, method="GET", keep_raw=True, _skip_guards=True,
                )
                if retry.get("ok"):
                    retry["metadata"]["jump_via"] = self.i2p_jump
                    retry["metadata"]["b32_host"] = host.endswith(".b32.i2p")
                    return self._stamp_circuit(retry, resolved_net)
        # Wayback fallback on 404/410/connection failures (clearnet only).
        if (
            self.via_archive
            and not result.get("ok")
            and result.get("target_tld") == "clearnet"
            and (result.get("status") in (404, 410) or result.get("error_code") in ("CONNECTION", "REQUEST", "TIMEOUT", "HTTP"))
            and not result.get("metadata", {}).get("via_archive")
        ):
            archived = self._via_archive(result.get("requested_url") or "")
            if archived and archived.get("ok"):
                archived["metadata"]["via_archive"] = True
                archived["metadata"]["archive_original_url"] = result.get("requested_url")
                return self._stamp_circuit(archived, "normal")
        return result

    def _fetch_impl(self, url, method="GET", *, data=None, form=True, headers=None, allow_errors=None, keep_raw=False,
              selector=None, _skip_guards=False, _interstitial_retried=False):
        t0 = time.monotonic()
        eff_allow_errors = self.allow_errors if allow_errors is None else allow_errors
        try:
            clean_url = self._validate_target(url)
        except FetchError as exc:
            safe_url = redact_url(url) if isinstance(url, str) else url
            result = error_result(
                exc.error_code, str(exc), requested_url=safe_url,
                network=NETWORK_LABEL[NETWORK_LABEL.get(self.network, self.network).lower()],
            )
            result["took_ms"] = int((time.monotonic() - t0) * 1000)
            return result
        net = self._network_for(clean_url)
        label = NETWORK_LABEL[net]
        # Suspected-dead fast-fail for .onion (ledger; no network burned).
        eff_selector = selector if selector is not None else self.selector
        host_clean = host_of(clean_url) or ""
        if self.dead_ledger and host_clean.endswith(".onion"):
            dead_entry = self._dead_entry(host_clean)
            if dead_entry and dead_entry.get("suspected_dead"):
                result = error_result(
                    "SUSPECTED_DEAD",
                    "onion %s suspected dead (%d consecutive failures)" % (
                        host_clean, dead_entry.get("consecutive_fails", 0)),
                    requested_url=clean_url, network=label,
                )
                result["suspected_dead"] = True
                result["took_ms"] = int((time.monotonic() - t0) * 1000)
                return result

        if not _skip_guards and self._robots is not None and method == "GET" and data is None:
            if not self._robots.allowed(clean_url):
                result = error_result(
                    "ROBOTS_BLOCKED", "blocked by robots.txt", requested_url=clean_url,
                    network=label,
                )
                result["took_ms"] = int((time.monotonic() - t0) * 1000)
                return result

        request_headers = dict(self.headers)
        request_headers.update(headers or {})
        # Watch mode: send validators for conditional revalidation.
        if self.watch:
            for key, value in self._validators_for(clean_url).items():
                if not value:
                    continue
                if key == "etag":
                    request_headers.setdefault("If-None-Match", value)
                elif key == "last_modified":
                    request_headers.setdefault("If-Modified-Since", value)
        use_cache = (
            self._cache_allowed(request_headers)
            and method == "GET"
            and data is None
        )
        cache_context = self._cache_context(method, request_headers, eff_allow_errors, net)
        if use_cache:
            cached = self.cache.get(
                net, clean_url, context=cache_context,
                max_bytes=self.max_bytes,
            )
            if cached is not None:
                return self._from_cache(
                    cached, clean_url, t0, keep_raw=keep_raw,
                    allow_errors=eff_allow_errors, network=net,
                )

        if data is not None and not form:
            request_headers.setdefault("Content-Type", "application/json")
            kwargs = {"data": data}
        elif data is not None:
            kwargs = {"data": data}
        else:
            kwargs = {}
        resp, req_error = self._request(
            method, clean_url, network=net, headers=request_headers, **kwargs
        )
        took = int((time.monotonic() - t0) * 1000)

        if req_error:
            code, message = req_error
            result = error_result(code, message, requested_url=clean_url, network=label)
            result["took_ms"] = took
            if host_clean.endswith(".onion"):
                self._record_dead(host_clean, False)
            return result

        body, text, read_error = self._read_and_decode(resp, clean_url, net)
        final_url = redact_url(resp.url)
        if read_error:
            read_error["took_ms"] = took
            read_error["final_url"] = final_url
            read_error["content_type"] = resp.headers.get("content-type", "")
            read_error["response_headers"] = _response_headers(resp.headers)
            read_error["fetched_at"] = _now_iso()
            read_error["content_sha256"] = hashlib.sha256(body or b"").hexdigest() if body else ""
            _oinfo = onion_info(host_of(clean_url) or "")
            read_error["onion_valid"] = _oinfo.get("valid") if _oinfo.get("is_onion") else None
            read_error["onion_version"] = _oinfo.get("version") if _oinfo.get("is_onion") else None
            read_error["validators"] = self._validators_for(clean_url)
            return read_error

        # 304 Not Modified (watch mode): no body to parse, but a successful read.
        if resp.status_code == 304:
            resp_headers = _response_headers(resp.headers)
            self._store_validators(clean_url, resp.headers)
            validators = self._validators_for(clean_url)
            oinfo = onion_info(host_of(clean_url) or "")
            result = Result(
                ok=True,
                error="",
                error_code=None,
                network=label,
                requested_url=clean_url,
                final_url=final_url,
                status=304,
                content_type=resp.headers.get("content-type", ""),
                content_length=0,
                title="",
                text_length=0,
                total_text_length=0,
                returned_text_length=0,
                estimated_tokens=0,
                estimated_tokens_total=0,
                took_ms=took,
                links=[],
                link_count=0,
                metadata={},
                needs_renderer=False,
                payload_extracted=False,
                items=[],
                text="",
                markdown="",
                headings=[],
                content_kind="unknown",
                cached=False,
                target_tld=_target_tld(clean_url),
                redirects=len(resp.history),
                redirect_chain=[redact_url(r.url) for r in resp.history] + [final_url],
                proxy_used=redact_url(self.proxy_url(net)) or None,
                fetched_at=_now_iso(),
                content_sha256="",
                response_headers=resp_headers,
                onion_valid=oinfo.get("valid") if oinfo.get("is_onion") else None,
                onion_version=oinfo.get("version") if oinfo.get("is_onion") else None,
                not_modified=True,
                validators=validators,
                metadata_only=bool(self.metadata_only),
                budget_exhausted=False,
                exit_ip=None,
                is_tor=None,
                exit_verified=False,
                suspected_dead=False,
                extracted=[],
            )
            if self.isolate and net == "tor":
                result["metadata"]["isolated"] = True
            result = self._apply_exit_verification(result, net)
            if keep_raw:
                result["_raw_body"] = RawBody(b"")
            return result

        content_type = resp.headers.get("content-type", "")
        ok = resp.status_code < 400 or eff_allow_errors
        kind = parse_document(
            text,
            content_type=content_type,
            requested_url=clean_url,
            final_url=final_url,
            source_len=len(body),
            max_links=self.max_links,
            raw_body=body,
        )
        rendered_text = _truncate(kind["text"], self.max_chars, "text")
        rendered_markdown = _truncate(kind["markdown"], self.max_chars, "text")
        if kind["kind"] in ("pdf", "image") and not kind["text"]:
            rendered_text = kind["text"]
        if self.metadata_only:
            # Keep title/headings/links/metadata; drop bodies to save tokens.
            rendered_text = ""
            rendered_markdown = ""

        expect_error = self._expect_check(kind["kind"])
        if expect_error:
            # --expect is a contract, not a hint: a mismatch stays a failure even
            # with allow_errors (which is about 4xx/5xx *bodies*).
            ok = False
        resp_headers = _response_headers(resp.headers)
        fetched_at = _now_iso()
        content_sha256 = hashlib.sha256(body or b"").hexdigest()
        oinfo = onion_info(host_of(clean_url) or "")
        # Mirror harvesting from response headers (Task 3).
        onion_hint, i2p_hints = _harvest_mirrors(resp_headers)
        if onion_hint:
            kind["metadata"]["onion_location"] = onion_hint
            kind["metadata"]["mirrors"] = [{"url": onion_hint, "source": "onion-location"}]
        if i2p_hints:
            kind["metadata"]["i2p_mirrors"] = [
                {"url": h, "source": "x-i2p-header"} for h in i2p_hints
            ]
        result = Result(
            ok=ok,
            error="" if ok else expect_error or "HTTP %d %s" % (resp.status_code, _reason(resp.status_code)),
            error_code="HTTP" if not ok and not expect_error else ("EXPECT_MISMATCH" if expect_error else None),
            network=label,
            requested_url=clean_url,
            final_url=final_url,
            status=resp.status_code,
            content_type=content_type,
            content_length=len(body),
            title=kind["title"] or "",
            text_length=len(kind["text"]),
            total_text_length=len(kind["text"]),
            returned_text_length=len(rendered_text),
            estimated_tokens=estimate_tokens(rendered_text),
            estimated_tokens_total=estimate_tokens(kind["text"]),
            took_ms=took,
            links=kind["links"],
            link_count=len(kind["links"]),
            metadata=kind["metadata"],
            needs_renderer=kind["needs_renderer"],
            payload_extracted=kind["payload_extracted"],
            items=kind["items"],
            text=rendered_text,
            markdown=rendered_markdown or "",
            headings=kind["headings"],
            content_kind=kind["kind"],
            cached=False,
            target_tld=_target_tld(clean_url),
            redirects=len(resp.history),
            redirect_chain=[redact_url(r.url) for r in resp.history] + [final_url],
            proxy_used=redact_url(self.proxy_url(net)) or None,
            fetched_at=fetched_at,
            content_sha256=content_sha256,
            response_headers=resp_headers,
            onion_valid=oinfo.get("valid") if oinfo.get("is_onion") else None,
            onion_version=oinfo.get("version") if oinfo.get("is_onion") else None,
            not_modified=False,
            validators=self._validators_for(clean_url),
            metadata_only=bool(self.metadata_only),
            budget_exhausted=False,
            exit_ip=None,
            is_tor=None,
            exit_verified=False,
            suspected_dead=False,
            extracted=[],
        )
        if self.isolate and net == "tor":
            result["metadata"]["isolated"] = True
        if expect_error:
            result["error"] = expect_error
        if not ok:
            result["error_code"] = result["error_code"] or "HTTP"
            result["error"] = result["error"] or "HTTP %d %s" % (resp.status_code, _reason(resp.status_code))
        self._store_validators(clean_url, resp.headers)
        result["validators"] = self._validators_for(clean_url)
        # v0.5: skip labels, index rows, selector extraction.
        if kind.get("kind") == "html":
            from .parse import index_rows, looks_like_index, page_label

            label = page_label(text, kind["text"])
            if label == "interstitial":
                result["metadata"]["interstitial"] = True
                result["metadata"]["skip_reason"] = "interstitial"
            elif label in ("gated", "challenge"):
                result["metadata"]["skip_reason"] = label
            if looks_like_index(len(kind["text"]), len(kind["links"])):
                result["metadata"]["is_index"] = True
                result["metadata"]["index_entry_count"] = len(kind["links"])
                result["metadata"]["index_rows"] = index_rows(kind["links"])
            # Interstitial: wait briefly and refetch once.
            if (
                label == "interstitial"
                and self.interstitial_retry
                and not _interstitial_retried
            ):
                if self.interstitial_wait > 0:
                    time.sleep(self.interstitial_wait)
                retried = self._fetch_impl(
                    url, method=method, data=data, form=form, headers=headers,
                    allow_errors=allow_errors, keep_raw=keep_raw,
                    selector=selector, _skip_guards=_skip_guards,
                    _interstitial_retried=True,
                )
                if retried.get("ok"):
                    retried["metadata"]["interstitial"] = True
                    retried["metadata"]["interstitial_retried"] = True
                    return retried
            if eff_selector:
                from .parse import extract_selector

                try:
                    result["extracted"] = extract_selector(text, eff_selector, base_url=final_url)
                except ValueError as exc:
                    bad = error_result("USAGE", str(exc), requested_url=clean_url, network=label)
                    bad["took_ms"] = took
                    return bad
            else:
                result["extracted"] = []
        else:
            result["extracted"] = []
        # Dead ledger + exit verification (v0.5).
        if host_clean.endswith(".onion"):
            self._record_dead(host_clean, bool(result.get("ok")))
        result = self._apply_exit_verification(result, net)
        self._jar.save(self._session())
        if (
            self._cache_allowed(request_headers)
            and method == "GET"
            and data is None
            and ok
            and not resp.history
            and not self.metadata_only
        ):
            self.cache.put(
                net,
                clean_url,
                resp.status_code,
                final_url,
                content_type,
                body,
                headers=dict(resp.headers),
                context=cache_context,
                fetched_at=fetched_at,
            )
            if self.cache_max_mb is not None:
                self.cache.evict_until_under(self.cache_max_mb)
        if keep_raw:
            result["_raw_body"] = RawBody(body)
        return result

    def post(self, url, *, data=None, form=True, headers=None, keep_raw=False, **kwargs):
        if data is not None and not form:
            headers = dict(headers or {})
            headers.setdefault("Content-Type", "application/json")
            if isinstance(data, dict):
                data = json.dumps(data)
        return self.fetch(url, method="POST", data=data, form=form, headers=headers, keep_raw=keep_raw, **kwargs)

    def check_proxy(self):
        """Probe the configured network/proxy and report reachability.

        ``network="auto"`` probes every network and returns one entry per
        network under ``probes``.
        """
        probes = {
            "tor": "http://check.torproject.org/api/ip",
            "i2p": "http://i2p-projekt.i2p/",
            "normal": "https://example.com/",
        }
        if self.network == "auto":
            per_network = {
                name: self.check_proxy_for(name) for name in ("normal", "tor", "i2p")
            }
            return {
                "ok": any(entry["ok"] for entry in per_network.values()),
                "network": "AUTO",
                "probes": per_network,
                "tor_proxy": redact_url(self.tor_proxy),
                "i2p_proxy": redact_url(self.i2p_proxy),
            }
        return self.check_proxy_for(self.network)

    def check_proxy_for(self, network):
        """Probe one concrete network and report reachability.

        ``network="auto"`` probes every network and returns the same shape as
        :meth:`check_proxy`.
        """
        if _normalize_network(network) == "auto":
            return self.check_proxy()
        probes = {
            "tor": "http://check.torproject.org/api/ip",
            "i2p": "http://i2p-projekt.i2p/",
            "normal": "https://example.com/",
        }
        net = _normalize_network(network)
        small = Fetcher(
            network=net,
            tor_proxy=self.tor_proxy,
            i2p_proxy=self.i2p_proxy,
            timeout=max(8, self.timeout),
            retries=1,
            retry_backoff=0.2,
            use_cache=False,
            max_bytes=65536,
            isolate=self.isolate,
        )
        result = small.fetch(probes[net], _skip_guards=True)
        out = {
            "ok": result["ok"],
            "network": result["network"],
            "status": result.get("status"),
            "error": result.get("error"),
            "error_code": result.get("error_code"),
            "proxy": redact_url(self.proxy_url(net)),
        }
        if net == "tor" and result["ok"] and isinstance(result.get("text"), str):
            try:
                payload = json.loads(result["text"])
                out["exit_ip"] = payload.get("IP")
                out["is_tor"] = payload.get("IsTor")
            except (ValueError, AttributeError):
                pass
        return out

    def _expect_check(self, kind):
        """Return an error message when ``self.expect`` disagrees with the
        parsed ``content_kind`` (catches exit-proxy interstitials that answer
        HTML when an API/JSON was requested)."""
        if not self.expect:
            return ""
        want = self.expect.lower()
        if want not in ("html", "json", "pdf", "rss", "text", "image", "unknown"):
            return ""
        if kind != want:
            return "expected content kind %r but got %r (server replied with a different content-type)" % (want, kind)
        return ""

    def _from_cache(self, cached, url, t0, keep_raw=False, allow_errors=None,
                    network=None):
        meta, body = cached["meta"], cached["body"]
        net = network or meta.get("network") or self.network
        eff_allow_errors = self.allow_errors if allow_errors is None else allow_errors
        content_type = meta.get("content_type") or ""
        final_url = redact_url(meta.get("final_url") or url)
        kind = parse_document(
            body.decode("utf-8", errors="replace"),
            content_type=content_type,
            requested_url=url,
            final_url=final_url,
            source_len=len(body),
            max_links=self.max_links,
            raw_body=body,
        )
        rendered_text = _truncate(kind["text"], self.max_chars, "text")
        rendered_markdown = _truncate(kind["markdown"], self.max_chars, "text")
        if self.metadata_only:
            rendered_text = ""
            rendered_markdown = ""
        status = meta.get("status")
        expect_error = self._expect_check(kind["kind"])
        ok = status is not None and (status < 400 or eff_allow_errors)
        if expect_error and not eff_allow_errors:
            ok = False
        if status is None:
            error = "cached response has no status"
        elif expect_error and not eff_allow_errors:
            error = expect_error
        else:
            error = "" if ok else "HTTP %s (from cache)" % status
        error_code = None
        if expect_error and not eff_allow_errors:
            error_code = "EXPECT_MISMATCH"
        elif not ok:
            error_code = "HTTP"
        cached_headers = meta.get("headers")
        try:
            resp_headers = {str(k).lower(): str(v) for k, v in dict(cached_headers or {}).items()}
        except Exception:
            resp_headers = {}
        onion_hint, i2p_hints = _harvest_mirrors(resp_headers)
        if onion_hint:
            kind["metadata"]["onion_location"] = onion_hint
            kind["metadata"]["mirrors"] = [{"url": onion_hint, "source": "onion-location"}]
        if i2p_hints:
            kind["metadata"]["i2p_mirrors"] = [
                {"url": h, "source": "x-i2p-header"} for h in i2p_hints
            ]
        oinfo = onion_info(host_of(url) or "")
        result = Result(
            ok=ok,
            error=error,
            error_code=error_code,
            network=NETWORK_LABEL[net],
            requested_url=url,
            final_url=final_url,
            status=status,
            content_type=content_type,
            content_length=len(body),
            title=kind["title"] or "",
            text_length=len(kind["text"]),
            total_text_length=len(kind["text"]),
            returned_text_length=len(rendered_text),
            estimated_tokens=estimate_tokens(rendered_text),
            estimated_tokens_total=estimate_tokens(kind["text"]),
            took_ms=0,
            links=kind["links"],
            link_count=len(kind["links"]),
            metadata=kind["metadata"],
            needs_renderer=kind["needs_renderer"],
            payload_extracted=kind["payload_extracted"],
            items=kind["items"],
            text=rendered_text,
            markdown=rendered_markdown or "",
            headings=kind["headings"],
            content_kind=kind["kind"],
            cached=True,
            target_tld=_target_tld(url),
            redirects=0,
            redirect_chain=[],
            proxy_used=redact_url(self.proxy_url(net)) or None,
            fetched_at=meta.get("fetched_at") or _now_iso(),
            content_sha256=hashlib.sha256(body or b"").hexdigest(),
            response_headers=resp_headers,
            onion_valid=oinfo.get("valid") if oinfo.get("is_onion") else None,
            onion_version=oinfo.get("version") if oinfo.get("is_onion") else None,
            not_modified=False,
            validators=self._validators_for(url),
            metadata_only=bool(self.metadata_only),
            budget_exhausted=False,
            exit_ip=None,
            is_tor=None,
            exit_verified=False,
            suspected_dead=False,
            extracted=[],
        )
        if kind.get("kind") == "html":
            from .parse import index_rows, looks_like_index, page_label

            raw_html = body.decode("utf-8", errors="replace")
            label = page_label(raw_html, kind["text"])
            if label == "interstitial":
                result["metadata"]["interstitial"] = True
                result["metadata"]["skip_reason"] = "interstitial"
            elif label in ("gated", "challenge"):
                result["metadata"]["skip_reason"] = label
            if looks_like_index(len(kind["text"]), len(kind["links"])):
                result["metadata"]["is_index"] = True
                result["metadata"]["index_entry_count"] = len(kind["links"])
                result["metadata"]["index_rows"] = index_rows(kind["links"])
            if self.selector:
                from .parse import extract_selector

                try:
                    result["extracted"] = extract_selector(raw_html, self.selector, base_url=final_url)
                except ValueError:
                    result["extracted"] = []
        result = self._apply_exit_verification(result, net)
        if keep_raw:
            result["_raw_body"] = RawBody(body)
        return result

    def fetch_many(self, urls, concurrency=4, keep_raw=False, **fetch_kwargs):
        """Fetch many URLs with a bounded thread pool. Order preserved."""
        results = [None] * len(urls)
        if concurrency <= 1 or len(urls) <= 1:
            return [self.fetch(u, keep_raw=keep_raw, **fetch_kwargs) for u in urls]
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {
                pool.submit(self.fetch, u, keep_raw=keep_raw, **fetch_kwargs): i
                for i, u in enumerate(urls)
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()
        return results


def _apparent_encoding(content):
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(content).best()
        if best and best.encoding:
            return best.encoding
    except ImportError:
        pass
    return "utf-8"


def _retry_after_seconds(resp):
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        import calendar
        from email.utils import parsedate

        parsed = parsedate(value)
        if parsed:
            then = calendar.timegm(parsed)
            return max(0.0, then - time.time())
        return None


def _reason(status):
    from http import HTTPStatus

    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return ""


def _truncate(text, max_chars, what="text"):
    if max_chars <= 0 or not text:
        return text
    if len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + "\n\n[OUTPUT TRUNCATED]"
        + "\nOriginal %s length: %d" % (what, len(text))
        + "\nReturned: %d" % max_chars
    )


_FORMATS = ("text", "markdown", "raw", "html")


def _apply_format(result, fmt):
    """Promote one field to ``result["text"]`` per the requested format.

    Applied by the convenience wrappers only: ``Fetcher`` always returns every
    field, and the CLI/MCP layers choose what to print.
    """
    if not fmt or fmt == "text":
        return result
    if fmt not in _FORMATS:
        raise FetchError("USAGE", "unknown format %r (use %s)" % (fmt, "|".join(_FORMATS)))
    if not result.ok:
        return result
    if fmt == "raw":
        result["text"] = raw_text(result)
    elif fmt == "html":
        result["text"] = result.get("text") or result.get("markdown") or ""
    else:
        result["text"] = result.get("markdown") or result.get("text") or ""
    return result


def fetch(url, format="text", method=None, data=None, form=True, headers=None, **kwargs):
    """Fetch a single URL through the default normal network (or override).

    ``format`` picks which field is promoted into ``result["text"]``
    (``text``/``markdown``/``raw``/``html``); every field is always present.
    """
    keep_raw = kwargs.pop("keep_raw", True)
    result = Fetcher(**kwargs).fetch(
        url, method=method or "GET", data=data, form=form, headers=headers,
        keep_raw=keep_raw,
    )
    return _apply_format(result, format)


def fetch_many(urls, concurrency=4, format="text", method=None, data=None, form=True, headers=None, **kwargs):
    """Fetch many URLs, returning results in input order."""
    keep_raw = kwargs.pop("keep_raw", True)
    fetcher = Fetcher(**kwargs)
    fetch_kw = {}
    if method is not None:
        fetch_kw["method"] = method
    if data is not None:
        fetch_kw["data"] = data
        fetch_kw["form"] = form
    if headers is not None:
        fetch_kw["headers"] = headers
    results = fetcher.fetch_many(urls, concurrency=concurrency, keep_raw=keep_raw, **fetch_kw)
    return [_apply_format(r, format) for r in results]