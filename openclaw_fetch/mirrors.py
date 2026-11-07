"""Mirror discovery: find the onion/I2P mirrors of a clearnet site.

``find_mirrors()`` combines the authoritative ``Onion-Location`` header with
search-index hits (tor66 for onions, marginalia/ddg for clearnet). Every
candidate is tagged with its ``source`` so agents can weigh authority.
"""

from urllib.parse import urlparse

from .result import Result


def _domain_of(site_or_url):
    raw = (site_or_url or "").strip()
    if "://" in raw:
        host = urlparse(raw).hostname or ""
        return host.lower(), raw
    host = raw.split("/", 1)[0].split("?", 1)[0].strip().lower()
    return host, "https://" + host + "/"


def find_mirrors(site_or_url, *, network="auto", fetcher=None, include_sponsored=False,
                 dry_run=False, **fetcher_kwargs):
    """Return ``{ok, clearnet, onions[], i2ps[], sources[]}`` for ``site_or_url``.

    ``sources[]`` entries are ``{"url", "source"}`` where source is one of
    ``onion-location`` (authoritative header), ``tor66-search``,
    ``marginalia-search``, ``ddg-search``, ``ahmia-search``, ``searxng-search``,
    or ``input``. Never raises for runtime failures.
    """
    from .fetcher import Fetcher, _now_iso
    from .search import search as run_search

    started = _now_iso()
    host, clearnet_url = _domain_of(site_or_url)
    if dry_run:
        from .util import estimate_tokens

        return Result(
            ok=True, error="", error_code=None, query=site_or_url,
            clearnet=None if host.endswith((".onion", ".i2p")) else clearnet_url,
            onions=[], i2ps=[], sources=[], dry_run=True,
            projected_searches=["tor66", "marginalia", "ddg"],
            projected_tokens_total=3 * estimate_tokens("x" * 2000),
            fetched_at=started,
        )
    is_onion = host.endswith(".onion")
    is_i2p = host.endswith(".i2p")
    clearnet = None if (is_onion or is_i2p) else clearnet_url
    onions, i2ps, sources = [], [], []
    if is_onion:
        onions.append(clearnet_url)
        sources.append({"url": clearnet_url, "source": "input"})
    if is_i2p:
        i2ps.append(clearnet_url)
        sources.append({"url": clearnet_url, "source": "input"})

    if fetcher is None:
        fetcher = Fetcher(network=network, **fetcher_kwargs)
    search_fetcher = fetcher.clone(max_chars=0) if hasattr(fetcher, "clone") else fetcher

    # 1. Authoritative header: fetch the input URL and harvest Onion-Location.
    if not is_onion:
        try:
            page = fetcher.fetch(clearnet_url)
            mirrors = (page.get("metadata") or {}).get("mirrors") or []
            for entry in mirrors:
                url = entry.get("url") if isinstance(entry, dict) else entry
                if url:
                    onions.append(url)
                    sources.append({"url": url, "source": "onion-location"})
        except Exception:
            pass
    # 2. Search indexes (best-effort; empty results are fine).
    for backend in ("tor66", "marginalia", "ddg"):
        try:
            sres = run_search(
                host, network=network, backend=backend, max_results=8,
                fetcher=search_fetcher, include_sponsored=include_sponsored,
            )
        except Exception:
            continue
        if not getattr(sres, "ok", False):
            continue
        for item in sres.get("results") or []:
            url = item.get("url")
            tld = item.get("target_tld")
            if not url:
                continue
            if tld == "onion" and url not in onions:
                onions.append(url)
                sources.append({"url": url, "source": "%s-search" % backend})
            elif tld == "i2p" and url not in i2ps:
                i2ps.append(url)
                sources.append({"url": url, "source": "%s-search" % backend})
    ok = bool(clearnet or onions or i2ps)
    return Result(
        ok=ok,
        error="" if ok else "no mirrors found",
        error_code=None if ok else "REQUEST",
        query=site_or_url,
        clearnet=clearnet,
        onions=onions,
        i2ps=i2ps,
        sources=sources,
        fetched_at=started,
    )
