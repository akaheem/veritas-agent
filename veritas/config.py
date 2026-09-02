"""Central configuration. Secrets come from env only; risk limits are code-first constants.

Risk limits intentionally live here (and config/risk.yaml) rather than in prompts:
the LLM can never weaken its own guardrails.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

NY_TZ = "America/New_York"


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    # --- Alpaca (paper only; VERITAS refuses to run against live endpoints) ---
    alpaca_api_key: str = field(default_factory=lambda: _env("ALPACA_API_KEY"))
    alpaca_api_secret: str = field(default_factory=lambda: _env("ALPACA_API_SECRET"))
    paper_base_url: str = "https://paper-api.alpaca.markets"
    data_feed: str = _env("VERITAS_DATA_FEED", "iex")  # free tier feed; 'sip' if subscribed

    # --- LLM ---
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    # claude-sonnet-5 rejects non-default temperature (400); determinism comes
    # from structured outputs (json_schema) instead. See decision.py.
    llm_model: str = _env("VERITAS_LLM_MODEL", "claude-sonnet-5")

    # --- Underliers ---
    underliers: tuple[str, ...] = ("SPY", "QQQ")

    # --- Loop cadence (seconds) ---
    cycle_seconds: int = int(_env("VERITAS_CYCLE_SECONDS", "900"))  # 15 min
    reconcile_seconds: int = 300  # broker-state reconciliation every 5 min

    # --- Spread construction ---
    dte_min: int = 0
    dte_max: int = 7
    short_delta_target: float = 0.30  # short-strike delta target
    width_target: float = 2.0  # dollars between strikes (auto-snaps to listed strikes)
    min_edge_ratio: float = 0.18  # credit / width must exceed this (18%)

    # --- Execution reality model (pre-trade slippage haircut) ---
    slippage_per_leg: float = 0.01  # $ per contract per leg charged against credit
    slippage_ratio: float = 0.10  # additionally shave 10% of credit off the top
    entry_credit_buffer: float = 0.85  # submit limit at >=85% of snapshot mid credit (indicative-data buffer)

    # --- Liquidity gates ---
    max_rel_spread: float = 0.30  # (ask-bid)/mid <= 30% on both legs
    min_open_interest: int = 100
    min_volume: int = 10

    # --- Risk gates (NOT LLM-overridable) ---
    max_loss_per_trade: float = 2_000.0  # 2% of $100k
    max_daily_loss: float = 2_000.0  # kill switch trigger
    kill_switch_mode: str = _env("VERITAS_KILL_MODE", "close_all")  # or 'halt_new'
    max_open_positions: int = 4
    max_portfolio_heat: float = 0.08  # sum(max_loss)/equity <= 8%
    max_contracts_per_leg: int = 5
    max_notional_per_spread: float = 3_000.0  # width*100*contracts
    entry_window_et: tuple[str, str] = ("10:00", "15:00")
    force_close_et: str = "15:30"  # force-close 0DTE positions (never hold shorts into expiry)
    force_close_all_et: str = "15:45"  # flatten everything before the close (pin-risk guard)

    # --- Profit taking / stops (on credit received) ---
    profit_take_pct: float = 0.50  # buy back at 50% of credit
    stop_loss_mult: float = 2.0  # buy back at 200% of credit

    # --- Paths ---
    data_dir: Path = field(default_factory=lambda: Path(_env("VERITAS_DATA_DIR", "./data")))
    audit_dir: Path = field(default_factory=lambda: Path("./logs"))
    dashboard_dir: Path = field(default_factory=lambda: Path("./dashboard/data"))

    def validate(self) -> list[str]:
        problems = []
        if not self.alpaca_api_key or not self.alpaca_api_secret:
            problems.append("ALPACA_API_KEY / ALPACA_API_SECRET not set")
        if not self.anthropic_api_key:
            problems.append("ANTHROPIC_API_KEY not set")
        if "api.alpaca.markets" in self.paper_base_url and "paper" not in self.paper_base_url:
            problems.append("refusing to run: live endpoint configured")
        return problems


SETTINGS = Settings()
