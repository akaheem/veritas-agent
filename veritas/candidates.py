"""Candidate spread builder — the deterministic "menu" generator.

For each underlier, constructs the best 0-7 DTE credit-spread candidates from
the live chain. Output is a list of SpreadCandidate objects with every number
computed by code (never by the LLM).
"""

from __future__ import annotations

from datetime import date, datetime

from .config import SETTINGS
from .models import OptionLeg, SpreadCandidate
from .spreads import execution_reality_haircut, put_delta, spread_math


def _parse_expiry(exp: str) -> date:
    return date.fromisoformat(exp[:10])


def _pick_strikes(chain: list[dict], spot: float, kind: str) -> tuple[dict, dict] | None:
    """Return (short, long) leg rows for the nearest delta-targeted spread."""
    t = 1 / 365  # rough; delta ranking does the real work
    puts = sorted(
        [r for r in chain if r.get("type") == "put" and _valid(r) and r["strike"] < spot],
        key=lambda r: r["strike"],
    )
    calls = sorted(
        [r for r in chain if r.get("type") == "call" and _valid(r) and r["strike"] > spot],
        key=lambda r: r["strike"],
    )
    if kind == "bull_put":
        # short put target delta ~ -0.30; long = next listed strike below by ~width
        shorts = [r for r in puts if r.get("greeks_delta") and abs(r["greeks_delta"] - -SETTINGS.short_delta_target) < 0.12]
        short = shorts[-1] if shorts else (puts[len(puts) // 2] if puts else None)
        if not short:
            return None
        target_long = short["strike"] - SETTINGS.width_target
        longs = [r for r in puts if r["strike"] <= short["strike"] - 0.5]
        if not longs:
            return None
        leg_l = min(longs, key=lambda r: abs(r["strike"] - target_long))
        return short, leg_l
    else:  # bear_call
        shorts = [r for r in calls if r.get("greeks_delta") and abs(r["greeks_delta"] - SETTINGS.short_delta_target) < 0.12]
        short = shorts[0] if shorts else (calls[len(calls) // 2] if calls else None)
        if not short:
            return None
        target_long = short["strike"] + SETTINGS.width_target
        longs = [r for r in calls if r["strike"] >= short["strike"] + 0.5]
        if not longs:
            return None
        leg_l = min(longs, key=lambda r: abs(r["strike"] - target_long))
        return short, leg_l


def _valid(r: dict) -> bool:
    bid, ask = r.get("bid", 0), r.get("ask", 0)
    if not bid or not ask or ask <= bid:
        return False
    mid = (bid + ask) / 2
    if mid <= 0:
        return False
    if (ask - bid) / mid > SETTINGS.max_rel_spread:
        return False
    if r.get("open_interest", 0) < SETTINGS.min_open_interest:
        return False
    if r.get("volume", 0) < SETTINGS.min_volume:
        return False
    return True


def build_candidates(snap) -> list[SpreadCandidate]:
    out: list[SpreadCandidate] = []
    for sym in SETTINGS.underliers:
        chain = snap.chains.get(sym, [])
        if not chain or "error" in chain[0]:
            continue
        u = snap.underliers.get(sym, {})
        spot = u.get("spot", 0)
        if not spot:
            continue
        # market-implied bias: choose kind by trend (deterministic default; LLM may override via menu)
        from .features import build_features

        feats = {f.underlier: f for f in build_features(snap)}
        f = feats.get(sym)
        kind = "bull_put" if (f and f.trend != "down") else "bear_call"
        picked = _pick_strikes(chain, spot, kind)
        if not picked:
            continue
        short, long = picked
        short_mid = round((short["bid"] + short["ask"]) / 2, 2)
        long_mid = round((long["bid"] + long["ask"]) / 2, 2)
        m = spread_math(short["strike"], long["strike"], short_mid, long_mid, kind.split("_")[1], spot, 1)
        if m["credit"] <= 0 or m["edge_ratio"] < SETTINGS.min_edge_ratio:
            continue
        legs = [
            OptionLeg(
                symbol=short["symbol"],
                side="sell",
                strike=short["strike"],
                expiry=short["expiry"],
                option_type=kind.split("_")[1],
                bid=short["bid"],
                ask=short["ask"],
                open_interest=short.get("open_interest", 0),
                volume=short.get("volume", 0),
                delta=short.get("greeks_delta"),
                implied_vol=short.get("implied_vol"),
            ),
            OptionLeg(
                symbol=long["symbol"],
                side="buy",
                strike=long["strike"],
                expiry=long["expiry"],
                option_type=kind.split("_")[1],
                bid=long["bid"],
                ask=long["ask"],
                open_interest=long.get("open_interest", 0),
                volume=long.get("volume", 0),
                delta=long.get("greeks_delta"),
                implied_vol=long.get("implied_vol"),
            ),
        ]
        adj_credit, adj_max_loss = execution_reality_haircut(
            m["credit"], m["width"], 1, SETTINGS.slippage_per_leg, SETTINGS.slippage_ratio
        )
        dte = (_parse_expiry(short["expiry"]) - datetime.now().date()).days
        pop = None
        if short.get("greeks_delta") is not None:
            # rough POP proxy: 1 - |short delta| adjusted
            pop = round(1 - abs(put_delta(spot, short["strike"], max(dte, 0.5) / 365, short.get("implied_vol") or 0.2)), 3)
        out.append(
            SpreadCandidate(
                kind=kind,  # type: ignore[arg-type]
                underlier=sym,
                spot=spot,
                legs=legs,
                contracts=1,
                credit=m["credit"],
                width=m["width"],
                max_loss=m["max_loss"],
                breakeven=m["breakeven"],
                edge_ratio=m["edge_ratio"],
                pop=pop,
                dte=dte,
                adjusted_credit=adj_credit,
                adjusted_max_loss=adj_max_loss,
                greeks_source="broker" if short.get("greeks_delta") is not None else "none",
                liquidity_ok=True,
            )
        )
    return out
