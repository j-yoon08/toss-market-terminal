# Changelog

## 0.12.0

### First-run onboarding

- Add an interactive `toss-market setup` flow with hidden Client Secret input, OAuth plus one read-only price verification, and no credential values in argv or output.
- Store new credentials atomically at `~/.config/toss-market-terminal/credentials.json` with private directories, exact `0600` file mode, ownership checks, and symlink/hardlink rejection.
- Preserve the secure legacy credential path as a compatibility fallback and add explicit status, migration, replacement, and confirmed removal commands.
- Make bare `toss-market` launch PAPER mode, running setup first only when credentials are missing and the terminal is interactive; non-interactive use fails without blocking for input.
- Securely initialize missing user-state lock directories as `0700` from a trusted existing parent so a completely fresh HOME reaches onboarding without weakening symlink or ownership checks.
- Add a credential-free, network-free `toss-market demo` Textual preview with LIVE disabled.

## 0.11.0

### AI direction support

- Add a dependency-free local k-nearest-neighbour direction classifier using only validated in-memory public candles.
- Show `BUY`, `HOLD`, `SELL`, or `INSUFFICIENT_DATA` as Korean display-only TUI state with model confidence, sample size, walk-forward balanced accuracy, evidence, counterpoints, risks, and invalidation conditions.
- Use horizon-embargoed chronological walk-forward validation and fail closed on stale/degraded data, malformed numbers, inadequate samples or class diversity, and validation below the required floor.
- Keep the model isolated from credentials, accounts, networking, PAPER preview, LIVE plans, and order transport; no AI result can create or submit an order.
- Throttle repeated inference for the same symbol/timeframe/candle while invalidating immediately on a new candle or stale-state transition.

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
