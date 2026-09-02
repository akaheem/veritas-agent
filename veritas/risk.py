"""Risk gate engine + kill switch. Hard limits; the LLM cannot override these.

Gates are evaluated against BROKER state (equity, positions), not memory.
"""

from __future__ import annotations

from datetime import datetime

from zoneinfo import ZoneInfo

from .config import SETTINGS, NY_TZ
from .models import RiskVerdict, SpreadCandidate


def _et_now() -> datetime:
    return datetime.now(ZoneInfo(NY_TZ))


def in_entry_window(now: datetime | None = None) -> bool:
    now = now or _et_now()
    start_h, start_m = map(int, SETTINGS.entry_window_et[0].split(":"))
    end_h, end_m = map(int, SETTINGS.entry_window_et[1].split(":"))
    return (now.hour, now.minute) >= (start_h, start_m) and (now.hour, now.minute) <= (end_h, end_m)


def daily_pnl(account: dict | None, last_equity: float) -> float:
    if not account or account.get("error"):
        return 0.0
    return round((account.get("equity", last_equity) or last_equity) - last_equity, 2)


def evaluate(
    spread: SpreadCandidate,
    account: dict | None,
    positions: list[dict],
    realized_today: float,
    unrealized_heat: float,
    position_units: int | None = None,
    entry_ok: bool | None = None,
    execution_confidence: int | None = None,
    open_spread_groups: dict[str, float] | None = None,
) -> RiskVerdict:
    """Master Plan v2 §7 gates. Hard limits; the LLM cannot override.

    open_spread_groups: {"SPY": max_loss_total, "QQQ": ...} incl. working
    orders — used for the correlated-exposure gate (SPY+QQQ move together,
    so their combined max loss is capped, not just per-trade).
    """
    gates: dict[str, bool] = {}
    failures: list[str] = []
    equity = (account or {}).get("equity", 0.0) or 0.0
    kill = False

    gates["account_available"] = equity > 0 and not (account or {}).get("error")
    if not gates["account_available"]:
        failures.append("account state unavailable — refusing to trade blind")

    gates["entry_window"] = in_entry_window() if entry_ok is None else entry_ok
    if not gates["entry_window"]:
        failures.append("outside entry window")

    gates["daily_loss_ok"] = realized_today > -SETTINGS.max_daily_loss
    if not gates["daily_loss_ok"]:
        failures.append(f"daily loss {realized_today} breached ${SETTINGS.max_daily_loss}")
        kill = True

    gates["per_trade_max_loss"] = spread.adjusted_max_loss <= SETTINGS.max_loss_per_trade
    if not gates["per_trade_max_loss"]:
        failures.append(f"max loss {spread.adjusted_max_loss} > ${SETTINGS.max_loss_per_trade}")

    units = len(positions) if position_units is None else position_units
    gates["position_count"] = units < SETTINGS.max_open_positions
    if not gates["position_count"]:
        failures.append(f"position units {units} (incl. working orders) >= max {SETTINGS.max_open_positions}")

    heat = (unrealized_heat + spread.adjusted_max_loss) / equity if equity else 9.9
    gates["portfolio_heat"] = heat <= SETTINGS.max_portfolio_heat
    if not gates["portfolio_heat"]:
        failures.append(f"portfolio heat {heat:.1%} > {SETTINGS.max_portfolio_heat:.0%}")

    # correlated exposure: SPY/QQQ rise and fall together — combined cap
    groups = dict(open_spread_groups or {})
    groups[spread.underlier] = groups.get(spread.underlier, 0.0) + spread.adjusted_max_loss
    correlated_total = sum(groups.values())
    gates["correlated_exposure"] = correlated_total <= SETTINGS.max_correlated_exposure
    if not gates["correlated_exposure"]:
        failures.append(
            f"correlated exposure ${correlated_total:.0f} > ${SETTINGS.max_correlated_exposure:.0f}"
        )

    # execution confidence gate (Master Plan v2 §8)
    if execution_confidence is not None:
        gates["execution_confidence"] = execution_confidence >= SETTINGS.min_execution_confidence
        if not gates["execution_confidence"]:
            failures.append(f"execution confidence {execution_confidence} < {SETTINGS.min_execution_confidence}")

    gates["notional_cap"] = spread.width * 100 * spread.contracts <= SETTINGS.max_notional_per_spread
    if not gates["notional_cap"]:
        failures.append(f"notional {spread.width * 100 * spread.contracts} > ${SETTINGS.max_notional_per_spread}")

    gates["liquidity"] = spread.liquidity_ok
    if not gates["liquidity"]:
        failures.append(f"liquidity gate failed: {spread.liq_flags}")

    gates["size_cap"] = spread.contracts <= SETTINGS.max_contracts_per_leg
    if not gates["size_cap"]:
        failures.append("contract cap exceeded")

    approved = all(gates.values())
    return RiskVerdict(approved=approved, gates=gates, failures=failures, kill_switch=kill)


def should_force_close(now: datetime | None = None) -> bool:
    now = now or _et_now()
    h, m = map(int, SETTINGS.force_close_all_et.split(":"))
    return (now.hour, now.minute) >= (h, m)
