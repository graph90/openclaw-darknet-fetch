"""Document collection: bucket a page's links without downloading them.

``collect_documents()`` fetches the seed once and groups its classified links
(see :func:`openclaw_fetch.parse.classify_link`) into ``document`` / ``archive``
/ ``download`` buckets — e.g. to list PDFs on an onion front-end before
spending the byte budget on downloads.
"""

from .result import Result


def collect_documents(seed_url, *, network="normal", fetcher=None, **fetcher_kwargs):
    """Fetch ``seed_url`` once and return its downloadable links bucketed.

    Returns ``{ok, seed_url, documents[], archive[], download[], total}``.
    Never raises for runtime failures.
    """
    from .fetcher import Fetcher, _now_iso

    started = _now_iso()
    if fetcher is None:
        fetcher = Fetcher(network=network, **fetcher_kwargs)
    page = fetcher.fetch(seed_url)
    documents, archive, download = [], [], []
    if page.get("ok") or page.get("links"):
        for link in page.get("links") or []:
            kind = link.get("kind")
            url = link.get("url")
            if not url:
                continue
            entry = {
                "url": url, "text": link.get("text") or "",
                "kind": kind, "target_tld": link.get("target_tld"),
            }
            if kind == "document":
                documents.append(entry)
            elif kind == "archive":
                archive.append(entry)
            elif kind == "download":
                download.append(entry)
    ok = bool(page.get("ok"))
    return Result(
        ok=ok,
        error=page.get("error") or "",
        error_code=page.get("error_code"),
        seed_url=seed_url,
        network=page.get("network"),
        documents=documents,
        archive=archive,
        download=download,
        total=len(documents) + len(archive) + len(download),
        fetched_at=started,
    )
