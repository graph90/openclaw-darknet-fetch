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
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url).hostname
    except (TypeError, ValueError):
        return None
    return host.lower() if host else None


def redact_url(url):
    """Remove URL userinfo and return a safe URL for logs/results."""
    from urllib.parse import urlsplit, urlunsplit

    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url)
        if not parts.netloc:
            return url
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = "[%s]" % host
        port = parts.port
        netloc = host
        if port is not None and not (
            (parts.scheme.lower() == "http" and port == 80)
            or (parts.scheme.lower() == "https" and port == 443)
        ):
            netloc += ":%d" % port
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except (TypeError, ValueError):
        return "<invalid-url>"


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

class RawBody(str):
    """A raw response body that serializes as text but keeps the exact bytes.

    ``Result`` promises ``json.dumps(result)`` works, so the private ``_raw_body``
    value must be a ``str`` -- but ``--raw`` and ``--save-raw`` must hand back the
    original bytes, not a lossy decode. This is both: use :func:`raw_bytes` to get
    the original.
    """

    __slots__ = ("_raw",)

    def __new__(cls, data, encoding="utf-8"):
        if isinstance(data, str):
            obj = super().__new__(cls, data)
            obj._raw = data.encode("utf-8", errors="replace")
        else:
            obj = super().__new__(cls, bytes(data).decode(encoding, errors="replace"))
            obj._raw = bytes(data)
        return obj

    @property
    def raw(self):
        """The original, undecoded bytes."""
        return self._raw

    def __len__(self):
        return len(self._raw)


def raw_text(result, default=""):
    """Return a result's raw body as text (bytes are decoded, never dropped)."""
    body = result.get("_raw_body") if hasattr(result, "get") else None
    if body is None:
        body = (result or {}).get("text") if hasattr(result, "get") else None
    if body is None:
        return default
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body


def raw_bytes(result, default=b""):
    """Return a result's original raw bytes (text bodies are encoded)."""
    body = result.get("_raw_body") if hasattr(result, "get") else None
    if body is None:
        body = (result or {}).get("text") if hasattr(result, "get") else None
    if body is None:
        return default
    if isinstance(body, RawBody):
        return body.raw
    if isinstance(body, bytes):
        return body
    return str(body).encode("utf-8", errors="replace")


# -- SimHash near-duplicate detection (v0.6) ----------------------------------
def simhash(text, bits=64):
    """64-bit SimHash of ``text`` (word-tokenized, FNV-free hash)."""
    import hashlib

    if not text:
        return 0
    tokens = re.findall(r"[a-z0-9]{2,}", text.lower())
    if not tokens:
        return 0
    weights = [0] * bits
    for tok in set(tokens):
        digest = int.from_bytes(hashlib.sha1(tok.encode("utf-8")).digest()[:8], "big")
        for i in range(bits):
            weights[i] += 1 if (digest >> i) & 1 else -1
    fingerprint = 0
    for i, w in enumerate(weights):
        if w > 0:
            fingerprint |= 1 << i
    return fingerprint


def hamming(a, b):
    """Hamming distance between two integers (xor popcount)."""
    return bin(int(a) ^ int(b)).count("1")


def near_duplicates(texts, threshold=3):
    """Return indices that are near-duplicates of an earlier text.

    ``texts`` is a list of strings; returns ``[i, ...]`` for every ``i`` whose
    SimHash is within ``threshold`` bits of any earlier text.
    """
    seen = []
    dupes = []
    for i, text in enumerate(texts):
        h = simhash(text or "")
        if any(hamming(h, prev) <= threshold for prev in seen):
            dupes.append(i)
        else:
            seen.append(h)
    return dupes


# -- cheap offline language detection (v0.6) ----------------------------------
_LANG_STOPWORDS = {
    "en": ["the", "and", "of", "to", "is", "in", "for", "that", "with", "this"],
    "es": ["el", "la", "de", "que", "y", "en", "los", "del", "una", "para"],
    "fr": ["les", "des", "une", "est", "pour", "dans", "que", "qui", "sur", "avec"],
    "de": ["und", "der", "die", "das", "ein", "eine", "von", "mit", "für", "ist"],
    "ru": ["и", "в", "не", "что", "он", "на", "я", "бы", "с", "по"],
}


def detect_language(text):
    """Guess language from stopword frequencies.

    Returns ``{"lang": code, "confidence": 0.0..1.0, "scores": {...}}``.
    Never raises; empty text yields ``{"lang": "unknown", "confidence": 0.0}``.
    """
    if not text or not str(text).strip():
        return {"lang": "unknown", "confidence": 0.0, "scores": {}}
    tokens = re.findall(r"[^\s]+", text.lower())
    tokens = [t.strip(".,;:!?()[]{}<>\"'") for t in tokens]
    scores = {}
    for lang, stopwords in _LANG_STOPWORDS.items():
        scores[lang] = sum(1 for t in tokens if t in stopwords)
    best_lang = max(scores, key=scores.get) if scores else "unknown"
    best = scores.get(best_lang, 0)
    second = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0
    if best == 0:
        return {"lang": "unknown", "confidence": 0.0, "scores": scores}
    confidence = best / (best + second) if (best + second) else 0.0
    return {"lang": best_lang, "confidence": round(confidence, 3), "scores": scores}


def estimate_total_tokens(texts):
    """Sum of :func:`estimate_tokens` over an iterable of texts."""
    return sum(estimate_tokens(t) for t in (texts or []))
