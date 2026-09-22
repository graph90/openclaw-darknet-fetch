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


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.flush_cache:
        TtlCache().flush()
        sys.stderr.write("cache flushed\n")
        return 0

    if args.normal is not None:
        network = "normal"
        urls = list(args.normal)
    elif args.tor is not None:
        network = "tor"
        urls = list(args.tor)
    elif args.i2p is not None:
        network = "i2p"
        urls = list(args.i2p)
    else:
        parser.error("one of -n/--normal, -t/--tor, -i/--i2p is required")

    if args.batch:
        urls.extend(_read_batch(args.batch))

    if not urls and not args.check_proxy:
        parser.error("at least one URL is required (or use --check-proxy)")

    if args.max_bytes is not None and args.max_bytes <= 0:
        parser.error("--max-bytes must be greater than zero")
    if args.max_chars is not None and args.max_chars < 0:
        parser.error("--max-chars must be zero or greater")
    if args.retries is not None and args.retries < 0:
        parser.error("--retries must be zero or greater")

    fmt = _resolve_format(args)

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
    )

    if args.check_proxy:
        result = fetcher.check_proxy()
        if fmt == "json":
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


if __name__ == "__main__":
    sys.exit(main())