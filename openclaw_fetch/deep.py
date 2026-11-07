"""Flagship composite: topic -> sourced markdown brief in one call.

``deep_fetch()`` is the agent's one-call discovery→read loop: search across
networks, dedupe by URL, fetch the top N in parallel (isolated circuits),
dedupe by ``content_sha256``, then emit a markdown brief with per-source
provenance and token accounting.
"""

from .result import Result


def deep_fetch(query, *, top_n=3, network="auto", backend="auto", search_url=None,
               fetcher=None, concurrency=3, time_range="", include_sponsored=False,
               dry_run=False, **fetcher_kwargs):
    """Search ``query``, fetch the top ``top_n`` hits, and return a sourced brief.

    Returns ``{ok, query, brief, sources[], token_accounting, search, pages}``.
    ``sources[]`` entries carry provenance (url/network/title/sha/fetched_at/tokens).
    Never raises for runtime failures.
    """
    from .fetcher import Fetcher, _now_iso
    from .search import search as run_search
    from .util import estimate_tokens

    started = _now_iso()
    if dry_run:
        n = max(0, int(top_n))
        return Result(
            ok=True, error="", error_code=None, query=query, brief="",
            sources=[], returned_sources=0, fetched_pages=0, dry_run=True,
            projected_pages=n,
            projected_tokens_total=n * estimate_tokens("x" * 2000),
            projected_bytes_total=n * 2000,
            token_accounting={"total_tokens": n * estimate_tokens("x" * 2000), "total_bytes": n * 2000, "pages": n, "dry_run": True},
            search=None, pages=[], fetched_at=started,
        )
    if fetcher is None:
        fetcher = Fetcher(network=network, **fetcher_kwargs)
    search_fetcher = fetcher.clone(max_chars=0) if hasattr(fetcher, "clone") else fetcher
    sres = run_search(
        query, network=network, backend=backend, max_results=max(top_n, 1),
        search_url=search_url, fetcher=search_fetcher,
        time_range=time_range or "", include_sponsored=include_sponsored,
    )
    pages = []
    if getattr(sres, "ok", False):
        urls = []
        seen = set()
        for item in (sres.get("results") or [])[:top_n]:
            url = item.get("url")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
        if urls:
            pages = fetcher.fetch_many(urls, concurrency=concurrency)
    # Near-dedupe: exact hash first, then SimHash near-duplicates.
    from .util import near_duplicates

    by_hash = {}
    deduped = []
    texts = []
    for page in pages:
        digest = (page.get("content_sha256") or "") if page.get("ok") else ""
        if digest and digest in by_hash:
            continue
        if digest:
            by_hash[digest] = page
        deduped.append(page)
        texts.append((page.get("markdown") or page.get("text") or "") if page.get("ok") else "")
    if len(deduped) > 1:
        dupes = set(near_duplicates(texts, threshold=3))
        deduped = [p for i, p in enumerate(deduped) if i not in dupes]
    sources = []
    for page in deduped:
        sources.append({
            "url": page.get("requested_url"),
            "network": page.get("network"),
            "target_tld": page.get("target_tld"),
            "title": page.get("title") or "",
            "status": page.get("status"),
            "ok": bool(page.get("ok")),
            "content_sha256": page.get("content_sha256") or "",
            "estimated_tokens": page.get("estimated_tokens") or 0,
            "fetched_at": page.get("fetched_at") or "",
            "exit_ip": page.get("exit_ip"),
            "is_tor": page.get("is_tor"),
        })
    lines = ["# Brief: %s" % query, ""]
    ok_pages = [p for p in deduped if p.get("ok")]
    if not ok_pages and not (getattr(sres, "ok", False) and sres.get("results")):
        lines.append("_No readable sources were found._")
    for source, page in zip(sources, deduped):
        if not page.get("ok"):
            continue
        lines.append("## %s" % (page.get("title") or source["url"]))
        lines.append("")
        lines.append("- Source: %s (%s, %s)" % (
            source["url"], source["network"], source["target_tld"]))
        lines.append("- SHA256: %s" % (source["content_sha256"] or "n/a"))
        lines.append("")
        body = (page.get("markdown") or page.get("text") or "").strip()
        if body:
            lines.append(body[:4000])
            lines.append("")
    brief = "\n".join(lines).strip() + "\n"
    total_tokens = sum(s["estimated_tokens"] for s in sources)
    total_bytes = sum((p.get("content_length") or 0) for p in deduped)
    ok = bool(ok_pages) or bool(getattr(sres, "ok", False) and (sres.get("results") or []))
    if not (sres.get("results") if hasattr(sres, "get") else []):
        # Search itself failed or found nothing useful.
        ok = bool(ok_pages)
    error = "" if ok else (getattr(sres, "error", "") or "no readable sources")
    error_code = None if ok else (sres.get("error_code") or "REQUEST")
    if ok:
        error, error_code = "", None
    return Result(
        ok=ok,
        error=error,
        error_code=error_code,
        query=query,
        brief=brief,
        sources=sources,
        returned_sources=len(sources),
        fetched_pages=len(deduped),
        token_accounting={
            "total_tokens": total_tokens,
            "total_bytes": total_bytes,
            "pages": len(deduped),
        },
        search=sres,
        pages=deduped,
        fetched_at=started,
    )


deep = deep_fetch
