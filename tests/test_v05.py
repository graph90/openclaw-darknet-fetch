"""v0.5 additions: one test class per task in task-v05.md (all offline)."""

import json
import os
import subprocess
import sys

from openclaw_fetch import Fetcher, collect_documents, deep_fetch, find_mirrors, doctor, fetch
from openclaw_fetch.parse import extract_selector, page_label

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(args):
    return subprocess.run(
        [sys.executable, "-m", "openclaw_fetch", *args],
        capture_output=True, text=True, cwd=REPO, timeout=60)


class TestTask1Doctor:
    def test_doctor_structure(self):
        out = doctor(network="auto", tor_proxy="socks5h://127.0.0.1:1", i2p_proxy="http://127.0.0.1:1")
        assert "checks" in out and "isolation" in out and "torrc_hints" in out
        assert out["ok"] is False
        assert out["isolation"].startswith("unverified")
        names = {c["name"] for c in out["checks"]}
        assert {"tor_socks", "i2p_proxy", "tor_controlport"} <= names

    def test_cli_tor_doctor(self):
        p = run_cli(["--tor-doctor", "--json"])
        assert p.returncode in (0, 2)
        data = json.loads(p.stdout)
        assert "isolation" in data and "torrc_hints" in data


class TestTask2ExitVerify:
    def test_verify_stamps_exit(self, server, socks_proxy, monkeypatch):
        f = Fetcher(network="tor", tor_proxy=socks_proxy.url(), use_cache=False, tor_verify=True)
        monkeypatch.setattr(f, "_verify_exit", lambda: ("203.0.113.7", True))
        r = f.fetch(server.url("/"))
        assert r.ok and r["exit_verified"] is True
        assert r["exit_ip"] == "203.0.113.7" and r["is_tor"] is True

    def test_verify_fail_closed(self, server, socks_proxy, monkeypatch):
        f = Fetcher(network="tor", tor_proxy=socks_proxy.url(), use_cache=False, tor_verify=True)
        monkeypatch.setattr(f, "_verify_exit", lambda: ("203.0.113.99", False))
        r = f.fetch(server.url("/"))
        assert not r.ok and r.error_code == "ANONYMITY_UNVERIFIED"


class TestTask3Config:
    def test_toml_and_precedence(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.toml"
        cfg.write_text('tor_proxy = "socks5h://127.0.0.1:9999"\nmax_chars = 123\n')
        monkeypatch.delenv("OPENCLAW_TOR_PROXY", raising=False)
        monkeypatch.delenv("OPENCLAW_MAX_CHARS", raising=False)
        f = Fetcher(config_path=str(cfg), use_cache=False)
        assert f.tor_proxy == "socks5h://127.0.0.1:9999"
        assert f.max_chars == 123
        monkeypatch.setenv("OPENCLAW_TOR_PROXY", "socks5h://127.0.0.1:8888")
        f2 = Fetcher(config_path=str(cfg), use_cache=False)
        assert f2.tor_proxy == "socks5h://127.0.0.1:8888"

    def test_cli_config(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('max_chars = 321\n')
        p = run_cli(["--env-report", "--config", str(cfg)])
        assert p.returncode == 0
        data = json.loads(p.stdout)
        assert data["max_chars"] == 321 and data["config_source"] == str(cfg)


class TestTask4DeepFetch:
    def test_deep_dedupes_by_hash(self, server, monkeypatch):
        from openclaw_fetch.result import Result

        def fake_search(query, **kw):
            return Result(ok=True, error="", error_code=None, network="NORMAL",
                          query=query, total_results=2, results=[
                              {"title": "A", "url": server.url("/preview"), "snippet": "", "target_tld": "clearnet", "rank": 1},
                              {"title": "B", "url": server.url("/preview"), "snippet": "", "target_tld": "clearnet", "rank": 2},
                          ], took_ms=1)
        # deep_fetch resolves search at call time via `from .search import search`.
        import openclaw_fetch.deep as deep_mod
        monkeypatch.setattr(deep_mod, "run_search", fake_search) if hasattr(deep_mod, "run_search") else None
        import sys as _sys
        search_mod = _sys.modules["openclaw_fetch.search"]
        monkeypatch.setattr(search_mod, "search", fake_search)
        out = deep_fetch("x", top_n=2, network="normal", use_cache=False)
        assert out["ok"] is True
        # Same preview page twice -> hash-deduped to one source.
        assert out["returned_sources"] == 1
        assert "Brief: x" in out["brief"]

    def test_cli_deep_not_needed_but_mcp_exposed(self):
        from openclaw_fetch import deep_fetch as df
        assert callable(df)


class TestTask5Mirrors:
    def test_find_mirrors_header(self, server, monkeypatch):
        import sys as _sys
        from openclaw_fetch.result import Result

        onion = "http://" + "a" * 56 + ".onion/"
        def fake_search(query, **kw):
            return Result(ok=True, error="", error_code=None, network="AUTO",
                          query=query, total_results=1, results=[
                              {"title": "T", "url": onion, "snippet": "", "target_tld": "onion", "rank": 1}], took_ms=1)
        search_mod = _sys.modules["openclaw_fetch.search"]
        monkeypatch.setattr(search_mod, "search", fake_search)
        out = find_mirrors(server.url("/"), network="normal", use_cache=False)
        assert out["onions"] == [onion] or onion in out["onions"]
        assert any(s["source"] == "tor66-search" for s in out["sources"])


class TestTask6Documents:
    def test_bucketing(self, server):
        out = collect_documents(server.url("/links"), use_cache=False)
        assert out.ok
        assert [d["url"] for d in out["documents"]] == [server.url("/paper.pdf")]
        assert [a["url"] for a in out["archive"]] == [server.url("/bundle.zip")]
        assert [d["url"] for d in out["download"]] == [server.url("/img.png")]

    def test_cli(self, server):
        p = run_cli(["-n", server.url("/links"), "--collect-documents", "--json", "--no-cache"])
        assert p.returncode == 0, p.stderr
        data = json.loads(p.stdout)
        assert data["total"] == 3


class TestTask7Interstitial:
    def test_label_function(self):
        assert page_label("<p>Please wait...</p>", "Please wait...") == "interstitial"
        assert page_label("", "Members only. Please log in") == "gated"
        assert page_label("", "Solve this captcha") == "challenge"

    def test_retry_returns_real_page(self, server):
        f = Fetcher(use_cache=False, interstitial_wait=0.1)
        r = f.fetch(server.url("/please-wait"))
        assert r.ok and "Real Page" in r.text
        assert r["metadata"].get("interstitial") is True

    def test_gated_label(self, server):
        r = fetch(server.url("/gated"), use_cache=False)
        assert r.ok and r["metadata"].get("skip_reason") == "gated"


class TestTask8Index:
    def test_index_flagged(self, server):
        r = fetch(server.url("/index"), use_cache=False)
        assert r["metadata"].get("is_index") is True
        assert r["metadata"]["index_entry_count"] == 10
        assert len(r["metadata"]["index_rows"]) == 10

    def test_article_not_index(self, server):
        r = fetch(server.url("/"), use_cache=False)
        assert not r["metadata"].get("is_index")


class TestTask9DeadLedger:
    def test_fast_fail_after_threshold(self, tmp_path):
        ledger = str(tmp_path / "dead.json")
        f = Fetcher(network="tor", use_cache=False, retries=0, timeout=2,
                    dead_ledger=ledger, dead_threshold=3, tor_proxy="socks5h://127.0.0.1:1")
        url = "http://" + "a" * 56 + ".onion/"
        for _ in range(3):
            r = f.fetch(url)
            assert r.error_code != "SUSPECTED_DEAD"
        r4 = f.fetch(url)
        assert r4.error_code == "SUSPECTED_DEAD" and r4["suspected_dead"] is True
        data = json.loads(open(ledger).read())
        assert data["a" * 56 + ".onion"]["suspected_dead"] is True

    def test_cli_dead_ledger_flag(self, server, tmp_path):
        ledger = str(tmp_path / "d.json")
        p = run_cli(["-n", server.url("/preview"), "--json", "--no-cache",
                     "--dead-ledger", ledger])
        assert p.returncode == 0, p.stderr
        # Clearnet fetch never writes a dead entry, but the flag must parse.
        assert json.loads(p.stdout)["ok"] is True


class TestTask10Selector:
    def test_tag_class_id(self, server):
        r = fetch(server.url("/"), use_cache=False, selector="h1")
        assert r["extracted"] and r["extracted"][0]["text"] == "First Heading"
        r2 = fetch(server.url("/index"), use_cache=False, selector="div.entry a")
        assert len(r2["extracted"]) == 10

    def test_attribute_operators(self):
        html = '<p><a href="/docs/x">d</a><a href="https://ext.test/y">e</a></p>'
        out = extract_selector(html, 'a[href^="/"]', base_url="http://x.test/")
        assert len(out) == 1 and out[0]["attrs"]["href"] == "http://x.test/docs/x"
        out2 = extract_selector(html, 'a[href*="ext"]')
        assert len(out2) == 1

    def test_bad_selector_returns_usage(self, server):
        r = fetch(server.url("/"), use_cache=False, selector="@@@")
        assert not r.ok and r.error_code == "USAGE"

    def test_cli_selector(self, server):
        p = run_cli(["-n", server.url("/"), "--selector", "h1", "--json", "--no-cache"])
        assert p.returncode == 0, p.stderr
        assert json.loads(p.stdout)["extracted"][0]["text"] == "First Heading"
