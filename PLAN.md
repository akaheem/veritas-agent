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
→ M8/M9: repo + dashboard LIVE. Remaining: (1) user runs agent in Codespace with their Alpaca paper keys (RUNBOOK §2 smoke test), (2) create FRESH $100k competition account Sep 3 pre-market, run autonomous session, (3) one-pager + slides + video + social posts.

## VERIFIED FACTS (workflow research, Sep 2 2026 — all with sources)
- Deadline: **Sep 4, 11:00 AM EDT** (event page) — NOT the usual lablab Sunday 23:59 CEST. Submit ≥6h early for badge. Final trading window: Fri 9:30–11:00 ET only.
- MCP server: `alpaca-mcp-server` v2.3.1 (PyPI), 72 tools, `place_option_order` does mleg (legs array, position_intent, negative limit=credit, issue #97 only bites stringifying clients). Close = reverse mleg with *_to_close. PATCH replace hard-disabled (403) — cancel+new only.
- Options on paper: enabled by default, fresh accounts show level 3; verify status==ACTIVE + levels==3 on day 0. New paper accounts default to exactly $100k. Up to ~3 paper accounts; delete+create for fresh one.
- Data: free tier = indicative feed (15-min delayed trades) — submit limit at ≤85–90% of mid credit (entry_credit_buffer=0.85). OI lives on contracts endpoint, not snapshots.
- Mleg payload: order_class=mleg, qty=strategy units, limit_price positive=debit/NEGATIVE=credit, GCD-reduced ratio_qty, position_intent REQUIRED. Closing per-leg 403s — always close via one reverse mleg.
- Paper hazards: NTAs next-day only; pin-risk liquidation can orphan a leg (alpaca-py #774); XSP settlement bug. → force_close 15:30/15:45 ET, never hold shorts into expiry.
- Codespaces: 2-core fits free 120 core-hrs for 16–24h; idle timeout max 240 min (set it); tmux survives disconnect NOT machine-stop; terminal I/O is the keep-alive; Actions cron = recovery executor (watchdog.yml included).
- Claude: use `claude-sonnet-5` — it REJECTS non-default temperature (omitted in decision.py); structured outputs via output_config.format; ~$0.009/decision.

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

### M0 — Foundations — DEVELOPED
- [x] Public GitHub repo (https://github.com/akaheem/veritas-agent), MIT license
- [x] Devcontainer (Python 3.11) — Codespace creation is user's 5-minute step (RUNBOOK §1)
- [x] Alpaca keys — NOT YET PROVIDED (user must generate; .env.example ready)
- [x] MCP integration implemented (veritas/broker.py, MCP SDK v2 stdio client) — end-to-end verify pending keys
- [x] LLM integration (claude-sonnet-5, temp-omitted per Sonnet 5 constraint)
- [x] Secrets: .env gitignored + sensitive-info/ gitignored; Codespace secrets step in RUNBOOK

### M1 — Market Data + Feature Engine — DEVELOPED
- [x] Bars/quotes/chains via alpaca-py (veritas/data.py) — single snapshot/cycle source of truth
- [x] Features: EMA trend, RSI14, RV20 vs IV, day range (veritas/features.py)
- [ ] First live snapshot with real keys (pending user keys)

### M2 — LLM Decision Core + Deterministic Validator — DEVELOPED
- [x] Menu-only policy prompt + TradeProposal JSON (veritas/decision.py)
- [x] Validator recomputes credit/max-loss/breakeven/width, caps contracts, rejects custom legs (veritas/validator.py)
- [x] 12 offline tests green (tests/test_spine.py: LLM-hallucination rejection verified)

### M3 — Risk Gate Engine + Kill Switch — DEVELOPED
- [x] All gates implemented (veritas/risk.py): per-trade $2k, daily $2k kill, 4 positions, 8% heat, liquidity, DTE 0–7, entry window, contract cap
- [x] Execution-reality haircut in candidate + validator (survives-haircut gate)

### M4 — Execution Layer via Alpaca MCP — DEVELOPED
- [x] MCP client w/ explicit env allow-list, backoff retries, tool-error handling (veritas/broker.py)
- [x] Idempotent client_order_id; mleg open at 85% of mid credit; reverse-mleg close
- [x] No order replacement anywhere (cancel+new policy per 403 "replace mleg disabled")
- [x] Watchdog Actions workflow (recovery executor, halt_new mode)
- [ ] First real MCP round-trip (pending keys)

### M5 — Position Manager — DEVELOPED
- [x] Profit-take 50% / stop 200% / 0DTE 15:30 / all 15:45 force-close (veritas/position.py)
- [x] Activities polling (assignment NTAs) + broker-is-truth reconciliation

### M6 — Audit Log + P&L Engine — DEVELOPED
- [x] JSONL per-day audit trail, all 9 stages (veritas/audit.py)
- [x] Dashboard exporter (tools/export_dashboard.py)

### M7 — Dashboard + Demo URL — DEVELOPED
- [x] Static dashboard (dashboard/index.html): P&L tiles, decision-chain table, kill-switch visibility
- [x] LIVE: https://akaheem.github.io/veritas-agent/dashboard/index.html
- [ ] User's custom UI design still pending — swap-in when provided

### M8 — Fresh $100k Competition Account + Autonomous Run — TODO
- [ ] User: create new paper account (defaults to exactly $100k), new keys, verify ACTIVE + options level 3
- [ ] Smoke test → autonomous tmux run Sep 3 + Fri 9:30–11:00 ET
- [ ] Account ID recorded

### M9 — Submission Package — DEVELOPING
- [x] One-pager (docs/ONE-PAGER.md) — DEVELOPED
- [x] Social posts ×5 (X+LinkedIn), video script 9 scenes, 10-slide deck, cover SVG — generated in submission/materials.json
- [x] Cover image at assets/veritas_cover.svg
- [ ] User: post social #1–2 TODAY (window closing)
- [ ] Video recording + slides build (script + content ready)
- [ ] lablab.ai draft submission (title/descriptions/tags in materials.json) — deadline Sep 4 11:00 EDT

---

## TIMELINE
- **Sep 2 (today, done):** M0→M7 built, tested, pushed, dashboard live. **USER ACTIONS TONIGHT: add keys, run RUNBOOK §2 smoke test, post social #1–2.**
- **Sep 3:** fresh $100k account pre-market → autonomous tmux session full day → M9 drafts (one-pager, slides, video).
- **Sep 4:** agent trades 9:30–11:00 ET only (deadline 11:00 EDT) · final dashboard export · SUBMIT EARLY (badge needs ≥6h buffer — target 05:00 EDT).

## OPEN QUESTIONS (demand from user)
1. Alpaca paper keys (dev) — RUNBOOK §1.2 2. Anthropic key — where to put it 3. UI design file (still owed) 4. Confirm solo vs team
