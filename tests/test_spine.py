"""Offline tests for the deterministic spine — no network, no keys.

Run: uv run pytest tests/ -q
These verify the "LLM proposes, math disposes" invariant: given hallucinated
numbers, the validator/risk gates must reject or correct them.
"""

import math
from datetime import date

import pytest

from veritas.config import SETTINGS
from veritas.features import ema, realized_vol, rsi
from veritas.models import OptionLeg, SpreadCandidate, TradeProposal
from veritas.spreads import (
    bs_price_delta,
    execution_reality_haircut,
    implied_vol_from_price,
    spread_math,
)
from veritas.validator import validate


def mk_spread(credit=0.40, width=2.0, contracts=1, dte=2, liq=True):
    legs = [
        OptionLeg(symbol="SPY260904P00445000", side="sell", strike=445.0, expiry="2026-09-04",
                  option_type="put", bid=1.0, ask=1.04, open_interest=500, volume=200, delta=-0.30),
        OptionLeg(symbol="SPY260904P00443000", side="buy", strike=443.0, expiry="2026-09-04",
                  option_type="put", bid=0.58, ask=0.62, open_interest=400, volume=150, delta=-0.18),
    ]
    m = spread_math(445.0, 443.0, 1.02, 0.60, "put", 446.0, 1)
    adj_c, adj_ml = execution_reality_haircut(m["credit"], m["width"], 1,
                                              SETTINGS.slippage_per_leg, SETTINGS.slippage_ratio)
    return SpreadCandidate(kind="bull_put", underlier="SPY", spot=446.0, legs=legs,
                           contracts=contracts, credit=m["credit"], width=m["width"],
                           max_loss=m["max_loss"], breakeven=m["breakeven"],
                           edge_ratio=m["edge_ratio"], dte=dte,
                           adjusted_credit=adj_c, adjusted_max_loss=adj_ml,
                           liquidity_ok=liq)


# ---------- pure math ----------
def test_ema_rises_with_uptrend():
    up = [100 + i for i in range(50)]
    assert ema(up, 20) > up[0]


def test_rsi_bounds():
    assert 0 <= rsi([100 - i for i in range(20)]) <= 100
    assert rsi([100] * 20) == 100.0


def test_realized_vol_positive():
    import random
    random.seed(7)
    closes = [100 * math.exp(0.001 * i + random.gauss(0, 0.01)) for i in range(30)]
    assert 0 < realized_vol(closes) < 5


def test_spread_math_credit_and_max_loss():
    m = spread_math(445, 443, 1.02, 0.60, "put", 446, 1)
    assert m["credit"] == pytest.approx(0.42, abs=0.01)
    assert m["width"] == 2.0
    assert m["max_loss"] == pytest.approx((2.0 - 0.42) * 100, abs=1.0)
    assert m["edge_ratio"] == pytest.approx(0.21, abs=0.01)


def test_haircut_reduces_credit():
    c, ml = execution_reality_haircut(0.42, 2.0, 1, 0.01, 0.10)
    assert c < 0.42 * 100
    assert ml > (2.0 - 0.42) * 100


def test_bs_call_delta_bounds():
    price, delta, _ = bs_price_delta(100, 100, 0.1, 0.2)
    assert 0.4 < delta < 0.6
    assert price > 0


def test_iv_recovery():
    # ATM call, S=K=100, T=0.1: price 1.5 ⇒ IV ≈ 1.5/(0.4·100·√0.1) ≈ 0.119
    iv = implied_vol_from_price(1.5, 100, 100, 0.1, is_put=False)
    assert iv is not None and 0.08 < iv < 0.2


# ---------- validator: the anti-hallucination core ----------
def test_validator_rejects_out_of_range_index():
    r = validate(TradeProposal(action="PROPOSE", candidate_index=99, contracts=1), [mk_spread()])
    assert not r.passed


def test_validator_rejects_custom_legs():
    r = validate(TradeProposal(action="PROPOSE", spread=mk_spread(), contracts=1), [mk_spread()])
    assert not r.passed
    assert any("custom legs disabled" in f for f in r.failures)


def test_validator_recomputes_and_passes_menu_pick():
    s = mk_spread()
    r = validate(TradeProposal(action="PROPOSE", candidate_index=0, contracts=2), [s])
    assert r.passed, r.failures
    assert r.corrected.contracts == 2
    assert r.corrected.max_loss == pytest.approx(s.max_loss * 2, rel=0.15)


def test_validator_contracts_capped():
    r = validate(TradeProposal(action="PROPOSE", candidate_index=0, contracts=50), [mk_spread()])
    assert r.passed  # corrected, not rejected
    assert r.corrected.contracts == SETTINGS.max_contracts_per_leg


def test_no_trade_always_passes():
    assert validate(TradeProposal(action="NO_TRADE"), []).passed


# ---------- post-competition audit fixes (regression guards) ----------
def test_occ_meta_parse():
    from veritas.data import _occ_meta

    m = _occ_meta("SPY260908P00450000")
    assert m == {"expiry": "2026-09-08", "type": "put", "strike": 450.0}
    c = _occ_meta("QQQ260909C00485000")
    assert c["type"] == "call" and c["strike"] == 485.0
    assert _occ_meta("NOT_AN_OCC") is None


def test_leg_mid_uses_leg_own_symbol():
    from veritas.position import _leg_mid

    # per-contract keyed response: the LONG leg must resolve via ITS symbol
    qr = {"ok": True, "data": {"SPY260908P00440000": {"latest_quote": {"bid_price": 1.0, "ask_price": 1.2}}}}
    assert _leg_mid(qr, "SPY260908P00440000") == 1.1
    assert _leg_mid(qr, "SPY260908P00441000") is None
    # flat quote object shape
    qr2 = {"ok": True, "data": {"bid_price": 0.5, "ask_price": 0.7}}
    assert _leg_mid(qr2, "ANY") == 0.6


def test_pending_submit_can_fill():
    from veritas.orderstate import OrderState, can_transition

    assert can_transition(OrderState.PENDING_SUBMIT, OrderState.FILLED)
    assert can_transition(OrderState.PENDING_SUBMIT, OrderState.PARTIALLY_FILLED)


def test_volume_gate_disabled_by_default():
    # alpaca-py exposes no volume field → gate must be off unless re-enabled
    assert SETTINGS.min_volume == 0


def test_validator_no_trade_report_shape():
    r = validate(TradeProposal(action="NO_TRADE"), [])
    assert r.passed and r.checks.get("no_trade") is True and r.corrected is None
