"""Position manager — exits, force-close, assignment polling, reconciliation.

Exits (on credit received):
- profit take at 50% of credit
- stop at 200% of credit (buy back)
- force-close 0DTE and everything by 15:55 ET

Reconciliation (5 min): broker positions vs internal state; internal memory is
a hypothesis, broker is truth. Assignment events surface via activities polling
(documented: assignments NOT delivered on websocket).
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .audit import AuditLog
from .broker import McpBroker
from .config import NY_TZ, SETTINGS
from .models import SpreadCandidate, utcnow


def _et_now() -> datetime:
    return datetime.now(ZoneInfo(NY_TZ))


class PositionManager:
    def __init__(self, audit: AuditLog, broker: McpBroker) -> None:
        self.audit = audit
        self.broker = broker
        # internal hypothesis of open spreads: tag -> {spread, entry_credit, opened_at, order_id}
        self.open_spreads: dict[str, dict] = {}

    def register_open(self, spread: SpreadCandidate, order_id: str) -> str:
        tag = f"{spread.underlier}-{spread.kind}-{utcnow().strftime('%H%M%S')}"
        self.open_spreads[tag] = {
            "spread": spread,
            "entry_credit": spread.credit,
            "opened_at": utcnow().isoformat(),
            "order_id": order_id,
        }
        return tag

    def mark_filled(self, tag: str, order: dict) -> None:
        if tag in self.open_spreads:
            self.open_spreads[tag]["order_id"] = order.get("id", self.open_spreads[tag]["order_id"])
            self.open_spreads[tag]["status"] = "filled"

    async def manage(self, mark_prices: dict[str, float]) -> list[dict]:
        """Check exits for each open spread. Returns list of close actions taken."""
        actions = []
        et = _et_now()
        force_all = (et.hour, et.minute) >= tuple(map(int, SETTINGS.force_close_all_et.split(":")))
        for tag, info in list(self.open_spreads.items()):
            spread: SpreadCandidate = info["spread"]
            # current mark from latest quotes of both legs
            cur_cost = mark_prices.get(tag)
            if cur_cost is None:
                continue
            credit = info["entry_credit"]
            pct_of_credit = cur_cost / credit if credit > 0 else 9.9
            reason = None
            if pct_of_credit >= SETTINGS.stop_loss_mult:
                reason = f"stop: cost {pct_of_credit:.0%} of credit"
            elif pct_of_credit <= SETTINGS.profit_take_pct:
                reason = f"profit take: cost {pct_of_credit:.0%} of credit"
            elif force_all:
                # pin-risk guard: never hold spreads into the close (alpaca-py #774)
                reason = "force close before market close (pin-risk guard)"
            elif spread.dte == 0 and (et.hour, et.minute) >= tuple(
                map(int, SETTINGS.force_close_et.split(":"))
            ):
                reason = "force close 0DTE"
            if reason:
                # closing debit: willing to pay up to stop level (or parity for forced close)
                max_debit = credit * (SETTINGS.stop_loss_mult if not force_all else 10.0)
                r = await self.broker.close_credit_spread(spread, max_debit, idem_tag=tag)
                self.audit.write("position", "close_submitted", tag=tag, reason=reason, result=r)
                if r.get("ok"):
                    actions.append({"tag": tag, "reason": reason})
                    self.open_spreads.pop(tag, None)
        return actions

    async def reconcile(self) -> dict:
        """Broker is truth. Fetch positions + activities; report drift; detect assignments."""
        positions = await self.broker.get_positions()
        activities = await self.broker.get_activities()
        opt_positions = [p for p in positions if "P" in str(p.get("symbol", ""))[-9:] or "C" in str(p.get("symbol", ""))[-9:]]
        assignment_events = [
            a for a in activities if a.get("activity_type") in ("ASSIGNMENT", "EXERCISE", "EXPIRATION")
        ]
        report = {
            "broker_position_count": len(opt_positions),
            "internal_spread_count": len(self.open_spreads),
            "assignments": assignment_events,
            "drift": len(opt_positions) != len(self.open_spreads) * 2,
        }
        self.audit.write("reconcile", "report", **report)
        return report

    async def kill_switch_close_all(self, reason: str) -> dict:
        self.audit.write("kill", "close_all", reason=reason)
        results = []
        for tag, info in list(self.open_spreads.items()):
            spread: SpreadCandidate = info["spread"]
            r = await self.broker.close_credit_spread(spread, credit_x10 := info["entry_credit"] * 10, idem_tag=tag)
            results.append({tag: r})
            if r.get("ok"):
                self.open_spreads.pop(tag, None)
        return {"reason": reason, "closed": results}
