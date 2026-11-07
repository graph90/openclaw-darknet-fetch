# Examples

## OpenClaw tool descriptor

Paste into an OpenClaw `tools.json` (or equivalent) to expose the fetcher:

```json
{
  "name": "openclaw-fetch",
  "command": "openclaw-fetch",
  "args": ["-a", "{url}", "--format", "markdown", "--max-chars", "6000"]
}
```

## MCP config

```json
{
  "mcpServers": {
    "openclaw-fetch": {
      "command": "openclaw-fetch",
      "args": ["mcp", "--network", "auto"]
    }
  }
}
```

Tools: `fetch`, `fetch_many`, `crawl`, `search`, `search_fetch`, `compare`,
`deep_fetch`, `find_mirrors`, `collect_documents`, `watch`, `doctor`,
`proxy_status`, `estimate`. See `openclaw_fetch/mcp_server.py`.

## torrc snippet

```
IsolateSOCKSAuth 1
SocksPort 127.0.0.1:9050
ControlPort 127.0.0.1:9051
CookieAuthentication 1
```

## Common invocations

```bash
openclaw-fetch -a http://example.i2p/ --format markdown
openclaw-fetch -t "http://abc.onion" --tor-verify --json
openclaw-fetch --search "linux kernel" -a --search-fetch-top 3 --format jsonl
openclaw-fetch -n http://x.test --selector "h1" --json
openclaw-fetch --tor-doctor
```
