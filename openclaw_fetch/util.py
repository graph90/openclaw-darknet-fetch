"""Small shared utilities: token estimation, text helpers, URL helpers."""

import re

_WORD_SPLIT = re.compile(r"\s+")


def estimate_tokens(text):
    """Approximate token count of ``text`` (heuristic, charset-independent).

    Agents use this to budget their context window before accepting a body.
    Rule of thumb: ~4 chars per token for mixed text, plus a small structural
    overhead for markdown/JSON.
    """
    if not text:
        return 0
    chars = len(text)
    words = _WORD_SPLIT.split(text)
    word_est = sum(max(1, len(w) / 4.0) for w in words if w)
    return int(round(chars / 4.0 * 0.9 + word_est * 0.1)) or 1


def host_of(url):
    """Return the lowercase host of ``url`` (without port/user) or ``None``."""
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc.rsplit("@", 1)[-1].split(":")[0].lower()
    except Exception:
        return None
    return host or None


def public_ip_addresses(host):
    """Return (public_yes_no, private_yes_no, resolution_error).

    Resolves a hostname and reports whether any resolved address is
    loopback/private/link-local. Used by the ``--no-private-ip`` guard.
    """
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, False, "DNS resolution failed for %r" % host
    addrs = []
    for info in infos:
        ip = info[4][0]
        if "%" in ip:
            ip = ip.split("%", 1)[0]
        addrs.append(ip)
    if not addrs:
        return False, False, "no addresses returned for %r" % host
    private = False
    for ip in addrs:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_unspecified
        ):
            private = True
    return True, private, None


def normalize_url(url):
    """Lowercase scheme/host, strip default ports and fragments."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if (scheme == "http" and netloc.endswith(":80")) or (
        scheme == "https" and netloc.endswith(":443")
    ):
        netloc = netloc.rsplit(":", 1)[0]
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))