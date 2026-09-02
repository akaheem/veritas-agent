"""Deterministic feature engine — pure functions, no LLM, no broker calls.

Given a Snapshot, compute trend/vol/IV features for each underlier.
"""

from __future__ import annotations

import math
from datetime import date

from .config import SETTINGS
from .models import MarketFeatures


def ema(values: list[float], span: int) -> float:
    if not values:
        return 0.0
    k = 2 / (span + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = values[-i] - values[-i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def realized_vol(closes: list[float], window: int = 20) -> float:
    if len(closes) < window + 1:
        return 0.0
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - window, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0
    return math.sqrt(var) * math.sqrt(252)


def atm_iv(chain: list[dict], spot: float, kind: str) -> float | None:
    ivs = [
        row.get("implied_vol")
        for row in chain
        if row.get("type") == kind
        and row.get("implied_vol")
        and abs(row.get("strike", 0) - spot) <= spot * 0.02
    ]
    return round(sum(ivs) / len(ivs), 4) if ivs else None


def build_features(snap) -> list[MarketFeatures]:
    feats: list[MarketFeatures] = []
    for sym in SETTINGS.underliers:
        u = snap.underliers.get(sym, {})
        if "error" in u or not u:
            continue
        closes = u.get("closes_40d", [])
        spot = u.get("spot", 0.0)
        e20, e50 = ema(closes, 20), ema(closes, 50)
        trend = "up" if e20 > e50 * 1.001 else ("down" if e20 < e50 * 0.999 else "flat")
        r = rsi(closes)
        rv = realized_vol(closes)
        chain = snap.chains.get(sym, [])
        iv_atm = atm_iv(chain, spot, "put") or atm_iv(chain, spot, "call")
        day_hi, day_lo = u.get("day_high", spot), u.get("day_low", spot)
        feats.append(
            MarketFeatures(
                underlier=sym,
                snapshot_ts=snap.ts,
                spot=spot,
                ema20=round(e20, 2),
                ema50=round(e50, 2),
                trend=trend,  # type: ignore[arg-type]
                rsi14=round(r, 1),
                realized_vol_20d=round(rv, 4),
                implied_vol_atm=iv_atm,
                iv_rv_ratio=round(iv_atm / rv, 2) if iv_atm and rv > 0 else None,
                day_range_pct=round((day_hi - day_lo) / spot * 100, 2) if spot else 0.0,
            )
        )
    return feats
