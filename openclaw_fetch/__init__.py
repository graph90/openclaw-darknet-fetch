"""OpenClaw Darknet Fetch v2 — multi-network fetcher toolkit for AI agents.

Public API:

    from openclaw_fetch import fetch, fetch_many, Fetcher

    r = fetch("https://example.com", network="tor", format="markdown", max_chars=6000)
    # r.ok, r.status, r.final_url, r.title, r.text, r.markdown, r.links, r.metadata

    results = fetch_many([url1, url2, url3], network="tor", concurrency=4)

    f = Fetcher(network="tor", cookie_jar="/tmp/tor.jar", retries=3)
    r = f.post(url, data={"q": "..."})

Results are ``Result`` dicts (attribute access compatible). Runtime failures
are returned as ``ok: false`` with a stable ``error_code``; they are never
raised as exceptions.
"""

from .fetcher import Fetcher, fetch, fetch_many, validate_url
from .errors import FetchError, exit_code_for
from .result import Result
from .version import __version__

__all__ = [
    "Fetcher",
    "fetch",
    "fetch_many",
    "validate_url",
    "FetchError",
    "Result",
    "exit_code_for",
    "__version__",
]