# VERITAS — Agent Runbook (Codespaces)

The agent runs **unattended** in a GitHub Codespace. The laptop is only a viewer.

## 0. Step-by-step: keys → preflight → competition run

### Step 1 — Generate Alpaca paper keys (dev account) — ~5 min
1. Go to https://app.alpaca.markets and log in (or sign up with email — paper-only signup is instant, no brokerage approval needed).
2. Make sure you're in the **Paper Trading** view (toggle at the top).
3. Click your **account number** (top area) or go to **API Keys** → **Generate New Key**.
4. Copy the **Key ID** (`PK...`) and **Secret Key** somewhere safe — the secret is shown ONCE.

### Step 2 — Put keys into Codespace secrets — ~3 min
1. On GitHub, open the repo: `github.com/akaheem/veritas-agent`
2. **Settings → Secrets and variables → Actions** is for Actions; for Codespaces use **Secrets → Codespaces** tab (`Settings → Secrets and variables → Codespaces`).
3. Click **New repository secret**, add each:
   - Name: `ALPACA_API_KEY` · Secret: your Key ID
   - Name: `ALPACA_API_SECRET` · Secret: your Secret Key
   - Name: `ANTHROPIC_API_KEY` · Secret: your Anthropic key (platform.claude.com → API keys)
4. (Existing codespace? Restart it so it picks up new secrets: Code → Codespaces → ⋯ → Restart, or just create a fresh one.)

### Step 3 — Create the Codespace — ~3 min
1. Repo page → green **Code** button → **Codespaces** tab → **Create codespace on main**.
2. Machine: **2-core** (default). Wait for the devcontainer build + `uv sync` (first time ~3-5 min).
3. Verify secrets landed: `env | grep ALPACA` and `env | grep ANTHROPIC` — each should print a value.

### Step 4 — Dev preflight (smoke test) — ~2 min
```bash
uv run python tools/preflight.py
```
Read the PASS/FAIL lines. Everything must PASS except "candidates generated"
(can be legitimately zero outside market hours) and "market clock"
(is_open=false is fine when the market is closed).

### Step 5 — One full dry-run cycle (no orders) — ~1 min
```bash
uv run python -m veritas.main --mode paper --once --dry-run
```
Expect `"status": "dry_run"` (or `no_trade`/`rejected_by_execution`) and a
fresh line in `logs/audit-<today>.jsonl`.

### Step 6 — Fresh $100k COMPETITION account — ~10 min (do Sep 3 pre-market)
1. Back in https://app.alpaca.markets (paper view): click your **paper account number** (top-left) → **Open New Paper Account**.
2. New accounts start at **$100,000.00 by default** — verify on the dashboard.
3. Open the new account's **API Keys** page → generate NEW keys (they are per-account).
4. Update the Codespace secrets from Step 2 with the NEW values (same names).
5. Restart the codespace again so it re-reads secrets.
6. Run the competition preflight:
```bash
uv run python tools/preflight.py --competition
```
   It now enforces: status ACTIVE · options level 3 · equity exactly $100,000.
7. **Record the account ID** (starts `PA...` — shown on the dashboard and in the
   preflight output). You must paste it into the lablab submission form.

### Step 6b — Update an EXISTING codespace after pulling fixes
If fixes were pushed while your codespace was running:
```bash
git pull
uv sync                      # pick up dependency changes
# tmux missing? install once:
sudo apt-get update -qq && sudo apt-get install -y -qq tmux
```
(The devcontainer now installs tmux automatically for NEW codespaces.)

### Step 7 — Launch the autonomous session (tmux) — ~2 min
```bash
uv run python -m veritas.main --mode paper --loop --dry-run   # rehearsal first
# if rehearsal looks clean over 1-2 cycles, the real thing:
tmux new -s veritas
uv run python -m veritas.main --mode paper --loop
# detach: Ctrl+B then D   |   reattach later: tmux attach -t veritas
```
The loop runs the full cycle every 15 min, reconciles every 5 min, force-closes
everything by 15:45 ET, and sleeps until the next open.

### Step 8 — Keep the Codespace awake
- GitHub Settings (your profile, not the repo) → **Codespaces** → set **Default idle timeout** to **240 minutes**.
- Keep a second terminal pane with `tail -f logs/audit-$(date +%F).jsonl` — terminal output counts as presence and resets the idle timer.
- If the machine still stops: Code → Codespaces → restart, then `tmux attach -t veritas` — state recovers from broker + disk (that's the tested recovery path).

### Step 9 — Export dashboard state (whenever you like)
```bash
uv run python tools/export_dashboard.py
git add dashboard/data && git commit -m "state: update" && git push
```
Dashboard: https://akaheem.github.io/veritas-agent/dashboard/index.html

---

## 1. One-time setup (already done in repo)
Devcontainer (Python 3.11 + uv) is committed; `gh` CLI feature included.

## 2. Smoke test shortcuts
```bash
uv run pytest tests/ -q                 # 12 offline tests
uv run python tools/preflight.py        # live smoke (keys required)
```

## 3. Kill switch
- `VERITAS_KILL_MODE=halt_new` (default): daily-loss breach stops NEW entries; open spreads still run their stops + EOD flatten.
- `VERITAS_KILL_MODE=close_all`: breach flattens everything immediately.
- `tmux kill-session -t veritas` stops the loop; positions remain (check the Alpaca dashboard).

## 4. Failure recovery
- Restart process → state rebuilt from broker + `data/open_spreads.json` + `data/order_states.json`; unknown orders resolved by `client_order_id` — no duplicate orders.
- Watchdog Actions workflow only reconciles/dry-runs; it never trades.

## 5. First real MLeg round-trip (once, on the fresh account, before the session)
```bash
# inside the codespace, market hours:
uv run python -m veritas.main --mode paper --once     # single real cycle
# then verify on app.alpaca.markets: order appears, position appears
# then let the agent's own exits (50% profit / 200% stop / 15:45 ET) close it,
# or close manually via the dashboard and confirm the reverse-mleg fills.
```

## 6. Push audit state for the dashboard
(See Step 9.)

## Original runbook (condensed)
- Secrets in Codespace env vars only — never in the repo (`.env` gitignored).
- `--dry-run` never submits anything, even with keys set.

