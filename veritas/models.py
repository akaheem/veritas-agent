"""Pydantic models shared across the pipeline."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def cycle_id() -> str:
    return utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


Side = Literal["buy", "sell"]
PositionIntent = Literal["buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"]
SpreadKind = Literal["bull_put", "bear_call"]


class OptionLeg(BaseModel):
    symbol: str  # OCC, e.g. SPY260904P00445000
    side: Side
    ratio_qty: int = 1
    strike: float
    expiry: str  # YYYY-MM-DD
    option_type: Literal["call", "put"]
    bid: float = 0.0
    ask: float = 0.0
    open_interest: int = 0
    volume: int = 0
    delta: float | None = None
    implied_vol: float | None = None


class SpreadCandidate(BaseModel):
    """A fully-specified, mathematically-determined credit spread. Deterministic output."""

    kind: SpreadKind
    underlier: str
    spot: float
    legs: list[OptionLeg]  # 2 legs; leg[0] = short, leg[1] = long
    contracts: int = 1
    credit: float  # net credit per spread, dollars (per 1 contract)
    width: float  # dollars between strikes
    max_loss: float  # dollars for this size (width*100*contracts - credit*contracts)
    breakeven: float
    edge_ratio: float  # credit / width
    pop: float | None = None  # probability of profit (delta-based proxy)
    dte: int
    adjusted_credit: float  # credit after execution-reality haircut
    adjusted_max_loss: float
    greeks_source: Literal["broker", "local_bs", "none"] = "none"
    liquidity_ok: bool = False
    liq_flags: list[str] = Field(default_factory=list)


class MarketFeatures(BaseModel):
    underlier: str
    snapshot_ts: str
    spot: float
    ema20: float
    ema50: float
    trend: Literal["up", "down", "flat"]
    rsi14: float
    realized_vol_20d: float  # annualized
    implied_vol_atm: float | None = None
    iv_rv_ratio: float | None = None
    iv_rank_proxy: float | None = None  # within-session rank vs 30d history if available
    day_range_pct: float
    notes: list[str] = Field(default_factory=list)


class TradeProposal(BaseModel):
    """What the LLM returns. Either proposes one candidate (by id from the menu) or custom legs."""

    action: Literal["NO_TRADE", "PROPOSE"]
    candidate_index: int | None = None  # menu selection (preferred)
    spread: SpreadCandidate | None = None  # custom proposal (validator recomputes everything)
    contracts: int = Field(default=1, ge=1)  # upper cap enforced by validator (correct, not crash)
    thesis: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ValidationReport(BaseModel):
    passed: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    corrected: SpreadCandidate | None = None


class RiskVerdict(BaseModel):
    approved: bool
    gates: dict[str, bool] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    kill_switch: bool = False


class AuditEvent(BaseModel):
    ts: str
    cycle: str
    stage: str
    payload: dict = Field(default_factory=dict)
