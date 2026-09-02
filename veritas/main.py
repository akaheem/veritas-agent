"""VERITAS main loop — capture → features → menu → LLM → validate → risk → execute.

Run modes:
  --once      one full cycle, exit (smoke tests)
  --loop      continuous autonomous loop (market-hours aware)
  --dry-run   everything except order submission (default until keys verified)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from .audit import AuditLog
from .broker import McpBroker
from .candidates import build_candidates
from .config import SETTINGS, NY_TZ
from .data import MarketData
from .decision import DecisionCore
from .features import build_features
from .models import cycle_id
from .position import PositionManager
from .risk import daily_pnl, evaluate, should_force_close
from .validator import validate


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="veritas")
    p.add_argument("--mode", choices=["paper"], default="paper")
    p.add_argument("--once", action="store_true", help="run a single cycle")
    p.add_argument("--loop", action="store_true", help="continuous autonomous loop")
    p.add_argument("--dry-run", action="store_true", help="never submit orders")
    return p.parse_args()


async def run_cycle(audit: AuditLog, data: MarketData, brain: DecisionCore,
                    broker: McpBroker | None, pm: PositionManager, dry_run: bool) -> dict:
    cyc = cycle_id()
    # 1) single snapshot — the only source of truth this cycle
    snap = data.capture()
    if snap.account and snap.account.get("error"):
        audit.write(cyc, "abort", reason="account unavailable")
        return {"cycle": cyc, "status": "abort_account"}

    # 2) deterministic features + candidate menu
    feats = build_features(snap)
    candidates = build_candidates(snap)
    audit.write(cyc, "features", features=[f.model_dump() for f in feats])
    audit.write(cyc, "candidates", n=len(candidates),
                summary=[c.model_dump(exclude={"legs"}) for c in candidates])

    # 3) P&L context
    last_eq = (snap.account or {}).get("last_equity", 0.0)
    pnl_today = daily_pnl(snap.account, last_eq)
    audit.write(cyc, "pnl_context", daily_pnl=pnl_today, equity=(snap.account or {}).get("equity"))

    # 4) LLM decision (proposes only)
    proposal = brain.decide(feats, candidates, snap.positions, snap.account, pnl_today)
    audit.write(cyc, "llm_proposal", proposal=proposal.model_dump())

    # 5) deterministic validation (math disposes)
    report = validate(proposal, candidates)
    audit.write(cyc, "validation", report=report.model_dump(exclude_none=True))
    if not report.passed or report.corrected is None:
        return {"cycle": cyc, "status": "no_trade", "why": report.failures or "NO_TRADE"}

    # 6) risk gates (hard limits)
    unrealized_heat = sum(abs(p.get("market_value", 0)) for p in snap.positions if "P" in p["symbol"][-9:])
    verdict = evaluate(report.corrected, snap.account, snap.positions, pnl_today, unrealized_heat)
    audit.write(cyc, "risk", verdict=verdict.model_dump())
    if verdict.kill_switch and SETTINGS.kill_switch_mode == "close_all" and broker:
        await pm.kill_switch_close_all("daily loss limit")
        return {"cycle": cyc, "status": "kill_switch_close_all"}
    if not verdict.approved:
        return {"cycle": cyc, "status": "blocked_by_risk", "why": verdict.failures}

    # 7) execution via MCP (or dry run)
    if dry_run or broker is None:
        audit.write(cyc, "execution", action="dry_run_skipped",
                    legs=[l.symbol for l in report.corrected.legs],
                    credit=report.corrected.credit, contracts=report.corrected.contracts)
        return {"cycle": cyc, "status": "dry_run"}

    r = await broker.open_credit_spread(report.corrected, idem_tag=cyc)
    audit.write(cyc, "execution", submit_result=r)
    if r.get("ok"):
        order = r.get("data", {})
        tag = pm.register_open(report.corrected, str(order.get("id", "unknown")))
        pm.mark_filled(tag, order if isinstance(order, dict) else {})
    return {"cycle": cyc, "status": "submitted" if r.get("ok") else "submit_failed", "result": r}


async def reconcile_loop(pm: PositionManager, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await pm.reconcile()
        except Exception as e:  # noqa: BLE001
            pm.audit.write("reconcile", "error", error=str(e))
        try:
            await asyncio.wait_for(stop.wait(), timeout=SETTINGS.reconcile_seconds)
        except asyncio.TimeoutError:
            pass


async def main_loop(args: argparse.Namespace) -> None:
    audit = AuditLog(SETTINGS.audit_dir)
    data = MarketData(audit)
    brain = DecisionCore()
    pm = PositionManager(audit, broker=None)  # broker attached below

    problems = SETTINGS.validate()
    if problems:
        for p in problems:
            audit.write("boot", "config_problem", problem=p)
        print("CONFIG PROBLEMS:", problems)
        if not args.dry_run:
            return

    async with McpBroker(audit) as broker:
        pm.broker = broker
        stop = asyncio.Event()
        recon = asyncio.create_task(reconcile_loop(pm, stop))
        try:
            if args.once:
                out = await run_cycle(audit, data, brain, broker, pm, dry_run=args.dry_run)
                print(json.dumps(out, indent=2, default=str))
            elif args.loop:
                while True:
                    if should_force_close():
                        await pm.kill_switch_close_all("eod force close")
                        audit.write("loop", "eod_done")
                        # sleep until next session start (coarse)
                        time.sleep(max(60, 3600))
                        continue
                    await run_cycle(audit, data, brain, broker, pm, dry_run=args.dry_run)
                    await asyncio.sleep(SETTINGS.cycle_seconds)
        finally:
            stop.set()
            recon.cancel()


def cli() -> None:
    args = parse_args()
    asyncio.run(main_loop(args))


if __name__ == "__main__":
    cli()
