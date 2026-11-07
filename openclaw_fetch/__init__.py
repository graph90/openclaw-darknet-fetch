"""OpenClaw Darknet Fetch v2 — multi-network fetcher toolkit for AI agents.

Public API:

    from openclaw_fetch import fetch, fetch_many, Fetcher

    r = fetch("https://example.com", network="tor", format="markdown", max_chars=6000)
    # r.ok, r.status, r.final_url, r.title, r.text, r.markdown, r.links, r.metadata

    results = fetch_many([url1, url2, url3], network="tor", concurrency=4)

    f = Fetcher(network="tor", cookie_jar="/tmp/tor.jar", retries=3)
    r = f.post(url, data={"q": "..."})

    sres = search("bitcoin onion", network="tor")       # Ahmia over Tor
    bundle = search_fetch("bitcoin onion", top_n=3, network="tor")
    crawl_result = crawl("http://seed.example/", depth=2, max_pages=20)

Results are ``Result`` dicts (attribute access compatible). Runtime failures
are returned as ``ok: false`` with a stable ``error_code``; they are never
raised as exceptions.
"""

from .fetcher import Fetcher, fetch, fetch_many, validate_url, is_valid_onion, onion_info
from .errors import FetchError, exit_code_for
from .result import Result
from .search import search, search_fetch
from .crawl import crawl
from .compare import compare, compare_mirrors
from .doctor import doctor
from .deep import deep_fetch
from .mirrors import find_mirrors
from .documents import collect_documents
from .watch import watch
from .util import detect_language, estimate_tokens, estimate_total_tokens, hamming, near_duplicates, simhash
from .version import __version__

__all__ = [
    "Fetcher",
    "fetch",
    "fetch_many",
    "validate_url",
    "is_valid_onion",
    "onion_info",
    "search",
    "search_fetch",
    "crawl",
    "compare",
    "compare_mirrors",
    "doctor",
    "deep_fetch",
    "find_mirrors",
    "collect_documents",
    "watch",
    "detect_language",
    "estimate_tokens",
    "estimate_total_tokens",
    "simhash",
    "hamming",
    "near_duplicates",
    "FetchError",
    "Result",
    "exit_code_for",
    "__version__",
]