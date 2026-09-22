"""CLI integration tests: subprocess runs of `python -m openclaw_fetch`
and the legacy `openclaw_darknet_fetch.py` shim."""

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(args, cwd=REPO):
    return subprocess.run(
        [sys.executable, "-m", "openclaw_fetch", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=60,
    )


def run_legacy(args, cwd=REPO):
    return subprocess.run(
        [sys.executable, os.path.join(REPO, "openclaw_darknet_fetch.py"), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=60,
    )


class TestCliBasics:
    def test_version(self):
        from openclaw_fetch import __version__

        p = run_cli(["--version"])
        assert p.returncode == 0
        assert __version__ in p.stdout

    def test_legacy_shim_version(self):
        from openclaw_fetch import __version__

        p = run_legacy(["--version"])
        assert p.returncode == 0
        assert __version__ in p.stdout

    def test_single_json(self, server):
        p = run_cli(["-n", server.url("/"), "--json"])
        assert p.returncode == 0
        data = json.loads(p.stdout)
        assert data["ok"] is True
        assert data["title"] == "Index Title"
        assert "link_count" in data
        assert "error_code" in data

    def test_legacy_human_default(self, server):
        p = run_legacy(["-n", server.url("/"), "--max-chars", "50"])
        assert p.returncode == 0
        assert "NETWORK: NORMAL" in p.stdout
        assert "First Heading" in p.stdout

    def test_404_exit_code_5(self, server):
        p = run_cli(["-n", server.url("/404"), "--json"])
        assert p.returncode == 5
        data = json.loads(p.stdout)
        assert data["ok"] is False
        assert data["error_code"] == "HTTP"

    def test_404_with_allow_errors_exit_0(self, server):
        p = run_cli(["-n", server.url("/404"), "--allow-errors", "--json"])
        assert p.returncode == 0
        data = json.loads(p.stdout)
        assert data["ok"] is True
        assert data["status"] == 404

    def test_usage_error_exit_1(self):
        p = run_cli(["https://example.com"])
        assert p.returncode == 1

    def test_no_url_exit_1(self):
        p = run_cli(["-n"])
        assert p.returncode == 1

    def test_scheme_blocked_exit_1(self):
        p = run_cli(["-n", "ftp://example.com/x", "--json"])
        assert p.returncode == 1
        assert json.loads(p.stdout)["error_code"] == "SCHEME_BLOCKED"

    def test_onion_on_normal_exit_1_guidance(self):
        p = run_cli(["-n", "http://abcabcabcabc.onion/", "--json"])
        assert p.returncode == 1
        data = json.loads(p.stdout)
        assert data["error_code"] == "NETWORK_MISMATCH"
        assert "tor" in data["error"]

    def test_raw_mode(self, server):
        p = run_cli(["-n", server.url("/"), "--raw", "--max-chars", "0"])
        assert p.returncode == 0
        assert "<html" in p.stdout
        assert "Index Title" in p.stdout

    def test_markdown_format(self, server):
        p = run_cli(["-n", server.url("/"), "--format", "markdown"])
        assert p.returncode == 0
        assert "# First Heading" in p.stdout


class TestCliMulti:
    def test_jsonl(self, server):
        urls = [server.url("/preview?i=%d" % i) for i in range(3)]
        p = run_cli(["-n", *urls, "--format", "jsonl", "--concurrency", "2"])
        assert p.returncode == 0
        lines = [json.loads(line) for line in p.stdout.splitlines()]
        assert len(lines) == 3
        assert all(l["ok"] for l in lines)

    def test_batch_file(self, server, tmp_path):
        batch = tmp_path / "urls.txt"
        batch.write_text(
            "# comment\n%s\n%s\n\n" % (server.url("/"), server.url("/preview"))
        )
        p = run_cli(["-n", "--batch", str(batch), "--format", "jsonl"])
        assert p.returncode == 0
        lines = [json.loads(line) for line in p.stdout.splitlines()]
        assert len(lines) == 2
        assert all(l["ok"] for l in lines)

    def test_multi_human_has_separators(self, server):
        p = run_cli(["-n", server.url("/"), server.url("/preview")])
        assert p.returncode == 0
        assert p.stdout.count("~" * 60) == 1


class TestCliCache:
    def test_cache_hit_marked(self, server):
        cache = os.path.join(REPO, ".tmp-cli-cache")
        args = ["-n", server.url("/preview"), "--json"]
        try:
            p1 = run_cli([*args, "--no-cache"])
            assert json.loads(p1.stdout)["cached"] is False
            # force a writable cache dir via env
            env = {"CACHE": cache}
            os.environ["OPENCLAW_CACHE_DIR"] = cache
            p2 = run_cli([*args, "--cache-ttl", "60"])
            p2b = run_cli([*args, "--cache-ttl", "60"])
            assert json.loads(p2.stdout)["cached"] is False
            assert json.loads(p2b.stdout)["cached"] is True
            del os.environ["OPENCLAW_CACHE_DIR"]
        finally:
            import shutil

            shutil.rmtree(cache, ignore_errors=True)

    def test_flush_cache(self):
        p = run_cli(["--flush-cache"])
        assert p.returncode == 0
        assert "flushed" in p.stderr

    def test_check_proxy_flag_shows_proxy(self, server):
        # Fast path: tor check probes a live endpoint; just assert wiring works
        # with a fake proxy target we can't reach -> returns nonzero, structured
        p = run_cli(["-t", "--check-proxy", "--json", "--tor-proxy", "socks5h://127.0.0.1:1", "--timeout", "2", "--retries", "0"])
        assert p.returncode != 0
        out = json.loads(p.stdout)
        assert out["ok"] is False
        assert "error_code" in out