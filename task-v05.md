# task-v05.md — v0.5 sprint: 10 additions (builds on v0.4.0)

Baseline: **206 passed, 2 deselected**, live Tor + I2P verified (`exit_ip 185.100.85.25 is_tor:true`, `i2p-projekt.i2p` fetch ok).
All tasks are additive and backward-compatible. Offline tests run against the local
HTTP fixture + fake SOCKS5 proxy; live verification is CLI/library checks over real
Tor/I2P (no live test in the offline suite).

## Task 1 — `doctor()` + `--tor-doctor`
- New `openclaw_fetch/doctor.py`: `doctor(network="auto", tor_proxy=None, i2p_proxy=None, ...)`
  socket-probes the configured Tor/I2P proxies + ControlPort (`9051`), reports
  `isolate` setting, and emits exact `torrc` hints. Without a ControlPort it reports
  `isolation: "unverified (requires ControlPort 9051)"` — never claims a guarantee it
  cannot check.
- CLI `--tor-doctor` (no URL required, like `--check-proxy`); JSON with `--json`, human otherwise.
- Acceptance: doctor returns `{ok, checks:[{name,ok,detail}], isolation, torrc_hints}`; dead
  proxies yield `ok:false` without raising.

## Task 2 — `--tor-verify` (per-request exit verification)
- `Fetcher(tor_verify=False)` + CLI `--tor-verify`. When on and the resolved network is
  Tor, the first fetch triggers one `check.torproject.org/api/ip` probe (cached per
  Fetcher) and stamps `exit_ip`, `is_tor`, `exit_verified` on the result.
- Fail-closed codes: add `ANONYMITY_UNVERIFIED` (exit 6) for a failed verification.
  New shape keys default to `exit_ip:null, is_tor:null, exit_verified:false`.
- Acceptance: with verification stubbed `is_tor:true`, a Tor result carries `exit_ip`;
  with it stubbed `false`, the result is `ok:false ANONYMITY_UNVERIFIED`. Live `--tor-verify`
  over real Tor succeeds.

## Task 3 — `config.toml` + `--config`
- Minimal TOML loader (stdlib `tomllib` when available, else a small fallback parser)
  for the flat keys already in `config.py` plus `[tor]/[i2p]/[cache]` tables.
  Precedence: built-in default < `config.toml` < `OPENCLAW_*` env < explicit arg.
- `Fetcher(config_path=None)` + CLI `--config PATH`. `--env-report` gains `config_source`.
- Acceptance: a temp TOML changing `tor_proxy`/`max_chars` is picked up by `Fetcher`
  and by the CLI; env vars override the file.

## Task 4 — `deep_fetch()` + MCP `deep_fetch`
- New `openclaw_fetch/deep.py`: `deep_fetch(query, top_n=3, network="auto", ...)` =
  search → dedupe URLs → parallel isolated fetch → `content_sha256` dedupe → markdown
  brief with per-source provenance + token accounting.
- Returns `{ok, query, brief, sources[], search, pages[], token_accounting}`; exported
  from `openclaw_fetch`; MCP tool `deep_fetch`.
- Acceptance (offline, stubbed search): brief cites sources, duplicates by hash are
  dropped; live over Tor it returns a non-empty brief.

## Task 5 — `find_mirrors()`
- `find_mirrors(site_or_url, network="auto", ...)` → `{ok, clearnet, onions[], i2ps[], sources[]}`.
  Sources: `Onion-Location` headers (authoritative) + search indexes (tor66/marginalia).
- Early returns a structure even when no mirrors exist; never raises for runtime failures.
- Acceptance: stubbed search with `onion`/`i2p` hits yields those URLs; header-hinted
  mirrors are tagged `onion-location`.

## Task 6 — `--collect-documents`
- Library `collect_documents(seed_url, ...)` + CLI `--collect-documents`: fetches the
  seed once, buckets its classified links into `document/archive/download` without
  downloading them. Result: `{ok, seed_url, documents[], archive[], download[]}`.
- Acceptance: `/links` fixture buckets `paper.pdf→document`, `bundle.zip→archive`, `img.png→download`.

## Task 7 — Interstitial detection + skip labels
- `metadata.interstitial` + `skip_reason` (`interstitial|gated|challenge|empty_index`)?
  Detection: thin visible text (<200 chars) + language-rotating "please wait" markers.
- One automatic refetch after a short wait when `interstitial_retry=True` (default).
- Acceptance: a please-wait shell is labelled `interstitial:true` and the retry returns
  the real page; gated/challenge markers set `skip_reason` without an error.

## Task 8 — Directory/index extraction
- Heuristic: many links + little prose → `metadata.is_index:true`, `metadata.index_rows[]`
  (`{title,url,kind}`), `metadata.index_entry_count`. `content_kind` stays `html` so
  `--expect` does not break.
- Acceptance: a link-farm page is flagged with rows; a normal article is not.

## Task 9 — `suspected_dead` ledger
- `Fetcher(dead_ledger="/path/dead.json", dead_threshold=3)`: on-disk JSON ledger of
  `.onion` consecutive failures; after threshold, later fetches return fast with
  `suspected_dead:true`, `error_code:SUSPECTED_DEAD` (exit 4). Reset on success.
  Off by default; CLI `--dead-ledger PATH` enables it.
- Acceptance: 3 failing onion fetches trip the ledger; the 4th does not hit the network.

## Task 10 — `--selector CSS` extraction
- Minimal stdlib CSS subset: `tag`, `.class`, `#id`, `[attr]`, `[attr=value]`,
  `[attr^=/ *=/ ~=]`, descendant. `Fetcher(selector=None)` + CLI `--selector CSS`
  fills `result["extracted"] = [{tag, text, attrs}]` from the raw HTML.
  Bad selectors return `USAGE` (exit 1), never a crash.
- Acceptance: `h1`, `a[href^="/"]`, `.ext` selectors extract correctly from fixtures.

## Out of scope (still open after v0.5)
- Tor ControlPort/`stem` circuits + NEWNYM, `--tor-exit-avoid`, continuous `watch(interval)`,
  resumable crawls, `dry_run`, `.b32.i2p`/jump service, outproxy honesty, SimHash dedupe,
  language detection, headless `--render`, Docker, `docs/EXAMPLES.md`.
