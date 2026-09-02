"""Black-Scholes helpers + deterministic spread math.

We compute our own greeks rather than trusting the feed alone (documented
weakness: data endpoints can be inconsistent). Pure python, no scipy.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def years_to_expiry(expiry: date, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    # options stop trading 16:00 ET on expiry day; approximate with calendar-day fraction
    exp_dt = datetime(expiry.year, expiry.month, expiry.day, 21, 0, tzinfo=timezone.utc)  # 16:00 ET
    secs = max((exp_dt - now).total_seconds(), 0.0)
    return secs / (365.0 * 24 * 3600)


def bs_price_delta(
    spot: float, strike: float, t_years: float, vol: float, r: float = 0.0, q: float = 0.0
) -> tuple[float, float, float]:
    """Return (price, delta, vega_per_vol_point) for a call. Put = call - spot*e^-qT + K*e^-rT."""
    if t_years <= 0 or vol <= 0:
        intrinsic = max(spot - strike, 0.0)
        delta = 1.0 if spot > strike else (0.5 if spot == strike else 0.0)
        return intrinsic, delta, 0.0
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * vol * vol) * t_years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    call = spot * math.exp(-q * t_years) * norm_cdf(d1) - strike * math.exp(-r * t_years) * norm_cdf(d2)
    delta = math.exp(-q * t_years) * norm_cdf(d1)
    vega = spot * math.exp(-q * t_years) * norm_pdf(d1) * sqrt_t / 100.0
    return call, delta, vega


def put_delta(spot: float, strike: float, t_years: float, vol: float, r: float = 0.0, q: float = 0.0) -> float:
    call_delta, _ = bs_delta_only(spot, strike, t_years, vol, r, q)
    return call_delta - math.exp(-q * t_years)


def bs_delta_only(
    spot: float, strike: float, t_years: float, vol: float, r: float = 0.0, q: float = 0.0
) -> tuple[float, float]:
    if t_years <= 0 or vol <= 0:
        return (1.0 if spot > strike else 0.0), 0.0
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * vol * vol) * t_years) / (vol * sqrt_t)
    return norm_cdf(d1), vega_from_d1(spot, q, norm_pdf(d1), sqrt_t)


def vega_from_d1(spot: float, q: float, pdf_d1: float, sqrt_t: float) -> float:
    return spot * math.exp(-q * sqrt_t * 0) * pdf_d1 * sqrt_t / 100.0


def implied_vol_from_price(
    price: float,
    spot: float,
    strike: float,
    t_years: float,
    is_put: bool,
    lo: float = 0.01,
    hi: float = 5.0,
) -> float | None:
    """Bisection IV from mid price. Returns None if unpriceable."""
    if price <= 0 or spot <= 0 or t_years <= 0:
        return None

    def model(p: float) -> float:
        c, _, _ = bs_price_delta(spot, strike, t_years, p)
        return c if not is_put else c - spot + strike  # put via parity (r=q=0 approx)

    f_lo, f_hi = model(lo) - price, model(hi) - price
    if f_lo > 0 or f_hi < 0:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        f_mid = model(mid) - price
        if abs(f_mid) < 1e-4:
            return mid
        if f_mid < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def spread_math(
    short_strike: float,
    long_strike: float,
    short_mid: float,
    long_mid: float,
    option_type: str,
    spot: float,
    contracts: int,
) -> dict:
    """Deterministic credit spread economics. Works for bull_put and bear_call.

    Credit spread: sell near-the-money leg, buy farther leg.
    credit = (short_mid - long_mid); width = |short - long|; max loss = width*100 - credit*100 per contract.
    """
    credit = round(max(short_mid - long_mid, 0.0), 2)
    width = round(abs(short_strike - long_strike), 2)
    if option_type == "put":
        breakeven = short_strike - credit
        pop = None  # caller fills via delta
    else:
        breakeven = short_strike + credit
        pop = None
    credit_total = credit * 100 * contracts
    max_loss = round(width * 100 * contracts - credit_total, 2)
    return {
        "credit": credit,
        "width": width,
        "breakeven": round(breakeven, 2),
        "max_loss": max_loss,
        "edge_ratio": round(credit / width, 4) if width > 0 else 0.0,
        "credit_total": round(credit_total, 2),
    }


def execution_reality_haircut(credit: float, width: float, contracts: int, per_leg: float, ratio: float) -> tuple[float, float]:
    """Conservative fill model: assume we lose `ratio` of the credit plus `per_leg` dollars
    per contract on each of the two legs. Returns (adjusted_credit_total, adjusted_max_loss)."""
    shaved = credit * (1.0 - ratio) - 2 * per_leg
    adjusted_credit = max(shaved, 0.0) * 100 * contracts
    adjusted_max_loss = width * 100 * contracts - adjusted_credit
    return round(adjusted_credit, 2), round(max(adjusted_max_loss, 0.0), 2)
