# VERITAS — Execution-Aware Options Agent
## Master Progress Tracker · Alpaca AI Trading Agents Hackathon (lablab.ai × Alpaca)

**Hackathon window:** Aug 28 – Sep 4, 2026 · **Today:** Sep 2, 2026 · **Deadline:** Sep 4 (exact time TBD — CONFIRM)
**Strategy:** Short-DTE (0–7d) credit spreads on SPY/QQQ, defined risk, fast P&L cycles
**Runtime:** GitHub Codespaces (2-core, tmux-detached agent loop) — local machine is thin client only

---

## HOW TO USE THIS FILE (after a disconnect)
- Status tokens: `DEVELOPED` (done + verified) · `DEVELOPING` (in progress right now) · `TODO`
- `RESUME FROM` always points to the exact next action. Update it EVERY time work stops.
- Mirrored in session task tracker as tasks M0–M9.

## RESUME FROM
→ M0: scaffold repo + devcontainer while waiting on user info (Alpaca keys, LLM key, GitHub username, deadline time, UI design).

---

## DECISIONS LOCKED
1. **Concept:** VERITAS — an options agent that audits its own execution. It trusts no one: not the paper simulator, not the broker, not its own LLM.
2. **Core requirement mapping:** autonomous agent (full decide→execute loop, no human) · options mandatory (credit spreads) · Alpaca MCP Server as execution path (CLI as ops backup) · paper trading on fresh dedicated $100k account for judging.
3. **Principles:** LLM proposes, math disposes · broker state is truth, memory is hypothesis · identify→measure→guard→document (never exploit simulator flaws).
4. **Weak-point → guardrail map** (this is the creativity pitch, reuse in one-pager):
   - No market impact/liquidity in fills → execution-reality simulator: conservative slippage haircut pre-trade; edge must survive it
   - Fills ignore NBBO size → hard cap 1–5 contracts
   - Real-time vs historical bar mismatch → one snapshot per cycle, logged once, single source of truth
   - Multi-leg order-replacement bug → never replace orders; cancel + fresh idempotent order
   - 502/timeouts ambiguity → client_order_id idempotency + 5-min reconciliation loop
   - Assignments not on websocket → REST activity polling every cycle
   - LLM numerical hallucination → deterministic validator recomputes credit/max-loss/breakeven pre-gate
   - Reproducibility gap (survey: 1/19 model costs) → full JSONL audit log, replayable decision chains
5. **Dashboard:** static site (GitHub Pages/Vercel) reading state.json + audit summary committed by agent → survives disconnects, free hosting.

---

## MILESTONES

### M0 — Foundations — DEVELOPING
- [ ] Public GitHub repo, MIT license (requirement: original + MIT-compliant)
- [ ] Devcontainer (Python 3.11) + Codespace on 2-core
- [ ] Alpaca DEV paper account keys (dev may use any paper account; judging needs fresh one — M8)
- [ ] Alpaca MCP Server installed in Codespace; verify one tool call (get account) end-to-end
- [ ] LLM key configured (Claude API default; Featherless only if chasing partner prize)
- [ ] Secrets in Codespace env vars — never in repo

### M1 — Market Data + Feature Engine — TODO
- [ ] Bars (1m/5m) + quotes for SPY/QQQ via Alpaca data API
- [ ] Options chain snapshot: strikes, bid/ask, OI, volume, IV, greeks
- [ ] Deterministic features: IV rank, realized-vs-implied vol, EMA trend, RSI, intraday range, spread quality
- [ ] Snapshot logger (single source of truth rule)

### M2 — LLM Decision Core + Deterministic Validator — TODO
- [ ] Structured prompt: market state → TradeProposal JSON (strategy, legs, size, thesis)
- [ ] Deterministic validator recomputes credit, max loss, breakeven, width; rejects hallucinated numbers
- [ ] Normalized TradeProposal output object

### M3 — Risk Gate Engine + Kill Switch — TODO (hard limits, NOT LLM-overridable)
- [ ] Max loss/trade: $2,000 (2% of $100k)
- [ ] Max daily loss: $2,000 → kill switch, halt new entries
- [ ] Max 4 open positions; max portfolio heat 8%
- [ ] Liquidity gates: spread ≤ 30% of mid, OI ≥ 100, volume ≥ 10
- [ ] DTE window 0–7; entry window 10:00–15:00 ET
- [ ] Execution-reality check: edge must survive slippage haircut

### M4 — Execution Layer via Alpaca MCP — TODO
- [ ] Entries/exits routed through MCP server
- [ ] client_order_id idempotency; retry with backoff on 5xx/timeout; order-status polling
- [ ] 5-min reconciliation: broker positions/orders vs internal state; auto-heal + log
- [ ] Multi-leg spread orders (MLeg); NO order replacement — cancel + fresh
- [ ] (Optional hardening) GitHub Actions cron backup runner

### M5 — Position Manager — TODO
- [ ] Exits: 50% credit profit target · 200% credit stop · 15:45 ET force-close
- [ ] REST polling of activities/assignments every cycle
- [ ] Kill-switch behaviors: halt-new / close-all modes

### M6 — Audit Log + P&L Engine — TODO
- [ ] JSONL events: snapshot → features → LLM output → validator → risk verdicts → order → fill → exit → P&L
- [ ] Daily summary generator (feeds one-pager, slides, dashboard)

### M7 — Dashboard + Demo URL — TODO
- [ ] Adopt user's UI design (arriving — adapt, don't redesign)
- [ ] Static dashboard on GitHub Pages/Vercel: live P&L, open positions, decision chains, risk-gate status, kill switch
- [ ] Public demo URL recorded for submission

### M8 — Fresh $100k Competition Account + Autonomous Run — TODO
- [ ] Create NEW paper account dedicated to hackathon (reused accounts = disqualified)
- [ ] Verify $100,000 starting balance; dedicated keys
- [ ] Pre-market smoke test → autonomous run Sep 3–4 full sessions
- [ ] Account ID recorded for submission

### M9 — Submission Package — TODO
- [ ] One-pager: AI logic · risk gates · Alpaca implementation (3 mandated sections)
- [ ] Slides · video presentation · cover image
- [ ] ≤5 social posts (X/LinkedIn, tag @lablabai @AlpacaHQ) — must post DURING hackathon
- [ ] lablab.ai submission: title, short/long description, tags, repo, demo URL, account ID

---

## TIMELINE
- **Sep 2 (today):** M0→M6 MVP path; smoke-test trade on DEV account
- **Sep 3:** M7 dashboard live; M8 fresh account pre-market + full autonomous session; M9 drafts; social post #1–2
- **Sep 4:** monitor run; finish M9; SUBMIT EARLY (deadline time TBD)

## OPEN QUESTIONS (demand from user)
1. Alpaca API keys status (dev account exists?) 2. LLM key (Anthropic / Featherless?) 3. GitHub username/repo name
4. Exact deadline time + timezone 5. UI design file 6. Team size / roles
