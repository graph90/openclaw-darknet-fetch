"""Cache tests: key derivation, TTL, atomic roundtrip, flush, auth edge case."""

import json
import os
import time

import pytest

from openclaw_fetch.cache import TtlCache, has_cookies


class TestCacheBasics:
    def test_key_differs_by_network(self):
        url = "http://example.com/x"
        assert TtlCache.key_for("tor", url) != TtlCache.key_for("normal", url)
        assert TtlCache.key_for("normal", url) == TtlCache.key_for("normal", url)

    def test_roundtrip(self, cache_dir):
        cache = TtlCache(cache_dir=cache_dir, ttl=60)
        cache.put("normal", "http://a.test", 200, "http://a.test/", "text/html", b"<h1>hi</h1>")
        hit = cache.get("normal", "http://a.test")
        assert hit is not None
        assert hit["body"] == b"<h1>hi</h1>"
        assert hit["meta"]["status"] == 200
        assert hit["meta"]["final_url"] == "http://a.test/"

    def test_miss_when_disabled(self, cache_dir):
        cache = TtlCache(cache_dir=cache_dir, enabled=False)
        cache.put("normal", "http://a.test", 200, "http://a.test/", "text/html", b"x")
        assert cache.get("normal", "http://a.test") is None

    def test_ttl_expiry(self, cache_dir):
        cache = TtlCache(cache_dir=cache_dir, ttl=0)
        cache.put("normal", "http://b.test", 200, "http://b.test/", "text/html", b"x")
        assert cache.get("normal", "http://b.test") is None

    def test_flush(self, cache_dir):
        cache = TtlCache(cache_dir=cache_dir, ttl=60)
        cache.put("normal", "http://c.test", 200, "http://c.test/", "text/html", b"x")
        assert cache.get("normal", "http://c.test") is not None
        cache.flush()
        assert cache.get("normal", "http://c.test") is None
        assert not os.listdir(cache_dir)

    def test_atomic_files_exist(self, cache_dir):
        cache = TtlCache(cache_dir=cache_dir, ttl=60)
        key = cache.put("tor", "http://onion.test", 200, "http://onion.test/", "text/html", b"body")
        assert os.path.exists(os.path.join(cache_dir, key + ".json"))
        assert os.path.exists(os.path.join(cache_dir, key + ".body"))


class TestAuthEdge:
    def test_has_cookies_true(self, tmp_path):
        jar = tmp_path / "j.json"
        jar.write_text(json.dumps({"cookies": [{"name": "a", "value": "b"}]}))
        assert has_cookies(str(jar)) is True

    def test_has_cookies_empty_false(self, tmp_path):
        jar = tmp_path / "j.json"
        jar.write_text(json.dumps({"cookies": []}))
        assert has_cookies(str(jar)) is False

    def test_has_cookies_missing_false(self, tmp_path):
        assert has_cookies(str(tmp_path / "nope.json")) is False


def test_cache_disabled_when_jar_has_cookies(server, tmp_path):
    """Authenticated contexts must never be served from the disk cache."""
    from openclaw_fetch import Fetcher

    jar = tmp_path / "j.json"
    jar.write_text(json.dumps({"cookies": [{"name": "sid", "value": "x"}]}))
    f = Fetcher(cookie_jar=str(jar), use_cache=True, cache_dir=str(tmp_path / "c"))
    r1 = f.fetch(server.url("/"))
    r2 = f.fetch(server.url("/"))
    assert r1.ok and r2.ok
    assert not r1.cached and not r2.cached