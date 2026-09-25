"""Tests for the v2.1 active features: RSS/PDF, embedded JSON, search
parsers, crawl, rate-limit/robots/private-ip guard, expect matching."""

import pytest

from openclaw_fetch import Fetcher, fetch
from openclaw_fetch.parse import (
    extract_embedded_json,
    extract_pdf_text,
    parse_document,
    parse_feed,
)
from openclaw_fetch.search import _parse_ahmia, _parse_ddg, _parse_searxng, search
from openclaw_fetch.util import estimate_tokens


class TestTokens:
    def test_estimate_tokens_present_in_result(self, server):
        r = fetch(server.url("/"), use_cache=False)
        assert "estimated_tokens" in r
        assert r["estimated_tokens"] >= 1
        assert r["estimated_tokens_total"] >= r["estimated_tokens"]

    def test_estimate_tokens_heuristic(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens("hello world") >= 1


class TestRssPdf:
    RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Onion Feed</title>
  <item><title>First Post</title>
    <link>http://example.onion/p1</link>
    <guid>g1</guid>
    <pubDate>Mon, 01 Jan 2026 00:00:00 GMT</pubDate>
    <description>Summary one.</description>
    <category>News</category>
    <category>Dark</category>
  </item>
  <item><title>Second Post</title>
    <link>http://example.onion/p2</link>
    <guid>g2</guid>
  </item>
</channel></rss>"""

    def test_rss_items(self):
        doc = parse_document(
            self.RSS, content_type="application/rss+xml", final_url="http://example.onion/"
        )
        assert doc["kind"] == "rss"
        assert doc["title"] == "Onion Feed"
        assert len(doc["items"]) == 2
        assert doc["items"][0]["link"] == "http://example.onion/p1"
        assert doc["items"][0]["tags"] == ["News", "Dark"]
        assert "First Post" in doc["text"]
        assert doc["link_count"] == 2

    def test_rss_dedupes_by_guid(self):
        feed = parse_feed(
            "<rss><channel>"
            "<item><guid>a</guid><title>T</title></item>"
            "<item><guid>a</guid><title>T</title></item>"
            "</channel></rss>"
        )
        assert len(feed["items"]) == 1

    def test_pdf_extraction_pdftotext(self):
        val = extract_pdf_text(b"%PDF-1.4 not really a pdf")
        # with no pypdf and no valid pdf, must return empty tuple, never raise
        assert isinstance(val, tuple)
        assert val[0] == "" and val[1] is None

    def test_pdf_stub_when_no_tool(self, monkeypatch):
        monkeypatch.setattr("openclaw_fetch.parse.shutil.which", lambda _: None)
        # pypdf absent in CI -> stub path
        doc = parse_document("", content_type="application/pdf", source_len=10)
        assert "pdf content" in doc["text"]


class TestEmbeddedJson:
    def test_next_data_payload(self):
        html = (
            "<html><head><script id='__NEXT_DATA__' type='application/json'>"
            '{"props":{"hello":"world"},"page":"home"}'
            "</script></head><body></body></html>"
        )
        payload = extract_embedded_json(html)
        assert '"hello"' in payload

    def test_initial_state_assignment(self):
        html = '<script>window.__INITIAL_STATE__ = {"a": 1, "b": [1, 2]};</script>'
        payload = extract_embedded_json(html)
        assert payload and "1" in payload

    def test_js_heavy_page_marks_payload_extracted(self, server):
        r = fetch(server.url("/js-heavy"), use_cache=False)
        assert r.needs_renderer is True
        assert r.payload_extracted is True
        assert "next_data_key" in r.text


class TestSearchParsers:
    def test_parse_ddg(self):
        html = (
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fx&amp;rut=a">Result Title</a>'
            '<a class="result__snippet" href="//duckduckgo.com/l/?x=1">the snippet text</a>'
        )
        items = _parse_ddg(html)
        assert len(items) == 1
        assert items[0]["url"] == "https://example.org/x"
        assert items[0]["title"] == "Result Title"
        assert items[0]["snippet"] == "the snippet text"

    def test_parse_ddg_unwraps_https(self):
        items = _parse_ddg('<a class="result__a" href="//example.org/h">T</a>')
        assert items[0]["url"] == "https://example.org/h"

    def test_parse_ahmia(self):
        html = '<h4><a href="http://deadbeefdeadbeef.onion">Onion Site</a></h4><cite>http://deadbeefdeadbeef.onion</cite>'
        items = _parse_ahmia(html)
        assert len(items) == 1
        assert items[0]["target_tld"] == "onion"
        assert items[0]["title"] == "Onion Site"

    def test_parse_searxng(self):
        html = (
            '<article class="result"><a href="/search?q=a&amp;url=https%3A%2F%2Fall.example%2Fx">Example</a>'
            '<p class="content">a snippet</p></article>'
        )
        items = _parse_searxng(html, "https://searx.be/search?q=a")
        assert len(items) == 1
        assert items[0]["url"] == "https://all.example/x"

    def test_search_searxng_requires_url(self):
        res = search("t", backend="searxng", network="normal")
        assert not res.ok
        assert res.error_code == "USAGE"

    def test_search_structure_on_failure(self):
        # dead proxy -> structured ok:false without raising
        res = search(
            "darkfeed", network="tor", backend="ddg",
            tor_proxy="socks5h://127.0.0.1:1", retries=0, timeout=3,
        )
        assert not res.ok
        assert "error_code" in res
        assert res["results"] == []


class TestCrawl:
    def test_crawl_depth_zero_fetches_seed_only(self, server):
        from openclaw_fetch import crawl

        r = crawl(server.url("/"), depth=0, max_pages=5, fetcher=Fetcher(use_cache=False))
        assert r.ok
        assert r.pages_fetched == 1
        assert r.pages[0]["depth"] == 0

    def test_crawl_same_host_only(self, server):
        from openclaw_fetch import crawl

        r = crawl(server.url("/"), depth=1, max_pages=10, fetcher=Fetcher(use_cache=False))
        assert r.ok
        pages = [p.requested_url for p in r.pages]
        # external link example.org must never be fetched
        assert not any("example.org" in u for u in pages)
        # nav menu link is on the same host -> discovered & fetched
        assert server.url("/nav-link") in pages
        assert all("depth" in p for p in r.pages)

    def test_crawl_limits_pages(self, server):
        from openclaw_fetch import crawl

        r = crawl(
            server.url("/"), depth=2, max_pages=2,
            fetcher=Fetcher(use_cache=False),
        )
        assert r.pages_fetched <= 2
        assert r.max_pages_reached is True


class TestGuards:
    def test_no_private_ip_blocks_loopback(self, server):
        f = Fetcher(no_private_ip=True, use_cache=False)
        r = f.fetch(server.url("/"))
        assert not r.ok
        assert r.error_code == "HOST_BLOCKED"

    def test_no_private_ip_off_allows_loopback(self, server):
        f = Fetcher(no_private_ip=False, use_cache=False)
        assert f.fetch(server.url("/")).ok

    def test_rates_fine(self, server):
        f = Fetcher(rate_limit=50, use_cache=False)
        assert f.fetch(server.url("/preview")).ok

    def test_robots_blocks_disallowed(self, server):
        f = Fetcher(respect_robots=True, use_cache=False)
        assert f.fetch(server.url("/blocked")).error_code == "ROBOTS_BLOCKED"
        assert f.fetch(server.url("/preview")).ok

    def test_expect_mismatch(self, server):
        f = Fetcher(expect="html", use_cache=False)
        r = f.fetch(server.url("/json"))
        assert not r.ok
        assert r.error_code == "EXPECT_MISMATCH"

    def test_expect_match(self, server):
        f = Fetcher(expect="json", use_cache=False)
        r = f.fetch(server.url("/json"))
        assert r.ok
        assert r.content_kind == "json"


class TestCliNew:
    def test_env_report(self):
        import json
        import subprocess
        import sys

        p = subprocess.run(
            [sys.executable, "-m", "openclaw_fetch", "--env-report"],
            capture_output=True, text=True, timeout=30,
        )
        assert p.returncode == 0
        data = json.loads(p.stdout)
        assert data["version"].startswith("0.3")

    def test_mcp_help(self):
        import subprocess
        import sys

        p = subprocess.run(
            [sys.executable, "-m", "openclaw_fetch", "mcp", "--help"],
            capture_output=True, text=True, timeout=30,
        )
        assert p.returncode == 0
        assert "--network" in p.stdout

    def test_crawl_cli_json(self, server):
        import json
        import os
        import subprocess
        import sys

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = subprocess.run(
            [
                sys.executable, "-m", "openclaw_fetch",
                "-n", server.url("/"), "--follow", "0", "--max-pages", "2",
                "--json", "--no-cache",
            ],
            capture_output=True, text=True, cwd=repo, timeout=60,
        )
        assert p.returncode == 0
        data = json.loads(p.stdout)
        assert "seed_url" in data
        assert data["pages_fetched"] == 1


class TestMcpDefaults:
    def test_server_network_and_max_chars_defaults(self, monkeypatch):
        import sys
        import types

        from openclaw_fetch.mcp_server import _build_server
        from openclaw_fetch.result import Result

        calls = []

        class FakeFetcher:
            def __init__(self, **kwargs):
                calls.append(kwargs)

            def fetch(self, url, **kwargs):
                return Result(ok=True, text="ok", markdown="# ok")

            def fetch_many(self, urls, concurrency=4):
                return [self.fetch(url) for url in urls]

        class FakeMCP:
            def __init__(self, name):
                self.name = name
                self.tools = {}

            def tool(self):
                def decorate(fn):
                    self.tools[fn.__name__] = fn
                    return fn

                return decorate

        fastmcp = types.ModuleType("mcp.server.fastmcp")
        fastmcp.FastMCP = FakeMCP
        server_module = types.ModuleType("mcp.server")
        server_module.fastmcp = fastmcp
        mcp_module = types.ModuleType("mcp")
        mcp_module.server = server_module
        monkeypatch.setitem(sys.modules, "mcp", mcp_module)
        monkeypatch.setitem(sys.modules, "mcp.server", server_module)
        monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp)
        monkeypatch.setattr("openclaw_fetch.fetcher.Fetcher", FakeFetcher)

        server = _build_server(network="tor", max_chars=321)
        server.tools["fetch"]("https://example.test")
        assert calls[-1]["network"] == "tor"
        assert calls[-1]["max_chars"] == 321
        assert calls[-1]["no_private_ip"] is True

        server.tools["fetch_many"](["https://example.test"])
        assert calls[-1]["max_chars"] == 321

        server.tools["fetch"]("https://example.test", network="normal")
        assert calls[-1]["network"] == "normal"

    def test_server_network_auto_and_isolate(self, monkeypatch):
        import sys
        import types

        from openclaw_fetch.mcp_server import _build_server
        from openclaw_fetch.result import Result

        calls = []

        class FakeFetcher:
            def __init__(self, **kwargs):
                calls.append(kwargs)

            def fetch(self, url, **kwargs):
                return Result(ok=True, text="ok", markdown="# ok")

            def fetch_many(self, urls, concurrency=4):
                return [self.fetch(url) for url in urls]

            def check_proxy(self):
                return {"ok": True, "network": "AUTO", "probes": {}}

        class FakeMCP:
            def __init__(self, name):
                self.name = name
                self.tools = {}

            def tool(self):
                def decorate(fn):
                    self.tools[fn.__name__] = fn
                    return fn

                return decorate

        fastmcp = types.ModuleType("mcp.server.fastmcp")
        fastmcp.FastMCP = FakeMCP
        server_module = types.ModuleType("mcp.server")
        server_module.fastmcp = fastmcp
        mcp_module = types.ModuleType("mcp")
        mcp_module.server = server_module
        monkeypatch.setitem(sys.modules, "mcp", mcp_module)
        monkeypatch.setitem(sys.modules, "mcp.server", server_module)
        monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp)
        monkeypatch.setattr("openclaw_fetch.fetcher.Fetcher", FakeFetcher)

        server = _build_server(network="normal")
        server.tools["fetch"]("http://example.onion.test", network="auto")
        assert calls[-1]["network"] == "auto"
        assert calls[-1]["no_private_ip"] is True

        server.tools["fetch"]("https://example.test", isolate=False)
        assert calls[-1]["isolate"] is False

        out = server.tools["proxy_status"](network="auto")
        assert out["network"] == "AUTO"
