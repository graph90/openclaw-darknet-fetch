# Security

## Anonymity model

- This is a neutral network tool: the agent chooses the network. Clearnet is
  **not** anonymous. Tor/I2P reduce, but do not guarantee, anonymity.
- I2P clearnet goes through **outproxies** (see `--i2p-jump` docs):
  `metadata.i2p_outproxy_used` is set when it happens. Use `-i` for eepsites.
- `IsolateSOCKSAuth` (per-request circuits) only works when the Tor daemon is
  configured for it; `doctor()` reports the honest state (`unverified` otherwise).
- `--tor-verify` fails closed (`ANONYMITY_UNVERIFIED`) when the exit cannot be
  confirmed as Tor.

## No-JS contract

- The tool **never executes page JavaScript**, never loads third-party
  subresources, and strips `<script>` before extraction. A headless renderer
  (`--render`, if added) would be opt-in, clearnet-only, and never implicit
  for `.onion`/`.i2p`.

## No telemetry

- No third-party requests beyond the target + chosen search/mirror backends.
  `--log-jsonl` is local only.

## Reporting

- Open an issue or email the maintainer with a PoC. Do not post onion
  addresses from private investigations in public issues.
