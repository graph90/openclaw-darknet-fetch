"""Corroboration: fetch several mirrors of the same content in parallel.

``compare()`` answers the darknet agent question "the clearnet copy is stale /
blocked — is the onion copy fresh and do they agree?" in one call: it fetches
each URL through its routed network, reports a per-URL availability matrix with
provenance, and flags whether all reachable copies are byte-identical.
"""

from .result import Result


def compare(urls, *, network="normal", fetcher=None, concurrency=4, **fetcher_kwargs):
    """Fetch ``urls`` in parallel and compare availability + content identity.

    Returns ``{ok, entries, all_reachable, all_identical_content, ...}`` where
    each entry carries ``url, network, target_tld, ok, status, title,
    content_sha256, estimated_tokens, text_length, fetched_at``. Never raises
    for runtime failures; ``ok`` is true when at least one URL is reachable.
    """
    from .fetcher import Fetcher, _now_iso

    started = _now_iso()
    url_list = list(urls or [])
    if fetcher is None:
        fetcher = Fetcher(network=network, **fetcher_kwargs)
    if url_list:
        results = fetcher.fetch_many(url_list, concurrency=concurrency)
    else:
        results = []
    entries = []
    for result in results:
        entries.append({
            "url": result.get("requested_url"),
            "network": result.get("network"),
            "target_tld": result.get("target_tld"),
            "ok": bool(result.get("ok")),
            "status": result.get("status"),
            "title": result.get("title") or "",
            "content_sha256": result.get("content_sha256") or "",
            "estimated_tokens": result.get("estimated_tokens") or 0,
            "text_length": result.get("text_length") or 0,
            "fetched_at": result.get("fetched_at") or "",
        })
    succeeded = [e for e in entries if e["ok"]]
    ok = bool(succeeded)
    all_reachable = bool(entries) and len(succeeded) == len(entries)
    hashes = {e["content_sha256"] for e in succeeded if e["content_sha256"]}
    all_identical = bool(succeeded) and len(hashes) == 1 and all(
        e["content_sha256"] for e in succeeded
    )
    if not entries:
        error = "no URLs to compare"
        error_code = "USAGE"
    elif not succeeded:
        error = "no URL could be fetched (%d attempted, 0 succeeded)" % len(entries)
        error_code = "REQUEST"
    else:
        error = ""
        error_code = None
    return Result(
        ok=ok,
        error=error,
        error_code=error_code,
        network=(network or "normal").upper(),
        entries=entries,
        all_reachable=all_reachable,
        all_identical_content=all_identical,
        succeeded_count=len(succeeded),
        failed_count=len(entries) - len(succeeded),
        total_compared=len(entries),
        fetched_at=started,
    )


# Alias matching the plan's mirror vocabulary.
compare_mirrors = compare
