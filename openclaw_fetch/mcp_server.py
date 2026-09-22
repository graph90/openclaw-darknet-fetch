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

    def base_kwargs(**kw):
        merged = {"network": default_network}
        merged.update(defaults)
        merged.update({k: v for k, v in kw.items() if v is not None})
        return merged

    @mcp.tool()
    def fetch(url: str, network: str = "normal", format: str = "text",
              max_chars: int = 12000, max_links: int = 50,
              allow_errors: bool = False) -> dict:
        """Fetch one URL over normal/Tor/I2P and return structured content."""
        from .fetcher import Fetcher

        kw = base_kwargs(network=network)
        fetcher = Fetcher(
            max_chars=max_chars, max_links=max_links,
            allow_errors=allow_errors, **kw,
        )
        result = fetcher.fetch(url)
        out = result.to_dict()
        out.pop("_raw_body", None)
        if format == "markdown":
            out["text"] = out.get("markdown") or out.get("text") or ""
        return out

    @mcp.tool()
    def fetch_many(urls: list, network: str = "normal", concurrency: int = 4,
                   max_chars: int = 12000) -> list:
        """Fetch many URLs in parallel, returning results in input order."""
        from .fetcher import Fetcher

        fetcher = Fetcher(
            max_chars=max_chars, network=network, **{
                k: v for k, v in base_kwargs().items() if k != "network"
            },
        )
        results = fetcher.fetch_many(list(urls), concurrency=concurrency)
        out = []
        for result in results:
            d = result.to_dict()
            d.pop("_raw_body", None)
            out.append(d)
        return out

    @mcp.tool()
    def crawl(seed_url: str, depth: int = 1, max_pages: int = 10,
              network: str = "normal", concurrency: int = 3) -> dict:
        """Crawl up to `depth` hops from a seed URL, bounded by max_pages."""
        from .crawl import crawl as run_crawl

        return run_crawl(
            seed_url, depth=depth, max_pages=max_pages,
            network=network, concurrency=concurrency,
            same_host=True,
        )

    @mcp.tool()
    def search(query: str, network: str = "normal", backend: str = "auto",
               max_results: int = 8) -> dict:
        """Search the web (DuckDuckGo / Ahmia / SearXNG) over a network."""
        from .search import search as run_search

        return run_search(query, network=network, backend=backend,
                          max_results=max_results)

    @mcp.tool()
    def search_fetch(query: str, top_n: int = 3, network: str = "normal",
                     backend: str = "auto") -> dict:
        """Search, then fetch the top results through the same network."""
        from .search import search_fetch as run_search_fetch

        return run_search_fetch(query, top_n=top_n, network=network,
                                backend=backend)

    @mcp.tool()
    def proxy_status(network: str = "normal") -> dict:
        """Probe a network's proxy and report reachability (exit IP over Tor)."""
        from .fetcher import Fetcher

        return Fetcher(network=network).check_proxy()

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