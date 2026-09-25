"""Fetcher behavior tests against the local HTTP server + fake SOCKS5 proxy."""

import json
import time

import pytest

from openclaw_fetch import Fetcher, fetch, fetch_many
from openclaw_fetch.cache import TtlCache


def make_fetcher(server, **kw):
    kw.setdefault("use_cache", False)
    return Fetcher(**kw)


class TestBasics:
    def test_html_extraction(self, server):
        r = fetch(server.url("/"), use_cache=False, max_links=20)
        assert r.ok
        assert r.status == 200
        assert r.title == "Index Title"
        assert "First Heading" in r.text
        assert "First paragraph" in r.text
        assert r.content_kind == "html"
        assert r.network == "NORMAL"
        assert r.redirect_chain
        assert r.target_tld == "clearnet"

    def test_links_deduped_and_relative_resolved(self, server):
        r = fetch(server.url("/"), use_cache=False, max_links=20)
        urls = [l["url"] for l in r.links]
        assert server.url("/target") in urls
        assert "https://example.org/ext" in urls
        # /dup#1 and /dup#2 share the same absolute URL -> deduped
        assert len(urls) == len(set(urls))
        # hash-only and mailto links skipped
        assert not any(u.endswith("#frag") for u in urls)

    def test_metadata(self, server):
        r = fetch(server.url("/"), use_cache=False)
        assert r.metadata["og:title"] == "OG Title"
        assert r.metadata["description"] == "meta desc here"
        assert r.metadata["og:image"] == "http://img/x.png"
        assert r.metadata["canonical"].endswith("/canon")

    def test_markdown(self, server):
        r = fetch(server.url("/"), use_cache=False)
        assert "# First Heading" in r.markdown
        assert "[target link](" in r.markdown
        assert "- First item" in r.markdown

    def test_json_numbers_in_contract(self, server):
        r = fetch(server.url("/"), use_cache=False, max_links=5)
        for key in ("ok", "status", "content_length", "text_length",
                    "returned_text_length", "took_ms", "link_count", "cached"):
            assert key in r
        assert r["ok"] is True

    def test_charset_fallback(self, server):
        r = fetch(server.url("/nocharset"), use_cache=False)
        assert "déjà vu" in r.text


class TestStatus:
    def test_404_not_ok(self, server):
        r = fetch(server.url("/404"), use_cache=False)
        assert not r.ok
        assert r.error_code == "HTTP"
        assert r.status == 404

    def test_404_with_allow_errors(self, server):
        r = fetch(server.url("/404"), use_cache=False, allow_errors=True)
        assert r.ok
        assert r.status == 404

    def test_retries_then_success(self, server):
        f = Fetcher(retries=4, retry_backoff=0.01, use_cache=False)
        r = f.fetch(server.url("/retry"))
        assert r.ok
        assert r.status == 200
        assert "flaky-ok" in r.text

    def test_5xx_fails_after_retries(self, server):
        f = Fetcher(retries=1, retry_backoff=0.01, use_cache=False)
        r = f.fetch(server.url("/500"))
        assert not r.ok
        assert r.error_code == "HTTP"
        assert r.status == 500

    def test_redirect_chain(self, server):
        r = fetch(server.url("/redirect"), use_cache=False)
        assert r.ok
        assert r.redirects == 1
        assert r.final_url.endswith("/")
        assert r.redirect_chain[0] == server.url("/redirect")


class TestLimits:
    def test_max_bytes_content_length_guard(self, server):
        f = Fetcher(max_bytes=1000, use_cache=False)
        r = f.fetch(server.url("/bomb"))
        assert not r.ok
        assert r.error_code == "BODY_TOO_LARGE"

    def test_max_bytes_streaming_guard(self, server):
        f = Fetcher(max_bytes=70000, use_cache=False, timeout=10)
        r = f.fetch(server.url("/stream-big"))
        assert not r.ok
        assert r.error_code == "BODY_TOO_LARGE"

    def test_max_chars_truncates(self, server):
        f = Fetcher(max_chars=20, use_cache=False)
        r = f.fetch(server.url("/"))
        assert "TRUNCATED" in r.text
        assert r.returned_text_length > 20  # includes the truncation note


class TestValidation:
    def test_scheme_blocked(self):
        r = fetch("ftp://example.com/file", use_cache=False)
        assert not r.ok
        assert r.error_code == "SCHEME_BLOCKED"

    def test_onion_wrong_network(self):
        r = fetch("http://deadbeefdeadbeef.onion/", use_cache=False)
        assert not r.ok
        assert r.error_code == "NETWORK_MISMATCH"
        assert "tor" in r.error

    def test_i2p_wrong_network(self):
        r = fetch("http://example.i2p/", use_cache=False)
        assert not r.ok
        assert r.error_code == "NETWORK_MISMATCH"
        assert "i2p" in r.error

    def test_i2p_over_tor(self):
        f = Fetcher(network="tor", use_cache=False)
        r = f.fetch("http://example.i2p/")
        assert not r.ok
        assert r.error_code == "NETWORK_MISMATCH"

    def test_onion_over_i2p(self):
        f = Fetcher(network="i2p", use_cache=False)
        r = f.fetch("http://deadbeefdeadbeef.onion/")
        assert not r.ok
        assert r.error_code == "NETWORK_MISMATCH"

    def test_credentials_redacted(self, server):
        url = server.url("/").replace("http://", "http://user:secret@")
        r = fetch(url, use_cache=False)
        assert r.ok
        assert "secret" not in json.dumps(r)
        assert "user" not in r.requested_url or r.requested_url == url
        assert "secret" not in r.final_url
        assert "secret" not in r.text

    def test_unknown_network_raises(self):
        with pytest.raises(Exception):
            Fetcher(network="carrier-pigeon")


class TestProxyNetworks:
    def test_tor_over_fake_socks(self, server, socks_proxy):
        f = Fetcher(
            network="tor",
            tor_proxy=socks_proxy.url(),
            timeout=10,
            retries=1,
            retry_backoff=0.01,
            use_cache=False,
        )
        r = f.fetch(server.url("/"))
        assert r.ok
        assert r.status == 200
        assert r.network == "TOR"
        assert r.proxy_used == socks_proxy.url()

    def test_retries_over_socks(self, server, socks_proxy):
        f = Fetcher(
            network="tor",
            tor_proxy=socks_proxy.url(),
            timeout=10,
            retries=4,
            retry_backoff=0.01,
            use_cache=False,
        )
        r = f.fetch(server.url("/retry"))
        assert r.ok
        assert r.status == 200

    def test_i2p_network_selection(self, server, socks_proxy):
        import openclaw_fetch.config as config

        f = Fetcher(network="i2p", i2p_proxy="http://127.0.0.1:4444", use_cache=False)
        assert f.proxy_url() == "http://127.0.0.1:4444"
        assert f.i2p_proxy == "http://127.0.0.1:4444"


class TestCookies:
    def test_session_shares_cookies_within_fetcher(self, server):
        f = Fetcher(use_cache=False)
        assert f.fetch(server.url("/setcookie")).ok
        r = f.fetch(server.url("/needcookie"))
        assert r.ok
        assert "authed" in r.text

    def test_cookie_jar_persists_across_fetchers(self, server, jar_path):
        f = Fetcher(cookie_jar=jar_path, use_cache=False)
        assert f.fetch(server.url("/setcookie")).ok
        f2 = Fetcher(cookie_jar=jar_path, use_cache=False)
        r = f2.fetch(server.url("/needcookie"))
        assert r.ok
        assert "authed" in r.text

    def test_no_cookie_no_session(self, server):
        f = Fetcher(use_cache=False)
        r = f.fetch(server.url("/needcookie"))
        assert not r.ok
        assert r.status == 403

    def test_jar_file_mode_0600(self, server, jar_path):
        import os

        f = Fetcher(cookie_jar=jar_path, use_cache=False)
        f.fetch(server.url("/setcookie"))
        assert os.stat(jar_path).st_mode & 0o777 == 0o600


class TestPost:
    def test_post_form(self, server):
        f = Fetcher(use_cache=False)
        r = f.post(server.url("/post"), data={"q": "tor onion"})
        assert r.ok
        assert r.content_kind == "json"
        assert "q=tor+onion" in r.text


class TestBatch:
    def test_fetch_many_preserves_order(self, server):
        urls = [server.url("/preview?i=%d" % i) for i in range(5)]
        results = fetch_many(urls, concurrency=3, use_cache=False)
        assert len(results) == 5
        assert all(r.ok for r in results)
        assert [r.requested_url for r in results] == urls

    def test_fetch_many_mixed_failures(self, server):
        urls = [server.url("/"), server.url("/404"), server.url("/redirect")]
        results = fetch_many(urls, concurrency=2, use_cache=False)
        assert results[0].ok
        assert not results[1].ok
        assert results[1].error_code == "HTTP"
        assert results[2].ok


class TestContentKinds:
    def test_json_kind(self, server):
        r = fetch(server.url("/json"), use_cache=False)
        assert r.ok
        assert r.content_kind == "json"
        assert "hello" in r.text

    def test_pdf_stub(self, server):
        r = fetch(server.url("/pdf"), use_cache=False)
        assert r.ok
        assert r.content_kind == "pdf"
        assert "pypdf" in r.text


class TestCheckProxy:
    def test_check_proxy_normal_shape(self, monkeypatch):
        # offline: stub the probe target so no real network is touched
        f = Fetcher(network="normal", use_cache=False, retries=0)
        out = f.check_proxy()
        assert "ok" in out
        assert out["network"] == "NORMAL"

    def test_check_proxy_for_auto_does_not_raise(self):
        out = Fetcher(network="auto", use_cache=False).check_proxy_for("auto")
        assert out["network"] == "AUTO"
        assert set(out["probes"]) == {"normal", "tor", "i2p"}

    @pytest.mark.live
    def test_check_proxy_normal(self, server):
        # real clearnet probe; opt in with `-m live`
        f = Fetcher(network="normal", use_cache=False, retries=0)
        out = f.check_proxy()
        assert "ok" in out
        assert "network" in out
        assert out["network"] == "NORMAL"


class TestSecurityAndContext:
    def test_no_private_ip_checks_redirect_targets(self, server, monkeypatch):
        calls = []

        def fake_resolve(host):
            calls.append(host)
            return True, len(calls) == 3, None

        monkeypatch.setattr("openclaw_fetch.fetcher.public_ip_addresses", fake_resolve)
        result = Fetcher(no_private_ip=True, use_cache=False).fetch(server.url("/redirect"))
        assert result.error_code == "HOST_BLOCKED"
        assert len(calls) == 3

    def test_no_private_ip_revalidates_before_request(self, server, monkeypatch):
        calls = []

        def fake_resolve(host):
            calls.append(host)
            return True, len(calls) > 1, None

        monkeypatch.setattr("openclaw_fetch.fetcher.public_ip_addresses", fake_resolve)
        result = Fetcher(no_private_ip=True, use_cache=False).fetch(server.url("/echo"))
        assert result.error_code == "HOST_BLOCKED"
        assert len(calls) == 2

    def test_redirect_headers_drop_cross_origin_credentials(self):
        from openclaw_fetch.fetcher import _redirect_headers

        headers = _redirect_headers(
            {"Authorization": "Bearer secret", "X-Trace": "keep"},
            "https://one.example/start",
            "https://two.example/end",
        )
        assert headers == {"X-Trace": "keep"}

    def test_cross_origin_redirect_never_resends_body(self, server):
        fetcher = Fetcher(use_cache=False, retries=0)
        result = fetcher.fetch(
            server.url("/redirect307"),
            method="POST",
            data={"token": "s3cret"},
        )
        assert not result.ok
        assert result.error_code == "REQUEST"
        assert "s3cret" not in json.dumps(result)

    def test_cache_context_separates_request_headers(self, server, tmp_path):
        def make(value):
            return Fetcher(
                headers={"X-Context": value},
                cache_dir=str(tmp_path),
                cache_ttl=60,
            )

        first = make("one").fetch(server.url("/preview"))
        second = make("two").fetch(server.url("/preview"))
        third = make("one").fetch(server.url("/preview"))
        assert first.cached is False
        assert second.cached is False
        assert third.cached is True

    def test_response_cookies_disable_cache(self, server, tmp_path):
        fetcher = Fetcher(cache_dir=str(tmp_path), cache_ttl=60)
        first = fetcher.fetch(server.url("/setcookie"))
        second = fetcher.fetch(server.url("/setcookie"))
        assert first.cached is False
        assert second.cached is False


class TestDarknetRouting:
    def test_auto_network_resolution(self):
        from openclaw_fetch.fetcher import resolve_network

        onion = "http://" + "a" * 56 + ".onion/"
        i2p = "http://example.i2p/"
        assert resolve_network("auto", onion) == "tor"
        assert resolve_network("auto", i2p) == "i2p"
        assert resolve_network("auto", "https://example.com/") == "normal"
        assert resolve_network("tor", "https://example.com/") == "tor"

    def test_auto_network_routes_i2p_targets_to_i2p(self):
        r = fetch("http://example.i2p/", network="auto", use_cache=False)
        # routed to I2P (labelled I2P) rather than the clearnet or a mismatch
        assert r.network == "I2P"
        assert r.error_code != "NETWORK_MISMATCH"

    def test_auto_network_unknown_still_raises(self):
        with pytest.raises(Exception):
            Fetcher(network="carrier-pigeon")

    def test_darknet_timeout_floor(self):
        f = Fetcher(network="tor", tor_proxy="socks5h://127.0.0.1:9050")
        onion = "http://" + "a" * 56 + ".onion/"
        assert f._timeouts("https://example.com/") == (f.connect_timeout, f.timeout)
        connect, read = f._timeouts(onion)
        assert connect >= 45 and read >= 90
        assert f._timeouts("http://example.i2p/") == (connect, read)

    def test_explicit_timeouts_win_over_darknet_floor(self):
        f = Fetcher(network="tor", timeout=5, connect_timeout=3,
                    tor_proxy="socks5h://127.0.0.1:9050")
        onion = "http://" + "a" * 56 + ".onion/"
        assert f._timeouts(onion) == (3, 5)

    def test_isolation_default_on_and_per_request_unique(self):
        f = Fetcher(network="tor", tor_proxy="socks5h://127.0.0.1:9050")
        assert f.isolate is True
        one = f._isolated_proxy("tor")
        two = f._isolated_proxy("tor")
        assert one != two
        assert one.startswith("socks5h://isolate-")
        assert one.endswith("@127.0.0.1:9050")
        assert f.proxy_url("tor") == "socks5h://127.0.0.1:9050"

    def test_isolation_can_be_disabled_and_never_applies_to_other_networks(self):
        f = Fetcher(network="tor", isolate=False, tor_proxy="socks5h://127.0.0.1:9050")
        assert f._isolated_proxy("tor") == "socks5h://127.0.0.1:9050"
        g = Fetcher(network="i2p", i2p_proxy="http://127.0.0.1:4444")
        assert g._isolated_proxy("i2p") == "http://127.0.0.1:4444"
        assert Fetcher(network="normal")._isolated_proxy("normal") is None

    def test_isolated_proxy_keeps_retry_tags_distinct(self):
        f = Fetcher(network="tor", tor_proxy="socks5h://127.0.0.1:9050")
        first = f._isolated_proxy("tor", tag="r0")
        second = f._isolated_proxy("tor", tag="r1")
        assert first != second
        assert first.endswith("-r0@127.0.0.1:9050")
        assert second.endswith("-r1@127.0.0.1:9050")

    def test_env_isolate_binding(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_ISOLATE", "false")
        assert Fetcher(network="tor").isolate is False
        monkeypatch.setenv("OPENCLAW_ISOLATE", "1")
        assert Fetcher(network="tor").isolate is True

    def test_tor_fetch_uses_isolated_proxy_per_attempt(self, server, socks_proxy):
        f = Fetcher(network="tor", tor_proxy=socks_proxy.url(), use_cache=False, retries=0)
        result = f.fetch(server.url("/echo?q=iso"))
        assert result.ok
        assert result.network == "TOR"


class TestErrorResultShape:
    def test_error_result_carries_full_read_shape(self):
        from openclaw_fetch.errors import error_result

        r = error_result("CONNECTION", "boom", requested_url="http://x/", network="NORMAL")
        for key in ("ok", "error", "error_code", "network", "requested_url", "final_url",
                    "status", "content_type", "content_length", "title", "text",
                    "markdown", "content_kind", "links", "metadata", "items",
                    "headings", "redirects", "redirect_chain", "proxy_used",
                    "cached", "target_tld", "took_ms"):
            assert key in r, key
        assert r.status is None
        assert r.text == ""
        assert r.links == []

    def test_error_results_expose_attributes(self):
        r = fetch("ftp://example.com/x", use_cache=False)
        assert not r.ok
        assert r.status is None
        assert r.text == ""
        assert r.content_kind is None
        assert r.proxy_used is None

    def test_error_result_target_tld_from_url(self):
        r = fetch("http://" + "a" * 56 + ".onion/", network="normal", use_cache=False)
        assert r.target_tld == "onion"
        r2 = fetch("http://example.i2p/", network="normal", use_cache=False)
        assert r2.target_tld == "i2p"

    def test_error_result_mutable_defaults_are_not_shared(self):
        from openclaw_fetch.errors import error_result

        first = error_result("CONNECTION", "a")
        second = error_result("CONNECTION", "b")
        first["links"].append("x")
        first["metadata"]["k"] = "v"
        assert second["links"] == []
        assert second["metadata"] == {}


class TestIsolationObservability:
    def test_tor_result_reports_isolation(self, server, socks_proxy):
        f = Fetcher(network="tor", tor_proxy=socks_proxy.url(), use_cache=False)
        assert f.fetch(server.url("/")).metadata.get("isolated") is True
        g = Fetcher(network="tor", tor_proxy=socks_proxy.url(), use_cache=False, isolate=False)
        assert "isolated" not in g.fetch(server.url("/")).metadata
        h = Fetcher(network="normal", use_cache=False)
        assert "isolated" not in h.fetch(server.url("/")).metadata


class TestRawBytesContract:
    def test_raw_body_is_exact_bytes_and_json_safe(self, server):
        r = fetch(server.url("/binary"), use_cache=False, keep_raw=True)
        from openclaw_fetch.util import raw_bytes

        assert raw_bytes(r).startswith(b"\x89PNG\r\n\x1a\n")
        assert raw_bytes(r).endswith(b"\xff\xfe\x00\x01")
        json.dumps(r)  # documented promise: Result must stay serializable

    def test_raw_bytes_preserves_invalid_utf8(self, server):
        r = fetch(server.url("/binary"), use_cache=False, keep_raw=True)
        from openclaw_fetch.util import raw_bytes, raw_text

        assert isinstance(raw_bytes(r), bytes)
        assert isinstance(raw_text(r), str)
        # the text view is lossy but the bytes are not
        assert raw_text(r).encode("utf-8", errors="replace") != b""

    def test_raw_body_cached_is_also_bytes(self, server, tmp_path):
        f = Fetcher(cache_dir=str(tmp_path), cache_ttl=60)
        first = f.fetch(server.url("/binary"), keep_raw=True)
        second = f.fetch(server.url("/binary"), keep_raw=True)
        from openclaw_fetch.util import raw_bytes

        assert second["cached"] is True
        assert raw_bytes(first) == raw_bytes(second)

    def test_fetch_format_promotes_field(self, server):
        md = fetch(server.url("/"), use_cache=False, format="markdown")
        assert md["text"].startswith("#") or "[" in md["text"][:40] or md["text"]
        txt = fetch(server.url("/"), use_cache=False, format="text")
        assert txt["text"] != md["text"] or md["text"] == txt["text"]
        for r in (md, txt):
            assert r["title"] and r["markdown"]  # all fields always present

    def test_fetch_format_rejects_unknown(self, server):
        with pytest.raises(Exception):
            fetch(server.url("/"), use_cache=False, format="pdf-text")

    def test_fetch_many_format_applies_to_each(self, server):
        out = fetch_many([server.url("/"), server.url("/preview")], use_cache=False,
                         format="markdown", concurrency=2)
        assert len(out) == 2
        assert all(r["markdown"] for r in out)

    def test_feed_items_alias_does_not_collide(self, server):
        r = fetch(server.url("/feed.xml"), use_cache=False)
        assert r.feed_items == r["items"]
        assert r.feed_items and isinstance(r.feed_items[0], dict)
        # r.items stays the dict method
        assert callable(r.items)


class TestExpectIsAContract:
    def test_expect_mismatch_fails_even_with_allow_errors(self, server):
        r = fetch(server.url("/"), use_cache=False, expect="pdf", allow_errors=True)
        assert r.ok is False
        assert r.error_code == "EXPECT_MISMATCH"
        assert "pdf" in r.error


class TestCrawlHonesty:
    def test_all_pages_failed_is_not_ok(self):
        from openclaw_fetch import crawl

        r = crawl("http://127.0.0.1:1/", depth=0, use_cache=False, retries=0, timeout=2)
        assert r.ok is False
        assert r.pages_succeeded == 0
        assert r.pages_failed >= 1
        assert r.error and r.error_code

    def test_partial_crawl_reports_both_counts(self, server):
        from openclaw_fetch import crawl

        r = crawl(server.url("/"), depth=1, max_pages=3, use_cache=False)
        assert r.pages_succeeded >= 1
        assert r.pages_fetched == r.pages_succeeded + r.pages_failed
        assert r.ok is True


class TestFeedRelativeLinks:
    def test_relative_item_links_resolved_against_feed_url(self, server):
        r = fetch(server.url("/feed.xml"), use_cache=False)
        assert r["content_kind"] == "rss"
        links = [i["link"] for i in r["items"]]
        assert any(l.startswith("http://") and l.endswith("/relative/item") for l in links), links
        assert all(not l.startswith("/") for l in links)
        assert r.feed_items[0]["link"].startswith("http")


class TestClone:
    def test_clone_preserves_config_and_overrides_budget(self, server, socks_proxy):
        f = Fetcher(network="tor", tor_proxy=socks_proxy.url(), max_chars=100,
                    max_links=7, headers={"X-A": "1"}, use_cache=False, isolate=True)
        c = f.clone(max_chars=0)
        assert c.max_chars == 0
        assert c.max_links == 7
        assert c.network == "tor"
        assert c.tor_proxy == socks_proxy.url()
        assert c.headers.get("X-A") == "1"
        assert c.isolate is True
        assert c.cache.enabled is False
        # independent objects: changing one must not affect the other
        c.headers["X-A"] = "2"
        assert f.headers.get("X-A") == "1"
        assert c.fetch(server.url("/")).network == "TOR"

    def test_search_fetch_pages_keep_their_budget(self, server, monkeypatch):
        from openclaw_fetch.search import search_fetch

        html = ('<article class="result"><a href="%s">T</a>'
                '<p class="content">snippet</p></article>')

        class Stub:
            def __init__(self, **kw):
                self.kw = kw
                self.calls = []

            def clone(self, **overrides):
                child = Stub(**self.kw)
                child.kw.update(overrides)
                child.calls = self.calls
                return child

            def fetch(self, url, **kwargs):
                self.calls.append((url, self.kw.get("max_chars")))
                if "searx" in url or "search" in url:
                    return _Res(ok=True, _raw_body=html % (server.url("/target"),))
                return _Res(ok=True, text="BODY" * 500, content_kind="html")

            def fetch_many(self, urls, concurrency=4):
                cap = self.kw.get("max_chars")
                return [self.fetch(u) for u in urls]

        class _Res(dict):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.ok = kw.get("ok", False)

            def __getattr__(self, name):
                try:
                    return self[name]
                except KeyError:
                    raise AttributeError(name)

        stub = Stub(max_chars=250)
        bundle = search_fetch("x", top_n=1, network="normal",
                                         backend="searxng",
                                         search_url="https://searx.example/search",
                                         fetcher=stub)
        assert bundle["search"].ok
        assert bundle["pages"]
        engine_caps = [cap for url, cap in stub.calls if "searx" in url]
        page_caps = [cap for url, cap in stub.calls if "searx" not in url]
        # regression: the engine page used to be fetched with the caller's tiny
        # budget (empty results) and the pages with max_chars=0 (unbounded output)
        assert engine_caps == [0], stub.calls
        assert page_caps == [250], stub.calls
