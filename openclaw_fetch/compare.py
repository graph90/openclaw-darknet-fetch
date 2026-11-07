"""Corroboration: fetch several mirrors of the same content in parallel.

``compare()`` answers the darknet agent question "the clearnet copy is stale /
blocked — is the onion copy fresh and do they agree?" in one call: it fetches
each URL through its routed network, reports a per-URL availability matrix with
provenance, and flags whether all reachable copies are byte-identical.
"""

from .result import Result


def compare(urls, *, network="normal", fetcher=None, concurrency=4, dry_run=False, **fetcher_kwargs):
    """Fetch ``urls`` in parallel and compare availability + content identity.

    Returns ``{ok, entries, all_reachable, all_identical_content, ...}`` where
    each entry carries ``url, network, target_tld, ok, status, title,
    content_sha256, estimated_tokens, text_length, fetched_at``. Never raises
    for runtime failures; ``ok`` is true when at least one URL is reachable.
    """
    from .fetcher import Fetcher, _now_iso
    from .util import estimate_tokens

    started = _now_iso()
    url_list = list(urls or [])
    if dry_run:
        projected = len(url_list) * estimate_tokens("x" * 2000)
        return Result(
            ok=True, error="", error_code=None, network=(network or "normal").upper(),
            entries=[{"url": u, "network": (network or "normal").upper(), "target_tld": "unknown",
                      "ok": True, "status": None, "title": "", "content_sha256": "",
                      "estimated_tokens": estimate_tokens("x" * 2000), "text_length": 2000,
                      "fetched_at": started, "freshness": {"last_modified": None, "date": None}}
                     for u in url_list],
            all_reachable=True, all_identical_content=False,
            succeeded_count=len(url_list), failed_count=0, total_compared=len(url_list),
            dry_run=True, projected_tokens_total=projected,
            pairwise_diffs=[], fetched_at=started,
        )
    if fetcher is None:
        fetcher = Fetcher(network=network, **fetcher_kwargs)
    if url_list:
        results = fetcher.fetch_many(url_list, concurrency=concurrency)
    else:
        results = []
    entries = []
    for result in results:
        headers = result.get("response_headers") or {}
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
            "freshness": {
                "last_modified": headers.get("last-modified") or headers.get("Last-Modified"),
                "date": headers.get("date") or headers.get("Date"),
            },
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
    # Pairwise markdown diffs (token-budgeted preview).
    import difflib

    pair_diffs = []
    ok_pairs = [(e, r) for e, r in zip(entries, results) if e.get("ok")]
    for i in range(len(ok_pairs)):
        for j in range(i + 1, len(ok_pairs)):
            e1, r1 = ok_pairs[i]
            e2, r2 = ok_pairs[j]
            md1 = (r1.get("markdown") or r1.get("text") or "")[:8000].splitlines()
            md2 = (r2.get("markdown") or r2.get("text") or "")[:8000].splitlines()
            diff = list(difflib.unified_diff(md1, md2, fromfile="a", tofile="b", lineterm=""))
            pair_diffs.append({
                "a_url": e1.get("url"),
                "b_url": e2.get("url"),
                "same_content": e1.get("content_sha256") == e2.get("content_sha256"),
                "diff_lines": len(diff),
                "diff_preview": "\n".join(diff[:40])[:2000],
            })
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
        pairwise_diffs=pair_diffs,
        fetched_at=started,
    )


# Alias matching the plan's mirror vocabulary.
compare_mirrors = compare
