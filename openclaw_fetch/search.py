"""Web search backends routed through an OpenClaw-Fetch network.

Discovery layer for agents: turn a query into candidate URLs, optionally
fetching the top results through the *same* network (``search_fetch``).
Backends:

- ``ddg``     DuckDuckGo HTML (zero-API, no cookies)
- ``ahmia``   Ahmia (onion-aware; the default Tor backend)
- ``searxng`` any SearXNG instance via ``--search-backend URL``
- ``auto``    pick ahmia for Tor, ddg otherwise

Results always return the stable dict shape below so agents can branch on
them; failures are ``ok: false`` with an ``error_code``.
"""

import re
import time
from collections import OrderedDict
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from .result import Result

SEARCH_BACKENDS = ("auto", "ddg", "ahmia", "searxng")

_DDG_URL = "https://html.duckduckgo.com/html/"
_AHMIA_URL = "https://ahmia.fi/search/"

_RE_A = re.compile(r'<a[^>]+class=["\'][^"\']*result__a[^"\']*["\'][^>]*>(.*?)</a>',
                   re.IGNORECASE | re.DOTALL)
_RE_SNIPPET = re.compile(r'<a[^>]+class=["\'][^"\']*result__snippet[^"\']*["\'][^>]*>(.*?)</a>',
                         re.IGNORECASE | re.DOTALL)
_RE_AHMA = re.compile(
    r'<h4[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>\s*</h4>\s*'
    r'<cite[^>]*>(.*?)</cite>',
    re.IGNORECASE | re.DOTALL,
)
_RE_SX_ITEM = re.compile(
    r'<article[^>]*class="[^"]*result[^"]*"[^>]*>.*?'
    r'<a[^>]+href="([^"]+)"[^>]*.*?>(.*?)</a>.*?'
    r'<p[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)


def _strip_tags(html):
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _decode_ddg_href(href):
    """DuckDuckGo wraps real URLs in /l/?uddg=<encoded>."""
    if "uddg=" in href:
        parsed = parse_qs(urlparse(href).query)
        if "uddg" in parsed:
            return parsed["uddg"][0]
    return href


def _target_tld(url):
    host = urlparse(url).netloc.rsplit("@", 1)[-1].split(":")[0].lower()
    if host.endswith(".onion"):
        return "onion"
    if host.endswith(".i2p"):
        return "i2p"
    return "clearnet"


def _dedupe(items):
    seen = OrderedDict()
    for item in items:
        url = item.get("url")
        if url and url in seen:
            continue
        seen[url] = item
    return list(seen.values())


# --------------------------------------------------------------------------
# result shape helpers
# --------------------------------------------------------------------------

def _results(backend, network, query, items, took_ms, error=None, error_code=None):
    result = Result(
        ok=error is None,
        error=error or "",
        error_code=error_code,
        network=network.upper(),
        backend=backend,
        query=query,
        total_results=len(items),
        results=items,
        took_ms=took_ms,
    )
    return result


# --------------------------------------------------------------------------
# backends
# --------------------------------------------------------------------------

def _parse_ddg(raw):
    anchors = [(m.group(0), _strip_tags(m.group(1))) for m in _RE_A.finditer(raw)]
    snippets = [_strip_tags(m.group(1)) for m in _RE_SNIPPET.finditer(raw)]
    items = []
    for i, (anchor_html, title) in enumerate(anchors):
        href = re.search(r'href="([^"]+)"', anchor_html)
        if not href:
            continue
        url = _decode_ddg_href(href.group(1))
        if not url.startswith(("http://", "https://")):
            url = "https:" + url if url.startswith("//") else "https://" + url
        snippet = snippets[i] if i < len(snippets) else ""
        items.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "target_tld": _target_tld(url),
                "rank": len(items) + 1,
            }
        )
    return _dedupe(items)


def _parse_ahmia(raw):
    items = []
    for match in _RE_AHMA.finditer(raw):
        href, title, cite = match.group(1), match.group(2), match.group(3)
        title = _strip_tags(title)
        if "?" in href and "q=" in href:
            href = parse_qs(urlparse(href).query).get("q", [href])[0]
        if not href.startswith(("http://", "https://")):
            href = "https://ahmia.fi" + href if href.startswith("/") else "https://" + href
        items.append(
            {
                "title": title,
                "url": href,
                "snippet": _strip_tags(cite),
                "target_tld": _target_tld(href),
                "rank": len(items) + 1,
            }
        )
    return _dedupe(items)


def _parse_searxng(raw, base_url):
    items = []
    base_host = urlparse(base_url).netloc
    for match in _RE_SX_ITEM.finditer(raw):
        href, title, snippet = match.group(1), match.group(2), match.group(3)
        url = urljoin(base_url, href.replace("&amp;", "&")).replace("&amp;", "&")
        parsed = parse_qs(urlparse(url).query)
        if parsed.get("url") and urlparse(url).netloc == base_host:
            url = parsed["url"][0]
        items.append(
            {
                "title": _strip_tags(title),
                "url": url,
                "snippet": _strip_tags(snippet),
                "target_tld": _target_tld(url),
                "rank": len(items) + 1,
            }
        )
    return _dedupe(items)


def _backend_url(backend, query, search_url=None, time_range=""):
    if backend == "ddg":
        params = {"q": query}
        if time_range:
            params["df"] = time_range
        return _DDG_URL + "?" + urlencode(params)
    if backend == "ahmia":
        return _AHMIA_URL + "?" + urlencode({"q": query})
    if backend == "searxng":
        base = (search_url or "https://searx.be/search").replace("&amp;", "&")
        sep = "&" if "?" in base else "?"
        return base + sep + urlencode({"q": query})


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def search(query, *, network="normal", backend="auto", max_results=8,
           search_url=None, fetcher=None, time_range="", **fetcher_kwargs):
    """Run a query against a search backend over ``network``.

    Returns a result dict: ``{ok, backend, query, results[], total_results,
    took_ms}``. ``results`` items are ``{title, url, snippet, target_tld,
    rank}``. Runtime failures return ``ok: false``.
    """
    from .fetcher import Fetcher

    t0 = time.monotonic()
    if backend not in SEARCH_BACKENDS:
        return _results("auto", network, query, [], 0,
                        error="unknown search backend %r" % backend,
                        error_code="USAGE")
    eff_backend = backend
    if backend == "auto":
        eff_backend = "ahmia" if network == "tor" else "ddg"
    if eff_backend == "searxng" and not search_url:
        return _results("auto", network, query, [], 0,
                        error="--search-backend URL is required when using searxng",
                        error_code="USAGE")
    url = _backend_url(eff_backend, query, search_url=search_url, time_range=time_range)
    if fetcher is None:
        fetcher_kwargs.setdefault("max_chars", 0)
        fetcher = Fetcher(network=network, **fetcher_kwargs)
    resp = fetcher.fetch(url, keep_raw=True)
    took = int((time.monotonic() - t0) * 1000)
    if not resp.ok:
        return _results(eff_backend, network, query, [], took,
                        error=resp.error or "search failed",
                        error_code=resp.error_code or "REQUEST")
    raw = resp.get("_raw_body") or resp.get("text") or ""
    if eff_backend == "ddg":
        items = _parse_ddg(raw)
    elif eff_backend == "ahmia":
        items = _parse_ahmia(raw)
    else:
        items = _parse_searxng(raw, url)
    return _results(eff_backend, network, query, items[:max_results], took)


def search_fetch(query, *, top_n=3, network="normal", backend="auto",
                 search_url=None, fetcher=None, concurrency=3,
                 time_range="", search_kwargs=None, **fetcher_kwargs):
    """Search, then fetch the top ``top_n`` results through the same network.

    Returns ``{search: <search result>, pages: [<fetch results>]}`` — the
    exact discovery→read loop an agent performs, in one round trip.
    """
    from .fetcher import Fetcher

    if fetcher is None:
        fetcher_kwargs.setdefault("max_chars", 0)
        fetcher = Fetcher(network=network, **fetcher_kwargs)
    search_kwargs = dict(search_kwargs or {})
    search_kwargs.setdefault("search_url", search_url)
    search_kwargs.setdefault("time_range", time_range)
    sres = search(
        query,
        network=network,
        backend=backend,
        max_results=max(top_n, 1),
        fetcher=fetcher,
        **search_kwargs,
    )
    pages = []
    if sres.ok:
        urls = [item["url"] for item in sres.results[:top_n]]
        pages = fetcher.fetch_many(urls, concurrency=concurrency)
    return {"search": sres, "pages": pages}


# keep an alias matching the fetch_many naming style
search_and_fetch = search_fetch