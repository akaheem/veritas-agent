"""Execution Reality Layer — Master Plan v2 §8.

Turns the fixed "85% of mid" haircut into a scored, adaptive gate.

Execution Confidence Score (0–100), assembled from deterministic factors:
  - quote_quality: relative bid/ask width on both legs
  - liquidity: worst-leg OI + volume vs floors
  - feed: indicative (free tier) caps confidence; OPRA/SIP full marks
  - structure: edge ratio + DTE sensitivity (0DTE penalized)

Bands (Master Plan v2):
  85–100  eligible at normal size, adaptive price 90% of mid
   70–84  eligible at conservative price 80% of mid
     <70  REJECT — recorded in audit as rejected-by-execution (shadow book)
"""

from __future__ import annotations

from .config import SETTINGS
from .models import SpreadCandidate


def evaluate_confidence(c: SpreadCandidate, feed: str) -> tuple[int, dict]:
    factors: dict[str, int] = {}

    rels = []
    for leg in c.legs:
        mid = (leg.bid + leg.ask) / 2
        if mid > 0 and leg.ask >= leg.bid:
            rels.append((leg.ask - leg.bid) / mid)
        else:
            rels.append(1.0)
    avg_rel = sum(rels) / len(rels)
    quote_q = int(max(0.0, min(1.0, 1 - avg_rel / SETTINGS.max_rel_spread)) * 100)
    factors["quote_quality"] = quote_q

    oi = min(l.open_interest for l in c.legs)
    vol = min(l.volume for l in c.legs)
    liq = 50 + min(25, oi / 400 * 25) + min(25, vol / 100 * 25)
    factors["liquidity"] = int(min(100, liq))

    factors["feed"] = 100 if feed.lower() in ("sip", "opra") else 60

    edge = int(min(1.0, c.edge_ratio / 0.30) * 100)
    dte_pen = 25 if c.dte < 1 else 0
    factors["structure"] = max(0, edge - dte_pen)

    score = int(
        0.30 * quote_q + 0.25 * factors["liquidity"]
        + 0.20 * factors["feed"] + 0.25 * factors["structure"]
    )
    return max(0, min(100, score)), factors


def entry_credit_ratio(score: int) -> float:
    """Adaptive entry price as a fraction of snapshot mid credit."""
    if score >= 85:
        return 0.90
    if score >= 70:
        return 0.80
    return 0.0  # rejected — no price is safe enough


def confidence_band(score: int) -> str:
    if score >= 85:
        return "normal"
    if score >= 70:
        return "reduced"
    return "reject"
