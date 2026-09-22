#!/usr/bin/env python3

"""
openclaw_darknet_fetch.py

Multi-network web fetcher designed for AI agents.

NETWORKS
--------

Normal clearnet:
    python3 openclaw_darknet_fetch.py -n https://example.com

Tor / onion:
    python3 openclaw_darknet_fetch.py -t http://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/

Tor -> normal clearnet:
    python3 openclaw_darknet_fetch.py -t https://example.com

I2P:
    python3 openclaw_darknet_fetch.py -i http://i2p-projekt.i2p/


PROXIES
-------

Tor SOCKS5:
    127.0.0.1:9050

I2P HTTP proxy:
    127.0.0.1:4444


OUTPUT MODES
------------

Readable text:
    Default output

JSON:
    python3 openclaw_darknet_fetch.py -t URL --json

Markdown (token-efficient for LLMs):
    python3 openclaw_darknet_fetch.py -t URL --format markdown

Limit returned text:
    python3 openclaw_darknet_fetch.py -t URL --max-chars 12000

Raw HTML:
    python3 openclaw_darknet_fetch.py -t URL --raw

Batch (multiple URLs through one network):
    python3 openclaw_darknet_fetch.py -t URL1 URL2 URL3 --format jsonl --concurrency 4

New features: --format, --batch, --retries, --no-cache/--cache-ttl,
--cookie-jar, --max-bytes, --allow-errors, --check-proxy, --version.

DEPENDENCIES
------------

    pip install requests[socks]

This file is a thin forwarding shim for the openclaw_fetch package; all
logic lives there.
"""

import sys

from openclaw_fetch.cli import main

if __name__ == "__main__":
    sys.exit(main())