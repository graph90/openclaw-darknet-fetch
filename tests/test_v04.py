"""v0.4 additions: one test class per task in task.md (all offline)."""

import hashlib
import json
import os
import subprocess
import sys

from openclaw_fetch import Fetcher, compare, fetch
from openclaw_fetch.cache import TtlCache
from openclaw_fetch.fetcher import is_valid_onion, onion_info
from openclaw_fetch.parse import classify_link

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(args):
    return subprocess.run(
        [sys.executable, "-m", "openclaw_fetch", *args],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
    )


class TestTask1Provenance:
    def test_fetched_at_and_sha(self, server):
        r = fetch(server.url("/"), use_cache=False)
        assert r.ok
        assert r["fetched_at"].endswith("Z") and "T" in r["fetched_at"]
        sha = r["content_sha256"]
        assert len(sha) == 64
        assert all(c in "0123456789abcdef" for c in sha)

    def test_same_body_same_sha(self, server):
        a = fetch(server.url("/preview"), use_cache=False)
        b = fetch(server.url("/preview"), use_cache=False)
        assert a["content_sha256"] == b["content_sha256"]

    def test_error_carries_provenance_shape(self):
        r = fetch("ftp://example.com/x", use_cache=False)
        assert not r.ok
        assert r["fetched_at"] and r["content_sha256"] == ""


class TestTask2OnionValidation:
    def test_bad_checksum_is_invalid(self):
        assert is_valid_onion("a" * 56 + ".onion") is False
        info = onion_info("a" * 56 + ".onion")
        assert info["is_onion"] is True
        assert info["valid"] is False

    def test_short_name_is_invalid(self):
        assert is_valid_onion("abc.onion") is False

    def test_strict_mode_rejects(self):
        f = Fetcher(network="tor", strict_onion=True, use_cache=False)
        r = f.fetch("http://" + "a" * 56 + ".onion/")
        assert not r.ok
        assert r.error_code == "ONION_INVALID"

    def test_non_strict_still_reports_mismatch_first(self):
        # Without strict mode the routing error wins; strict check must not break it.
        r = fetch("http://" + "a" * 56 + ".onion/", use_cache=False)
        assert r.error_code == "NETWORK_MISMATCH"

    def test_cli_strict_onion(self):
        p = run_cli(["-t", "http://" + "a" * 56 + ".onion/", "--strict-onion", "--json", "--no-cache"])
        data = json.loads(p.stdout)
        assert data["error_code"] == "ONION_INVALID" and p.returncode == 1

    def test_real_tor66_is_valid(self):
        assert is_valid_onion("3bbad7fauom4d6sgppalyqddsqbf5u5p56b5k5uk2zxsy3d6ey2jobad.onion") is True


class TestTask3HeadersAndMirrors:
    def test_response_headers_present(self, server):
        r = fetch(server.url("/"), use_cache=False)
        assert r["response_headers"].get("content-type")
        assert "content-type" in r["response_headers"]

    def test_onion_location_harvested(self, server):
        r = fetch(server.url("/onion-location"), use_cache=False)
        assert r.ok
        onion = "http://" + "a" * 56 + ".onion/"
        assert r["metadata"]["onion_location"] == onion
        assert r["metadata"]["mirrors"] == [{"url": onion, "source": "onion-location"}]


class TestTask4LinksAndMetadataOnly:
    def test_classify_file_kinds(self):
        assert classify_link("https://x.test/paper.pdf") == ("document", "clearnet")
        assert classify_link("https://x.test/bundle.zip") == ("archive", "clearnet")
        assert classify_link("https://x.test/img.png") == ("download", "clearnet")
        assert classify_link("http://" + "a" * 56 + ".onion/") == ("onion", "onion")

    def test_fetch_links_are_classified(self, server):
        r = fetch(server.url("/links"), use_cache=False)
        by_url = {l["url"]: l for l in r.links}
        assert by_url[server.url("/paper.pdf")]["kind"] == "document"
        assert by_url[server.url("/bundle.zip")]["kind"] == "archive"
        assert by_url[server.url("/page")]["kind"] == "page"

    def test_metadata_only_drops_body_keeps_links(self, server):
        f = Fetcher(use_cache=False, metadata_only=True)
        r = f.fetch(server.url("/"))
        assert r.ok and r.title == "Index Title"
        assert r.text == "" and r.markdown == ""
        assert r.links and r["metadata_only"] is True

    def test_metadata_only_does_not_poison_cache(self, server, tmp_path):
        cache = str(tmp_path / "c")
        m = Fetcher(use_cache=True, cache_dir=cache, metadata_only=True)
        assert m.fetch(server.url("/")).text == ""
        full = Fetcher(use_cache=True, cache_dir=cache)
        first = full.fetch(server.url("/"))
        assert first.text and first["cached"] is False
        second = full.fetch(server.url("/"))
        assert second.text and second["cached"] is True

    def test_cli_metadata_only(self, server):
        p = run_cli(["-n", server.url("/"), "--metadata-only", "--json", "--no-cache"])
        assert p.returncode == 0, p.stderr
        data = json.loads(p.stdout)
        assert data["text"] == "" and data["title"] == "Index Title"


class TestTask5CliMethods:
    def test_post_json_body_library(self, server):
        f = Fetcher(use_cache=False)
        r = f.fetch(server.url("/post"), method="POST", data='{"q":"tor"}', form=False)
        assert r.ok and "tor" in r.text

    def test_head_library(self, server):
        r = Fetcher(use_cache=False).fetch(server.url("/"), method="HEAD")
        assert r.ok and r.status == 200 and r.text == ""

    def test_cli_post_json_body(self, server):
        p = run_cli(["-n", server.url("/post"), "--method", "POST",
                     "--json-body", '{"q":"cli"}', "--json", "--no-cache"])
        assert p.returncode == 0, p.stderr
        data = json.loads(p.stdout)
        assert data["ok"] is True and "cli" in data["text"]

    def test_cli_header_and_form(self, server):
        p = run_cli(["-n", server.url("/header-echo"), "--method", "GET",
                     "--header", "X-Test: hello", "--json", "--no-cache"])
        assert p.returncode == 0, p.stderr
        assert json.loads(p.stdout)["ok"] is True
        p2 = run_cli(["-n", server.url("/post"), "--method", "POST",
                      "--form", "a=1&b=2", "--json", "--no-cache"])
        assert p2.returncode == 0, p2.stderr
        assert json.loads(p2.stdout)["ok"] is True

    def test_cli_conflicting_bodies_exit_1(self, server):
        p = run_cli(["-n", server.url("/post"), "--data", "x", "--form", "a=1"])
        assert p.returncode == 1

    def test_cli_bad_header_exit_1(self, server):
        p = run_cli(["-n", server.url("/"), "--header", "NoColonHere"])
        assert p.returncode == 1


class TestTask6Budgets:
    def test_requests_budget(self, server):
        f = Fetcher(use_cache=False, budget_requests=1)
        assert f.fetch(server.url("/")).ok
        r2 = f.fetch(server.url("/preview"))
        assert not r2.ok and r2.error_code == "BUDGET" and r2["budget_exhausted"] is True

    def test_bytes_budget(self, server):
        f = Fetcher(use_cache=False, budget_bytes=10)
        assert f.fetch(server.url("/")).ok
        assert f.fetch(server.url("/preview")).error_code == "BUDGET"

    def test_cli_budget(self, server):
        p = run_cli(["-n", server.url("/"), server.url("/preview"),
                     "--budget-requests", "1", "--format", "jsonl", "--no-cache"])
        assert p.returncode == 6, p.stderr
        lines = [json.loads(l) for l in p.stdout.splitlines()]
        assert lines[0]["ok"] is True and lines[1]["error_code"] == "BUDGET"


class TestTask7LogJsonl:
    def test_log_lines(self, server, tmp_path):
        log = tmp_path / "events.jsonl"
        f = Fetcher(use_cache=False, log_jsonl=str(log))
        f.fetch(server.url("/"))
        lines = log.read_text().strip().splitlines()
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["event"] == "fetch" and event["network"] == "NORMAL"
        assert event["status"] == 200 and event["ok"] is True

    def test_cli_log_jsonl(self, server, tmp_path):
        log = tmp_path / "cli.jsonl"
        p = run_cli(["-n", server.url("/preview"), "--json", "--no-cache",
                     "--log-jsonl", str(log)])
        assert p.returncode == 0, p.stderr
        event = json.loads(log.read_text().strip())
        assert event["event"] == "fetch" and event["status"] == 200


class TestTask8Watch:
    def test_watch_returns_not_modified(self, server):
        f = Fetcher(use_cache=False, watch=True)
        r1 = f.fetch(server.url("/etag"))
        assert r1.ok and r1.status == 200
        assert r1["validators"]["etag"] == '"v1"'
        r2 = f.fetch(server.url("/etag"))
        assert r2.ok and r2.status == 304 and r2["not_modified"] is True
        assert r2.text == ""

    def test_no_watch_refetches(self, server):
        f = Fetcher(use_cache=False, watch=False)
        assert f.fetch(server.url("/etag")).status == 200
        assert f.fetch(server.url("/etag")).status == 200

    def test_cli_cache_stats(self):
        p = run_cli(["--cache-stats"])
        assert p.returncode == 0
        data = json.loads(p.stdout)
        assert "entries" in data and "bytes" in data


class TestTask9CacheStats:
    def test_stats_and_eviction(self, cache_dir):
        cache = TtlCache(cache_dir=cache_dir, ttl=60)
        cache.put("normal", "http://a.test", 200, "http://a.test/", "text/html", b"<h1>hi</h1>")
        s = cache.stats()
        assert s["entries"] == 1 and s["bytes"] > 0
        # Tiny cap evicts everything.
        assert cache.evict_until_under(0.0001) is True
        assert cache.stats()["entries"] == 0

    def test_fetcher_evicts_with_small_cap(self, server, tmp_path):
        cache = str(tmp_path / "c")
        f = Fetcher(cache_dir=cache, cache_ttl=60, cache_max_mb=0.001)
        r = f.fetch(server.url("/"))
        assert r.ok
        # After the put, eviction removed the entry again (cap ~1KB < body).
        assert TtlCache(cache_dir=cache).stats()["entries"] == 0


class TestTask10Compare:
    def test_compare_identical(self, server):
        out = compare([server.url("/preview"), server.url("/preview")], use_cache=False)
        assert out["all_reachable"] is True
        assert out["all_identical_content"] is True
        assert out["ok"] is True and out["succeeded_count"] == 2

    def test_compare_mixed(self, server):
        out = compare([server.url("/"), server.url("/404")], use_cache=False)
        assert out["ok"] is True and out["succeeded_count"] == 1
        assert out["all_reachable"] is False
        entries = {e["url"]: e for e in out["entries"]}
        assert entries[server.url("/")]["ok"] is True
        assert entries[server.url("/404")]["ok"] is False

    def test_compare_none_reachable(self, server):
        out = compare(["http://127.0.0.1:1/"], use_cache=False, retries=0, timeout=2)
        assert out["ok"] is False and out["error_code"] == "REQUEST"
