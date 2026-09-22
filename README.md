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

The goal is simple:

> Give a local AI agent one small CLI tool that can fetch a URL through the normal Internet, Tor, or I2P.

---

## Install

```bash
pip install -e .          # installs the `openclaw-fetch` console script
pip install requests[socks]
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

### Library API

```python
from openclaw_fetch import fetch, fetch_many, Fetcher

r = fetch("https://example.com", network="tor", format="markdown", max_chars=6000)
# r.ok, r.status, r.final_url, r.title, r.text, r.markdown, r.links, r.metadata

results = fetch_many([url1, url2, url3], network="tor", concurrency=4)

f = Fetcher(network="tor", cookie_jar="/tmp/tor.jar", retries=3)
r = f.post(url, data={"q": "..."})
```

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