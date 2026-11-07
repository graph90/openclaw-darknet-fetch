"""Continuous watch: poll a URL and report changes (ETag/hash-based).

``watch()`` reuses the conditional (ETag) machinery from ``Fetcher(watch=True)``
and compares ``content_sha256`` between polls, so unchanged onion/eepsite
pages cost a header exchange instead of a full download when the server
supports it.
"""

import time


def watch(url, *, interval=5, max_polls=3, until_change=False, network="normal",
          fetcher=None, **fetcher_kwargs):
    """Poll ``url`` up to ``max_polls`` times, sleeping ``interval`` seconds.

    Returns ``{ok, url, events:[{poll,ts,status,not_modified,content_sha256,
    changed}], changed}``. Never raises for runtime failures.
    """
    from .fetcher import Fetcher, _now_iso

    if fetcher is None:
        fetcher_kwargs.setdefault("watch", True)
        fetcher_kwargs.setdefault("use_cache", False)
        fetcher = Fetcher(network=network, **fetcher_kwargs)
    events = []
    previous_hash = None
    changed = False
    for poll in range(max(1, int(max_polls))):
        result = fetcher.fetch(url)
        digest = result.get("content_sha256") or ""
        is_change = bool(previous_hash is not None and digest and digest != previous_hash)
        events.append({
            "poll": poll + 1,
            "ts": _now_iso(),
            "status": result.get("status"),
            "ok": bool(result.get("ok")),
            "not_modified": bool(result.get("not_modified")),
            "content_sha256": digest,
            "changed": is_change,
        })
        if previous_hash is None:
            previous_hash = digest
        elif is_change:
            changed = True
            if until_change:
                break
        if poll < max(1, int(max_polls)) - 1 and interval:
            time.sleep(float(interval))
    return {
        "ok": True,
        "url": url,
        "network": (network or "normal").upper(),
        "events": events,
        "changed": changed,
        "polls": len(events),
    }
