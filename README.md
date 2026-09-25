# OpenClaw Darknet Fetch

Agent-oriented multi-network (Normal / Tor / I2P) web fetcher toolkit for AI agents:

- Normal clearnet HTTP/HTTPS
- Tor via local SOCKS5 (`.onion` included)
- I2P via local HTTP proxy (`.i2p` eepsites)
- Token-efficient output for LLMs: `--format markdown`
- Batch fetching (`--format jsonl`, `--concurrency`)
- TTL disk cache, retries, persistent cookie jars
- Structured JSON contract with stable `error_code` taxonomy
- Importable Python library: `fetch()`, `fetch_many()`, `Fetcher`
- **Search** the clearnet (DuckDuckGo / SearXNG) or the darknet (Ahmia) and
  fetch the hits through the same network
- **Follow-links crawl** mode (`--follow DEPTH`)
- **MCP server** (`openclaw-fetch mcp`) so agents can call fetch/search/crawl over stdio
- PDF text extraction, RSS/Atom parsing, embedded SPA JSON payloads
- Hardening: rate limiting, robots.txt respect, SSRF/private-IP guard, `--expect`

The goal is simple:

> Give a local AI agent one small CLI tool that can fetch a URL through the normal Internet, Tor, or I2P.

---

## Install

```bash
pip install -e .          # installs the `openclaw-fetch` console script
pip install requests[socks]

# optional extras
pip install -e ".[all]"   # adds mcp server support + pypdf PDF extraction
```

The legacy entry point still works and behaves identically:

```bash
python3 openclaw_darknet_fetch.py -t URL
```

---

## Requirements

- Python 3.9+
- Tor (optional): SOCKS5 proxy at `127.0.0.1:9050`
- I2P (optional): HTTP proxy at `127.0.0.1:4444`

---

## Network Modes

| Flag | Network | Proxy |
|---|---|---|
| `-n` | Normal clearnet | None |
| `-t` | Tor | `127.0.0.1:9050` |
| `-i` | I2P | `127.0.0.1:4444` |
| `-a` | Auto: `.onion` → Tor, `.i2p` → I2P, everything else → clearnet | per target |

`--network auto` is the library/MCP equivalent of `-a`. Auto works for plain
fetches, searches, crawls and batch URLs: each target is routed by TLD, so a
mixed list needs no per-item flags.

Over Tor every request gets **its own circuit** (unique SOCKS credentials per
request) so unrelated sites are not correlated by exit node. Disable it with
`--no-isolate` when you want one shared circuit. Darknet targets also get
automatic timeout floors (45s connect / 90s read) because onion handshakes are
far slower than the clearnet; explicit `--timeout` / `--connect-timeout` always
win.

Search engines are rate-limited and often bot-challenged, so failures name the
engines that blocked you and suggest `--search-backend` with your own SearXNG:

```
openclaw-fetch --search "linux kernel" -a --search-fetch-top 3
# -> backend: tor66 (onion index first)   backends_tried: [tor66]
# blocked: -> backend: marginalia  blocked_backends: [tor66, marginalia]
#            error: all search backends blocked
#            hint: use --search-backend with your own SearXNG instance
```

---

## Usage

### Normal Internet

```bash
openclaw-fetch -n https://example.com
```

### Tor → clearnet

```bash
openclaw-fetch -t https://example.com
```

### Tor → onion

```bash
openclaw-fetch -t "http://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/"
```

### I2P eepsite

```bash
openclaw-fetch -i http://i2p-projekt.i2p/
```

### JSON for agents

```bash
openclaw-fetch -t URL --json
```

### Markdown (token-efficient)

```bash
openclaw-fetch -t URL --format markdown --max-chars 6000
```

### Raw HTML

```bash
openclaw-fetch -t URL --raw
```

### Batch

```bash
openclaw-fetch -t URL1 URL2 URL3 --format jsonl --concurrency 4
# or from a file, one URL per line:
openclaw-fetch -t --batch urls.txt --format jsonl
```

### Search (then fetch the hits)

```bash
# clearnet DDG by default
openclaw-fetch --search "plausible analytics self hosted"

# darknet discovery: onion index first, then the clearnet
openclaw-fetch --search "linux kernel" -a
# pick an engine yourself
openclaw-fetch --search "linux kernel" -t --search-backend tor66
openclaw-fetch --search "bitcoin" --search-backend ahmia -t

# own SearXNG instance
openclaw-fetch --search "python" --search-backend https://searx.be

# fetch top-3 results through the same network
openclaw-fetch --search "news" --search-fetch-top 3 --format jsonl
```

Search never connects to a search engine by accident: without `-t/-i` it uses the
normal network; with `-t` it routes through Tor.

### Crawl (follow links, bounded)

```bash
openclaw-fetch -t http://seed.onion/index --follow 2 --max-pages 25 --format jsonl
```

### RSS / PDF / JSON

```bash
openclaw-fetch -n "https://blog.example/rss.xml"          # items[] extracted
openclaw-fetch -n https://files.example/paper.pdf          # text via pypdf/pdftotext
openclaw-fetch -n https://spa.example/ --expect json       # fail fast if not JSON
```

### MCP server (for agent runtimes)

```bash
openclaw-fetch mcp --network tor     # or --network auto
```

Exposes tools: `fetch`, `fetch_many`, `crawl`, `search`, `search_fetch`,
`proxy_status`, `estimate_tokens`. Every tool takes an optional `network`
(`normal` / `tor` / `i2p` / `auto`) and `isolate`; `proxy_status(network="auto")`
probes all three networks and returns one entry per network. `no_private_ip` is
always forced on.

### Library API

```python
from openclaw_fetch import fetch, fetch_many, Fetcher, search, search_fetch, crawl

r = fetch("https://example.com", network="tor", format="markdown", max_chars=6000)
# r.ok, r.status, r.final_url, r.title, r.text, r.markdown, r.links, r.metadata,
# r.items (RSS), r.payload_extracted (SPA JSON), r.estimated_tokens

results = fetch_many([url1, url2, url3], network="tor", concurrency=4)

f = Fetcher(network="tor", cookie_jar="/tmp/tor.jar", retries=3, rate_limit=5,
            isolate=True)     # per-request Tor circuits (default)
r = f.post(url, data={"q": "..."})

r = fetch("http://abc.onion/", network="auto")         # routed to Tor by TLD
sres = search("bitcoin onion", network="auto")         # onion index, then clearnet
# sres.backend, sres["backends_tried"], sres["blocked_backends"]
bundle = search_fetch("bitcoin onion", top_n=3, network="tor")
crawl_result = crawl("http://seed.example/", depth=2, max_pages=20)
```

Note: `search_fetch` and `crawl` return dicts (bundle/crawl stats + pages),
while `fetch`/`fetch_many`/`search` return `Result` objects.

---

## Key Flags

| Flag | Purpose |
|---|---|
| `--format human\|text\|markdown\|json\|jsonl\|raw` | Output format |
| `--max-chars N` | Cap returned text/markdown length |
| `--max-links N` | Cap links returned per result |
| `--max-bytes N` | Hard body cap (decompression-bomb guard) |
| `--retries N`, `--retry-backoff SEC` | Retry transient failures |
| `--timeout SEC`, `--connect-timeout SEC` | Split read/connect timeouts |
| `--concurrency N` | Parallel fetches in batch mode |
| `--cookie-jar PATH` | Persistent cookie jar (mode 0600) |
| `--no-cache`, `--cache-ttl SEC`, `--flush-cache` | Disk cache control |
| `--allow-errors` | Treat 4xx/5xx bodies as ok |
| `--check-proxy` | Probe network/proxy reachability (reports exit IP over Tor) |
| `--tor-proxy URL`, `--i2p-proxy URL` | Proxy overrides |
| `--isolate`, `--no-isolate` | One Tor circuit per request (default on) |
| `--search QUERY` | Search instead of fetching (auto backend) |
| `--search-backend auto\|ddg\|ahmia\|tor66\|marginalia\|URL` | Search engine; a URL = SearXNG instance |
| `--search-fetch-top N` | After searching, fetch top N hits |
| `--time-range day\|week\|month\|year` | Search time filter |
| `--include-sponsored` | Keep sponsored search hits (default: dropped) |
| `--follow DEPTH`, `--max-pages N` | Bounded BFS crawl from the seed URL |
| `--expect html\|json\|pdf\|rss\|text\|image` | Require content kind (exit 5 on mismatch) |
| `--rate-limit RPS` | Token-bucket max requests/second |
| `--no-private-ip` | Reject private/loopback targets (SSRF guard) |
| `--respect-robots` | Honor robots.txt before GET (clearnet) |
| `--env-report` | Print resolved configuration as JSON, exit 0 |
| `mcp` | Subcommand: run the MCP stdio server (`--network tor`) |

Run `openclaw-fetch --help` for the full list.

---

## Tested Setup

- Linux
- OpenClaw
- A local **Ornith 9B** model running through OpenClaw
- Tor SOCKS5 `127.0.0.1:9050`
- I2P HTTP proxy `127.0.0.1:4444`
- Python 3.9+

The tool is model-agnostic.

---

## Tests

```bash
python -m pytest
```

The suite uses a local HTTP server plus an in-process fake SOCKS5 proxy, so the Tor
code path runs in CI without real Tor.

---

## I2P Note

This project is a fetcher, not an I2P discovery service. The agent needs an actual
I2P hostname/destination that the local I2P router can resolve.

## License

MIT — see [LICENSE](LICENSE).