"""Pytest fixtures: a local threadsafe HTTP server and a fake SOCKS5 proxy.

The fake SOCKS5 proxy lets the Tor code path run in CI without real Tor.
The I2P path shares the same fetch machinery (only the proxy scheme differs),
so a unit test on proxy selection is enough for CI.
"""

import io
import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


# --------------------------------------------------------------------------
# HTTP test server
# --------------------------------------------------------------------------

class _State:
    def __init__(self):
        self.retry_hits = 0
        self.slow_hits = 0
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TestSrv/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        parts = self.path.split("?", 1)
        query = parts[1] if len(parts) > 1 else ""
        if path == "/":
            body = INDEX_HTML.encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
        elif path == "/js-heavy":
            body = JS_HEAVY_HTML.encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
        elif path == "/404":
            self._send(404, "text/html; charset=utf-8", b"<html><body>not found</body></html>")
        elif path == "/500":
            self._send(500, "text/html; charset=utf-8", b"<html><body>boom</body></html>")
        elif path == "/429":
            self._send(429, "text/html; charset=utf-8", b"too many", extra={"Retry-After": "0"})
        elif path == "/retry":
            with self.server.state.lock:
                self.server.state.retry_hits += 1
                hits = self.server.state.retry_hits
            if hits < 3:
                self._send(500, "text/plain; charset=utf-8", b"flaky", extra={"Retry-After": "0"})
            else:
                self._send(200, "text/plain; charset=utf-8", b"flaky-ok")
        elif path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/json":
            payload = json.dumps({"hello": "world", "n": 42}).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", payload)
        elif path == "/bomb":
            # Lies about size: pre-fetch Content-Length guard must abort.
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "999999999")
            self.end_headers()
            self.wfile.write(b"small")
        elif path == "/stream-big":
            # No Content-Length; streaming guard must abort mid-body.
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = b"x" * 65536
            for _ in range(200):
                try:
                    self.wfile.write(b"%x\r\n" % len(chunk))
                    self.wfile.write(chunk + b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    break
            self.wfile.write(b"0\r\n\r\n")
        elif path == "/setcookie":
            self.send_response(200)
            self.send_header("Set-Cookie", "sid=abc123; Path=/")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        elif path == "/needcookie":
            cookie = self.headers.get("Cookie", "")
            if "sid=abc123" in cookie:
                self._send(200, "text/plain; charset=utf-8", b"authed")
            else:
                self._send(403, "text/plain; charset=utf-8", b"no-cookie")
        elif path == "/nocharset":
            body = "\u00e9\u00e8\u00ea d\u00e9j\u00e0 vu".encode("iso-8859-1")
            self._send(200, "text/html", body)
        elif path == "/pdf":
            self._send(200, "application/pdf", b"%PDF-1.4 fake")
        elif path == "/robots.txt":
            self._send(200, "text/plain; charset=utf-8", b"User-agent: *\nDisallow: /blocked\n")
        elif path == "/blocked":
            self._send(200, "text/plain; charset=utf-8", b"robots said no")
        elif path == "/echo":
            self._send(200, "text/plain; charset=utf-8", b"echo:" + query.encode("utf-8"))
        elif path == "/preview":
            self._send(200, "text/html; charset=utf-8", b"<html><head><title>Pr</title></head><body><h1>Sky</h1></body></html>")
        else:
            self._send(404, "text/plain; charset=utf-8", b"no route")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        content_type = self.headers.get("Content-Type", "")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body = raw.decode("utf-8", errors="replace")
        payload = json.dumps({"method": "POST", "content_type": content_type, "body": body}).encode("utf-8")
        self._send(200, "application/json; charset=utf-8", payload)

    def _send(self, status, content_type, body, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


INDEX_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Index Title</title>
  <meta name="description" content="meta desc here">
  <meta property="og:title" content="OG Title">
  <meta property="og:image" content="http://img/x.png">
  <link rel="canonical" href="/canon">
</head>
<body>
  <nav><a href="/nav-link">Nav Link</a></nav>
  <h1>First Heading</h1>
  <p>First paragraph with a <a href="/target">target link</a> inside.</p>
  <p>Second paragraph.<br>Line two.</p>
  <ul>
    <li>First item</li>
    <li>Second item <a href="https://example.org/ext">ext</a></li>
  </ul>
  <a href="#frag">skip me hash</a>
  <a href="/dup#1">dup one</a>
  <a href="/dup#2">dup two</a>
  <script>JS_HERE</script>
</body>
</html>
"""

_BIG_SCRIPT = "var data = '" + ("x" * 70000) + "';"
JS_HEAVY_HTML = """\
<!doctype html>
<html><head><title>Tiny</title></head>
<body>
<script>""" + _BIG_SCRIPT + """</script>
<script type="application/json">{"next_data_key": 1}</script>
<div id="root"></div>
</body>
</html>
"""


class _ServerFixture:
    def __init__(self):
        self.server = None
        self.ready = threading.Event()

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(5)

    def _run(self):
        handler = Handler
        handler.state = _State()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.state = handler.state
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.ready.set()
        self.server.serve_forever()

    def url(self, path):
        return self.base + path


@pytest.fixture(scope="session")
def server():
    sf = _ServerFixture()
    sf.start()
    yield sf
    sf.server.shutdown()


# --------------------------------------------------------------------------
# Fake SOCKS5 proxy
# --------------------------------------------------------------------------

class Socks5Proxy:
    """Minimal RFC1928 SOCKS5 CONNECT relay, in-process."""

    def __init__(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(64)
        self.host, self.port = self.server.getsockname()
        self._running = True
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def url(self):
        return "socks5h://%s:%d" % (self.host, self.port)

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(20)
            header = self._read_exact(conn, 2)
            if len(header) < 2 or header[0] != 0x05:
                conn.close()
                return
            nmethods = header[1]
            if nmethods:
                conn.recv(nmethods)
            conn.sendall(b"\x05\x00")
            req = self._read_exact(conn, 4)
            if len(req) < 4 or req[0] != 0x05 or req[1] != 0x01:
                conn.close()
                return
            atyp = req[3]
            if atyp == 0x01:
                addr = socket.inet_ntoa(self._read_exact(conn, 4))
            elif atyp == 0x03:
                n = self._read_exact(conn, 1)[0]
                addr = self._read_exact(conn, n).decode("utf-8", errors="replace")
            elif atyp == 0x04:
                addr = socket.inet_ntop(socket.AF_INET6, self._read_exact(conn, 16))
            else:
                conn.close()
                return
            port_bytes = self._read_exact(conn, 2)
            port = int.from_bytes(port_bytes, "big")
            remote = socket.create_connection((addr, port), timeout=20)
            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        except Exception:
            try:
                conn.close()
            except OSError:
                pass
            return
        try:
            self._relay(conn, remote)
        finally:
            for sock in (conn, remote):
                try:
                    sock.close()
                except OSError:
                    pass

    def _relay(self, a, b):
        socks = [a, b]
        while True:
            readable, _, _ = select.select(socks, [], [], 1.0)
            for s in readable:
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                other = b if s is a else a
                try:
                    other.sendall(data)
                except OSError:
                    return

    def _read_exact(self, conn, n):
        data = b""
        while len(data) < n:
            try:
                chunk = conn.recv(n - len(data))
            except OSError:
                break
            if not chunk:
                break
            data += chunk
        return data

    def close(self):
        self._running = False
        try:
            self.server.close()
        except OSError:
            pass


@pytest.fixture(scope="session")
def socks_proxy():
    proxy = Socks5Proxy()
    yield proxy
    proxy.close()


# --------------------------------------------------------------------------
# Cache dir fixture
# --------------------------------------------------------------------------

@pytest.fixture()
def cache_dir(tmp_path):
    return str(tmp_path / "cache")


@pytest.fixture()
def jar_path(tmp_path):
    return str(tmp_path / "cookies.json")