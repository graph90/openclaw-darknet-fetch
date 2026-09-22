"""Network layer: per-network sessions, retries, cookies, redirects, limits.

The public surface is ``Fetcher`` plus the module-level convenience
functions ``fetch`` and ``fetch_many`` (re-exported from __init__).

Runtime failures are returned as ``ok: false`` result dicts, never raised.
``FetchError`` is reserved for usage-level violations (bad scheme, wrong
network for a target, non-string URL).
"""

import json
import os
import random
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from . import config
from .cache import TtlCache, has_cookies
from .errors import FetchError, error_result, exit_code_for
from .parse import parse_document
from .result import Result

NETWORKS = ("normal", "tor", "i2p")
NETWORK_LABEL = {"normal": "NORMAL", "tor": "TOR", "i2p": "I2P"}

# Retriable statuses: rate-limited (429) and server errors (5xx).
def _retriable_status(status):
    return status == 429 or 500 <= status < 600


def _normalize_network(network):
    net = (network or "normal").lower()
    if net in NETWORKS:
        return net
    if net.upper() in NETWORK_LABEL.values():
        return net.lower()
    raise FetchError("USAGE", "unknown network %r (expected normal/tor/i2p)" % (network,))


def _target_tld(url):
    try:
        host = urlparse(url).netloc.rsplit("@", 1)[-1].split(":")[0].lower()
    except Exception:
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
    parsed = urlparse(url)
    if not _is_http_scheme(url):
        raise FetchError(
            "SCHEME_BLOCKED",
            "only http/https are allowed (got %r)" % url.split(":", 1)[0],
        )
    # Strip user:pass@ evidence before any output field / request.
    clean = parsed._replace(
        netloc=parsed.netloc.rsplit("@", 1)[-1],
    )
    # Punycode IDN hostnames.
    host = _idna_host(clean.hostname or "")
    port = clean.port
    netloc = host
    if port is not None:
        netloc = "%s:%d" % (host, port)
    if clean.username is not None and "@" in parsed.netloc:
        netloc = netloc  # already stripped
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


def check_route(network, url):
    """Return (error_code, message) when ``network`` cannot reach ``url``."""
    net = _normalize_network(network)
    tld = _target_tld(url)
    if net == "normal":
        return route_error(url)
    if net == "tor" and tld == "i2p":
        return "NETWORK_MISMATCH", "I2P eepsites cannot be reached over Tor: use network='i2p' (-i)"
    if net == "i2p" and tld == "onion":
        return "NETWORK_MISMATCH", "Tor onion services cannot be reached over I2P: use network='tor' (-t)"
    return None, None


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


def _mkstemp(directory):
    import tempfile

    return tempfile.mkstemp(prefix=".ocf-cookies-", suffix=".tmp", dir=directory)


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
    ):
        opts = config.resolve({})
        self.network = _normalize_network(network)
        self.tor_proxy = tor_proxy or os.environ.get("OPENCLAW_TOR_PROXY") or opts.get("tor_proxy", config.TOR_PROXY_DEFAULT)
        self.i2p_proxy = i2p_proxy or os.environ.get("OPENCLAW_I2P_PROXY") or opts.get("i2p_proxy", config.I2P_PROXY_DEFAULT)
        self.timeout = timeout if timeout is not None else opts.get("timeout", config.DEFAULT_TIMEOUT)
        self.connect_timeout = connect_timeout if connect_timeout is not None else opts.get("connect_timeout", config.DEFAULT_CONNECT_TIMEOUT)
        self.retries = retries if retries is not None else opts.get("retries", config.DEFAULT_RETRIES)
        self.retry_backoff = retry_backoff if retry_backoff is not None else opts.get("retry_backoff", config.DEFAULT_RETRY_BACKOFF)
        self.cookie_jar = cookie_jar
        self.max_bytes = max_bytes if max_bytes is not None else opts.get("max_bytes", config.DEFAULT_MAX_BYTES)
        self.max_links = max_links if max_links is not None else opts.get("max_links", config.DEFAULT_MAX_LINKS)
        self.max_chars = max_chars if max_chars is not None else opts.get("max_chars", config.DEFAULT_MAX_CHARS)
        self.max_redirects = max_redirects if max_redirects is not None else opts.get("max_redirects", config.DEFAULT_MAX_REDIRECTS)
        self.allow_errors = allow_errors
        self.respect_robots = respect_robots
        self.ua = ua or opts.get("ua") or config.DEFAULT_UA
        self.headers = dict(headers or {})
        self.headers.setdefault("User-Agent", self.ua)
        self._extra_headers = dict(headers or {})
        self._local = threading.local()
        self._jar = _CookieStore(cookie_jar)
        ttl = cache_ttl if cache_ttl is not None else opts.get("cache_ttl", config.DEFAULT_CACHE_TTL)
        self.cache = TtlCache(cache_dir=cache_dir, ttl=ttl, enabled=use_cache)
        # Auth edge case: once a non-empty jar exists, stop using the disk cache
        # so authenticated responses can never leak to other contexts.
        self._cache_safe = use_cache and not has_cookies(cookie_jar)

    # -- sessions ---------------------------------------------------------
    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._new_session()
            self._local.session = session
        return session

    def _new_session(self):
        session = requests.Session()
        session.max_redirects = self.max_redirects
        if self.network != "normal":
            session.trust_env = False
        if self.network == "tor":
            proxy = self.tor_proxy
        elif self.network == "i2p":
            proxy = self.i2p_proxy
        else:
            proxy = None
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        self._jar.load(session)
        return session

    def proxy_url(self):
        if self.network == "tor":
            return self.tor_proxy
        if self.network == "i2p":
            return self.i2p_proxy
        return None

    # -- request loop -----------------------------------------------------
    def _request(self, method, url, **request_kw):
        """Run the request with retries. Returns (response, error) tuple."""
        session = self._session()
        attempts = self.retries + 1
        last_error = None
        last_response = None
        for attempt in range(attempts):
            try:
                resp = session.request(
                    method,
                    url,
                    timeout=(self.connect_timeout, self.timeout),
                    stream=True,
                    **request_kw,
                )
                if not _retriable_status(resp.status_code):
                    return resp, None
                last_response = resp
                if attempt == attempts - 1:
                    return resp, None
                delay = self._backoff(attempt)
                retry_after = _retry_after_seconds(resp)
                if retry_after is not None:
                    delay = max(delay, min(retry_after, 30))
                time.sleep(delay)
            except requests.exceptions.ProxyError:
                if attempt == attempts - 1:
                    return None, ("PROXY_DOWN", "could not connect to %s proxy at %s" % (self.network, self.proxy_url()))
                time.sleep(self._backoff(attempt))
            except requests.exceptions.Timeout:
                if attempt == attempts - 1:
                    return None, ("TIMEOUT", "request timed out")
                time.sleep(self._backoff(attempt))
            except requests.exceptions.TooManyRedirects:
                return None, ("REQUEST", "too many redirects")
            except requests.exceptions.ConnectionError:
                if attempt == attempts - 1:
                    return None, ("CONNECTION", "connection failed")
                time.sleep(self._backoff(attempt))
            except requests.exceptions.RequestException as exc:
                return None, ("REQUEST", str(exc))
        return last_response, last_error

    def _backoff(self, attempt):
        base = self.retry_backoff * (2 ** attempt)
        return base * (0.5 + random.random())

    # -- body / decode ----------------------------------------------------
    def _read_and_decode(self, resp, requested_url):
        headers = resp.headers
        content_length = headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > self.max_bytes:
                    return None, None, error_result(
                        "BODY_TOO_LARGE",
                        "Content-Length %s exceeds max-bytes %d" % (content_length, self.max_bytes),
                        requested_url=requested_url,
                        network=NETWORK_LABEL[self.network],
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
                        network=NETWORK_LABEL[self.network],
                        status=resp.status_code,
                    )
                chunks.append(chunk)
        except requests.exceptions.ChunkedEncodingError:
            return None, None, error_result(
                "CONNECTION",
                "connection lost while reading body",
                requested_url=requested_url,
                network=NETWORK_LABEL[self.network],
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

    # -- public API -------------------------------------------------------
    def fetch(self, url, method="GET", *, data=None, form=True, headers=None, allow_errors=None, keep_raw=False):
        """Fetch one URL and return a result dict (never raises for runtime errors)."""
        t0 = time.monotonic()
        eff_allow_errors = self.allow_errors if allow_errors is None else allow_errors
        try:
            clean_url = validate_url(url, self.network)
        except FetchError as exc:
            result = error_result(
                exc.error_code, str(exc), requested_url=url, network=NETWORK_LABEL[self.network]
            )
            result["took_ms"] = int((time.monotonic() - t0) * 1000)
            return result
        code, message = check_route(self.network, clean_url)
        if code:
            result = error_result(code, message, requested_url=clean_url, network=NETWORK_LABEL[self.network])
            result["took_ms"] = int((time.monotonic() - t0) * 1000)
            return result

        use_cache = self._cache_safe and method == "GET" and not data
        if use_cache:
            cached = self.cache.get(self.network, clean_url)
            if cached is not None:
                return self._from_cache(cached, clean_url, t0, keep_raw=keep_raw)

        request_headers = dict(self.headers)
        request_headers.update(headers or {})
        if data is not None and not form:
            request_headers.setdefault("Content-Type", "application/json")
            kwargs = {"data": data}
        elif data is not None:
            kwargs = {"data": data}
        else:
            kwargs = {}
        resp, req_error = self._request(method, clean_url, headers=request_headers, **kwargs)
        took = int((time.monotonic() - t0) * 1000)

        if req_error:
            code, message = req_error
            result = error_result(code, message, requested_url=clean_url, network=NETWORK_LABEL[self.network])
            result["took_ms"] = took
            if self.respect_robots and method == "GET":
                self.cache.put(self.network, clean_url, None, clean_url, "", b"")
            return result

        body, text, read_error = self._read_and_decode(resp, clean_url)
        final_url = resp.url
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
        )
        rendered_text = _truncate(kind["text"], self.max_chars, "text")
        rendered_markdown = _truncate(kind["markdown"], self.max_chars, "text")
        if kind["kind"] in ("pdf", "image") and not kind["text"]:
            rendered_text = kind["text"]
        result = Result(
            ok=ok,
            error="" if ok else "HTTP %d %s" % (resp.status_code, _reason(resp.status_code)),
            error_code="HTTP" if not ok else None,
            network=NETWORK_LABEL[self.network],
            requested_url=clean_url,
            final_url=final_url,
            status=resp.status_code,
            content_type=content_type,
            content_length=len(body),
            title=kind["title"] or "",
            text_length=len(kind["text"]),
            total_text_length=len(kind["text"]),
            returned_text_length=len(rendered_text),
            took_ms=took,
            links=kind["links"],
            link_count=len(kind["links"]),
            metadata=kind["metadata"],
            needs_renderer=kind["needs_renderer"],
            text=rendered_text,
            markdown=rendered_markdown or "",
            headings=kind["headings"],
            content_kind=kind["kind"],
            cached=False,
            target_tld=_target_tld(clean_url),
            redirects=len(resp.history),
            redirect_chain=[r.url for r in resp.history] + [final_url],
            proxy_used=self.proxy_url() or None,
        )
        if not ok:
            result["error_code"] = "HTTP"
            result["error"] = "HTTP %d %s" % (resp.status_code, _reason(resp.status_code))
        if self._cache_safe and method == "GET" and ok and not resp.history:
            self.cache.put(
                self.network,
                clean_url,
                resp.status_code,
                final_url,
                content_type,
                body,
                headers=dict(resp.headers),
            )
        if keep_raw:
            result["_raw_body"] = text
        self._jar.save(self._session())
        return result

    def post(self, url, *, data=None, form=True, headers=None, keep_raw=False, **kwargs):
        if data is not None and not form:
            headers = dict(headers or {})
            headers.setdefault("Content-Type", "application/json")
            if isinstance(data, dict):
                data = json.dumps(data)
        return self.fetch(url, method="POST", data=data, form=form, headers=headers, keep_raw=keep_raw, **kwargs)

    def check_proxy(self):
        """Probe the configured network/proxy and report reachability."""
        probes = {
            "tor": "http://check.torproject.org/api/ip",
            "i2p": "http://i2p-projekt.i2p/",
            "normal": "https://example.com/",
        }
        small = Fetcher(
            network=self.network,
            tor_proxy=self.tor_proxy,
            i2p_proxy=self.i2p_proxy,
            timeout=max(8, self.timeout),
            retries=1,
            retry_backoff=0.2,
            use_cache=False,
            max_bytes=65536,
        )
        result = small.fetch(probes[self.network])
        out = {
            "ok": result["ok"],
            "network": result["network"],
            "status": result.get("status"),
            "error": result.get("error"),
            "error_code": result.get("error_code"),
            "proxy": self.proxy_url(),
        }
        if self.network == "tor" and result["ok"] and isinstance(result.get("text"), str):
            try:
                payload = json.loads(result["text"])
                out["exit_ip"] = payload.get("IP")
                out["is_tor"] = payload.get("IsTor")
            except (ValueError, AttributeError):
                pass
        return out

    def _from_cache(self, cached, url, t0, keep_raw=False):
        meta, body = cached["meta"], cached["body"]
        content_type = meta.get("content_type") or ""
        kind = parse_document(
            body.decode("utf-8", errors="replace"),
            content_type=content_type,
            requested_url=url,
            final_url=meta.get("final_url") or url,
            source_len=len(body),
            max_links=self.max_links,
        )
        rendered_text = _truncate(kind["text"], self.max_chars, "text")
        rendered_markdown = _truncate(kind["markdown"], self.max_chars, "text")
        status = meta.get("status")
        ok = status is not None and status < 400
        result = Result(
            ok=ok,
            error="" if ok else "HTTP %d (from cache)" % status,
            error_code=None if ok else "HTTP",
            network=NETWORK_LABEL[self.network],
            requested_url=url,
            final_url=meta.get("final_url") or url,
            status=status,
            content_type=content_type,
            content_length=len(body),
            title=kind["title"] or "",
            text_length=len(kind["text"]),
            total_text_length=len(kind["text"]),
            returned_text_length=len(rendered_text),
            took_ms=0,
            links=kind["links"],
            link_count=len(kind["links"]),
            metadata=kind["metadata"],
            needs_renderer=kind["needs_renderer"],
            text=rendered_text,
            markdown=rendered_markdown or "",
            headings=kind["headings"],
            content_kind=kind["kind"],
            cached=True,
            target_tld=_target_tld(url),
            redirects=0,
            redirect_chain=[],
            proxy_used=self.proxy_url() or None,
        )
        if keep_raw:
            result["_raw_body"] = body.decode("utf-8", errors="replace")
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


def fetch(url, **kwargs):
    """Fetch a single URL through the default normal network (or override)."""
    keep_raw = kwargs.pop("keep_raw", False)
    return Fetcher(**kwargs).fetch(url, keep_raw=keep_raw)


def fetch_many(urls, concurrency=4, **kwargs):
    """Fetch many URLs, returning results in input order."""
    keep_raw = kwargs.pop("keep_raw", False)
    fetcher = Fetcher(**kwargs)
    return fetcher.fetch_many(urls, concurrency=concurrency, keep_raw=keep_raw)