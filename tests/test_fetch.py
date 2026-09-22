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
    def test_check_proxy_normal(self, server):
        # point the normal probe at our own server by network override is not
        # possible; instead assert the method returns a structured dict today
        f = Fetcher(network="normal", use_cache=False, retries=0)
        out = f.check_proxy()
        assert "ok" in out
        assert "network" in out
        assert out["network"] == "NORMAL"