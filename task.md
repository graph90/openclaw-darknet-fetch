# task.md — 10 suggested additions (v0.4 sprint)

Baseline (verified before coding): `171 passed, 2 deselected` (`pytest -m "not live"`),
no live network used. All tasks below are additive and backward-compatible:
existing flags, `error_code` strings, and JSON shape are preserved; new fields are
added, never renamed. Every task ships with an offline test against the local
HTTP fixture + fake SOCKS5 proxy in `tests/conftest.py`.

## Task 1 — Provenance fields: `fetched_at` + `content_sha256`
- Add `fetched_at` (ISO-8601 UTC, e.g. `2026-11-07T08:00:00Z`) and `content_sha256`
  (hex digest of the raw body bytes; `""` for failures/empty bodies) to every
  successful, cached, and error result.
- Also add both to the canonical read shape in `errors._READ_SHAPE` so error
  results carry them.
- Acceptance: `fetch()` and cached second fetch both expose the fields; two
  identical bodies share the same `content_sha256`; `fetched_at` parses as
  ISO-8601.

## Task 2 — Onion v3 address validation + `strict_onion` mode
- Implement `is_valid_onion(host)` (v3 checksum + base32 + version) and expose
  `onion_valid` / `onion_version` on results for `.onion` targets.
- Add `Fetcher(strict_onion=False)` (default off so legacy short/fake names still
  work) — when on, an invalid v3 onion returns `error_code="ONION_INVALID"`
  (exit 1) instead of a timeout. Also add `--strict-onion` CLI flag.
- Acceptance: real checksum failure is caught only in strict mode; non-strict
  mode is unchanged; `ONION_INVALID` appears in `--env-report`/exit-code table
  when needed.

## Task 3 — Response headers + `Onion-Location` harvesting
- Expose `response_headers` (lower-cased dict) on every result (success, cached,
  and error results carry `{}`).
- Harvest `Onion-Location` / `X-Onion-Location` into `metadata.onion_location`
  and `metadata.mirrors` (list with `{"url": ..., "source": "onion-location"}`).
  Also harvest `X-I2P-*` location-ish headers into `metadata.i2p_mirrors` when
  present.
- Acceptance: a local-server endpoint returning `Onion-Location: http://….onion/`
  yields the parsed mirror; `response_headers["content-type"]` is present on 200s.

## Task 4 — Link classification + `--metadata-only` mode
- Enrich every link dict with `target_tld` (`onion`/`i2p`/`clearnet`) and `kind`:
  `onion`, `i2p`, `mail`, `magnet`, `document` (`.pdf/.doc/.docx/.xls/.xlsx/.epub/.txt`),
  `archive` (`.zip/.tar.gz/.7z/.rar`), `download` (other file extensions),
  `page` (default). Keep existing `url/text/title` keys unchanged.
- Add `Fetcher(metadata_only=False)` + CLI `--metadata-only`: still fetches,
  parses links/headings/title/metadata, but returns empty `text`/`markdown`
  (`estimated_tokens` computed on the empty body). Cache still works and is
  metadata-aware (a metadata-only fetch must not poison a full-body cache entry).
- Acceptance: fixture page links get the right `kind`; metadata-only result has
  title + links but empty body; a following full fetch is not served a truncated
  cache entry.

## Task 5 — CLI HTTP methods: `--method`, `--data`, `--form`, `--json-body`, `--header`
- The library already supports POST; the CLI only does GET. Add:
  - `--method GET|HEAD|POST|PUT|PATCH|DELETE` (default GET)
  - `--data 'raw string'` (sent as-is)
  - `--form 'k=v&k2=v2'` (form-encoded, `Content-Type: application/x-www-form-urlencoded`)
  - `--json-body '{"k":"v"}'` (JSON body, `Content-Type: application/json`)
  - `--header 'K: V'` (repeatable; invalid entries exit 1 with a usage error)
  `--data`/`--form`/`--json-body` are mutually exclusive. Headers merge with
  `--ua` (explicit `--header` wins). POST via CLI must round-trip through the
  existing `/post` fixture.
- Acceptance: `openclaw-fetch -n <server>/post --method POST --json-body '{"q":"x"}' --json`
  returns `ok:true` with the echoed body; `HEAD` returns headers with empty text;
  conflicting body flags exit 1.

## Task 6 — Execution budgets (`BUDGET`)
- `Fetcher(budget_seconds=None, budget_bytes=None, budget_requests=None, budget_tokens=None)`
  + CLI `--budget-seconds/--budget-bytes/--budget-requests/--budget-tokens`.
  Track per-instance wall-clock, cumulative `content_length`, request count, and
  `estimated_tokens`. Once any budget is exhausted, further `fetch()` calls return
  `error_code="BUDGET"` with `error="budget exhausted (...)"` and
  `budget_exhausted: true` — never a crash. `fetch_many` applies the same
  accounting and marks late items `BUDGET`.
- Acceptance: `budget_requests=1` lets the first fetch succeed and the second
  return `BUDGET`; `budget_bytes` below the body size trips on the next fetch;
  `BUDGET` maps to exit 6 (existing table entry).

## Task 7 — Structured JSONL event log (`--log-jsonl`)
- `Fetcher(log_jsonl=None)` + CLI `--log-jsonl FILE`: append one JSON line per
  fetch outcome with `ts` (ISO-8601 UTC), `network`, `url` (redacted),
  `status`, `took_ms`, `cached`, `ok`, `error_code`, `event="fetch"`. Writes are
  thread-safe and never raise into the fetch path (a bad path logs to stderr only).
- Acceptance: after a two-URL run the file contains two parseable JSON lines with
  the expected keys; a malformed path does not fail the fetch.

## Task 8 — Conditional / watch fetch (ETag + Last-Modified + 304)
- `Fetcher(watch=False)`: when on, remembers `ETag`/`Last-Modified` per URL and
  sends `If-None-Match`/`If-Modified-Since` on the next fetch of that URL. A `304`
  returns `ok:true`, `status:304`, `not_modified:true`, empty text, and the
  previously-known validators in `metadata`.
- Also expose `res["validators"] = {"etag": ..., "last_modified": ...}` on 200s.
- Acceptance: with `watch=True` a second fetch of an unchanged resource against
  the local server returns `not_modified:true`; with `watch=False` it returns the
  full body again.

## Task 9 — Cache stats + size eviction (`--cache-stats`, `--cache-max-mb`)
- `TtlCache.stats()` → `{"entries": N, "bytes": M, "dir": path}` and
  `TtlCache.evict_until_under(max_mb)` (oldest-first by file mtime).
  `Fetcher(cache_max_mb=None)` + CLI `--cache-max-mb` evicts after each `put`.
  CLI `--cache-stats` prints stats as JSON and exits 0 (like `--env-report`).
- Acceptance: stats reflect entries/bytes after a fetch; setting a tiny max-MB
  evicts the oldest entry; default behaviour (no cap) is unchanged.

## Task 10 — `compare()` corroboration (availability matrix + hash identity)
- New public `compare(urls, network="normal", ...)` (and `deep` alias
  `compare_mirrors`) returning:
  `{ok, entries: [{url, network, target_tld, ok, status, title, content_sha256,
  estimated_tokens, text_length, fetched_at}], all_reachable, all_identical_content}`.
  Fetches in parallel via `fetch_many`, never raises for runtime failures, exposes
  the same provenance fields as `fetch`. Exported from `openclaw_fetch` and
  exposed as an MCP `compare` tool.
- Acceptance: comparing a URL with itself is `all_reachable:true` and
  `all_identical_content:true`; comparing a good URL with a 404 URL reports the
  per-entry `ok` correctly and `ok:false` overall when nothing is reachable.

## Out of scope (explicitly not done here)
- `config.toml` file format, Tor ControlPort/`stem`, headless renderer,
  `--find-mirrors` index crawling. These are plan.md M1/M4/M11 follow-ups; the
  mirror harvesting in Task 3 is the stepping stone.
