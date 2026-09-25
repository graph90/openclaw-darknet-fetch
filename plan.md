# Plan: OpenClaw Darknet Fetch v2

Multi-network fetcher for AI agents. Current version is a single-file CLI that fetches a URL
over Normal/Tor/I2P and returns text/links as human text or JSON. This plan grows it from a
"fetch one page" tool into a production-grade agent toolkit without breaking existing usage.

> **Part II (from section 15 onward) is the live roadmap** and supersedes the historical
> plans below. Sections 1-14 are kept as the shipped record.

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

---

## 14. Shipped 2026-09-25 — v2.2 darknet depth (verified on live Tor + I2P)

### Search engines
- **`tor66` backend** (new, the one engine that actually works today): v3 onion
  `http://3bbad7fauom4d6sgppalyqddsqbf5u5p56b5k5uk2zxsy3d6ey2jobad.onion` plus the
  `https://tor66.org` clearnet mirror. Parses both front-ends, prefers the plaintext
  `div.link` target over the `/ads/click?s=` hop, flags `sponsored` blocks, dedupes targets,
  stops at the pagination sentinel.
- **`marginalia` backend** (new): independent index, no CAPTCHA on a normal cadence.
- **`AUTO_BACKENDS`** fallback chain per network — `tor: tor66 → ahmia → ddg`,
  `normal/i2p: marginalia → ddg`, `auto: tor66 → marginalia → ddg` (onion index first, then
  the clearnet; the fetcher routes each engine itself because `auto` is TLD-aware).
- **`_looks_blocked`** detects engine interstitials (DDG's 202 "bots use DuckDuckGo too",
  Marginalia's "aggressive bot activity" wall, generic CAPTCHA/consent pages) and fails with
  `error_code=REQUEST` + `blocked_backends` + a hint to point `--search-backend` at your own
  SearXNG, instead of a misleading "0 results".
- **Sponsored filtering**: dropped by default (rank renumbered), `include_sponsored=True` /
  `--include-sponsored` to keep them.
- `backends_tried` / `blocked_backends` on every search result; `html.unescape` in `_strip_tags`.

Measured live: Ahmia is JS-only (0 server-rendered results), DDG `html.`/`lite.` always
202-challenge this IP, Marginalia throttles intermittently, so the onion path (tor66) is the
default for Tor and `auto`.

### Network routing
- **`network="auto"`** everywhere (fetch/fetch_many/crawl/search/search_fetch/MCP/`--network`):
  `.onion` → Tor, `.i2p` → I2P, else clearnet, per target. New CLI flag `-a`.
  Cache keys include the *resolved* network so auto and explicit fetches never collide.
- **`check_proxy()` with `auto`** probes all three networks and returns a `probes` map
  (one entry per network, each with `ok`/`proxy`/`exit_ip`/`is_tor`).

### Tor / I2P depth
- **Per-request circuit isolation** (default on, `isolate=` ctor arg, `--isolate/--no-isolate`,
  `OPENCLAW_ISOLATE` env): each Tor request gets unique SOCKS credentials
  (`socks5h://isolate-<uuid>:<uuid>-<tag>@127.0.0.1:9050`), so a multi-URL run does not share
  an exit node. Retry attempts get distinct tags. Ignored for normal/I2P (no stream isolation
  primitive there). This is *stream* isolation, not a new identity.
- **Darknet timeout floors** (45s connect / 90s read) applied to `.onion`/`.i2p` only, and
  only when the caller did not pass `--timeout`/`--connect-timeout` (explicit values always
  win). Tor66's cold onion connect measured 11.1s, well past the old 10s connect default.

### Contract / robustness
- `error_result` now returns the **full canonical read shape** (status/text/content_kind/links/
  metadata/items/…) so agents can index failed results without attribute errors; per-result
  mutable defaults are no longer shared.
- `target_tld` is derived from the URL for failures too (onion/i2p/clearnet).

### Live verification
`auto` clearnet/I2P/Tor routing 200s; `check_proxy` auto-probes all three; tor66 onion search
returns real onion targets; a cold onion fetch over Tor completes in ~3.7s with the default
(isolated) settings. Offline suite: 144 tests green (local HTTP server + fake SOCKS5, no
network needed).

### Still open
`--find-mirrors`/`--mirrors`, `--collect-documents`, `--i2p-jump`, circuit rotation via
ControlPort, `Result.items` collision, raw/binary contract, RSS relative links, library JSON
validation, robots `allow_errors`, search-result cache poisoning, CI (ruff/mypy), docs/EXAMPLES.

---
---

# Part II — v3 roadmap: the agent's darknet browser

Everything above (Part I) is the shipped history. This part is the forward plan and it
**supersedes Part I sections 10-14** wherever they disagree.

**Defining goal for v3:** an agent should be able to *find* darknet content, *read* it even
when it is JavaScript-only, *corroborate* it across clearnet/Tor/I2P, and *prove* how it got
there — with one tool call each, and never silently degrade its own anonymity to do it.

## 15. Design principles (read before adding any feature)

1. **Provenance over payload.** Every result carries *how it was fetched*, not just what:
   resolved network, isolation mode, exit IP + `is_tor`, circuit id when available,
   redirect chain, cache hit, content hash, and `fetched_at`. Agents reason about anonymity
   domains; we should hand them the evidence instead of adjectives.
2. **Anonymity is verified, never assumed.** If isolation is on but the torrc lacks
   `IsolateSOCKSAuth`, the feature is *not working*. We must detect and report that, not
   claim a guarantee we cannot check.
3. **Never silently fall back to a weaker network.** An onion fetch that quietly degrades to
   the clearnet is a privacy bug, not a feature. Fail with guidance, or ask.
4. **Every feature ships a JSON contract and an offline test.** No new output shape without
   a fixture-based test in `tests/`, and no new network call without an injectable seam.
5. **Composite beats primitive.** An agent does not want "fetch 3 URLs"; it wants "read this
   topic from the darknet and give me a sourced brief". Design top-down from agent tasks.
6. **Token economics are a first-class output.** Every multi-fetch operation reports
   estimated tokens, per-source token cost, and supports a budget.
7. **Never execute page JavaScript.** Darknet users reject JS because it widens their
   fingerprinting surface; running a browser against an onion service makes the tool an
   attack surface instead of a reader. Server-rendered HTML plus embedded-JSON extraction is
   the darknet-native path; any renderer is opt-in, never default, never implicit.
8. **Darknet reality is hostile to nice clients:** sites vanish, handshakes stall, engines
   challenge, mirrors rot. The tool's job is to absorb that pathology and report it as data
   (`suspected_dead`, `blocked_by`, `rtt_ms`) rather than as exceptions.

## 16. Audit snapshot (2026-09-25, verified by reading the code)

Real today: `network=auto` TLD routing, tor66 + marginalia + ahmia + ddg + searxng search
with per-network fallback and block detection, `--search-fetch-top`, crawl, RSS/PDF/SPA-JSON
extraction, disk cache, cookie jar, SSRF/redirect hardening, `error_result` full shape, MCP
server, 145 tests, live-verified on Tor and I2P.

Known contract bugs found while planning (all P0, all cheap):

| # | Bug | Location | Impact |
|---|---|---|---|
| 1 | `--search Q` with no `--search-backend` sends `backend=None` → `USAGE` ("unknown search backend None"); human output hides it as `BACKEND: auto / RESULTS: 0` | `cli.py:99`, `cli.py:328` | **flagship path is broken** |
| 2 | `check_proxy_for("auto")` raises `KeyError: 'auto'` | `fetcher.py:895-913` | MCP/library crash |
| 3 | `fetch(..., format="markdown")` (documented in README + `__init__.py`) → `TypeError` | `fetcher.py:77-109` | docs lie; agents crash |
| 4 | `crawl()` returns `ok=True` when every page failed; `pages_fetched` counts attempts | `crawl.py:116-126` | agents treat failure as success |
| 5 | `r.items` is `dict.items`, not RSS items (only `r["items"]` works) | `result.py:11-30` | documented API broken |
| 6 | `--raw` returns decoded text, not bytes; `--save-raw` is a documented no-op | `output.py:67-71`, `cli.py:125` | binary content unusable |
| 7 | `parse_feed()` has no `base_url` → relative RSS links stay relative | `parse.py:462-538` | broken item URLs |
| 8 | `expect=` mismatch + `allow_errors=True` → `ok=True` **with** `error_code` set | `fetcher.py:797-839` | exit 0 on contract violation |
| 9 | `search()` on `auto` chains can return `ok=True` with 0 results when one engine was empty and another failed | `search.py:364-443` | silent empty results |
| 10 | Two tests hit live endpoints while README claims the suite is fully offline | `tests/test_fetch.py:270`, `tests/test_cli.py:196` | flaky CI story |

## 17. Phase 0 — contract honesty (do first, ~1 day) — **SHIPPED 2026-09-25**

Nothing else matters if the documented contract is a lie. Fix #1-#10, then **stop
appending "shipped" sections and keep `plan.md` as a live status board** (a `status:`
column per item, updated in the same commit as the code).

- `#1` one-liner: `backend = args.search_backend or "auto"`; add offline CLI test that
  `--search` with a stub engine returns results.
- `#3` implement `format=` on the module-level `fetch()` (post-process the `Result`, not the
  `Fetcher`) and add `format` to the library docstrings; `fetch_many` likewise.
- `#4` `crawl()` gains `pages_succeeded`, `pages_failed`, and `ok = pages_succeeded > 0`.
- `#5` never remove the `items` key; document `r["items"]`; add `Result.feed_items` as a
  non-colliding alias and a regression test.
- `#6` define raw as bytes: keep `_raw_body` bytes, write via `sys.stdout.buffer` in `--raw`,
  make `--save-raw PATH` actually write the file (and fix the image hint in `parse.py`).
- `#8` `allow_errors` must not flip `ok` to true for `expect` mismatches; separate
  `error_code="EXPECT_MISMATCH"` from `ok`.
- `#9` auto search distinguishes "no results" from "all backends failed": if every backend
  failed, `ok=False`; if at least one succeeded with 0 hits, `ok=True, total_results=0`.
- `#10` mark live tests `@pytest.mark.live`, add `pytest -m "not live"` to CI.

Delivered: `#1` search default is `auto`; `#2` `check_proxy_for("auto")` delegates;
`#3` `format=` on `fetch`/`fetch_many`; `#4` crawl reports `pages_succeeded`/`pages_failed`
and fails when nothing readable came back; `#5` `Result.feed_items` alias + docs;
`#6` `RawBody` keeps exact bytes *and* `json.dumps` working, `--raw` writes bytes,
`--save-raw PATH` implemented; `#7` `parse_feed(base_url=...)` resolves relative item links;
`#8` `--expect` mismatch stays a failure with `--allow-errors`; `#9` search reports
`backends_failed` and only fails when *no* engine answered; `#10` live markers registered
(`pytest` now defaults to `-m "not live"`). Also fixed along the way: human search output no
longer hides failures behind `RESULTS: 0`; MCP `crawl` passed a bogus `fetcher_kwargs=` into
`Fetcher()` (crashed on every call); MCP `search_fetch` leaked `max_chars=0` into fetched
pages (fixed with `Fetcher.clone()`); MCP search tools now expose `search_url`,
`time_range`, `include_sponsored`, `isolate`. 171 offline tests, 2 live.

## 18. Phase 1 — Tor you can see and control

The differentiator. Today "isolation" is a string we generate and hope `torrc` honors.

1. **`--tor-doctor` / `doctor()` — verify the daemon actually supports what we claim.**
   Check `ControlPort` reachable, `IsolateSOCKSAuth`/`IsolateClientAddr` present,
   `CookieAuthentication`, SocksPort host/port, `ClientUseIPv4/6`, `StrictNodes`,
   and print the exact torrc lines to add. **If `IsolateSOCKSAuth` is off, warn that
   per-request isolation is a no-op.** This is the single most important honesty fix in the
   whole roadmap.
2. **Optional ControlPort support (`[tor]` extra: `stem`).** `TorControl` wrapper:
   `GETINFO status/bootstrap-phase`, `GETINFO circuit-status`, `GETINFO onions/current`,
   `SIGNAL NEWNYM`, `SETCONF`. Degrade to a clear message when unavailable.
3. **`circuit_id` + guard/exit in provenance.** Map the in-flight stream to a circuit
   (`stem` stream events) and attach `circuit_id`, `guard`, `exit_node`, `exit_country`
   (via a tiny offline GeoIP table or an optional `maxminddb` extra) to the result.
4. **`--tor-verify` (default on for `auto`/`tor` in agent mode): per-request exit
   verification.** After each fetch, confirm the response actually came through Tor
   (`is_tor` check, cached per circuit, not per request). Fail closed with
   `error_code="ANONYMITY_UNVERIFIED"`. An agent must be able to *trust* a darknet read.
5. **`--tor-exit-avoid COUNTRY[,ASNS...]` / `--tor-exit-require`.** Refuse exits in
   jurisdictions an agent should not touch (mass surveillance, blocking). Implemented by
   retrying until an acceptable exit appears (with a budget) — this is a *policy* primitive
   no generic HTTP client offers.
6. **NEWNYM on failure, not only on demand.** After N consecutive failures on the same
   circuit, `SIGNAL NEWNYM` and retry (the "stuck exit" case: captchas, 403 walls, 503
   walls). Also rotate on `blocked_by` detection.
7. **Onion service primitives.**
   - v3 address validation (56 chars, base32, checksum) and clear errors for typos —
     a wrong onion currently looks like a timeout.
   - `Onion-Location` header harvesting: fetch the clearnet site, collect
     `Onion-Location: <url>` and `X-I2P-*`-style hints into `metadata.mirrors` so an agent
     learns the *official* onion without an index.
   - `suspected_dead` accounting: a small on-disk ledger of onion↔(ok, rtt, last_seen);
     after k consecutive failures, return it fast with `suspected_dead: true` instead of
     burning 90s of timeouts. Agents crawl flaky onions constantly; this is a real speedup.
8. **Guarded exits vs `strict_nodes`:** document and expose whether we set `StrictNodes` for
   isolation guarantees, and what that means for the "random exit" property.

## 19. Phase 2 — I2P depth

I2P is the least mature path and the most neglected; the plan should say so honestly.

1. **Addressbook + jump service.** Many `.b32.i2p` hostnames do not resolve through the HTTP
   proxy. Implement `.b32.i2p` → base32-address conversion, jump-service resolution
   (`--i2p-jump HOST[:port]`, default `router.i2p:port=...`), and **multi-destination
   fallback**: on failure, try the router's alternate jump destinations and report which one
   worked in `metadata.jump_via`.
2. **Outproxy honesty.** I2P clearnet access is via *outproxies*, which are not anonymous and
   are a known correlation/leak point. Requirements:
   - detect when a clearnet fetch went through an outproxy and set
     `metadata.i2p_outproxy_used: true`,
   - refuse/warn when an agent asks for "anonymous clearnet" over I2P,
   - document that `-i` is for eepsites, and that `-i` + `.onion` is impossible (no onion
     support in I2P) — surface `NETWORK_MISMATCH` with that exact reason.
3. **A real eepsite index.** I2P has no onion-style search index, so build one:
   - `--i2p-index-refresh` crawls the known eepsite directory pages (Postman index,
     i2p-projekt eepsite lists, curated seeds) into a local signed JSON index with
     `last_seen`/`dead` fields,
   - a `postman` search backend serves that local index (fast, offline-friendly),
   - dead-link detection marks entries dead so agents stop retrying them.
4. **I2P-appropriate timeouts and pacing.** Eepsites are slow (tunnel build dominates).
   Per-network timeout floors (I2P > Tor > clearnet), lower default concurrency for I2P, and
   `--i2p-patience` for "I will wait as long as it takes" mode.
5. **Eepsite-specific extraction.** Many eepsites are old-style HTML forms and directory
   index pages; add link-list extraction ("directory page" content kind) and a
   "collect every `.b32.i2p` link on this page" primitive, which is how eepsite graphs
   actually grow.

## 20. Phase 3 — discovery, mirrors, corroboration

1. **`find_mirrors(site) -> {clearnet, onions[], i2ps[], sources[]}`** from three sources:
   `Onion-Location` headers (authoritative), search indexes (tor66/marginalia), and curated
   lists. `sources[]` records provenance so an agent can weigh authority.
2. **`compare(urls|site) -> availability matrix + content diff`, fetched in parallel across
   networks.** This is the single most agent-useful primitive in the darknet: "the
   clearnet copy is stale/blocked, the onion copy is fresh" in one call. Include
   `freshness` (Last-Modified/date), `content_sha256`, and a real text diff
   (`difflib` over extracted markdown, token-budgeted).
3. **`--collect-documents`**: from a page, bucket discovered links (pdf/doc/xls/epub/torrent/
   magnet) without downloading them, and optionally download with a byte budget.
4. **Link classification** for crawl/search results: `onion`, `i2p`, `onion-mail`, `torrc`,
   `magnet`, `download`, `nav`, `article`, `leave-site`. Agents should be able to ask for
   "only the substantive links" and get a shortlist, not 200 nav items.

## 21. Phase 4 — agent-native operations (the wow layer)

1. **`deep_fetch(topic, ...) -> sourced brief`.** The flagship composite: search across
   networks → dedupe URLs → fetch top N in parallel (isolated circuits) → content-hash
   dedupe → order by agreement/credibility → emit a single markdown brief with per-sentence
   provenance, `sources[]` (url, network, exit_ip, is_tor, fetched_at, sha256) and a token
   accounting block. One MCP tool call replaces ~10 agent steps.
2. **`watch(url, interval, until_change)`**: poll with `ETag`/`Last-Modified`/hash and emit
   JSONL change events. Onion sites rotate content and appear/disappear; "did this change"
   is a real agent need, and cheap on Tor via conditional requests.
3. **Resumable crawls**: `--checkpoint FILE` persisting frontier + visited set +
   per-URL last status, so a flaky-onion crawl can be resumed across invocations. Onion
   crawls *will* be interrupted; make that a first-class flow.
4. **Budgets**: implement the already-reserved `BUDGET` error code —
   `--budget-seconds/--budget-bytes/--budget-tokens/--budget-requests`, enforced in fetch,
   crawl, and `deep_fetch`, with `budget_exhausted` reported (never a crash).
5. **`dry_run` / `estimate` for every composite**: return projected pages, tokens, and cost
   *before* any network call, so an agent can decide. Extend `estimate_tokens`.
6. **MCP surface**: expose `deep_fetch`, `compare`, `find_mirrors`, `watch`, `doctor`, and
   `estimate`; add `search_url`/`time_range`/`include_sponsored` to the search tools;
   validate `network`/`format` enums in Python (not just argparse); fix the
   `max_chars=0` fetcher leak in MCP `search_fetch`.
7. **Structured event log** (`--log-jsonl FILE`, one line per request/hop/event with
   `{ts, network, url, status, took_ms, cached, event}`) so an agent's whole run is
   auditable and replayable. This is the "no telemetry" story made verifiable.

## 22. Phase 5 — read what darknet actually serves (measured, not guessed)

**Measured 2026-09-25** (60 onion URLs from tor66 across 7 queries, fetched over real Tor;
`/tmp/opencode/onion_sample.json`):

| Signal | Result |
|---|---|
| Returned 200 | 56 / 60 (93%) |
| Usable extracted text (>=500 chars) | 41 / 60 (68%) |
| Thin text (<500 chars) | 15 — mostly *genuinely* thin (empty search pages, "please wait", member-gated) |
| Tripped `needs_renderer` | 6 (10%) |
| Pages telling the reader to enable JS | 1 |

**Conclusion: the darknet is server-rendered on purpose.** Users reject JS because it
widens the fingerprinting/attack surface, so the original "add a headless browser" milestone
was wrong — it would be near-useless *and* philosophically backwards for this audience.
The headless renderer is demoted to an opt-in, clearnet-only escape hatch (see 22.5).
The real work is closing the gaps the sample actually exposed:

1. **Embedded-payload extraction is the JS answer for the darknet.** 3 of the 6
   `needs_renderer` pages were returning raw `__NEXT_DATA__` / Apollo state as *text*
   (one leaked 12.5k chars of JSON instead of an article). Extract, walk and flatten those
   payloads server-side: no JS engine, no fingerprint, real content. Target: the
   `needs_renderer` share of *usable text* should approach zero.
2. **Interstitial/"please wait" handling.** Several darknet front-ends answer with a
   language-rotating "Proszę czekać... / Bitte warten... / Please wait..." shell and then
   serve the real page. Detect it, wait briefly, refetch once, and label the result
   `metadata.interstitial=true` instead of returning 40 characters of nothing.
3. **Honest labels for pages an agent should skip**: `gated` (members-only/login),
   `empty_index` (directory with no entries), `search_no_hits`, `challenge`. These are not
   errors, but an agent spending tokens on them is wasting budget — the label tells it to
   move on.
4. **Directory/index extraction.** Onion front-ends (oniondir, hidden wiki, dark.fail
   lists) are link farms; extract structured `{title, url, snippet, category}` rows rather
   than prose, and detect "index page" content kind.
5. **Cheap extraction modes for token budgets**: `--metadata-only` (title + headings +
   classified links), `--selector CSS`, `--extract json|csv`.
6. **Content dedupe/clustering across networks** (SimHash/MinHash) so mirrors and
   aggregators do not eat the budget.
7. **Cheap offline language detection** — darknet is heavily non-English, and the
   interstitials above prove it matters for triage.
8. **`--render` (demoted, opt-in, clearnet-first).** Only for JS apps where the user asks;
   per-browser-context proxy = per-context circuit. Document plainly that running a
   browser *adds* fingerprinting surface and therefore must never be the default, and never
   silently enabled for `.onion`/`.i2p`.
9. **No-JS contract in the docs**: the tool never executes page JS, never loads third-party
   subresources, and strips `<script>` before extraction. Make that a stated guarantee
   (a `docs/SECURITY.md` item) because for this audience it is the point.

## 23. Phase 6 — ops, quality, distribution

- CI running only offline tests (`-m "not live"`), ruff + mypy, a real fake-I2P-proxy
  fixture (today I2P is only tested by config selection), a real-PDF fixture, and a
  `live` marker for the Tor/I2P tests.
- Extras matrix: `[pdf] [mcp] [tor]` (stem) `[render]` (playwright) `[all]`. `plan.md`
  currently claims a `[tor]` extra that does not exist.
- Config file support (`config.toml`) — `config.py` docstring claims it; there is no loader.
  Per-network sections (`[tor]`, `[i2p]`, `[auto]`) so darknet defaults live in one place.
- Docker image bundling `tor` + `i2pd` so `--env-report`/doctor can self-verify; release
  workflow; PyPI publish; `docs/EXAMPLES.md`; `SECURITY.md` with the anonymity model
  (what is guaranteed, what is best-effort, what is out of scope).

## 24. Milestones (dependency-ordered, with honest sizing)

| # | Milestone | Depends on | Size | Why now |
|---|---|---|---|---|
| M0 | Phase 0 contract honesty | — | 1d | documented behavior must be true first |
| M1 | `doctor()` + isolation verification | M0 | 2d | makes the current isolation claim honest |
| M2 | Provenance block + `--tor-verify` per request | M0 | 3d | the evidence agents need to trust darknet reads |
| M3 | ControlPort/stem: circuits, `circuit_id`, NEWNYM-on-fail, exit policy | M1, M2 | 5d | real Tor control, the differentiator |
| M4 | I2P addressbook/jump + outproxy honesty + eepsite index | M0 | 5d | I2P is the weakest path today |
| M5 | `find_mirrors` + `compare` (availability + diff) | M2 | 4d | highest agent utility per line of code |
| M6 | `deep_fetch` composite + budgets + `dry_run` | M5 | 5d | the flagship one-call agent workflow |
| M7 | `Onion-Location` harvesting, dead-onion ledger, link classification | M2 | 3d | discovery + crawl efficiency |
| M8 | Darknet-native extraction: embedded payload walking (22.1), interstitial retry + skip labels (22.2-3), directory extraction (22.4) | M2 | 4d | measured 68% usable text; the real gap, not JS |
| M9 | `watch` + resumable crawl checkpoints | M2 | 3d | flaky-site reality |
| M10 | MCP expansion + structured log + enums | M5 | 3d | agent runtime completeness |
| M11 | CI/ruff/mypy/fake-I2P fixture/extras/Docker/config.toml/docs | M0 | 5d | makes the rest sustainable |

## 25. Success metrics (how we know the goal was met)

- **Usable-text rate on real darknet pages:** baseline measured at 68% of onion URLs
  (41/60 with >=500 chars, 93% reachable) — target >= 85% *without* a JS engine, by fixing
  embedded-payload extraction (22.1) and interstitial retries (22.2). Re-measure with the
  same sampler script each milestone; the number, not the feature, is the goal.
- **Provenance completeness:** 100% of agent-visible results carry network, isolation mode,
  exit-verification state, content hash, and timestamp.
- **Anonymity honesty:** 0 cases where a darknet request silently used a weaker network; 0
  cases where isolation is claimed without `IsolateSOCKSAuth` being verified.
- **Task success on the canonical agent task set** (each must pass end-to-end, JSON output,
  offline-safe tests where possible):
  1. "Find onion forums about topic X" → tor66/ahmia results with real onion URLs.
  2. "This clearnet site is blocked for me, read it anyway" → mirror discovery + compare →
     freshest copy.
  3. "Read this eepsite and its onion mirror, tell me if they agree" → i2p + onion + diff.
  4. "Tell me if this onion changed in the last hour" → watch/checkpoint.
  5. "Is this onion alive?" → fast `suspected_dead` verdict within seconds, not minutes.
- **Contract integrity:** every documented example in README/plan runs; 0 `TypeError`s from
  documented kwargs; `pytest -m "not live"` green with no network.

## 26. Non-goals (deliberate, to protect focus)

- No native I2CP/tunnel transport (stay an HTTP client; I2P via its proxy).
- No Tor-internals reimplementation, no fingerprint evasion, no deanonymization research.
- No scraping of private/paid services, no credentialed logins beyond the user's own jar.
- No attempt to be a general search engine: we orchestrate existing indexes and clearly
  report which engine produced what.
- No anonymity claims we cannot verify (Principle 2).
