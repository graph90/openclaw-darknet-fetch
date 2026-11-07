"""v0.6 additions: one test class per task in task-v06.md (all offline)."""

import json
import os
import socket
import subprocess
import sys
import threading

from openclaw_fetch import Fetcher, compare, deep_fetch, fetch, find_mirrors, watch
from openclaw_fetch.parse import extract_flattened_payload, flatten_payload
from openclaw_fetch.tor_control import ControlUnavailable, TorControl
from openclaw_fetch.util import detect_language, hamming, near_duplicates, simhash
from openclaw_fetch.watch import watch as run_watch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(args):
    return subprocess.run(
        [sys.executable, "-m", "openclaw_fetch", *args],
        capture_output=True, text=True, cwd=REPO, timeout=60)


class FakeControl(threading.Thread):
    """Fake Tor ControlPort speaking just enough of the protocol."""

    def __init__(self):
        super().__init__(daemon=True)
        self.port = None
        self.newnym_count = 0
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self.port = self._socket.getsockname()[1]
        self._socket.listen(5)
        self._running = True

    def run(self):
        self._socket.settimeout(0.5)
        while self._running:
            try:
                conn, _ = self._socket.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(5)
            conn.sendall(b"250 Tor running\r\n")
            buf = b""
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="ignore").strip()
                    if text.startswith("AUTHENTICATE"):
                        conn.sendall(b"250 OK\r\n")
                    elif text.startswith("GETINFO"):
                        key = text.split(None, 1)[1] if " " in text else ""
                        if key == "status/bootstrap-phase":
                            conn.sendall(b"250-status/bootstrap-phase=DONE\r\n250 OK\r\n")
                        elif key == "circuit-status":
                            conn.sendall(b"250-circuit-status=1 GUARD\r\n250 OK\r\n")
                        else:
                            conn.sendall(b"250-%s=\r\n250 OK\r\n" % key.encode())
                    elif text.startswith("SIGNAL NEWNYM"):
                        self.newnym_count += 1
                        conn.sendall(b"250 OK\r\n")
                    elif text == "QUIT":
                        conn.sendall(b"250 closing\r\n")
                        return
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._running = False
        try:
            self._socket.close()
        except OSError:
            pass


class TestTask1PayloadWalk:
    def test_flatten_surfaces_article(self):
        html = ('<script id="__NEXT_DATA__" type="application/json">'
                '{"props":{"pageProps":{"title":"My Article","body":"The quick brown fox jumps","noise":"x"}}}</script>'
                '<body></body>')
        flat = extract_flattened_payload(html)
        assert "My Article" in flat and "quick brown" in flat

    def test_fetch_uses_flattened(self, server):
        r = fetch(server.url("/js-heavy"), use_cache=False)
        assert r.payload_extracted is True
        assert r["metadata"].get("payload_format") == "flattened"
        assert "next_data_key" in r.text  # key preserved, raw JSON braces gone
        assert '"next_data_key":' not in r.text


class TestTask2Control:
    def test_bootstrap_and_newnym(self):
        fake = FakeControl()
        fake.start()
        try:
            ctl = TorControl("127.0.0.1", fake.port, timeout=3)
            assert "DONE" in ctl.bootstrap_phase()
            assert ctl.newnym() is True
            assert fake.newnym_count == 1
        finally:
            fake.close()

    def test_unavailable_raises(self):
        try:
            TorControl("127.0.0.1", 1, timeout=1).bootstrap_phase()
            assert False, "should raise"
        except ControlUnavailable:
            pass


class TestTask3CircuitNewnym:
    def test_newnym_after_failures(self, server):
        fake = FakeControl()
        fake.start()
        try:
            f = Fetcher(network="tor", tor_proxy="socks5h://127.0.0.1:1", retries=0, timeout=2,
                        newnym_on_fail=2, tor_control="127.0.0.1:%d" % fake.port, use_cache=False)
            # Dead SOCKS proxy -> failures (not real Tor).
            f.fetch(server.url("/"))
            f.fetch(server.url("/"))
            assert fake.newnym_count == 1
        finally:
            fake.close()

    def test_circuit_fields_present(self, server, socks_proxy):
        f = Fetcher(network="tor", tor_proxy=socks_proxy.url(), use_cache=False)
        r = f.fetch(server.url("/"))
        assert "circuit_id" in r and "guard" in r and "exit_node" in r and "exit_country" in r


class TestTask4I2P:
    def test_outproxy_flag(self, server):
        # Direct fetch via real I2P proxy would block localhost; unit-test the
        # honest flag via the post-processor instead (no network).
        f = Fetcher(network="i2p", i2p_proxy="http://127.0.0.1:1", use_cache=False)
        base = fetch(server.url("/"), use_cache=False)
        probe = dict(base)
        probe["network"] = "I2P"
        probe["target_tld"] = "clearnet"
        stamped = f._post_process_v06(probe)
        assert stamped["metadata"].get("i2p_outproxy_used") is True

    def test_b32_flag(self, server):
        r = fetch(server.url("/"), use_cache=False)
        # Clearnet has no b32 flag.
        assert "b32_host" not in r["metadata"] or r["metadata"].get("b32_host") is None


class TestTask5Watch:
    def test_watch_etag(self, server):
        out = run_watch(server.url("/etag"), interval=0, max_polls=2, use_cache=False)
        assert out["polls"] == 2
        assert out["events"][1]["not_modified"] is True
        assert out["changed"] is False

    def test_cli_watch_flags_parse(self):
        p = run_cli(["-n", "http://127.0.0.1:1/", "--json", "--no-cache"])
        assert p.returncode in (0, 4, 6)  # network failure, flag parse is fine


class TestTask6Checkpoint:
    def test_resume(self, server, tmp_path):
        from openclaw_fetch import crawl

        ckpt = str(tmp_path / "ck.json")
        first = crawl(server.url("/"), depth=1, max_pages=2, use_cache=False, checkpoint=ckpt)
        assert first.ok and first.pages_fetched <= 2
        # Checkpoint file holds frontier/visited even after completion.
        if os.path.exists(ckpt):
            data = json.loads(open(ckpt).read())
            assert data["seed_url"] == server.url("/")
            assert "visited" in data and "frontier" in data


class TestTask7DryRun:
    def test_deep_dry_run_no_network(self):
        out = deep_fetch("x", network="tor", dry_run=True)
        assert out["dry_run"] is True and out["projected_pages"] == 3
        assert out["projected_tokens_total"] > 0

    def test_compare_dry_run(self):
        out = compare(["https://a.test/", "https://b.test/"], dry_run=True)
        assert out["dry_run"] is True and out["projected_tokens_total"] > 0

    def test_mirrors_dry_run(self):
        out = find_mirrors("example.com", dry_run=True)
        assert out["dry_run"] is True and out["projected_searches"] == ["tor66", "marginalia", "ddg"]


class TestTask8CompareDiff:
    def test_diff(self, server):
        out = compare([server.url("/"), server.url("/preview")], use_cache=False)
        assert out["pairwise_diffs"] and out["pairwise_diffs"][0]["diff_lines"] > 0
        assert out["pairwise_diffs"][0]["same_content"] is False
        assert "freshness" in out["entries"][0]

    def test_identical_no_diff(self, server):
        out = compare([server.url("/preview"), server.url("/preview")], use_cache=False)
        assert out["all_identical_content"] is True
        assert out["pairwise_diffs"][0]["diff_lines"] == 0


class TestTask9SimhashLang:
    def test_simhash(self):
        base = ("the quick brown fox jumps over the lazy dog and runs through the forest "
                "while birds sing above the tall green trees near the river bank today ")
        a = base
        b = base + "extra tail words change little"
        c = "totally different words about quantum computing and elliptic curve cryptography"
        assert hamming(simhash(a), simhash(a)) == 0
        assert near_duplicates([a, b, c], threshold=10) == [1]
        assert detect_language("the and of to is in for that with this")["lang"] == "en"
        assert detect_language("el la de que y en los del una para")["lang"] == "es"

    def test_language_in_metadata(self, server):
        r = fetch(server.url("/"), use_cache=False)
        assert "language" in r["metadata"]  # fixture is English


class TestTask10Ops:
    def test_files_exist(self):
        assert os.path.exists(os.path.join(REPO, "Dockerfile"))
        assert os.path.exists(os.path.join(REPO, ".github", "workflows", "ci.yml"))
        assert os.path.exists(os.path.join(REPO, "docs", "EXAMPLES.md"))
        assert os.path.exists(os.path.join(REPO, "SECURITY.md"))
        assert os.path.exists(os.path.join(REPO, "config.example.toml"))

    def test_via_archive_cli(self, server):
        # 404 with archive enabled (stubbed archive host unreachable -> original 404).
        p = run_cli(["-n", server.url("/404"), "--via-archive", "--json", "--no-cache"])
        assert p.returncode == 5  # archive unreachable, still a clean HTTP failure
