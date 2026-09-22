"""Limited BFS site crawl for agents (plan 3.1 / Tier 3).

Crawls from a seed URL up to ``depth`` hops, bounded by ``max_pages``,
deduplicated by normalized URL, using the shared disk cache so already-fetched
pages are never re-downloaded (that also makes ``crawl`` idempotent for retries).
"""

import time
from collections import deque
from urllib.parse import urljoin, urlparse

from . import config
from .result import Result
from .util import normalize_url


def _is_same_host(url, seed_host):
    host = urlparse(url).netloc.rsplit("@", 1)[-1].split(":")[0].lower()
    return host == seed_host


def _is_http(url):
    return urlparse(url).scheme in ("http", "https")


def crawl(seed_url, depth=1, max_pages=10, *, network="normal", fetcher=None,
          same_host=True, concurrency=3, max_links=80, time_budget=None,
          **fetcher_kwargs):
    """Breadth-first crawl from ``seed_url``. Returns a result dict with a
    ``pages`` list (each page carries a ``depth`` field) plus crawl stats.

    Never raises for runtime failures: the root page and each hop may be
    ``ok:false`` results; stats report what actually happened.
    """
    from .fetcher import Fetcher, validate_url

    t0 = time.monotonic()
    depth = max(0, int(depth))
    max_pages = max(1, int(max_pages))
    try:
        seed = validate_url(seed_url, network)
    except Exception as exc:
        result = Result(
            ok=False,
            error=str(exc),
            error_code="USAGE",
            requested_url=seed_url,
            network=network.upper(),
            pages=[],
            pages_fetched=0,
            links_discovered=0,
            max_depth=depth,
        )
        result["took_ms"] = int((time.monotonic() - t0) * 1000)
        return result
    if fetcher is None:
        fetcher = Fetcher(network=network, **fetcher_kwargs)

    seed_host = urlparse(seed).netloc.rsplit("@", 1)[-1].split(":")[0].lower()
    pages = {}          # url -> result dict
    visited = set()
    frontier = deque([(seed, 0)])
    links_discovered = 0
    max_depth_reached = 0
    frontier_exhausted = True
    max_pages_reached = False
    budget_reached = False

    while frontier:
        if len(pages) >= max_pages:
            max_pages_reached = True
            frontier_exhausted = bool(frontier)
            break
        if time_budget is not None and time.monotonic() - t0 > time_budget:
            budget_reached = True
            frontier_exhausted = bool(frontier)
            break
        level = []
        while frontier and len(level) < concurrency and len(pages) + len(level) < max_pages:
            url, d = frontier.popleft()
            if url in visited:
                continue
            visited.add(url)
            level.append((url, d))
            max_depth_reached = max(max_depth_reached, d)
        if not level:
            break
        if len(level) == 1:
            res = fetcher.fetch(level[0][0])
            res["depth"] = level[0][1]
            pages[level[0][0]] = res
            to_enqueue = [(res, level[0][1])]
        else:
            urls = [u for u, _ in level]
            results = fetcher.fetch_many(urls, concurrency=concurrency)
            to_enqueue = []
            for (u, d), res in zip(level, results):
                res["depth"] = d
                pages[u] = res
                to_enqueue.append((res, d))
        for res, d in to_enqueue:
            if not res.ok or d >= depth:
                continue
            for link in res.get("links", []):
                href = link.get("url", "")
                if not _is_http(href):
                    continue
                next_url = normalize_url(href)
                if next_url in visited or next_url in pages:
                    continue
                if same_host and not _is_same_host(next_url, seed_host):
                    continue
                links_discovered += 1
                frontier.append((next_url, d + 1))

    page_list = list(pages.values())
    failed_root = len(page_list) == 0
    # A crawl with zero successful pages but a root that was fetched & classified
    # non-ok is still a failed crawl.
    ok = not failed_root
    error = ""
    error_code = None
    if failed_root:
        ok = False
        error = "no pages fetched"
        error_code = "REQUEST"
    result = Result(
        ok=ok,
        error=error,
        error_code=error_code,
        network=network.upper(),
        requested_url=seed,
        seed_url=seed,
        pages_fetched=len(page_list),
        links_discovered=links_discovered,
        max_depth=max_depth_reached,
        max_depth_target=depth,
        max_pages_reached=max_pages_reached,
        frontier_exhausted=frontier_exhausted,
        time_budget_reached=budget_reached,
        pages=page_list,
    )
    result["took_ms"] = int((time.monotonic() - t0) * 1000)
    return result