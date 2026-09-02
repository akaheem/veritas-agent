"""VERITAS main loop — capture → features → menu → LLM → validate → risk → execute.

Run modes:
  --once      one full cycle, exit (smoke tests)
  --loop      continuous autonomous loop (market-hours aware)
  --dry-run   everything except order submission (default until keys verified)

Fixes the CONFIRMED critical findings from the adversarial review:
- kill-switch check hoisted BEFORE the LLM path (runs every cycle, even NO_TRADE)
- working-order awareness: open orders count toward position cap and heat
- manage() (profit-take/stop/force-close) called every cycle + reconcile loop
- working veritas orders cancelled on kill/EOD before closing positions
- --loop guarded against transient exceptions (no more death on one error)
- EOD sleep replaced by an event-loop-friendly asyncio wait synced to next open
- data capture + LLM call wrapped in to_thread so the reconcile task still runs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .audit import AuditLog
from .broker import McpBroker
from .candidates import build_candidates
from .config import NY_TZ, SETTINGS
from .data import MarketData
from .decision import DecisionCore
from .features import build_features
from .models import cycle_id
from .position import PositionManager
from .risk import daily_pnl, evaluate, in_entry_window, should_force_close
from .validator import validate

log = logging.getLogger("veritas.main")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="veritas")
    p.add_argument("--mode", choices=["paper"], default="paper")
    p.add_argument("--once", action="store_true", help="run a single cycle")
    p.add_argument("--loop", action="store_true", help="continuous autonomous loop")
    p.add_argument("--dry-run", action="store_true", help="never submit orders")
    return p.parse_args()


async def _daily_kill_check(audit: AuditLog, data: MarketData, pm: PositionManager, cyc: str) -> bool:
    """Kill switch evaluated EVERY cycle, independent of the LLM path."""
    account = await asyncio.to_thread(data.trading.get_account)
    try:
        eq, last = float(account.equity), float(account.last_equity)
    except Exception:  # noqa: BLE001
        return False
    pnl = round(eq - last, 2)
    if pnl <= -SETTINGS.max_daily_loss:
        audit.write(cyc, "kill", trigger="daily_loss", pnl=pnl)
        if SETTINGS.kill_switch_mode == "close_all":
            await pm.broker.cancel_all_working() if pm.broker else None
            await pm.kill_switch_close_all(f"daily loss {pnl} breached ${SETTINGS.max_daily_loss}")
        return True
    return False


async def run_cycle(audit: AuditLog, data: MarketData, brain: DecisionCore,
                    broker: McpBroker | None, pm: PositionManager, dry_run: bool) -> dict:
    cyc = cycle_id()
    # 0) kill switch first — every cycle, regardless of the LLM
    if await _daily_kill_check(audit, data, pm, cyc):
        return {"cycle": cyc, "status": "kill_switch_fired"}

    # 0b) position management (profit-take / stop / force-close) every cycle
    marks = await pm.fetch_mark_costs() if broker else {}
    await pm.manage(marks)

    # 1) single snapshot — the only source of truth this cycle (sync SDK → thread)
    snap = await asyncio.to_thread(data.capture)
    if snap.account and snap.account.get("error"):
        audit.write(cyc, "abort", reason="account unavailable")
        return {"cycle": cyc, "status": "abort_account"}

    # 1b) reconcile against broker truth: adopt unknowns, drop confirmed closes
    await pm.reconcile(snap.positions)

    # 2) deterministic features + candidate menu (pure CPU)
    feats = build_features(snap)
    candidates = build_candidates(snap)
    audit.write(cyc, "features", features=[f.model_dump() for f in feats])
    audit.write(cyc, "candidates", n=len(candidates),
                summary=[c.model_dump(exclude={"legs"}) for c in candidates])

    # 3) P&L context
    last_eq = (snap.account or {}).get("last_equity", 0.0)
    pnl_today = daily_pnl(snap.account, last_eq)
    audit.write(cyc, "pnl_context", daily_pnl=pnl_today, equity=(snap.account or {}).get("equity"))

    # 3b) working orders count as positions (gates must see accepted-but-unfilled risk)
    working_orders = await broker.get_open_veritas_orders() if broker else []
    working_count = len(working_orders)
    working_heat = sum(o.get("adjusted_max_loss", 0.0) for o in working_orders)

    # 4) LLM decision (proposes only) — sync HTTP → thread
    proposal = await asyncio.to_thread(
        brain.decide, feats, candidates, snap.positions, snap.account, pnl_today
    )
    audit.write(cyc, "llm_proposal", proposal=proposal.model_dump())

    # 5) deterministic validation (math disposes)
    report = validate(proposal, candidates)
    audit.write(cyc, "validation", report=report.model_dump(exclude_none=True))
    if not report.passed or report.corrected is None:
        return {"cycle": cyc, "status": "no_trade", "why": report.failures or "NO_TRADE"}

    # 6) risk gates (hard limits) — include working-order risk
    spread = report.corrected
    filled_leg_syms = {p.get("symbol") for p in snap.positions}
    opt_positions = [p for p in snap.positions if any(leg.symbol in filled_leg_syms for leg in [])]
    n_spread_units = _count_spread_units(snap.positions) + working_count
    verdict = evaluate(
        spread, snap.account, snap.positions, pnl_today,
        unrealized_heat=_option_heat(snap.positions) + working_heat,
        position_units=n_spread_units,
        entry_ok=in_entry_window(),
    )
    audit.write(cyc, "risk", verdict=verdict.model_dump(), working_orders=working_count)
    if verdict.kill_switch and SETTINGS.kill_switch_mode == "close_all" and broker:
        await broker.cancel_all_working()
        await pm.kill_switch_close_all("daily loss limit")
        return {"cycle": cyc, "status": "kill_switch_close_all"}
    if not verdict.approved:
        return {"cycle": cyc, "status": "blocked_by_risk", "why": verdict.failures}

    # 7) execution via MCP (or dry run)
    if dry_run or broker is None:
        audit.write(cyc, "execution", action="dry_run_skipped",
                    legs=[l.symbol for l in spread.legs],
                    credit=spread.credit, contracts=spread.contracts)
        return {"cycle": cyc, "status": "dry_run"}

    r = await broker.open_credit_spread(spread, idem_tag=cyc)
    audit.write(cyc, "execution", submit_result=r)
    if r.get("ok"):
        order = r.get("data", {}) if isinstance(r.get("data"), dict) else {}
        order_id = str(order.get("id", "unknown"))
        tag = pm.register_open(spread, order_id)
        if broker:
            await broker.remember_working(order_id, tag, spread)
        status = str(order.get("status", ""))
        if status in ("filled",):
            pm.mark_filled(tag, order)
    return {"cycle": cyc, "status": "submitted" if r.get("ok") else "submit_failed", "result": r}


def _count_spread_units(positions: list[dict]) -> int:
    """Count distinct option position groups (approx: unique OCC roots+expiry)."""
    keys = set()
    for p in positions:
        sym = str(p.get("symbol", ""))
        if len(sym) > 15 and (sym[-9] in ("P", "C")):
            keys.add(sym[-15:])  # root+date+c/p block approximates the spread group
    return max(1, len(keys) // 2) if keys else 0


def _option_heat(positions: list[dict]) -> float:
    """Heat contribution of open option positions: max-loss proxy = abs(market_value)."""
    total = 0.0
    for p in positions:
        sym = str(p.get("symbol", ""))
        if len(sym) > 15 and sym[-9] in ("P", "C"):
            total += abs(float(p.get("market_value", 0) or 0))
    return total


async def reconcile_loop(pm: PositionManager, broker: McpBroker | None, stop: asyncio.Event) -> None:
    """Every 5 min: reconcile + manage — survives cycles, catches 0DTE deadlines."""
    while not stop.is_set():
        try:
            marks = await pm.fetch_mark_costs() if broker else {}
            await pm.manage(marks)
            await pm.reconcile()
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile error: %s", e)
            pm.audit.write("reconcile", "error", error=str(e))
        try:
            await asyncio.wait_for(stop.wait(), timeout=SETTINGS.reconcile_seconds)
        except asyncio.TimeoutError:
            pass


def _seconds_until_next_open() -> float:
    now = datetime.now(ZoneInfo(NY_TZ))
    target = now.replace(hour=9, minute=35, second=0, microsecond=0)
    while target <= now or target.weekday() >= 5:
        target += timedelta(days=1)
        target = target.replace(hour=9, minute=35)
    return (target - now).total_seconds()


async def main_loop(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
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
        recon = asyncio.create_task(reconcile_loop(pm, broker, stop))
        try:
            if args.once:
                out = await run_cycle(audit, data, brain, broker, pm, dry_run=args.dry_run)
                print(json.dumps(out, indent=2, default=str))
            elif args.loop:
                while not stop.is_set():
                    try:
                        if should_force_close():
                            await broker.cancel_all_working()
                            await pm.kill_switch_close_all("eod force close")
                            audit.write("loop", "eod_done")
                            wait_s = _seconds_until_next_open()
                            log.info("EOD done — sleeping %.0f min until next open", wait_s / 60)
                            await asyncio.sleep(min(wait_s, 3600))
                            continue
                        await run_cycle(audit, data, brain, broker, pm, dry_run=args.dry_run)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001 — one bad cycle must not kill the run
                        log.exception("cycle error (loop continues)")
                        audit.write("loop", "cycle_error", error=f"{type(e).__name__}: {e}")
                        await asyncio.sleep(30)
                    await asyncio.sleep(SETTINGS.cycle_seconds)
        finally:
            stop.set()
            recon.cancel()


def cli() -> None:
    args = parse_args()
    asyncio.run(main_loop(args))


if __name__ == "__main__":
    cli()
