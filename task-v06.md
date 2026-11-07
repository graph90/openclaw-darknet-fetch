# task-v06.md — v0.6 sprint: 10 additions (builds on v0.5.0)

Baseline: **228 passed, 2 deselected**, live Tor + I2P verified (`dfb158a` pushed).
All tasks additive; offline tests vs local HTTP fixture + fake SOCKS5. Live checks
optional (Tor/I2P up on the dev box).

## Task 1 — Embedded-payload walking (§22.1)
- Walk `__NEXT_DATA__` / `__INITIAL_STATE__` / `window.store` JSON server-side and
  flatten to article text (content keys first), instead of returning raw JSON as the
  body. Keep `extract_embedded_json()` returning pretty JSON (parsed form), new
  `extract_flattened_payload(html)` for the flattened text. `parse_document` uses the
  flattened text when `needs_renderer`, sets `metadata["payload_format"]="flattened"`.
- Acceptance: a nested payload with `title/body` fields yields those values in
  `text`; `payload_extracted` + `needs_renderer` still true; raw JSON is not the body.

## Task 2 — ControlPort client (`[tor]` extra)
- New `openclaw_fetch/tor_control.py`: `TorControl(host, port, cookie_path)` with
  `authenticate()`, `getinfo(key)`, `bootstrap_phase()`, `newnym()` over raw sockets
  (stem optional/lazy). Unavailable ports raise `ControlUnavailable` (never crashes
  doctor/fetch paths). `doctor()` uses it for bootstrap when reachable.
- Add `[tor]` extra (`stem`) to `pyproject`; `openclaw-fetch[tor]` documented.
- Acceptance: `TorControl` against a fake control server returns `bootstrap_phase`
  and `newnym()` sends `SIGNAL NEWNYM`; without a server it raises unavailable.

## Task 3 — Circuit provenance + NEWNYM-on-fail (M3)
- Result fields `circuit_id`, `guard`, `exit_node`, `exit_country` (None when control
  unavailable — never faked). `Fetcher(newnym_on_fail=0)` sends `SIGNAL NEWNYM` after
  N consecutive Tor failures and stamps `metadata["newnym_sent"]=True`.
- Acceptance: with a fake control server, N failures trigger `newnym` exactly once
  per threshold; without control, results still carry the fields as `None`.

## Task 4 — I2P jump + outproxy honesty (M4)
- `.b32.i2p` hosts flagged (`metadata.b32_host`); `Fetcher(i2p_jump="host,…cards")` +
  `--i2p-jump HOST` tries a jump-service fetch on `.i2p` failure and stamps
  `metadata.jump_via`. Clearnet via I2P sets `metadata.i2p_outproxy_used=True`
  (outproxies are not anonymous); `-i` + `.onion` still fails `NETWORK_MISMATCH`.
- Acceptance: clearnet fetch via `i2p` carries the outproxy flag; jump fallback
  resolves a host via the jump URL stub.

## Task 5 — Continuous `watch()` + MCP `watch` (M9)
- New `openclaw_fetch/watch.py`: `watch(url, interval=5, max_polls=3, until_change=False)`
  polls with ETag/hash, returns `{events:[{ts,status,not_modified,content_sha256}], changed}`.
  MCP tool `watch`.
- Acceptance: watching an `/etag` URL yields a `304 not_modified` event; `until_change`
  stops on first change.

## Task 6 — Resumable crawls `--checkpoint` (M9)
- `crawl(..., checkpoint="/path.json")` + CLI `--checkpoint FILE`: persists
  `{seed_url, frontier, visited}` after each level; a later run resumes from it.
- Acceptance: a crawl interrupted mid-frontier resumes without re-fetching visited pages.

## Task 7 — `dry_run` / `estimate` for composites (M6)
- `deep_fetch()`, `compare()`, `find_mirrors()` (+ `collect_documents`) accept
  `dry_run=True`: return projections (`{projected_pages, estimated_tokens_total, ...}`)
  with **zero network calls**. MCP `estimate` tool; `estimate_total_tokens()` helper.
- Acceptance: `dry_run` succeeds even with a dead proxy + returns token estimates.

## Task 8 — `compare` diff (complete M5)
- Per-entry `freshness` (`last_modified`/`date` from headers) and pairwise
  `difflib` markdown diffs (token-budgeted preview) + `same_content` flags.
- Acceptance: identical pages diff empty; differing pages list changed lines.

## Task 9 — SimHash dedupe + language detection (§22.6–7)
- `simhash(text)`, `hamming(a,b)`, `near_duplicates(texts, threshold)` in `util.py`;
  `deep_fetch` drops near-duplicates, not just exact hashes. `detect_language(text)`
  (EN/ES/FR/DE/RU stopwords) → `metadata.language` (+ `language_confidence`).
- Acceptance: near-identical texts cluster; EN/ES samples detect correctly.

## Task 10 — Ops, distribution & `--via-archive`
- `Dockerfile` (tor + package, `openclaw-fetch mcp` entrypoint), GitHub CI
  (offline pytest + ruff + mypy), `config.example.toml`, `docs/EXAMPLES.md`,
  `SECURITY.md` (no-JS contract + anonymity model). `--via-archive`: on 404/410/
  connection failure, try the Wayback Machine and stamp `metadata.via_archive`.
- Acceptance: `Dockerfile`, workflow, docs exist; `--via-archive` recovers a local
  404 via the archive stub and stamps the flag.

## Out of scope (still open after v0.6)
- `--tor-exit-avoid/--tor-exit-require` (GeoIP policy retry loop), eepsite index +
  `postman` search backend, full `IsolateSOCKSAuth` verification via stem readback,
  headless `--render` (demoted, clearnet-only), PyPI/GHCR release automation.
