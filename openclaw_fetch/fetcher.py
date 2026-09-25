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
    ):
        opts = config.resolve({})
        self.network = _normalize_network(network)
        self.tor_proxy = tor_proxy or os.environ.get("OPENCLAW_TOR_PROXY") or opts.get("tor_proxy", config.TOR_PROXY_DEFAULT)
        self.i2p_proxy = i2p_proxy or os.environ.get("OPENCLAW_I2P_PROXY") or opts.get("i2p_proxy", config.I2P_PROXY_DEFAULT)
        self._timeout_explicit = timeout is not None
        self._connect_timeout_explicit = connect_timeout is not None
        self.timeout = timeout if timeout is not None else opts.get("timeout", config.DEFAULT_TIMEOUT)
        self.connect_timeout = connect_timeout if connect_timeout is not None else opts.get("connect_timeout", config.DEFAULT_CONNECT_TIMEOUT)
        self.isolate = bool(opts.get("isolate", config.DEFAULT_ISOLATE)) if isolate is None else bool(isolate)

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
            "max_bytes": self.max_bytes,
            "max_links": self.max_links,
            "max_chars": self.max_chars,
            "max_redirects": self.max_redirects,
            "respect_robots": self.respect_robots,
            "rate_limit": self.rate_limit,
            "no_private_ip": self.no_private_ip,
            "expect": self.expect,
            "isolate": self.isolate,
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
              _skip_guards=False):
        """Fetch one URL and return a result dict (never raises for runtime errors)."""
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
            return result

        body, text, read_error = self._read_and_decode(resp, clean_url, net)
        final_url = redact_url(resp.url)
        if read_error:
            read_error["took_ms"] = took
            read_error["final_url"] = final_url
            read_error["content_type"] = resp.headers.get("content-type", "")
            return read_error

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

        expect_error = self._expect_check(kind["kind"])
        if expect_error:
            # --expect is a contract, not a hint: a mismatch stays a failure even
            # with allow_errors (which is about 4xx/5xx *bodies*).
            ok = False
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
        )
        if self.isolate and net == "tor":
            result["metadata"]["isolated"] = True
        if expect_error:
            result["error"] = expect_error
        if not ok:
            result["error_code"] = result["error_code"] or "HTTP"
            result["error"] = result["error"] or "HTTP %d %s" % (resp.status_code, _reason(resp.status_code))
        self._jar.save(self._session())
        if (
            self._cache_allowed(request_headers)
            and method == "GET"
            and data is None
            and ok
            and not resp.history
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
            )
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
        )
        if keep_raw:
            result["_raw_body"] = RawBody(body)
        return result

    def fetch_many(self, urls, concurrency=4, keep_raw=False):
        """Fetch many URLs with a bounded thread pool. Order preserved."""
        results = [None] * len(urls)
        if concurrency <= 1 or len(urls) <= 1:
            return [self.fetch(u, keep_raw=keep_raw) for u in urls]
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(self.fetch, u, keep_raw=keep_raw): i for i, u in enumerate(urls)}
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


def fetch(url, format="text", **kwargs):
    """Fetch a single URL through the default normal network (or override).

    ``format`` picks which field is promoted into ``result["text"]``
    (``text``/``markdown``/``raw``/``html``); every field is always present.
    """
    keep_raw = kwargs.pop("keep_raw", True)
    result = Fetcher(**kwargs).fetch(url, keep_raw=keep_raw)
    return _apply_format(result, format)


def fetch_many(urls, concurrency=4, format="text", **kwargs):
    """Fetch many URLs, returning results in input order."""
    keep_raw = kwargs.pop("keep_raw", True)
    fetcher = Fetcher(**kwargs)
    results = fetcher.fetch_many(urls, concurrency=concurrency, keep_raw=keep_raw)
    return [_apply_format(r, format) for r in results]