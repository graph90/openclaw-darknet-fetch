"""Web search backends routed through an OpenClaw-Fetch network.

Discovery layer for agents: turn a query into candidate URLs, optionally
fetching the top results through the *same* network (``search_fetch``).
Backends:

- ``ddg``        DuckDuckGo HTML (zero-API, no cookies)
- ``ahmia``      Ahmia (onion-aware index; JS-driven, parse coverage varies)
- ``tor66``      Tor66 onion index (Tor66's own v3 onion over Tor, else the
                clearnet mirror) - the default Tor backend because it needs no
                clearnet hop
- ``marginalia`` Marginalia Search (text-browser friendly clearnet index)
- ``searxng``    any SearXNG instance via ``--search-backend URL``
- ``auto``       try this network's backends in order, first hit wins

Results always return the stable dict shape below so agents can branch on
them; failures are ``ok: false`` with an ``error_code``.
"""

import re
import time
from collections import OrderedDict
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from .result import Result
from .util import raw_text

SEARCH_BACKENDS = ("auto", "ddg", "ahmia", "tor66", "marginalia", "searxng")

_DDG_URL = "https://html.duckduckgo.com/html/"
_AHMIA_URL = "https://ahmia.fi/search/"
_TOR66_URL = "https://tor66.org/search"
#: Tor66's own v3 onion service (see tor66.org/about). Used first whenever the
#: caller is on Tor so the query never touches the clearnet.
_TOR66_ONION = "http://3bbad7fauom4d6sgppalyqddsqbf5u5p56b5k5uk2zxsy3d6ey2jobad.onion/search"
_MARGINALIA_URL = "https://search.marginalia.nu/search"

#: Ordered ``auto`` fallbacks per network. DuckDuckGo's HTML endpoint answers
#: bot traffic with a 202 challenge page, so it is last; Marginalia serves
#: text browsers and is the clearnet default.
AUTO_BACKENDS = {
    "tor": ("tor66", "ahmia", "ddg"),
    "normal": ("marginalia", "ddg"),
    "i2p": ("marginalia", "ddg"),
    # ``auto`` searches the onion index first, then the clearnet; the fetcher
    # routes each engine over the network its own address needs.
    "auto": ("tor66", "marginalia", "ddg"),
}


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
_RE_TOR66_BLOCK = re.compile(r'<div class="result-block"')
_RE_TOR66_TITLE = re.compile(
    r'<div class="title">\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_RE_TOR66_LINK = re.compile(
    r'<div class="link"[^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL
)
_RE_TOR66_DESC = re.compile(
    r'<(?:p id="desc"[^>]*|div class="desc"[^>]*)>(.*?)</(?:p|div)>',
    re.IGNORECASE | re.DOTALL,
)
_RE_TOR66_SPONSORED = re.compile(r'data-category="sponsored', re.IGNORECASE)
_RE_PLAIN_URL = re.compile(r'https?://[^\s"\'<>]+')

_RE_MARGINALIA_ITEM = re.compile(
    r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>\s*</h2>(.*?)(?=<h2|\Z)',
    re.IGNORECASE | re.DOTALL,
)
_RE_FIRST_P = re.compile(r'<p[^>]*>(.*?)</p>', re.IGNORECASE | re.DOTALL)

#: Markers of an engine anti-bot / rate-limit interstitial. Search engines
#: answer datacenter traffic with a challenge page instead of results, which
#: must not be reported to callers as "no results for this query".
_BLOCK_MARKERS = (
    "wait for a moment",
    "wait a moment",
    "barraged by",
    "aggressive bot activity",
    "javascript is required to complete this challe",
    "unusual traffic",
    "are you a robot",
    "bots use duckduckgo",
    "captcha",
    "checking your browser",
    "enable javascript and cookies",
)




def _strip_tags(html):
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
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

def _looks_blocked(raw):
    """True when a 200 response is an anti-bot/rate-limit page, not results."""
    lowered = raw[:20000].lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


def _results(backend, network, query, items, took_ms, error=None, error_code=None,
             backends_tried=None, blocked_backends=None, backends_failed=None):
    result = Result(
        ok=error is None,
        error=error or "",
        error_code=error_code,
        network=network.upper(),
        backend=backend,
        backends_tried=list(OrderedDict.fromkeys(backends_tried or [])),
        blocked_backends=list(blocked_backends or []),
        backends_failed=list(OrderedDict.fromkeys(backends_failed or [])),
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
                "sponsored": False,
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
                "sponsored": False,
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
                "sponsored": False,
            }
        )
    return _dedupe(items)


def _parse_tor66(raw, base_url=""):
    """Parse Tor66 result blocks from either front-end.

    Both front-ends render ``div.title > a``; the onion front-end additionally
    prints the real target in ``div.link`` and hides it behind an ``/r?s=`` or
    ``/ads/click?s=`` hop, so the plaintext link is preferred when present.
    Sponsored blocks are flagged so callers can drop them.
    """
    items = []
    chunks = _RE_TOR66_BLOCK.split(raw)[1:]
    for chunk in chunks:
        title_match = _RE_TOR66_TITLE.search(chunk)
        if not title_match:
            continue
        href = title_match.group(1).strip()
        url = href
        link_match = _RE_TOR66_LINK.search(chunk)
        if link_match:
            plain = _RE_PLAIN_URL.search(_strip_tags(link_match.group(1)))
            if plain:
                url = plain.group(0).rstrip(".,);\"'")
        elif base_url:
            url = urljoin(base_url, href)
        title = _strip_tags(title_match.group(2))
        desc = _RE_TOR66_DESC.search(chunk)
        snippet = _strip_tags(desc.group(1)) if desc else ""
        for boilerplate in (" Log In", " Log in"):
            if boilerplate in snippet:
                snippet = snippet.split(boilerplate)[0]
        items.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "target_tld": _target_tld(url),
                "rank": len(items) + 1,
                "sponsored": bool(_RE_TOR66_SPONSORED.search(chunk)),
            }
        )
    return _dedupe(items)



def _parse_marginalia(raw, base_url=""):
    """Parse Marginalia results: an ``h2 > a`` title then the first paragraph."""
    items = []
    for match in _RE_MARGINALIA_ITEM.finditer(raw):
        href, title, tail = match.group(1), match.group(2), match.group(3)
        url = urljoin(base_url, href) if base_url else href
        snippet_match = _RE_FIRST_P.search(tail)
        snippet = _strip_tags(snippet_match.group(1)) if snippet_match else ""
        items.append(
            {
                "title": _strip_tags(title),
                "url": url,
                "snippet": snippet,
                "target_tld": _target_tld(url),
                "rank": len(items) + 1,
                "sponsored": False,
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
    if backend == "tor66":
        base = search_url or _TOR66_URL
        sep = "&" if "?" in base else "?"
        return base + sep + urlencode({"q": query})
    if backend == "marginalia":
        base = search_url or _MARGINALIA_URL
        sep = "&" if "?" in base else "?"
        return base + sep + urlencode({"query": query})
    if backend == "searxng":
        base = (search_url or "https://searx.be/search").replace("&amp;", "&")
        sep = "&" if "?" in base else "?"
        return base + sep + urlencode({"q": query})


def _backend_urls(backend, query, network, search_url=None, time_range=""):
    """Candidate URLs for ``backend``, most preferred first.

    ``tor66`` over Tor prefers Tor66's own onion so the query stays inside the
    darknet; the clearnet mirror is kept as a fallback.
    """
    if search_url:
        return [_backend_url(backend, query, search_url=search_url, time_range=time_range)]
    if backend == "tor66" and network in ("tor", "auto"):
        return [
            _backend_url(backend, query, search_url=_TOR66_ONION, time_range=time_range),
            _backend_url(backend, query, search_url=_TOR66_URL, time_range=time_range),
        ]
    return [_backend_url(backend, query, time_range=time_range)]


def _auto_backends(network):
    return AUTO_BACKENDS.get(network, AUTO_BACKENDS["normal"])


_PARSERS = {
    "ddg": lambda raw, url: _parse_ddg(raw),
    "ahmia": lambda raw, url: _parse_ahmia(raw),
    "tor66": lambda raw, url: _parse_tor66(raw),
    "marginalia": lambda raw, url: _parse_marginalia(raw),
    "searxng": _parse_searxng,
}



# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def search(query, *, network="normal", backend="auto", max_results=8,
           search_url=None, fetcher=None, time_range="",
           include_sponsored=False, **fetcher_kwargs):
    """Run a query against a search backend over ``network``.

    Returns a result dict: ``{ok, backend, backends_tried, query, results[],
    total_results, took_ms}``. ``results`` items are ``{title, url, snippet,
    target_tld, rank, sponsored}``. Runtime failures return ``ok: false``.

    ``backend="auto"`` walks this network's backends in order and returns the
    first one that yields results, so a single blocked engine does not turn
    into an empty result set. Sponsored hits (Tor66 ads) are dropped unless
    ``include_sponsored`` is set.
    """
    from .fetcher import Fetcher

    t0 = time.monotonic()
    if backend not in SEARCH_BACKENDS:
        return _results("auto", network, query, [], 0,
                        error="unknown search backend %r" % backend,
                        error_code="USAGE")
    if backend == "auto":
        candidates = list(_auto_backends(network))
    else:
        candidates = [backend]
    if "searxng" in candidates and not search_url:
        if len(candidates) == 1:
            return _results("searxng", network, query, [], 0,
                            error="--search-backend URL is required when using searxng",
                            error_code="USAGE")
        candidates = [name for name in candidates if name != "searxng"]
    if fetcher is None:
        fetcher_kwargs.setdefault("max_chars", 0)
        fetcher = Fetcher(network=network, **fetcher_kwargs)
    tried = []
    blocked = []
    last_error = ""
    last_code = "REQUEST"
    empty_responses = 0
    failed = []
    for name in candidates:
        parser = _PARSERS.get(name)
        if parser is None:
            continue
        for url in _backend_urls(name, query, network, search_url=search_url,
                                 time_range=time_range):
            tried.append(name)
            resp = fetcher.fetch(url, keep_raw=True)
            if not resp.ok:
                last_error = resp.error or "search failed"
                last_code = resp.error_code or "REQUEST"
                failed.append(name)
                continue
            raw = raw_text(resp)
            items = parser(raw, url)
            if not include_sponsored:
                items = [item for item in items if not item.get("sponsored")]
            for position, item in enumerate(items, 1):
                item["rank"] = position
            if items:
                return _results(name, network, query, items[:max_results],
                                int((time.monotonic() - t0) * 1000),
                                backends_tried=tried)
            if _looks_blocked(raw):
                blocked.append(name)
            else:
                empty_responses += 1
    took = int((time.monotonic() - t0) * 1000)
    primary = candidates[0] if candidates else "auto"
    # ok=False only when no engine answered at all; "answered, zero hits" is a
    # successful empty result and must not be reported as a failure.
    if blocked and not empty_responses:
        return _results(
            primary, network, query, [], took,
            error="search backends blocked the request (%s); retry later or pass "
                  "--search-backend with your own searxng instance" % ", ".join(blocked),
            error_code="REQUEST", backends_tried=tried, blocked_backends=blocked,
        )
    if blocked or empty_responses:
        return _results(primary, network, query, [], took, backends_tried=tried,
                        blocked_backends=blocked)
    return _results(primary, network, query, [], took,
                    error=last_error or "search failed",
                    error_code=last_code, backends_tried=tried,
                    backends_failed=failed)





def search_fetch(query, *, top_n=3, network="normal", backend="auto",
                 search_url=None, fetcher=None, concurrency=3,
                 time_range="", include_sponsored=False, search_kwargs=None,
                 **fetcher_kwargs):

    """Search, then fetch the top ``top_n`` results through the same network.

    Returns ``{search: <search result>, pages: [<fetch results>]}`` —
    the exact discovery→read loop an agent performs, in one round trip.
    """
    from .fetcher import Fetcher

    if fetcher is None:
        fetcher = Fetcher(network=network, **fetcher_kwargs)
    # The engine's result page must never be truncated (it is parsed, not read),
    # but the articles we fetch afterwards must respect the caller's budget.
    search_fetcher = fetcher.clone(max_chars=0)
    search_kwargs = dict(search_kwargs or {})
    search_kwargs.setdefault("search_url", search_url)
    search_kwargs.setdefault("time_range", time_range)
    search_kwargs.setdefault("include_sponsored", include_sponsored)

    sres = search(
        query,
        network=network,
        backend=backend,
        max_results=max(top_n, 1),
        fetcher=search_fetcher,
        **search_kwargs,
    )
    pages = []
    if sres.ok:
        urls = [item["url"] for item in sres.results[:top_n]]
        pages = fetcher.fetch_many(urls, concurrency=concurrency)
    return {"search": sres, "pages": pages}


# keep an alias matching the fetch_many naming style
search_and_fetch = search_fetch