"""Tor/I2P doctor: verify the daemons behind the proxies we claim.

``doctor()`` socket-probes the configured proxies (and the Tor ControlPort),
reports whether per-request isolation can be verified, and prints the exact
``torrc`` lines to add. It never claims isolation it cannot check: without a
reachable ControlPort, ``isolation`` is ``"unverified (requires ControlPort)"``.
"""

import socket

TORRC_HINTS = [
    "IsolateSOCKSAuth 1",
    "SocksPort 127.0.0.1:9050",
    "ControlPort 127.0.0.1:9051",
    "CookieAuthentication 1",
]

TOR_CONTROL_HINTS = [
    "# torrc: allow per-request circuit isolation + control",
    "IsolateSOCKSAuth 1",
    "SocksPort 127.0.0.1:9050",
    "ControlPort 127.0.0.1:9051",
    "CookieAuthentication 1",
]


def _host_port(proxy_url, default_port):
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(proxy_url or "")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or default_port
        return host, int(port)
    except (ValueError, TypeError):
        return "127.0.0.1", default_port


def _reachable(host, port, timeout=2.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as exc:
        return False, str(exc)


def doctor(network="auto", tor_proxy=None, i2p_proxy=None, isolate=True, timeout=2.0):
    """Probe the proxies for ``network`` and report what can be verified.

    Returns ``{ok, network, checks:[{name, ok, detail}], isolation, tor_proxy,
    i2p_proxy, torrc_hints}``. Never raises for unreachable proxies.
    """
    from . import config as _config
    from .fetcher import _normalize_network, redact_url

    net = _normalize_network(network)
    tor_proxy = tor_proxy or _config.TOR_PROXY_DEFAULT
    i2p_proxy = i2p_proxy or _config.I2P_PROXY_DEFAULT
    checks = []

    if net in ("auto", "tor"):
        host, port = _host_port(tor_proxy, 9050)
        ok, err = _reachable(host, port, timeout=timeout)
        checks.append({"name": "tor_socks", "ok": ok,
                       "detail": "socks %s:%d %s" % (host, port, "reachable" if ok else ("unreachable: %s" % err))})
    if net in ("auto", "i2p"):
        host, port = _host_port(i2p_proxy, 4444)
        ok, err = _reachable(host, port, timeout=timeout)
        checks.append({"name": "i2p_proxy", "ok": ok,
                       "detail": "http %s:%d %s" % (host, port, "reachable" if ok else ("unreachable: %s" % err))})

    # ControlPort probe (only meaningful for Tor).
    control_ok = False
    control_detail = "not probed (network=%s)" % net
    bootstrap = None
    if net in ("auto", "tor"):
        ok, err = _reachable("127.0.0.1", 9051, timeout=timeout)
        control_ok = ok
        control_detail = "reachable" if ok else ("unreachable: %s" % err)
        if ok:
            try:
                from .tor_control import TorControl

                bootstrap = TorControl("127.0.0.1", 9051, timeout=timeout).bootstrap_phase()
                control_detail = "reachable (bootstrap: %s)" % bootstrap
            except Exception as exc:
                control_detail = "reachable (bootstrap unavailable: %s)" % exc
                bootstrap = None
        checks.append({"name": "tor_controlport", "ok": ok, "detail": control_detail})

    if not isolate:
        isolation = "disabled (--no-isolate)"
    elif control_ok:
        isolation = "verifiable (ControlPort reachable; verify IsolateSOCKSAuth via stem)"
    else:
        isolation = "unverified (requires ControlPort)"

    ok = any(c["ok"] and c["name"] in ("tor_socks", "i2p_proxy") for c in checks) if checks else False
    return {
        "ok": bool(ok),
        "network": net.upper(),
        "checks": checks,
        "isolation": isolation,
        "bootstrap": bootstrap,
        "tor_proxy": redact_url(tor_proxy),
        "i2p_proxy": redact_url(i2p_proxy),
        "torrc_hints": list(TOR_CONTROL_HINTS),
    }
