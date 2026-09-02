"""Persistence + adoption: broker state is truth, disk state is the backup net.

Fixes the CONFIRMED critical findings:
- open_spreads was memory-only: after restart the kill switch / EOD flatten
  iterated an empty dict while real positions lived at the broker.
- kill_switch_close_all() now iterates BROKER positions (reverse-mleg per
  spread group) rather than memory, and adopts unknown positions on boot.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .audit import AuditLog
from .broker import McpBroker
from .config import NY_TZ, SETTINGS
from .models import OptionLeg, SpreadCandidate, utcnow

STATE_PATH = Path("./data/open_spreads.json")

# OCC symbol -> (root, yymmdd, C/P, strike*1000)
OCC_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


def _et_now() -> datetime:
    return datetime.now(ZoneInfo(NY_TZ))


def parse_occ(sym: str) -> dict | None:
    m = OCC_RE.match(sym)
    if not m:
        return None
    ymd, strike = m["ymd"], m["strike"]
    return {
        "expiry": f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:6]}",
        "option_type": "call" if m["cp"] == "C" else "put",
        "strike": int(strike) / 1000,
    }


class PositionManager:
    def __init__(self, audit: AuditLog, broker: McpBroker | None) -> None:
        self.audit = audit
        self.broker = broker
        self.open_spreads: dict[str, dict] = {}
        self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        if STATE_PATH.exists():
            try:
                self.open_spreads = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self.open_spreads = {}
            self.audit.write("position", "state_loaded", n=len(self.open_spreads))

    def _save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(self.open_spreads, indent=0, default=str), encoding="utf-8")

    # ---------- registration ----------
    def register_open(self, spread: SpreadCandidate, order_id: str) -> str:
        tag = f"{spread.underlier}-{spread.kind}-{utcnow().strftime('%H%M%S')}"
        self.open_spreads[tag] = {
            "spread": spread.model_dump(),
            "entry_credit": spread.credit,
            "opened_at": utcnow().isoformat(),
            "order_id": order_id,
            "status": "submitted",
        }
        self._save()
        return tag

    def mark_filled(self, tag: str, order: dict) -> None:
        if tag in self.open_spreads:
            self.open_spreads[tag]["order_id"] = order.get("id", self.open_spreads[tag]["order_id"])
            self.open_spreads[tag]["status"] = "filled"
            self._save()

    def drop(self, tag: str) -> None:
        self.open_spreads.pop(tag, None)
        self._save()

    # ---------- adoption (restart / external positions) ----------
    def adopt_broker_positions(self, positions: list[dict]) -> list[str]:
        """Adopt any broker option position not in open_spreads into tracking.

        Groups legs by (root, expiry, type); entry_credit defaults to the
        current mid as a conservative proxy. Returns adopted tags."""
        seen: dict[str, set[str]] = {}
        for p in positions:
            sym = str(p.get("symbol", ""))
            if not parse_occ(sym):
                continue
            info = parse_occ(sym)
            key = f"{info['option_type']}-{info['expiry']}"
            seen.setdefault(key, set()).add(sym)
        adopted = []
        for key, syms in seen.items():
            if any(
                t.get("spread", {}).get("legs") and
                {leg["symbol"] for leg in t["spread"]["legs"]} == syms
                for t in self.open_spreads.values()
            ):
                continue  # already tracked
            legs = []
            for sym in sorted(syms):
                info = parse_occ(sym)
                legs.append(OptionLeg(symbol=sym, side="sell", strike=info["strike"],
                                      expiry=info["expiry"], option_type=info["option_type"]))
            tag = f"adopted-{key}-{utcnow().strftime('%H%M%S')}"
            self.open_spreads[tag] = {
                "spread": SpreadCandidate(
                    kind="bull_put", underlier=legs[0].symbol[:3], spot=0.0, legs=legs,
                    credit=0.0, width=abs(legs[0].strike - legs[1].strike) if len(legs) > 1 else 1.0,
                    max_loss=0.0, breakeven=0.0, edge_ratio=0.0,
                    dte=max(0, (datetime.fromisoformat(legs[0].expiry) - _et_now().date()).days),
                    adjusted_credit=0.0, adjusted_max_loss=0.0,
                ).model_dump(),
                "entry_credit": None,  # unknown — manage() treats None as adopt-only
                "adopted": True,
                "opened_at": utcnow().isoformat(),
                "order_id": "unknown",
                "status": "adopted",
            }
            adopted.append(tag)
        if adopted:
            self._save()
            self.audit.write("position", "adopted_positions", tags=adopted)
        return adopted

    # ---------- mark prices ----------
    async def fetch_mark_costs(self) -> dict[str, float]:
        """Current closing cost per tracked spread from latest leg quotes.
        Fetches via MCP get_option_latest_quote per leg; skips on failure."""
        marks: dict[str, float] = {}
        if self.broker is None:
            return marks
        for tag, info in list(self.open_spreads.items()):
            if info.get("adopted") or not info.get("entry_credit"):
                continue
            legs = info["spread"].get("legs", [])
            if len(legs) < 2:
                continue
            short_q = await self.broker.call("get_option_latest_quote", {"symbol_or_symbols": legs[0]["symbol"]})
            long_q = await self.broker.call("get_option_latest_quote", {"symbol_or_symbols": legs[1]["symbol"]})
            if not (short_q.get("ok") and long_q.get("ok")):
                continue

            def _mid(qr) -> float | None:
                d = qr.get("data")
                if isinstance(d, dict):
                    d = d.get(legs[0]["symbol"]) if "symbol" not in d else d
                    q = d.get("latest_quote") or d if isinstance(d, dict) else {}
                    bp, ap = q.get("bid_price", q.get("bp")), q.get("ask_price", q.get("ap"))
                    try:
                        if bp is not None and ap is not None:
                            return (float(bp) + float(ap)) / 2
                    except (TypeError, ValueError):
                        return None
                return None

            m_short, m_long = _mid(short_q), _mid(long_q)
            if m_short is not None and m_long is not None:
                # closing a credit spread costs (short_mid - long_mid) per share
                marks[tag] = round(max(m_short - m_long, 0.01), 2)
        return marks

    # ---------- exit management ----------
    async def manage(self, mark_costs: dict[str, float]) -> list[dict]:
        """Profit-take / stop / force-close. Called every cycle AND from the
        reconcile loop (previously dead code — the critical fix)."""
        actions = []
        et = _et_now()
        force_all = (et.hour, et.minute) >= tuple(map(int, SETTINGS.force_close_all_et.split(":")))
        for tag, info in list(self.open_spreads.items()):
            spread_d = info["spread"]
            spread = SpreadCandidate(**spread_d)
            credit = info.get("entry_credit")
            cur_cost = mark_costs.get(tag)
            reason = None
            if force_all:
                reason = "force close before market close (pin-risk guard)"
            elif credit in (None, 0) or info.get("adopted"):
                continue  # adopted position: exit via kill/close-all only
            elif cur_cost is not None:
                pct = cur_cost / credit
                if pct >= SETTINGS.stop_loss_mult:
                    reason = f"stop: cost {pct:.0%} of credit"
                elif pct <= SETTINGS.profit_take_pct:
                    reason = f"profit take: cost {pct:.0%} of credit"
                elif spread.dte == 0 and (et.hour, et.minute) >= tuple(
                    map(int, SETTINGS.force_close_et.split(":"))
                ):
                    reason = "force close 0DTE"
            if reason:
                max_debit = (credit or 1.0) * (SETTINGS.stop_loss_mult if not force_all else 10.0)
                r = await self.broker.close_credit_spread(spread, max_debit, idem_tag=tag)
                self.audit.write("position", "close_submitted", tag=tag, reason=reason, result=_ok(r))
                if _ok(r):
                    actions.append({"tag": tag, "reason": reason, "status": "close_accepted"})
                    # NOTE: tag is NOT dropped here — reconciliation drops it when
                    # the closing order FILLS (accepted != filled).
        return actions

    async def reconcile(self, positions: list[dict] | None = None) -> dict:
        """Broker is truth. Called every 5 min. Adopts unknowns; drops tags whose
        legs no longer exist at the broker (fills confirmed)."""
        if self.broker is None:
            return {"error": "no broker"}
        positions = positions if positions is not None else await self.broker.get_positions()
        broker_syms = {str(p.get("symbol")) for p in positions if parse_occ(str(p.get("symbol", "")))}
        self.adopt_broker_positions([p for p in positions if parse_occ(str(p.get("symbol", "")))])
        dropped = []
        for tag, info in list(self.open_spreads.items()):
            leg_syms = {leg["symbol"] for leg in info["spread"]["legs"]}
            if info.get("status") == "close_accepted" and not (leg_syms & broker_syms):
                dropped.append(tag)  # closing order fully filled
                self.drop(tag)
            elif not info.get("adopted") and info.get("status") == "filled" and not (leg_syms & broker_syms):
                # open order never filled and vanished -> drop silently-but-logged
                dropped.append(tag)
                self.drop(tag)
        activities = await self.broker.get_activities()
        assignments = [a for a in activities if str(a.get("activity_type", "")).startswith("OP")]
        report = {
            "broker_option_positions": len(broker_syms),
            "internal_spreads": len(self.open_spreads),
            "dropped": dropped,
            "assignments_seen": assignments,
            "drift": len(broker_syms) != 2 * len(self.open_spreads),
        }
        self.audit.write("reconcile", "report", **report)
        return report

    async def kill_switch_close_all(self, reason: str) -> dict:
        """Flatten EVERY option position at the broker — even untracked ones —
        by building reverse-mleg orders from broker position groups."""
        self.audit.write("kill", "close_all", reason=reason)
        results = []
        if self.broker is None:
            return {"reason": reason, "error": "no broker"}
        positions = await self.broker.get_positions()
        self.adopt_broker_positions(positions)
        for tag, info in list(self.open_spreads.items()):
            spread = SpreadCandidate(**info["spread"])
            r = await self.broker.close_credit_spread(spread, debit_limit=10.0, idem_tag=tag)
            results.append({tag: _ok(r)})
            if _ok(r):
                info["status"] = "close_accepted"
        self._save()
        return {"reason": reason, "closed": results}


def _ok(r: dict) -> dict:
    return {"ok": r.get("ok", False), "error": r.get("error", "") if not r.get("ok") else ""}
