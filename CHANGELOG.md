# Changelog

## 0.10.1

### Safety

- Derive quote freshness from provider timestamps and reject missing, naive, stale, or implausibly future observations.
- Keep trade TICK freshness separate from orderbook freshness and block PAPER/LIVE order paths when the quote is stale.
- Validate market numeric domains, timezone-aware timestamps, request/response symbols, and snapshot currency consistency.
- Disable environment proxy inheritance for default REST and WebSocket clients.
- Preserve one-shot LIVE order submission, accepted-not-filled semantics, existing per-order caps, and no automatic retry.

### Runtime

- Increase WebSocket reconnect backoff across immediate closes and reset it only after subscription acknowledgement.
- Persist post-submit account/open-order reconciliation results and update full portfolio state when available.
- Distinguish actively monitored watchlist alerts from alerts waiting for snapshot/candle data.
- Expose a bounded, allowlisted, in-memory operational transition history without tokens, account identifiers, or provider bodies.

### Quality

- Add Python 3.12/3.13 CI for Ruff and pytest coverage, plus production-source Pyright, Bandit, runtime dependency audit, and package build checks.
