# VERITAS — Execution-Aware Options Agent

> An autonomous AI agent that trades short-DTE options credit spreads on Alpaca paper trading — and **audits its own execution** before risking a dollar.

**VERITAS** is built for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai) (lablab.ai × Alpaca, Aug 28 – Sep 4, 2026). It doesn't just make trades: every proposed trade must survive a gauntlet of deterministic checks that model the documented weaknesses of broker simulators — liquidity-blind fills, inconsistent historical data, fragile multi-leg order management, and LLM numerical hallucination. The LLM proposes. The math disposes.

## Why "execution-aware"?

Alpaca's own documentation and issue tracker acknowledge that paper trading does **not** model market impact, liquidity-constrained fills, latency slippage, or fees — and that real-time vs historical bars can disagree. VERITAS treats these not as bugs to exploit, but as **risks to model and guard against**:

| Documented weakness | VERITAS guardrail |
|---|---|
| Fills ignore real liquidity / NBBO size | Hard position caps (1–5 contracts) + pre-trade slippage haircut — edge must survive conservative assumptions |
| Real-time vs historical bar inconsistency | Single logged snapshot per cycle is the one source of truth for every downstream decision |
| Multi-leg order replacement can fail | Never replace: cancel + fresh idempotent order (`client_order_id`) |
| API 5xx/timeouts leave order state ambiguous | Idempotent retries + 5-minute broker-state reconciliation loop |
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
- **Risk gates** — non-LLM-overridable: max loss per trade (2%), daily loss kill switch, position count, portfolio heat, liquidity gates (OI, volume, spread width), entry time windows, force-close deadline.
- **Execution-reality simulator** — pre-trade conservative slippage model; a trade whose edge dies under pessimistic fills is never sent.
- **Reconciliation engine** — broker state is truth; internal state is a hypothesis that gets re-verified every 5 minutes.
- **Audit-first** — full JSONL decision-chain logging for every cycle, making results independently reviewable.

## Tech stack

- **Python 3.11** — agent core
- **Alpaca MCP Server** — trade execution (hackathon requirement: MCP or CLI)
- **alpaca-py** — market data
- **Claude API** — trade-decision reasoning
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
```

## ⚠️ Disclaimer

Educational hackathon project. Paper trading only — **no real money**. Simulated fills do not model market impact, liquidity, or fees; historical performance in a simulator does not imply real-market profitability. Not financial advice.

## License

MIT
