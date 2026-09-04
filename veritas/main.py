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
from .execution import confidence_band, entry_credit_ratio, evaluate_confidence
from .features import build_features
from .models import cycle_id
from .orderstate import OrderState, OrderStateMachine, map_status
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
    """Kill switch evaluated EVERY cycle, independent of the LLM path.

    Master Plan v2 §7 default (halt_new): breach halts NEW entries immediately;
    open positions remain under their own stops + EOD force-close. close_all
    flattens now (used for watchdog safe-mode and explicit overrides).
    """
    account = await asyncio.to_thread(data.trading.get_account)
    try:
        eq, last = float(account.equity), float(account.last_equity)
    except Exception:  # noqa: BLE001
        return False
    pnl = round(eq - last, 2)
    if pnl <= -SETTINGS.max_daily_loss:
        audit.write(cyc, "kill", trigger="daily_loss", pnl=pnl, mode=SETTINGS.kill_switch_mode)
        if SETTINGS.kill_switch_mode == "close_all":
            if pm.broker:
                await pm.broker.cancel_all_working()
            await pm.kill_switch_close_all(f"daily loss {pnl} breached ${SETTINGS.max_daily_loss}")
        return True
    return False


async def _resolve_unknown_orders(broker: McpBroker | None, orders: OrderStateMachine, audit: AuditLog) -> None:
    """UNKNOWN after timeout: query broker by client_order_id. Adopt found
    orders into submitted state; only confirmed absence stays unresolved
    (a fresh idempotent submit is then safe on the next cycle)."""
    if broker is None:
        return
    for coid in orders.unresolved():
        found = await broker.find_order_by_client_id(coid)
        if found:
            st = map_status(str(found.get("status", "")))
            orders.transition(coid, st, "resolved via client_order_id query")
            audit.write("orders", "unknown_resolved", coid=coid, broker_status=str(found.get("status", "")))
        else:
            audit.write("orders", "unknown_still_absent", coid=coid,
                        note="not at broker; safe to resubmit with NEW coid next cycle")
            # PENDING_SUBMIT (pre-submit timeout) may restart; UNKNOWN that the
            # broker has never seen stays UNKNOWN for one more reconciliation.
            rec = orders.orders.get(coid, {})
            if rec.get("state") == OrderState.PENDING_SUBMIT.value:
                orders.transition(coid, OrderState.REJECTED, "never reached broker")


async def run_cycle(audit: AuditLog, data: MarketData, brain: DecisionCore,
                    broker: McpBroker | None, pm: PositionManager,
                    orders: OrderStateMachine, dry_run: bool) -> dict:
    cyc = cycle_id()
    feed = getattr(data, "feed_name", SETTINGS.data_feed)

    # 0) kill switch first — every cycle, regardless of the LLM
    if await _daily_kill_check(audit, data, pm, cyc):
        return {"cycle": cyc, "status": "kill_switch_fired"}

    # 0a) resolve UNKNOWN orders before anything else (never resubmit blind)
    await _resolve_unknown_orders(broker, orders, audit)

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

    # 6) Execution Reality Layer (Master Plan v2 §8): score → band → adaptive price
    spread = report.corrected
    exec_score, exec_factors = evaluate_confidence(spread, feed)
    band = confidence_band(exec_score)
    audit.write(cyc, "execution_reality", score=exec_score, band=band,
                factors=exec_factors, feed=feed)
    if band == "reject":
        # shadow book: valid candidate, risk-worthy, but execution quality too low
        audit.write(cyc, "shadow_reject", legs=[l.symbol for l in spread.legs],
                    score=exec_score, factors=exec_factors)
        return {"cycle": cyc, "status": "rejected_by_execution", "score": exec_score}
    credit_ratio = entry_credit_ratio(exec_score)

    # 7) risk gates (hard limits) — working orders + correlation + confidence
    working_orders = await broker.get_open_veritas_orders() if broker else []
    working_count = len(working_orders)
    working_heat = sum(o.get("adjusted_max_loss", 0.0) for o in working_orders)
    groups: dict[str, float] = {}
    for o in working_orders:
        u = str(o.get("underlier", ""))
        if u:
            groups[u] = groups.get(u, 0.0) + float(o.get("adjusted_max_loss", 0.0))
    for tag, info in pm.open_spreads.items():
        u = str(info.get("spread", {}).get("underlier", ""))
        if u:
            groups[u] = groups.get(u, 0.0) + float(info.get("spread", {}).get("adjusted_max_loss", 0.0))

    n_spread_units = _count_spread_units(snap.positions) + working_count
    verdict = evaluate(
        spread, snap.account, snap.positions, pnl_today,
        unrealized_heat=_option_heat(snap.positions) + working_heat,
        position_units=n_spread_units,
        entry_ok=in_entry_window(),
        execution_confidence=exec_score,
        open_spread_groups=groups,
    )
    audit.write(cyc, "risk", verdict=verdict.model_dump(), working_orders=working_count)
    if verdict.kill_switch and SETTINGS.kill_switch_mode == "close_all" and broker:
        await broker.cancel_all_working()
        await pm.kill_switch_close_all("daily loss limit")
        return {"cycle": cyc, "status": "kill_switch_close_all"}
    if not verdict.approved:
        return {"cycle": cyc, "status": "blocked_by_risk", "why": verdict.failures}

    # 8) execution via MCP (or dry run) — adaptive credit price
    if dry_run or broker is None:
        audit.write(cyc, "execution", action="dry_run_skipped",
                    legs=[l.symbol for l in spread.legs],
                    credit=spread.credit, contracts=spread.contracts,
                    exec_score=exec_score, credit_ratio=credit_ratio)
        return {"cycle": cyc, "status": "dry_run"}

    coid = f"veritas-open-{cyc}-{__import__('uuid').uuid4().hex[:8]}"
    orders.register(coid)
    # pass the registered coid through — the id we track for UNKNOWN
    # reconciliation MUST be the id the broker actually received
    r = await broker.open_credit_spread(spread, idem_tag=cyc, credit_ratio=credit_ratio,
                                        client_order_id=coid)
    if not r.get("ok") and "timeout" in str(r.get("error", "")).lower():
        # UNKNOWN state: order MAY exist — reconciliation loop will resolve by coid
        orders.on_timeout(coid)
        audit.write(cyc, "order_unknown", coid=coid)
        return {"cycle": cyc, "status": "order_unknown", "coid": coid}
    audit.write(cyc, "execution", submit_result=r)
    if r.get("ok"):
        order = r.get("data", {}) if isinstance(r.get("data"), dict) else {}
        orders.transition(coid, map_status(str(order.get("status", ""))), "open submit ok")
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


async def reconcile_loop(pm: PositionManager, broker: McpBroker | None,
                         orders: OrderStateMachine, stop: asyncio.Event) -> None:
    """Every 5 min: reconcile + manage + order-state resolution."""
    while not stop.is_set():
        try:
            marks = await pm.fetch_mark_costs() if broker else {}
            await pm.manage(marks)
            await pm.reconcile()
            if broker:
                await _resolve_unknown_orders(broker, orders, pm.audit)
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
    orders = OrderStateMachine()

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
        recon = asyncio.create_task(reconcile_loop(pm, broker, orders, stop))
        try:
            if args.once:
                out = await run_cycle(audit, data, brain, broker, pm, orders, dry_run=args.dry_run)
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
                        await run_cycle(audit, data, brain, broker, pm, orders, dry_run=args.dry_run)
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
