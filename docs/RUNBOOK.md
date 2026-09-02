# VERITAS — Agent Runbook (Codespaces)

The agent runs **unattended** in a GitHub Codespace. The laptop is only a viewer.

## 1. One-time setup
1. Push this repo to GitHub (public, MIT).
2. Repo → **Settings → Secrets and variables → Codespaces** → add:
   - `ALPACA_API_KEY`, `ALPACA_API_SECRET` (paper keys)
   - `ANTHROPIC_API_KEY`
3. Repo → **Code → Codespaces → Create codespace on main** (2-core machine).
4. In the codespace terminal: `uv sync` (devcontainer does this automatically).

## 2. Smoke test (single cycle, no orders)
```bash
uv run python -m veritas.main --mode paper --once --dry-run
```
Expect: JSON out with `"status": "dry_run"` and a full audit trail in `logs/`.

## 3. First real (paper) trade
```bash
uv run python -m veritas.main --mode paper --once
```

## 4. Autonomous session (survives disconnect)
```bash
tmux new -s veritas
uv run python -m veritas.main --mode paper --loop
# Ctrl+B, D to detach. The loop keeps running.
tmux attach -t veritas   # reattach later
```

## 5. Push audit state for the dashboard
```bash
uv run python tools/export_dashboard.py   # writes dashboard/data/*.json
git add dashboard/data && git commit -m "state: update" && git push
```
(GitHub Pages serves the dashboard; the agent's JSON updates it.)

## 6. Kill switch
- Env `VERITAS_KILL_MODE=halt_new` → stop new entries, manage existing.
- Delete the tmux session (`tmux kill-session -t veritas`) → loop stops;
  existing positions remain (check Alpaca dashboard).
- `VERITAS_KILL_MODE=close_all` (default) → daily-loss breach flattens everything.
