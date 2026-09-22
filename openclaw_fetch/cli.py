"""Command-line interface (thin wrapper around the Fetcher library API).

Backward compatible with the legacy single-file CLI: ``-n``/``-t``/``-i``,
``--raw``, ``--json``, ``--max-chars``, ``--max-links`` keep working.
"""

import argparse
import sys

from .version import __version__
from . import config
from .cache import TtlCache
from .errors import exit_code_for
from .fetcher import Fetcher, NETWORK_LABEL
from .output import render


class _CliParser(argparse.ArgumentParser):
    """argparse with usage errors exiting with code 1 (plan exit table)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, "%s: error: %s\n" % (self.prog, message))


def build_parser():
    parser = _CliParser(
        prog="openclaw-fetch",
        description="Multi-network (Normal/Tor/I2P) fetcher toolkit for AI agents.",
        epilog="Networks: -n normal, -t Tor (-t also reaches clearnet), -i I2P",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    network_group = parser.add_mutually_exclusive_group()
    network_group.add_argument(
        "-n", "--normal", metavar="URL", nargs="*", default=None,
        help="Fetch directly over the normal Internet.",
    )
    network_group.add_argument(
        "-t", "--tor", metavar="URL", nargs="*", default=None,
        help="Fetch through the Tor SOCKS5 proxy.",
    )
    network_group.add_argument(
        "-i", "--i2p", metavar="URL", nargs="*", default=None,
        help="Fetch through the I2P HTTP proxy.",
    )
    parser.add_argument("--batch", metavar="FILE", help="File with one URL per line (and in -n/-t/-i).")

    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument("--format", choices=("human", "text", "markdown", "json", "jsonl", "raw"),
                     help="Output format.")
    fmt.add_argument("--json", action="store_true", help="Legacy alias for --format json.")
    fmt.add_argument("--raw", action="store_true", help="Legacy alias for --format raw.")

    parser.add_argument("--max-chars", type=int, default=None, metavar="N",
                        help="Max chars returned in text/markdown bodies.")
    parser.add_argument("--max-links", type=int, default=None, metavar="N",
                        help="Max links returned per result.")
    parser.add_argument("--max-bytes", type=int, default=None, metavar="N",
                        help="Hard cap on downloaded body bytes (pre-fetch guard).")
    parser.add_argument("--retries", type=int, default=None, metavar="N",
                        help="Retries for transient failures (connection/timeout/5xx/429).")
    parser.add_argument("--retry-backoff", type=float, default=None, metavar="SECONDS",
                        help="Base backoff before retrying (jittered exponential).")
    parser.add_argument("--timeout", type=float, default=None, metavar="SECONDS",
                        help="Read timeout.")
    parser.add_argument("--connect-timeout", type=float, default=None, metavar="SECONDS",
                        help="Connect timeout.")
    parser.add_argument("--concurrency", type=int, default=None, metavar="N",
                        help="Parallel fetches for batch mode.")
    parser.add_argument("--cookie-jar", metavar="PATH", help="Persistent cookie jar (JSON, mode 0600).")
    parser.add_argument("--no-cache", action="store_true", help="Disable the TTL disk cache.")
    parser.add_argument("--cache-ttl", type=float, default=None, metavar="SECONDS",
                        help="Cache lifetime (seconds).")
    parser.add_argument("--flush-cache", action="store_true", help="Delete all cached entries and exit.")
    parser.add_argument("--allow-errors", action="store_true",
                        help="Return 4xx/5xx bodies as ok:true so agents can inspect them.")
    parser.add_argument("--tor-proxy", metavar="URL", help="Tor SOCKS5 proxy override.")
    parser.add_argument("--i2p-proxy", metavar="URL", help="I2P HTTP proxy override.")
    parser.add_argument("--ua", metavar="STRING", help="User-Agent override.")
    parser.add_argument("--check-proxy", action="store_true",
                        help="Probe the configured network/proxy and report reachability.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Log per-request details to stderr.")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)

    # -- discovery & workflow ----------------------------------------------
    parser.add_argument("--search", metavar="QUERY",
                        help="Search (DuckDuckGo / Ahmia / SearXNG) instead of fetching URLs.")
    parser.add_argument("--search-backend", metavar="BACKEND|URL",
                        help="Search backend: auto, ddg, ahmia, or a SearXNG instance URL.")
    parser.add_argument("--search-fetch-top", type=int, metavar="N",
                        help="After searching, fetch the top N results through the same network.")
    parser.add_argument("--time-range", metavar="RANGE",
                        help="Search time filter: day, week, month, or year.")
    parser.add_argument("--follow", metavar="DEPTH",
                        help="Crawl up to DEPTH hops from the seed URL (bounded by --max-pages).")
    parser.add_argument("--max-pages", type=int, default=10, metavar="N",
                        help="Max pages for --follow crawl mode.")

    # -- hardening ---------------------------------------------------------
    parser.add_argument("--expect", metavar="KIND",
                        choices=("html", "json", "pdf", "rss", "text", "image"),
                        help="Require this content kind; mismatch returns EXPECT_MISMATCH (exit 5).")
    parser.add_argument("--rate-limit", type=float, metavar="RPS",
                        help="Max requests/second across this invocation (token bucket).")
    parser.add_argument("--no-private-ip", action="store_true",
                        help="Block targets that resolve to private/loopback addresses (clearnet only).")
    parser.add_argument("--respect-robots", action="store_true",
                        help="Check robots.txt before fetching (clearnet; default off).")
    parser.add_argument("--env-report", action="store_true",
                        help="Print resolved configuration as JSON and exit.")
    parser.add_argument("--save-raw", action="store_true",
                        help="Legacy no-op alias kept for compat (raw body is returned under --raw).")
    return parser


def _resolve_format(args):
    if args.format:
        return args.format
    if args.json:
        return "json"
    if args.raw:
        return "raw"
    return "human"


def _read_batch(path):
    urls = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                url = line.strip()
                if not url or url.startswith("#"):
                    continue
                urls.append(url)
    except OSError as exc:
        sys.stderr.write("ERROR: cannot read batch file %s: %s\n" % (path, exc))
        sys.exit(1)
    return urls


def _mcp_command(argv):
    """Handle ``openclaw-fetch mcp [--network N]`` — run the stdio MCP server."""
    p = argparse.ArgumentParser(prog="openclaw-fetch mcp")
    p.add_argument("--network", default="normal", choices=("normal", "tor", "i2p"))
    p.add_argument("--max-chars", type=int, default=None)
    args, _ = p.parse_known_args(argv)
    from .mcp_server import run_mcp

    defaults = {}
    if args.max_chars is not None:
        defaults["max_chars"] = args.max_chars
    run_mcp(network=args.network, **defaults)
    return 0


def _env_report(args):
    opts = config.resolve({})
    opts["version"] = __version__
    opts["default_cache_dir"] = config.default_cache_dir()
    opts["networks"] = ["normal", "tor", "i2p"]
    opts["tor_proxy"] = opts.get("tor_proxy", config.TOR_PROXY_DEFAULT)
    opts["i2p_proxy"] = opts.get("i2p_proxy", config.I2P_PROXY_DEFAULT)
    opts.pop("cache_dir", None)
    import json as _json

    sys.stdout.write(_json.dumps(opts, indent=2) + "\n")
    return 0


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "mcp":
        return _mcp_command(argv[1:])

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.env_report:
        return _env_report(args)

    if args.flush_cache:
        TtlCache().flush()
        sys.stderr.write("cache flushed\n")
        return 0

    mode = "fetch"
    if args.search is not None:
        mode = "search"
    if args.follow is not None:
        mode = "crawl"

    if args.normal is not None:
        network = "normal"
        urls = list(args.normal)
    elif args.tor is not None:
        network = "tor"
        urls = list(args.tor)
    elif args.i2p is not None:
        network = "i2p"
        urls = list(args.i2p)
    elif mode == "fetch":
        parser.error("one of -n/--normal, -t/--tor, -i/--i2p is required (search/crawl default to normal)")
    else:
        network = "normal"
        urls = []

    if args.batch:
        urls.extend(_read_batch(args.batch))

    if mode == "fetch" and not urls and not args.check_proxy:
        parser.error("at least one URL is required (or use --check-proxy)")

    if args.max_bytes is not None and args.max_bytes <= 0:
        parser.error("--max-bytes must be greater than zero")
    if args.max_chars is not None and args.max_chars < 0:
        parser.error("--max-chars must be zero or greater")
    if args.retries is not None and args.retries < 0:
        parser.error("--retries must be zero or greater")
    if args.rate_limit is not None and args.rate_limit <= 0:
        parser.error("--rate-limit must be greater than zero")
    if args.follow is not None:
        try:
            if int(args.follow) < 0:
                parser.error("--follow DEPTH must be zero or greater")
        except ValueError:
            parser.error("--follow expects an integer depth")

    fmt = _resolve_format(args)
    json_mode = fmt in ("json", "jsonl")

    fetcher = Fetcher(
        network=network,
        tor_proxy=args.tor_proxy,
        i2p_proxy=args.i2p_proxy,
        timeout=args.timeout,
        connect_timeout=args.connect_timeout,
        retries=args.retries,
        retry_backoff=args.retry_backoff,
        cookie_jar=args.cookie_jar,
        use_cache=not args.no_cache,
        cache_ttl=args.cache_ttl,
        max_bytes=args.max_bytes,
        max_chars=args.max_chars,
        max_links=args.max_links,
        ua=args.ua,
        allow_errors=args.allow_errors,
        respect_robots=args.respect_robots,
        rate_limit=args.rate_limit,
        no_private_ip=args.no_private_ip,
        expect=args.expect,
    )

    if args.check_proxy:
        result = fetcher.check_proxy()
        if json_mode:
            import json as _json

            sys.stdout.write(_json.dumps(result, indent=2) + "\n")
        else:
            if result["ok"]:
                sys.stdout.write("proxy OK: %s (network=%s)\n" % (result["proxy"], result["network"]))
                if result.get("exit_ip"):
                    sys.stdout.write("exit_ip: %s  is_tor: %s\n" % (result["exit_ip"], result["is_tor"]))
            else:
                sys.stderr.write("proxy DOWN: %s\n" % result["proxy"])
        return 0 if result["ok"] else exit_code_for(result.get("error_code") or "PROXY_DOWN")

    if args.verbose:
        for url in urls:
            sys.stderr.write("[fetch] network=%s url=%s\n" % (network, url))

    keep_raw = fmt == "raw"

    if mode == "search":
        return _run_search(args, fetcher, network, fmt, json_mode)

    if mode == "crawl":
        if not urls:
            parser.error("--follow requires a seed URL via -n/-t/-i")
        return _run_crawl(args, fetcher, network, urls[0], fmt, json_mode)

    if len(urls) == 1:
        results = [fetcher.fetch(urls[0], keep_raw=keep_raw)]
    else:
        results = fetcher.fetch_many(urls, concurrency=args.concurrency or config.DEFAULT_CONCURRENCY,
                                     keep_raw=keep_raw)

    render(results, fmt=fmt)

    worst = 0
    for r in results:
        if not r.get("ok"):
            code = r.get("error_code")
            worst = max(worst, exit_code_for(code or "APP_ERROR"))
    return worst


def _run_search(args, fetcher, network, fmt, json_mode):
    from .search import SEARCH_BACKENDS, search, search_fetch

    backend = args.search_backend
    search_url = None
    if backend and backend not in SEARCH_BACKENDS:
        search_url = backend  # treat an unknown value as a SearXNG base URL
        backend = "searxng"
    top_n = args.search_fetch_top or 0

    if top_n > 0:
        bundle = search_fetch(
            args.search, top_n=top_n, network=network, backend=backend,
            search_url=search_url, fetcher=fetcher, time_range=args.time_range or "",
            concurrency=args.concurrency or config.DEFAULT_CONCURRENCY,
        )
        payload = bundle
        ok = bundle["search"].ok
        code = bundle["search"].get("error_code")
    else:
        sres = search(
            args.search, network=network, backend=backend,
            search_url=search_url, fetcher=fetcher, time_range=args.time_range or "",
        )
        payload = {k: v for k, v in sres.items()}
        ok = sres.ok
        code = sres.get("error_code")

    _emit_json(payload, fmt, json_mode, search_only=top_n == 0)
    return 0 if ok else exit_code_for(code or "APP_ERROR")


def _run_crawl(args, fetcher, network, seed, fmt, json_mode):
    from .crawl import crawl

    depth = int(args.follow)
    result = crawl(
        seed, depth=depth, max_pages=args.max_pages,
        network=network, fetcher=fetcher,
        concurrency=args.concurrency or config.DEFAULT_CONCURRENCY,
    )
    _emit_json(result, fmt, json_mode)
    return 0 if result.ok else exit_code_for(result.get("error_code") or "APP_ERROR")


def _emit_json(payload, fmt, json_mode, search_only=False):
    from .output import dump_json, dump_jsonl
    import json as _json

    if json_mode:
        if fmt == "jsonl" and isinstance(payload, dict):
            # sql/jsonl both accepted; search emits one object
            sys.stdout.write(dump_jsonl(payload) + "\n")
        else:
            sys.stdout.write(dump_json(payload) + "\n")
        return
    # human renderer
    if isinstance(payload, dict) and "search" in payload:
        sres = payload["search"]
        print("SEARCH: %s" % sres.get("query", ""))
        print("BACKEND: %s  NETWORK: %s  RESULTS: %s" % (
            sres.get("backend", ""), sres.get("network", ""), sres.get("total_results", 0)))
        for item in sres.get("results", []):
            print("- [%s] %s" % (item.get("target_tld", ""), item.get("title", "")))
            print("  %s" % item.get("url", ""))
            if item.get("snippet"):
                print("  %s" % (item["snippet"][:200]))
        print("\nFETCHED TOP-N PAGES:")
        for page in payload.get("pages", []):
            print("- %s [%s] ok=%s status=%s source=%s" % (
                page.get("requested_url", ""), page.get("content_kind", ""),
                page.get("ok"), page.get("status"), page.get("source", "")))
    elif isinstance(payload, dict) and "results" in payload and payload.get("query") is not None:
        print("SEARCH: %s" % payload.get("query", ""))
        print("BACKEND: %s  NETWORK: %s  RESULTS: %s" % (
            payload.get("backend", ""), payload.get("network", ""), payload.get("total_results", 0)))
        for item in payload.get("results", []):
            print("- [%s] %s" % (item.get("target_tld", ""), item.get("title", "")))
            print("  %s" % item.get("url", ""))
            if item.get("snippet"):
                print("  %s" % (item["snippet"][:200]))
    elif isinstance(payload, dict) and "pages" in payload:
        print("CRAWL SEED: %s" % payload.get("seed_url", ""))
        print("PAGES: %s  LINKS: %s  MAX_DEPTH: %s/%s  EXHAUSTED: %s  MAX_PAGES_HIT: %s" % (
            payload.get("pages_fetched"), payload.get("links_discovered"),
            payload.get("max_depth"), payload.get("max_depth_target"),
            payload.get("frontier_exhausted"), payload.get("max_pages_reached")))
        for page in payload.get("pages", []):
            print("- [depth=%s] %s ok=%s status=%s kind=%s stated=%s tokens=%s" % (
                page.get("depth"), page.get("requested_url", ""), page.get("ok"),
                page.get("status"), page.get("content_kind"), page.get("cached"),
                page.get("estimated_tokens")))
    else:
        print(_json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())