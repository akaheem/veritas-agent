# VERITAS — Execution-Aware Options Agent

> An autonomous AI agent that trades short-DTE options credit spreads on Alpaca paper trading — and **audits its own execution** before risking a dollar.

**Built for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai) (lablab.ai × Alpaca, Aug 28 – Sep 4, 2026) · Paper account: `PA3AHDBU6L4I` · Live dashboard: [akaheem.github.io/veritas-agent/dashboard](https://akaheem.github.io/veritas-agent/dashboard/index.html)**

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://python.org)
[![Alpaca MCP](https://img.shields.io/badge/Alpaca-MCP%20Server-orange)](https://github.com/alpacahq/alpaca-mcp-server)
[![Claude](https://img.shields.io/badge/LLM-Claude%20Sonnet%205-6C4CE7)](https://www.anthropic.com)
[![License](https://img.shields.io/badge/License-MIT-green)](#license)

**VERITAS** doesn't just make trades: every proposed trade must survive a gauntlet of deterministic checks that model the documented weaknesses of broker simulators — liquidity-blind fills, inconsistent historical data, fragile multi-leg order management, and LLM numerical hallucination. The LLM proposes. The math disposes.

## Why "execution-aware"?

Alpaca's own documentation and issue tracker acknowledge that paper trading does **not** model market impact, liquidity-constrained fills, latency slippage, or fees — and that real-time vs historical bars can disagree. VERITAS treats these not as bugs to exploit, but as **risks to model and guard against**:

| Documented weakness | VERITAS guardrail |
|---|---|
| Fills ignore real liquidity / NBBO size | Hard position caps (1–5 contracts) + pre-trade slippage haircut — edge must survive conservative assumptions |
| Real-time vs historical bar inconsistency | Single logged snapshot per cycle is the one source of truth for every downstream decision |
| Multi-leg order replacement can fail | Never replace: cancel + fresh idempotent order (`client_order_id`) |
| API 5xx/timeouts leave order state ambiguous | Idempotent retries + 5-minute broker-state reconciliation loop; the tracked `client_order_id` is the exact id submitted |
| Options assignments not delivered via websocket | REST activity polling every cycle |
| LLMs hallucinate numbers | A deterministic validator recomputes credit, max loss, breakeven, and width before any risk gate opens |

Every decision is written to a replayable **JSONL audit log**: snapshot → features → LLM proposal → validator → risk verdicts → order → fill → exit → P&L. The full decision chain for every trade can be replayed and inspected.

## Architecture

```
Market Data (Alpaca) ──► Feature Engine ──► LLM Decision Core (Claude)
                                                   │ TradeProposal (JSON)
                                                   ▼
                                        Deterministic Validator
                                                   │
                                                   ▼
                                        Risk Gate Engine / Kill Switch
                                                   │
                                                   ▼
                                        Execution Reality Check (slippage model)
                                                   │
                                                   ▼
                                        Alpaca MCP Server ──► Paper Execution
                                                   │
                                                   ▼
                                        Reconciliation Loop ──► Position Manager
                                                   │
                                                   ▼
                                        Audit Log (JSONL) ──► P&L + Dashboard
```

## Key features

- **Autonomous loop** — market analysis → opportunity detection → options selection → risk gates → execution → position management → P&L. No human in the loop.
- **Options-native** — trades 0–7 DTE vertical credit spreads (bull put / bear call) on liquid underliers (SPY, QQQ).
- **Risk gates** — non-LLM-overridable: max loss per trade (2%), daily loss kill switch, position count, portfolio heat, liquidity gates (OI, relative spread width), entry time windows, force-close deadline.
- **Execution-reality simulator** — pre-trade conservative slippage model; a trade whose edge dies under pessimistic fills is never sent.
- **Reconciliation engine** — broker state is truth; internal state is a hypothesis that gets re-verified every 5 minutes. An ambiguous empty book is re-read, never trusted.
- **Audit-first** — full JSONL decision-chain logging for every cycle, making results independently reviewable.

## Tech stack

- **Python 3.11** — agent core
- **Alpaca MCP Server** — trade execution (hackathon requirement: MCP or CLI)
- **alpaca-py** — market data
- **Claude API** (claude-sonnet-5) — trade-decision reasoning
- **GitHub Codespaces** — autonomous runtime (survives laptop disconnects)
- **GitHub Pages** — live dashboard demo

## Requirements

- Python 3.11+
- Alpaca paper-trading API keys (`ALPACA_API_KEY`, `ALPACA_API_SECRET`)
- Anthropic API key (`ANTHROPIC_API_KEY`)
- UV package manager (or pip)

## Quickstart

```bash
git clone https://github.com/akaheem/veritas-agent.git
cd veritas-agent
cp .env.example .env   # fill in your keys

uv sync                # or: pip install -r requirements.txt
uv run python -m veritas.main --mode paper --once   # single decision cycle
uv run python -m veritas.main --mode paper          # continuous loop
uv run python tools/preflight.py                    # pre-session health checks
uv run pytest tests/ -q                             # 17 offline tests
```

## Hackathon result & design retrospective

Submitted before the Sep 4, 2026 11:00 AM ET deadline with the paper account above.

During judging-season review, a 26-agent adversarial code audit of the final submission confirmed 12 defects — most of which meant the agent's risk posture was *"abstain by construction"* that day (empty candidate menus rather than wrong trades). All 12 are **fixed** in this repository, each with a regression test:

| # | Defect (as submitted) | Fix |
|---|---|---|
| 1 | MCP layer wrapped API rejections/timeouts as `ok:True` (the server returns errors as normal `isError=False` results) → phantom positions | `broker.call` inspects embedded `error` payloads; timeouts surface as `timeout:True` → UNKNOWN path, never a blind resubmit |
| 2 | Liquidity gate required `volume ≥ 10`, but alpaca-py models have **no volume field** → gate could never pass → zero candidates, ever | Gate disabled by default (`VERITAS_MIN_VOLUME=0`); OI + relative-spread floors carry the liquidity requirement |
| 3 | Chain rows read `c.strike_price`/`c.bid_price` off `OptionsSnapshot`, which nests those under `latest_quote`/`latest_trade` → `AttributeError` per row | Rows built from `latest_quote`/`latest_trade`; contract metadata merged from the contracts endpoint with an OCC-symbol fallback parser |
| 4 | OCC C/P flag read at index `-8` (a strike digit); `-9` is the flag → every contract typed "put" | `_occ_meta()` parses the OCC layout correctly (unit-tested) |
| 5 | OI lookup fetched only the first 500 contracts, unpaginated → most contracts got OI=0 | Paginated to 10 000/page until `next_page_token` is exhausted |
| 6 | `manage()` never persisted `close_accepted` → duplicate close orders every 5-minute tick | Close state persisted before the tag can re-fire; one close order per spread |
| 7 | State machine registered a `client_order_id` different from the one submitted (independent uuid suffixes) → UNKNOWN reconciliation dead code | Caller's coid passed through to `open_credit_spread` and submitted verbatim |
| 8 | `fetch_mark_costs` used the *short* leg's symbol as the lookup key for **both** legs → long-leg mid always None → marks never computed | `_leg_mid(qr, leg_symbol)` resolves each leg by its own symbol |
| 9 | `reconcile()` treated one empty broker read as a flat book → transient MCP error would drop every filled tag | Two consecutive empty reads required before treating the book as flat; drop logic also accepts `submitted`/`close_submitted` vanished legs |
| 10 | `PENDING_SUBMIT → FILLED` transition refused → instantly-filled marketable limits stranded the record | Transition legalized |
| 11 | Preflight's "LLM decision parses" could never fail — API errors degrade to a NO_TRADE fallback proposal that "parses" | Fallback-tagged theses (`[decision-fallback: …]`) now count as FAIL |
| 12 | Dashboard exporter zipped proposal/validation/risk events positionally; risk events exist only for passing cycles → verdicts mispaired across cycles | Events paired by cycle id |

The audit-first thesis cut both ways: the same JSONL log that would have evidenced trades also made every one of these failures diagnosable after the fact — without it, the empty session would have been unexplainable.

## ⚠️ Disclaimer

Educational hackathon project. Paper trading only — **no real money**. Simulated fills do not model market impact, liquidity, or fees; historical performance in a simulator does not imply real-market profitability. Not financial advice.

## License

MIT
