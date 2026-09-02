# VERITAS — One-Page Write-Up

**VERITAS** is an autonomous AI trading agent that sells short-DTE options credit spreads on Alpaca's paper environment — and audits its own execution before risking a dollar. Every trade must survive a gauntlet of deterministic checks that model the documented weaknesses of broker simulators and of LLMs themselves. The LLM proposes; the math disposes.

## AI Logic

Each cycle (~15 min during market hours) the agent:

1. **Captures one market snapshot** (equity quotes, 40-day bars, full SPY/QQQ options chains with greeks/IV from Alpaca's data API). The snapshot is the single source of truth for the whole cycle — logged once, never re-fetched, so no decision is ever made on mixed data.
2. **Computes deterministic features** (code, not AI): EMA20/50 trend, RSI-14, 20-day realized vol vs ATM implied vol (IV/RV ratio), intraday range. These select the regime-appropriate spread kind (bull put in up/flat, bear call in down).
3. **Builds a deterministic candidate menu**: for each underlier, the best liquid 0–7 DTE vertical credit spread near the 0.30-delta short strike — with credit, width, max loss, breakeven, edge ratio and a conservative post-slippage version of every number computed by code from the snapshot's bid/ask.
4. **Claude (claude-sonnet-5) decides** — and it may only pick a menu item or abstain (`NO_TRADE` is always available). It writes a falsifiable thesis and a confidence. It cannot invent strikes or prices; custom legs are structurally rejected.
5. **A deterministic validator recomputes every number** from raw leg quotes before anything proceeds: credit > 0, edge ratio ≥ 18% floor, credit must survive the execution-reality haircut, DTE within window, contracts capped at 5. Hallucinated economics cannot pass.

## Risk Gates

Hard limits, enforced in code, not overridable by the LLM:

| Gate | Limit |
|---|---|
| Max loss per trade | $2,000 (2% of $100k) |
| Daily loss → kill switch | $2,000 — flattens all positions |
| Open positions | ≤ 4 spreads |
| Portfolio heat | Σ max loss ≤ 8% of equity |
| Contracts per leg | ≤ 5 (liquidity-blind paper fills) |
| Liquidity | spread ≤ 30% of mid, OI ≥ 100, vol ≥ 10, both legs |
| Entry window | 10:00–15:00 ET only |
| Execution reality | edge must survive a slippage haircut (10% of credit + $0.01/leg) |
| Expiry protection | 0DTE force-close 15:30 ET, everything flat 15:45 ET (never hold shorts into expiry) |

Exits: buy back at 50% of credit (profit target) or 200% of credit (stop), plus EOD force-close.

## Alpaca Infrastructure Implementation

- **Alpaca MCP Server** (`alpaca-mcp-server` v2.3.1) is the execution path: the agent is a programmatic MCP client (official MCP Python SDK, stdio transport, explicit env allow-list) holding one session for the whole loop. `place_option_order` submits multi-leg (`mleg`) limit orders — net credit as a negative limit price, per-leg `position_intent`; closes are a single reverse-mleg order.
- **alpaca-py** provides market data (stock bars/quotes, option chains/snapshots).
- **Idempotency & reconciliation**: every order carries a unique `client_order_id` (safe retry after timeouts); orders are never replaced (cancel + fresh — `replace mleg` is hard-disabled by Alpaca); a 5-minute reconciliation loop polls broker positions/activities so **broker state is truth**, and catches assignment events that Alpaca does not stream over websockets.
- **Resilience**: runs in a GitHub Codespace inside tmux; a GitHub Actions watchdog re-runs reconciliation and a recovery cycle if the codespace dies.
- **Auditability**: every cycle writes a JSONL audit trail — snapshot → features → LLM proposal → validation → risk verdicts → order → fill → exit → P&L — making every trade independently replayable. Live dashboard: https://akaheem.github.io/veritas-agent/dashboard/index.html

*Paper trading only. Simulated fills do not model market impact, liquidity, or fees; not financial advice.*
