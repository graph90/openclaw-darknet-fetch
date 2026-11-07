"""Minimal Tor ControlPort client (raw sockets; stem is optional).

The ControlPort lets the fetcher verify isolation, read bootstrap/circuit
state, and send ``NEWNYM`` (new identity). Without a reachable port (or the
optional ``stem`` extra) every entry point degrades to a clear
``ControlUnavailable`` — never a crash.
"""

import os
import socket


class ControlUnavailable(RuntimeError):
    """Raised when the Tor ControlPort cannot be reached or authenticated."""


_DEFAULT_COOKIE_PATHS = (
    "~/.tor/control_auth_cookie",
    "/run/tor/control.authcookie",
    "/var/run/tor/control.authcookie",
)


def _read_cookie(cookie_path=None):
    for candidate in ([cookie_path] if cookie_path else list(_DEFAULT_COOKIE_PATHS)):
        if not candidate:
            continue
        try:
            with open(os.path.expanduser(candidate), "rb") as fh:
                return fh.read()
        except OSError:
            continue
    return None


class TorControl:
    """Tiny control client: authenticate, GETINFO, SIGNAL NEWNYM."""

    def __init__(self, host="127.0.0.1", port=9051, cookie_path=None, timeout=5.0):
        self.host = host
        self.port = int(port)
        self.cookie_path = cookie_path
        self.timeout = float(timeout)

    def _connect(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise ControlUnavailable("control port %s:%d unreachable: %s" % (self.host, self.port, exc)) from exc
        sock.settimeout(self.timeout)
        return sock

    def _send_line(self, sock, line):
        sock.sendall(line.encode("utf-8") + b"\r\n")

    def _read_reply(self, sock):
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8", errors="replace").strip()

    def authenticate(self):
        """Authenticate to the control port (cookie when present)."""
        sock = self._connect()
        try:
            sock.settimeout(self.timeout)
            # Greeting: read the initial line.
            self._read_reply(sock)
            cookie = _read_cookie(self.cookie_path)
            if cookie:
                # AUTHENTICATE "<hex-cookie>" — binary-safe via hex.
                self._send_line(sock, 'AUTHENTICATE "%s"' % cookie.hex())
            else:
                self._send_line(sock, "AUTHENTICATE")
            reply = self._read_reply(sock)
            if not reply.startswith("250"):
                raise ControlUnavailable("authentication failed: %s" % reply)
            return True
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def getinfo(self, key):
        """Return the value for ``key`` (e.g. ``status/bootstrap-phase``)."""
        sock = self._connect()
        try:
            self._read_reply(sock)
            if not self._auth_on(sock):
                raise ControlUnavailable("authentication failed")
            self._send_line(sock, "GETINFO %s" % key)
            reply = self._read_reply(sock)
            if reply.startswith("552") or reply.startswith("514"):
                raise ControlUnavailable("GETINFO %s failed: %s" % (key, reply))
            # GETINFO replies: "250-key=value" then "250 OK".
            first = reply.splitlines()[0] if reply.splitlines() else ""
            if "=" in first:
                # Strip leading "250-key=" (or "250 key=").
                _code, _, value = first.partition("=")
                return value.strip()
            return first.strip()
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def bootstrap_phase(self):
        """Human-readable bootstrap phase string."""
        return self.getinfo("status/bootstrap-phase")

    def newnym(self):
        """Request a new identity (SIGNAL NEWNYM). Returns True on success."""
        sock = self._connect()
        try:
            self._read_reply(sock)
            if not self._auth_on(sock):
                raise ControlUnavailable("authentication failed")
            self._send_line(sock, "SIGNAL NEWNYM")
            reply = self._read_reply(sock)
            if reply.startswith("250"):
                return True
            raise ControlUnavailable("NEWNYM failed: %s" % reply)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _auth_on(self, sock):
        cookie = _read_cookie(self.cookie_path)
        if cookie:
            self._send_line(sock, 'AUTHENTICATE "%s"' % cookie.hex())
        else:
            self._send_line(sock, "AUTHENTICATE")
        return self._read_reply(sock).startswith("250")


def control_available(host="127.0.0.1", port=9051, timeout=2.0):
    """True when a socket connects to the control port (no auth attempted)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
