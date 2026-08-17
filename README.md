# OpenClaw Darknet Fetch

Agent-oriented Python web fetcher for [OpenClaw](https://github.com/openclaw/openclaw) with support for:

- Normal clearnet HTTP/HTTPS
- Tor via local SOCKS5
- I2P via local HTTP proxy
- Agent-friendly JSON output
- HTML text and link extraction
- Configurable output limits

The goal is simple:

> Give a local AI agent one small CLI tool that can fetch a URL through the normal Internet, Tor, or I2P.

---

## Tested Setup

This project has been tested on:

- Linux
- OpenClaw
- A local **Ornith 9B** model running through OpenClaw
- Tor SOCKS5
- I2P HTTP proxy
- Python 3

The tool itself is model-agnostic. The testing setup uses Ornith 9B as the local agent, but the CLI can be driven by other OpenClaw-compatible models as well.

---
Requirements

Python 3.
Install the dependency:
pip install requests[socks]

Tor
A local Tor SOCKS5 proxy should be available at:
127.0.0.1:9050
I2P
A local I2P HTTP proxy should be available at:
127.0.0.1:4444
---
## Network Modes

| Flag | Network | Proxy |
|---|---|---|
| `-n` | Normal clearnet | None |
| `-t` | Tor | `127.0.0.1:9050` |
| `-i` | I2P | `127.0.0.1:4444` |

### Normal Internet

```bash
python3 openclaw_darknet_fetch.py \
    -n https://example.com
```

```bash
python3 openclaw_darknet_fetch.py \
    -t http://exampleonion.onion/
```
```bash
python3 openclaw_darknet_fetch.py \
    -t https://example.com
```
```bash
python3 openclaw_darknet_fetch.py \
    -i http://example.i2p/
```
```bash
python3 openclaw_darknet_fetch.py \
    -t http://exampleonion.onion/ \
    --json
```
---
I2P Discovery Note
This project is a fetcher, not an I2P discovery service.
The agent needs an actual I2P hostname/destination that the local I2P router can resolve.
---
How It Fits With OpenClaw
The intended workflow is:
                    OpenClaw
                       |
                       v
          openclaw_darknet_fetch.py
                       |
          +------------+------------+
          |            |            |
          v            v            v
       Normal         Tor          I2P
          |           |             |
       Internet     :9050         :4444
                      |             |
                   .onion         .i2p
Why I Built This
I wanted to experiment with what a local AI agent could do when given access to multiple network transports.
A normal web-enabled agent can already fetch Internet resources.
This project adds another layer:
Normal Internet
       +
     Tor
       +
     I2P
       ↓
  Local AI Agent
The project started as a small Python experiment and evolved into a single fetcher so OpenClaw doesn't need separate tools for Tor and I2P.
The focus is deliberately on keeping the tool small, predictable, and easy for an agent to invoke.
