# Plan: OpenClaw Darknet Fetch v2

Multi-network fetcher for AI agents. Current version is a single-file CLI that fetches a URL
over Normal/Tor/I2P and returns text/links as human text or JSON. This plan grows it from a
"fetch one page" tool into a production-grade agent toolkit without breaking existing usage.

**Defining goal:** a universal, production-safe network toolkit for AI agents that gets the
most out of clearnet, **Tor, and I2P** — maximizing the *reachable* information surface —
secure by default, safe under real-world network pathology, and consumable by any agent
runtime (OpenClaw, MCP clients, CLI, library).

---

## 1. What's missing today (gap analysis)

**Correctness / hygiene**
- `ok: true` even for 4xx/5xx responses (a 404 is returned as a success). No `--allow-errors` escape hatch.
- No retries. Tor clicks time out constantly; one failure kills the whole call.
- No caching: agents re-fetch the same URL repeatedly, wasting proxy load and tokens.
- No session/cookie state: agents can't log in or keep state across multiple fetches.
- No body-size guard: a decompression bomb can OOM the agent process.
- No charset fallback: `response.text` trusts the header; many pages have no charset or a wrong one.
- One-shot CLI only: all logic lives in `main()`, so nothing is importable or embeddable by the agent.
- No tests, no packaging, no config file, no stable error-code taxonomy.

**Field-test findings — live Tor + I2P on this machine (2026-09-12)**
- Both networks verified reachable: Tor SOCKS5 `127.0.0.1:9050` (exit confirmed
  via `check.torproject.org/api/ip`), I2P HTTP `127.0.0.1:4444` (i2p-projekt responds).
- **Page inflation is real:** DuckDuckGo onion = 177,105 body bytes, `text_length` 129,
  `link_count` 2. The tool downloads ~177KB to produce ~130 tokens of unusable UI noise
  ("Menu", "0 / 500"). Confirms the need for a pre-fetch `--max-bytes` guard and a
  JS-heavy-page signal (Tier 1/2 additions below).
- **Link extraction loses anchors on JS-heavy pages:** nav/search targets live in JS-rendered
  markup the parser can't see; 2/177KB is not a useful crawl surface.
- **Nav boilerplate pollutes text:** flat per-element lines keep menu scaffolding and
  "aria-hidden" filler; block structure would let agents skip it.
- **I2P classis-HTML pages extract cleanly** (1,878 chars, good title, dense links) — current
  pipeline is adequate there; improvement effort should target real-world JS/big pages.
- **JSON contract gap surfaced:** `link_count` is printed in human mode but absent from JSON —
  fields must be documented + stable.

**Agent capability gaps** (things agents currently cannot do)
- No mode that returns token-efficient content for LLM consumption (text blob only; markdown is ~30-50% cheaper).
- No way to read structured metadata (og tags, meta description, headings) for cheap page triage.
- No JSON/API pass-through (HTML parser runs on `application/json` bodies today).
- No PDF/text-file extraction.
- No POST/form submission -> no login or search flows.
- No way to fetch many URLs found on a page (no batch mode).
- No limited crawl / follow (can't explore a site from one seed URL).
- No selector extraction (CSS/XPATH) -> no table/scrape workflows.
- No search entry point (DDG HTML, Ahmia for .onion) -> agent can't discover URLs, only fetch known ones.
- No Tor identity control (new circuit) or per-network cookie isolation.

---

## 2. Non-negotiable design rules

1. Backward compatible: every current flag keeps working with identical output shape.
2. Token-efficient by default: output limits should be lower than today for LLM modes.
3. Fail safe: never fetch non-http(s) schemes by default, never exceed a hard body cap.
4. Library + CLI split: the core must be importable so an agent can call it in-process.
5. Errors are data: stable `error_code` strings in JSON, exit codes stay in a documented range.
6. Zero-heavy-deps core: `requests[socks]` stays; everything else optional and lazy-imported.

---

## 3. Tiered upgrades (ROI-ranked)

### Tier 1 — biggest wins, lowest effort (build first)

| # | Upgrade | ROI | Effort |
|---|---------|-----|--------|
| 1.1 | **Markdown output mode** (`--format markdown`) | Agents read markdown far cheaper than text blobs; token savings ~30-50% on typical pages | L |
| 1.2 | **Public library API** (`fetch()`, `fetch_many()`, `Fetcher` class) + keep CLI as thin wrapper | Unlocks embedding in agent runtimes (OpenClaw tools, python agents) | L |
| 1.3 | **Batch / multi-URL** (`args.urls` or `--batch file`, concurrency pool) | Agents routinely need the several links found on a page; one round trip instead of N | M |
| 1.4 | **TTL disk cache** (`~/.cache/openclaw-fetch/`, key = `network|url`, `--no-cache`, `--cache-ttl`) | Kills repeated fetches; huge proxy + latency + token savings | M |
| 1.5 | **Retries with jittered backoff** (`--retries N --retry-backoff`) | Tor is flaky; this is the single biggest reliability win | L |
| 1.6 | **Cookie jar + per-network sessions** (`--cookie-jar PATH`, auto-per-network jar) | Enables login flows and stateful browsing; also isolates Tor cookie leakage | M |
| 1.7 | **Status handling** (4xx/5xx -> `ok:false` unless `--allow-errors`) + stable `error_code` taxonomy | Agent can branch on failures instead of trusting bad content | L |
| 1.8 | **`--max-bytes` pre-fetch guard** (skip/abort oversized bodies; short-circuit when `max_chars` is small vs Content-Length) + add `link_count` to JSON | Kills the 177KB→130-char waste found in field test; makes `--max-chars` actually control bandwidth, not just output | L/M |

### Tier 2 — feature unlocks for complex behavior

| # | Upgrade | ROI | Effort |
|---|---------|-----|--------|
| 2.1 | **POST/form support** (`--method POST --data 'a=b' --form`) | Login, search submission, API calls — required for almost any interactive agent flow | L |
| 2.2 | **Content-aware extraction**: JSON pass-through, PDF->text via `pypdf`, plain text, image stubs (size/dims, optional `--download`) | Agents can read the actual web, not just HTML | M |
| 2.3 | **Rich metadata**: og:title/og:description/og:image, meta description, canonical, ld+json, heading outline (h1-h3) | Cheap page triage before deep-read; feeds chain-of-thought ranking of links | M |
| 2.4 | **Hard caps**: body cap (decompression-bomb guard), pre-check content-length, block non-http(s), reject evil headers | Prevents OOM/SSRF-adjacent disasters; makes the tool safe to expose to a model | L |
| 2.5 | **Robots.txt toggle** (default off, `--respect-robots`) + `--rate-limit` in batch mode | Keeps the agent a good citizen on clearnet | M |
| 2.6 | **Stepwise redirect + timing info**, `--max-redirects`, latency/round-trip fields per hop | Obsolete the `timeout hunting` debugging; gives the agent transparency | M |
| 2.7 | **JS-heavy-page detection + `"needs_renderer": true` hint** (JS-light heuristic: DOM size vs script weight, content signals) feeding Tier 3.6 fallback | Deprioritizes useless fetches like the 177KB/129-char DDG onion result before wasting tokens | M |
| 2.8 | **Link-quality extraction**: aria-label/title fallback, dedupe by target URL, first-N-links ranking option, skip icon/anchor-with-image-only links | Fixes the 2-links-from-177KB crawl gap; bigger exploration surface for `--follow` | M |
| 2.9 | **Block-structure text rendering** (headings + paragraphs, skip nav/footer heuristically, readable summary of actions) | Makes text output LLM-usable; field test shows menu noise dominates current output | M |

### Tier 3 — agent workflows (enables genuinely complex behavior)

| # | Upgrade | ROI | Effort |
|---|---------|-----|--------|
| 3.1 | **Limited crawl** (`--follow DEPTH [--max-pages N]`, BFS, cache-deduped) | Discover-and-explore a site from one seed URL without giving agent a loop | M |
| 3.2 | **Selector extraction** (`--extract 'css:table tr'` / `--xpath`) | Structured data/scraping workflows agents can actually reason over | M |
| 3.3 | **Search integration** (`--search "query"`): DDG HTML / Ahmia (.onion) | The missing discovery layer; agent goes from "given URLs" to "can find stuff" | M |
| 3.4 | **Mirror fallback** (`--via-archive`): Wayback Machine on failure | Resurrect dead on-page links; agents stop hitting 404 walls | L |
| 3.5 | **Tor control** (optional `stem`): `--tor-new-identity`, circuit status | Identity rotation for Tor workflows; advanced but high value on .onion | H |
| 3.6 | **Optional renderer** (`--renderer chromium`, lazy-loaded): fetch when static HTML is insufficient | Fills the JS-page gap without making it a hard dependency | H |

### Tier 4 — engineering foundations

| # | Upgrade | ROI | Effort |
|---|---------|-----|--------|
| 4.1 | `.pyproject.toml`, `openclaw-fetch` console script, `python -m openclaw_fetch` | Install once, call from any agent runtime | L |
| 4.2 | Config file `~/.config/openclaw-fetch/config.toml` + env overrides (proxies, timeouts, defaults) | Ops sanity across machines | M |
| 4.3 | `pytest` suite: local `http.server` fixtures + fake proxy for Tor/I2P tests; CI | The only thing standing between this repo and regressions | M |
| 4.4 | Type hints, docstrings on public API, docs/EXAMPLES.md | Agent-readable documentation; LLM can self-serve usage | L |

---

## 4. Target architecture

Split the single file into a package (do it when Tier 1 lands; until then keep one file plus
`openclaw_fetch/` shim if needed):

```
openclaw_fetch/
  __init__.py      # public API: fetch(), fetch_many(), Fetcher
  cli.py           # argparse wrapper (keeps current flags byte-identical)
  fetcher.py       # network layer: per-network sessions, retries, cookies, redirects
  cache.py         # TTL disk cache (key=hash(network|url), atomic writes)
  parse.py         # HTML -> text / markdown / metadata / links; content-type dispatch
  output.py        # json / human / markdown / raw formatters
  errors.py        # FetchError taxonomy + stable error_code mapping
  config.py        # config.toml + env + flag precedence
tests/
  test_fetch.py test_parse.py test_cache.py
pyproject.toml
README.md  plan.md
```

Public API sketch (available in Tier 1 milestone):

```python
from openclaw_fetch import fetch, fetch_many, Fetcher

r = fetch("http://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/",
          network="tor", format="markdown", max_chars=6000)
# r.ok, r.status, r.final_url, r.title, r.text, r.links, r.metadata

results = fetch_many([url1, url2, url3], network="tor", concurrency=4)

f = Fetcher(network="tor", cookie_jar="/tmp/tor.jar", retries=3)
r = f.post(url, data={"q": "..."})
```

**Backward-compat decision:** `openclaw_darknet_fetch.py` stays as a forwarding entry point
(`from openclaw_fetch.cli import main`) so every existing invocation still works.

---

## 5. Roadmap

- **Phase 0 (foundation):** pyproject + package skeleton, pytest harness with local-server
  fixtures, move existing logic verbatim into modules, `--version`. Existing behavior proven by tests.
- **Phase 1 (Tier 1):** markdown format, `fetch()`/`fetch_many()`/`Fetcher`, batch mode,
  TTL cache, retries, cookie jars, status/`ok` correctness, error taxonomy.
- **Phase 2 (Tier 2):** POST/form, content-type dispatch (json/pdf/plain/image), rich metadata,
  body caps, robots toggle, redirect/timing info.
- **Phase 3 (Tier 3):** follow/crawl, selector extraction, search, archive fallback, tor identity,
  optional renderer.
- **Phase 4 (polish):** config file, docs, CI, benchmarks for token cost per format.

Order matters: each phase leaves the tool fully usable and shippable.

---

## 6. Agent integration notes

- Keep one stable JSON contract: `{ok, error, error_code, network, requested_url, final_url,
  status, content_type, content_length, title, total_text_length, returned_text_length,
  took_ms, links[], link_count, metadata{}, needs_renderer, text}`. Add fields, never rename existing ones.
- Logs go to **stderr** so stdout stays machine-parseable in `--json` mode.
- Provide a ready-to-paste OpenClaw tool descriptor in `docs/EXAMPLES.md` showing the flags
  an agent should reach for (markdown format, `--max-chars`, `--follow`, `--search`).
- Document exit-code ranges: `0 ok, 1 usage, 2 proxy down, 3 timeout, 4 connection, 5 http/request,
  6 app error`, matched 1:1 to `error_code` strings.

---

## 7. Definition of done

- `pip install -e .` then `openclaw-fetch …` and legacy `python3 openclaw_darknet_fetch.py …`
  behave identically for every currently-documented command.
- All three networks still fetch on a machine with Tor + I2P up (regression-tested).
- `pytest` green in CI, with fake-proxy fixtures so CI never needs real Tor.
- A `fetch_many([...], network="tor", concurrency=4)` round trip is a documented example.
- No fetch can exceed the configured body cap under any response (bomb test in the suite).

---

## 8. Live-trial results + next backlog (2026-09-12, real Tor + I2P)

**Verified working live**
- Tor→clearnet markdown, Tor→DDG onion, I2P→i2p-projekt markdown (headings+text clean).
- **Ahmia search over Tor returned real .onion results** — full discovery loop (search → fetch).
- Batch over Tor with concurrency, crawl over Tor, POST-form over Tor, disk-cache hit
  (2nd fetch served `CACHED` from disk), `--extract` with new `^=`/`*=`/`~=` operators.
- `needs_renderer` correctly flags DDG onion (extracts 40 usable chars of a 177KB page).

**Bugs found & fixed during live trial**
- `-t/-i` with `nargs='*'` yields an empty *list* (falsy) → network silently fell back to
  `normal`, breaking search-backend choice (`--search` under `-t` hit DDG instead of Ahmia)
  and `--check-proxy`. Fixed by testing `is not None`.
- `--check-proxy`/`--tor-new-identity` with no URL hit the "No URL" guard. Fixed.
- CSS subset lacked attribute operators `^=`, `*=`, `~=`. Fixed (1361 matches on Wikipedia).
- Multi-URL human/text/markdown output concatenated bodies with no URL separators. Fixed,
  plus added `--format jsonl` (one JSON object per line) for agent/CI streams.

**Environmental notes from live run**
- DDG html backend serves a 202 challenge to non-browser clients → surfaced as `ok:false`.
- httpbin POST returned HTML (exit-proxy interstitial) not JSON — verify content-type dispatch
  against real APIs; add an explicit `--expect-json` error when `content_kind` mismatches.
- Tor ControlPort `9051` is closed on this box → `--tor-new-identity` needs
  `ControlPort 9051` in torrc + `stem`. pypdf absent → PDF note path active.

**Next sprint backlog (highest ROI first)**
1. `--search-fetch-top N` — run search then auto-fetch top N results through the same network
   (closes the search→fetch loop agents actually want).
2. **Embedded-JSON reader** for JS-heavy pages (`__NEXT_DATA__`, `window.__INITIAL_STATE__`,
   `window.store`): extract structured content from the SPA payload *before* falling back to a
   browser. Turn `needs_renderer` into `"payload_extracted": true`.
3. **More search backends**: Startpage HTML, Mojeek, Marginalia (non-JS friendly), SearXNG
   instances; allow `--search-backend URL` for arbitrary SearXNG.
4. **Tor identity rotation on stuck proxy**: if N consecutive retries fail, renew circuit
   automatically (needs ControlPort). Document torrc snippet for `9051`.
5. **JSON content-type hardening**: `--expect json|html|pdf` + clear error when server
   returns a mismatched kind (catches exit-proxy interstitials like httpbin's).
6. **Crawl hardening**: cache-aware crawl (skip previously-cached pages), per-host
   concurrency cap, strip fragments, optional `--respect-robots`.
7. **Thread-safe cookies in batch**: cookie save from one thread only / per-worker jars.
8. **Optional renderer backend** (Playwright/Chromium) gated behind `--renderer`, using
   `needs_renderer` as the trigger hint.
9. **`--verbose` wiring**: per-hop timing, proxy status, cache stats, retry log to stderr.
10. **PDF fallback**: use system `pdftotext` binary when pypdf is absent.

---

## 9. Universal agent integration

The tool must be consumable by *any* agent runtime, not just OpenClaw.

- **MCP server subcommand** (`openclaw-fetch mcp`): stdio Model Context Protocol server
  exposing tools: `fetch`, `fetch_many`, `crawl`, `search`, `extract`, `tor_identity`,
  `proxy_status`. Uses the `mcp` SDK as an optional extra. This single addition makes the
  tool available to Claude, Copilot, OpenClaw, Goose, and any MCP client in 2026.
- **Generated tool definitions** (`openclaw-fetch tool-def --runtime openclaw|goose|claude`):
  emit JSON function schemas with descriptions + `oneOf` network constraints so agents
  discover flags correctly.
- **Machine contract hygiene (already shaping output)**:
  - stdout is data only (all logs to stderr; `--log-json` for structured stderr logs).
  - `--format jsonl` (one result per line) added for streaming agent pipelines.
  - `--format = fetch|search|crawl` kinds stay stable; fields are additive only.
  - `--env-report` prints resolved config (proxy, timeouts, cache dir) as JSON for debugging.
- **Execution budgets**: `--budget-seconds N` (global wall-clock across URLs),
  `--budget-bytes N` (total body across URLs). After budget, remaining URLs return a
  dedicated `BUDGET` error code instead of hanging.
- **Prompt-friendly defaults**: `--format markdown` is the documented default for LLM
  consumption; `--max-chars` guidance in tool descriptions.
- **Idempotency contract**: GET + stable cache key ⇒ repeat calls are cheap; retries are
  documented as (connection, timeout, 5xx, 429) only.
- **Always-on safety hints in JSON**: `network`, `proxy_used`, `requested_url` vs `final_url`,
  `cached`, so agents can reason about provenance of the retrieved info.

## 10. Tor & I2P depth — reach beyond the clearnet

This is the project's core differentiator. Future work:

- **Onion discovery extension** (beyond Ahmia): add dark.fail mirror, danwin1210 index,
  DER-PINGER/index APIs, and `--search-backend ahmia` as the default Tor backend. Support a
  raw SearXNG/other URL via `--search-backend URL`.
- **`--search-fetch-top N`**: search, then auto-fetch the top N results *through the same
  network* in parallel, returning one combined document. Closes the exact loop an agent
  performs (find onion → read it) in a single command.
- **Hostname-aware network routing** (edge case, do early): if target contains `.onion`
  and network is `normal` → fail with guidance `use -t`; if `.i2p` and network is `normal`
  → guidance `use -i`. If network is `i2p`, `.onion` → suggest `-t` (I2P cannot reach
  onions). Prevents the "DNS error that means use-other-network" confusion.
- **Exit verification**: `--tor-verify-ip` calls `check.torproject.org/api/ip` through the
  chosen proxy and reports `exit_ip` + `is_tor` in the result. `--tor-info` adds circuit
  fingerprint/status when `stem` + ControlPort are available.
- **Circuit / stream isolation** (best effort, requires ControlPort): `--tor-new-identity`
  (exists), plus optional automatic circuit rotation when N consecutive retries fail under
  the same identity ("stuck proxy" recovery). Document torrc snippet: `ControlPort 9051`.
- **Onion mirrors for clearnet content**: `--find-mirrors SITE` searches Ahmia for onion
  mirrors (e.g., BBC/NYT/protonmail/facebook) and returns candidates; `--mirrors U1 U2 U3`
  fetches the same content via multiple networks in parallel and returns a comparison
  (title, length, ok) so agents can pick the most available/current copy.
- **I2P semantics**:
  - `.i2p` requires the router addressbook/jump service; add `--i2p-jump HOST` to resolve
    via the router's jump service before fetching (many hostnames fail DNS otherwise).
  - Document that I2P **outproxies are not anonymous** for clearnet URLs (multiple outproxy
    operators see traffic) — `-i` is for `.i2p` eepsites primarily.
  - I2P-only services often publish `.i2p` + a clearnet mirror; `--mirrors` covers this.
- **Deep-document collection**: `--collect-documents` scans extracted links for
  PDF/DOC/XLS/EPUB/torrent extensions and lists them (bucket for later download).
- **RSS/Atom feeds**: content-kind `rss`/`atom` parsing into structured `items[]`
  (title/link/date/summary/tags) — agents subsist on feeds, incl. onion-hosted feeds.
- **`.onion`/`.i2p` only in JSON contract**: add `target_tld` (`onion`/`i2p`/`clearnet`),
  `circuit_id` (when available) so agents can reason about anonymity domains.

## 11. Production hardening & edge cases

- **Network validation**:
  - Scheme allowlist `http/https` only (done); block `user:pass@` in URLs or redact the
    password in every output field (`requested_url`, `final_url`, logs).
  - IDN/unicode domains → punycode normalize before request.
  - Private/loopback addresses optional guard: `--no-private-ip` (clearnet only; Tor/I2P
    cannot be checked — documented) to prevent accidental LAN probing.
- **Redirect & retry correctness**:
  - Respect `Retry-After` on 429/503 (sleep instead of hammering); separate policies for
    transient (connection, timeout, 5xx, 429) vs permanent 4xx (no retry).
  - Redirect loop protection via `--max-redirects` (done in `Fetcher`); expose `redirects`
    and `redirect_chain` list for agents.
  - Timeouts split into connect/read phases; `--connect-timeout`, `--read-timeout`.
- **Cache correctness (auth edge case)**: when a non-empty cookie jar is loaded, either
  disable GET caching or include a cookie-handle in the cache key (e.g., jar mtime + host)
  so authenticated responses can never be served to other contexts. Concurrency-safe via
  atomic `os.replace` (done). Add `--flush-cache`, `--cache-max-mb` LRU eviction.
- **Security hygiene**:
  - Cookie jar files written with mode `0600`.
  - Decompression-bomb guard via `max-bytes` (done); add compressed-ratio cap when a
    tiny `Content-Length` explodes past `max-bytes` — abort early.
  - No telemetry, no third-party requests beyond target + chosen search/mirror backends.
  - Document that Tor/I2P reduce but do not guarantee anonymity and that clearnet is not
    anonymous (tool is neutral; agent chooses network).
- **Content-type hardening**: `--expect json|html|pdf|rss` returns `EXPECT_MISMATCH` when
  the server replies with a different kind (catches exit-proxy interstitials, login walls,
  and error pages served as HTML — we hit this with httpbin over Tor).
- **Edge content**: empty bodies, `Content-Encoding: br` (opt-in via `brotli` extra),
  huge JSON (streamed jsonl views), binary blobs (`--save-raw` exists), no-charset pages
  (decode fallback exists), HTTP/2 (requests handles via urllib3), onion with TLS (rare).
- **Politeness**: `--rate-limit REQS/SEC` (token bucket across concurrency),
  `--max-concurrency-cap` per network (e.g., ≤6 for Tor), `--respect-robots` (fetch +
  cache robots.txt over the same network, single-flight).
- **Failure taxonomy completeness**: add `BUDGET`, `EXPECT_MISMATCH`, `SCHEME_BLOCKED`,
  `HOST_BLOCKED`, `NETWORK_MISMATCH` (the `.onion`/`.i2p` routing case) to the error table
  with stable exit codes.

## 12. Ops, distribution & quality gates

- **Packaging**: `pyproject.toml` (done); publishable to PyPI; `openclaw-fetch` console
  script + `python -m` (done). Optional extras: `[pdf]`, `[tor]`, `[mcp]`, `[all]`.
- **Docker image** (`ghcr.io/openclaw/openclaw-fetch`): bundles Tor, exposes SOCKS5, entry
  point `openclaw-fetch mcp` by default so agents get an isolated, reproducible probe.
- **Config**: ship `config.example.toml`; `--config PATH` flag; per-network sections
  (timeout/retries/proxy overrides); `--tor-proxy`, `--i2p-proxy`, `--ua` flags.
- **Logging**: `--log-level`, `--log-json`; suppress urllib3 noise at INFO; each request
  logs `{network, url, status, took_ms, cached}` on one line.
- **Quality gates (CI)**: ruff + mypy; pytest with a fake SOCKS proxy fixture so the Tor
  code path runs in CI without real Tor; property tests for URL normalization; fuzz
  `parse_document` with random bytes (must never raise); tests tagged `live`/`slow`
  run only with real proxies; dependabot; release workflow (tags → PyPI + GHCR).
- **Documentation**: `docs/EXAMPLES.md` with OpenClaw tool-descriptor + MCP config +
  torrc snippet; `SECURITY.md` (anonymity expectations, no-telemetry, reporting).

## 13. Revised roadmap

- **v2.1 (protocol):** MCP server, `tool-def`, `--env-report`, `--budget-*`.
- **v2.2 (Tor/I2P reach):** `--search-fetch-top`, hostname-aware routing, `.onion/.i2p`
  mismatch errors, `--tor-verify-ip`, Ahmia+backends, `--i2p-jump`.
- **v2.3 (hardening):** cookie-aware cache keys, redaction, `--expect`, `--rate-limit`,
  retry-by-status + `Retry-After`, split timeouts, `--no-private-ip`, eviction.
- **v2.4 (content):** RSS/Atom, embedded-JSON reader (`__NEXT_DATA__`), `--collect-documents`,
  `--find-mirrors`/`--mirrors`, `pdftotext` fallback, optional Playwright renderer.
- **v2.5 (distribution):** Docker, example config, docs/EXAMPLES, SECURITY.md, CI quality gates.

Order within each version is dependency-driven; every version ships fully working.
