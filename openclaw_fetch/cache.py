"""TTL disk cache with atomic writes.

Key = ``sha256("network|url|context")``. Two files per entry:
``<key>.json`` (meta) and ``<key>.body`` (raw response bytes).
Writes are atomic via ``os.replace`` so concurrent batch fetches never
observe a torn entry.
"""

import hashlib
import json
import os
import tempfile
import time


class TtlCache:
    def __init__(self, cache_dir=None, ttl=3600, enabled=True):
        self.cache_dir = cache_dir
        self.ttl = ttl
        self.enabled = enabled

    @staticmethod
    def key_for(network, url, context=None):
        """Derive the opaque cache key for a request and its context."""
        digest = hashlib.sha256()
        digest.update(str(network).encode("utf-8"))
        digest.update(b"|")
        digest.update(str(url).encode("utf-8"))
        if context is not None:
            digest.update(b"|context|")
            digest.update(str(context).encode("utf-8"))
        return digest.hexdigest()

    def _paths(self, key):
        if self.cache_dir is None:
            import openclaw_fetch.config as config

            base = config.default_cache_dir()
        else:
            base = self.cache_dir
        return os.path.join(base, key + ".json"), os.path.join(base, key + ".body")

    def get(self, network, url, context=None, max_bytes=None):
        """Return cached meta+body or ``None``. Expired entries are purged."""
        if not self.enabled:
            return None
        key = self.key_for(network, url, context=context)
        meta_path, body_path = self._paths(key)
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
            fetched = meta.get("fetch_time", 0)
            if self.ttl is not None and self.ttl >= 0 and time.time() - fetched > self.ttl:
                self._remove(key)
                return None
            if meta.get("url") != url or meta.get("network") != network:
                self._remove(key)
                return None
            if context is not None and meta.get("context") != context:
                self._remove(key)
                return None
            if max_bytes is not None and os.path.getsize(body_path) > max_bytes:
                self._remove(key)
                return None
            with open(body_path, "rb") as fh:
                body = fh.read()
        except (OSError, ValueError, KeyError):
            return None
        body_digest = meta.get("body_sha256")
        if body_digest and body_digest != hashlib.sha256(body).hexdigest():
            self._remove(key)
            return None
        return {"meta": meta, "body": body}

    def put(self, network, url, status, final_url, content_type, content, headers=None,
            context=None):
        """Store an entry. ``status``/``final_url`` may be ``None`` for errors."""
        if not self.enabled:
            return None
        key = self.key_for(network, url, context=context)
        meta_path, body_path = self._paths(key)
        body = b"" if content is None else content
        meta = {
            "key": key,
            "network": network,
            "url": url,
            "context": context,
            "fetch_time": time.time(),
            "status": status,
            "final_url": final_url,
            "content_type": content_type,
            "content_length": len(body),
            "headers": headers,
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        os.makedirs(self.cache_dir or meta_path.rsplit(os.sep, 1)[0], exist_ok=True)
        self._atomic_write_bytes(body_path, body)
        self._atomic_write_json(meta_path, meta)
        return key

    def flush(self):
        """Delete every entry in the cache directory."""
        base = self.cache_dir
        if base is None:
            import openclaw_fetch.config as config

            base = config.default_cache_dir()
        if not os.path.isdir(base):
            return
        for name in os.listdir(base):
            if name.endswith((".json", ".body")):
                try:
                    os.remove(os.path.join(base, name))
                except OSError:
                    pass

    def _remove(self, key):
        for path in self._paths(key):
            try:
                os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _atomic_write_bytes(path, data, mode=0o600):
        base = os.path.dirname(path)
        os.makedirs(base, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=base, prefix=".ocf-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def _atomic_write_json(cls, path, data, mode=0o600):
        cls._atomic_write_bytes(path, json.dumps(data).encode("utf-8"), mode)


def has_cookies(cookie_jar_path):
    """Whether a cookie jar file exists with at least one stored cookie."""
    if not cookie_jar_path:
        return False
    try:
        with open(cookie_jar_path, "r", encoding="utf-8") as fh:
            return bool(json.load(fh).get("cookies"))
    except (OSError, ValueError):
        return False