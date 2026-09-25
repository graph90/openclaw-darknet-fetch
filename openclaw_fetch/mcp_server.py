"""MCP (Model Context Protocol) server exposing the toolkit to any agent runtime.

Run with::

    openclaw-fetch mcp --network tor

Requires the optional ``mcp`` extra::

    pip install "openclaw-fetch[mcp]"

Exposes tools: ``fetch``, ``fetch_many``, ``crawl``, ``search``,
``search_fetch``, ``proxy_status``, and ``estimate_tokens``.
Works over stdio, so it plugs into Claude, Cursor, Cline, Goose, OpenClaw,
and any MCP client without glue code.
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .util import estimate_tokens

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ocf-mcp")


def _build_server(network="normal", **defaults):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised only w/o mcp
        raise RuntimeError(
            "the MCP server requires the optional 'mcp' dependency: "
            "pip install 'openclaw-fetch[mcp]'"
        ) from exc

    mcp = FastMCP("openclaw-fetch")
    default_network = network
    server_defaults = dict(defaults)
    server_defaults["no_private_ip"] = True

    def selected_network(value):
        return value or default_network

    def base_kwargs(network=None, **overrides):
        merged = {"network": default_network}
        merged.update(server_defaults)
        if network is not None:
            merged["network"] = network
        merged.update({k: v for k, v in overrides.items() if v is not None})
        return merged

    @mcp.tool()
    def fetch(url: str, network: Optional[str] = None, format: str = "text",
              max_chars: Optional[int] = None, max_links: Optional[int] = None,
              allow_errors: Optional[bool] = None, isolate: Optional[bool] = None) -> dict:
        """Fetch one URL over normal/Tor/I2P and return structured content.

        ``network="auto"`` routes .onion over Tor, .i2p over I2P and everything
        else over the clearnet. ``isolate`` gives each Tor request its own
        circuit.
        """
        from .fetcher import Fetcher

        options = base_kwargs(
            network=network, max_chars=max_chars, max_links=max_links,
            allow_errors=allow_errors, isolate=isolate,
        )
        options.setdefault("max_chars", 12000)
        options.setdefault("max_links", 50)
        options.setdefault("allow_errors", False)
        fetcher = Fetcher(**options)
        result = fetcher.fetch(url)
        out = result.to_dict()
        out.pop("_raw_body", None)
        if format == "markdown":
            out["text"] = out.get("markdown") or out.get("text") or ""
        return out

    @mcp.tool()
    def fetch_many(urls: list, network: Optional[str] = None,
                   concurrency: int = 4, max_chars: Optional[int] = None,
                   isolate: Optional[bool] = None) -> list:
        """Fetch many URLs in parallel, returning results in input order."""
        from .fetcher import Fetcher

        options = base_kwargs(network=network, max_chars=max_chars, isolate=isolate)
        options.setdefault("max_chars", 12000)
        fetcher = Fetcher(**options)
        results = fetcher.fetch_many(list(urls), concurrency=concurrency)
        out = []
        for result in results:
            d = result.to_dict()
            d.pop("_raw_body", None)
            out.append(d)
        return out

    @mcp.tool()
    def crawl(seed_url: str, depth: int = 1, max_pages: int = 10,
              network: Optional[str] = None, concurrency: int = 3,
              isolate: Optional[bool] = None) -> dict:
        """Crawl up to `depth` hops from a seed URL, bounded by max_pages."""
        from .crawl import crawl as run_crawl

        options = base_kwargs(network=network, isolate=isolate)
        fetcher_options = {k: v for k, v in options.items() if k != "network"}
        return run_crawl(
            seed_url, depth=depth, max_pages=max_pages,
            network=selected_network(network), concurrency=concurrency,
            same_host=True, **fetcher_options,
        )

    @mcp.tool()
    def search(query: str, network: Optional[str] = None, backend: str = "auto",
               max_results: int = 8, search_url: Optional[str] = None,
               time_range: str = "", include_sponsored: bool = False,
               isolate: Optional[bool] = None) -> dict:
        """Search over a network; engine auto-picks per network.

        Backends: tor66 + ahmia (onion indexes) over Tor, marginalia + ddg on the
        clearnet, or pass `search_url` for your own SearXNG instance.
        """
        from .search import search as run_search

        options = base_kwargs(network=network, isolate=isolate)
        fetcher_options = {k: v for k, v in options.items() if k != "network"}
        # the engine page itself must not be truncated; results are parsed, not read
        fetcher_options["max_chars"] = 0
        return run_search(
            query, network=selected_network(network), backend=backend,
            max_results=max_results, search_url=search_url,
            time_range=time_range or "",
            include_sponsored=bool(include_sponsored),
            **fetcher_options,
        )

    @mcp.tool()
    def search_fetch(query: str, top_n: int = 3, network: Optional[str] = None,
                     backend: str = "auto", search_url: Optional[str] = None,
                     isolate: Optional[bool] = None) -> dict:
        """Search, then fetch the top results through the same network."""
        from .search import search_fetch as run_search_fetch

        options = base_kwargs(network=network, isolate=isolate)
        fetcher_options = {k: v for k, v in options.items() if k != "network"}
        # search page untruncated, but the fetched pages keep the caller's cap
        fetcher_options["max_chars"] = 0
        return run_search_fetch(
            query, top_n=top_n, network=selected_network(network),
            backend=backend, search_url=search_url,
            search_kwargs={"max_chars": 0}, **fetcher_options,
        )

    @mcp.tool()
    def proxy_status(network: Optional[str] = None) -> dict:
        """Probe a network's proxy and report reachability (exit IP over Tor).

        ``network="auto"`` probes normal, Tor and I2P and returns one entry per
        network.
        """
        from .fetcher import Fetcher

        return Fetcher(network=selected_network(network), no_private_ip=True).check_proxy()

    @mcp.tool()
    def estimate_tokens(text: str) -> dict:
        """Approximate how many tokens a text body will consume."""
        return {"text_length": len(text), "estimated_tokens": estimate_tokens(text)}

    return mcp


def run_mcp(network="normal", **defaults):
    """Build and run the stdio MCP server (blocking)."""
    mcp = _build_server(network=network, **defaults)
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    run_mcp()