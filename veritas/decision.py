"""LLM decision core (Claude) — proposes; never disposes.

The LLM sees features + a deterministic candidate menu and either picks one
(by index) or abstains. If it invents custom legs, the validator recomputes
every number from scratch and rejects hallucinated math.
"""

from __future__ import annotations

import json

from anthropic import Anthropic

from .config import SETTINGS
from .models import MarketFeatures, SpreadCandidate, TradeProposal

SYSTEM = """You are VERITAS, an autonomous options credit-spread trader.
You receive deterministic market features and a menu of pre-priced credit spread
candidates. Every number in the menu was computed by deterministic code from a
single market snapshot — trust the menu's numbers, not your own estimates.

Your job:
1. Decide whether current conditions justify opening a NEW credit spread.
2. If yes, choose ONE candidate from the menu (by index) and a contract count.
3. Write a concise thesis: what regime/signal justifies it, and what would make it wrong.
4. If conditions are poor (choppy, post-move exhaustion, low IV ratio, existing
   exposure too high, near entry-window close), answer NO_TRADE. Abstaining is a
   valid, often superior, decision.

Rules:
- You may ONLY select from the menu or abstain. Never invent strikes, symbols, or prices.
- contracts must be 1-5.
- Sell premium when IV > realized vol; prefer bull_put in up/flat regimes, bear_call in down regimes.
- Respond with the JSON object only."""

USER_TMPL = """Market features:
{features}

Deterministic candidate menu (all prices are mid; every number pre-validated):
{menu}

Open positions:
{positions}

Account equity: ${equity:,.2f} | Cash: ${cash:,.2f}
Daily P&L so far: ${daily_pnl:,.2f}
Open position count: {pos_count} (max {max_pos})

Respond with JSON: {{"action": "NO_TRADE" | "PROPOSE", "candidate_index": <int|null>, "contracts": <1-5>, "thesis": "...", "confidence": <0-1>}}"""


class DecisionCore:
    def __init__(self) -> None:
        self.client = Anthropic(api_key=SETTINGS.anthropic_api_key)

    def decide(
        self,
        features: list[MarketFeatures],
        candidates: list[SpreadCandidate],
        positions: list[dict],
        account: dict | None,
        daily_pnl: float,
    ) -> TradeProposal:
        menu = [
            {
                "index": i,
                "kind": c.kind,
                "underlier": c.underlier,
                "dte": c.dte,
                "credit": c.credit,
                "width": c.width,
                "max_loss_per_spread": c.max_loss,
                "edge_ratio": c.edge_ratio,
                "breakeven": c.breakeven,
                "short_leg": {"strike": c.legs[0].strike, "delta": c.legs[0].delta, "expiry": c.legs[0].expiry},
                "long_leg": {"strike": c.legs[1].strike, "delta": c.legs[1].delta, "expiry": c.legs[1].expiry},
                "pop_proxy": c.pop,
            }
            for i, c in enumerate(candidates)
        ]
        user = USER_TMPL.format(
            features=json.dumps([f.model_dump() for f in features], indent=1),
            menu=json.dumps(menu, indent=1) if menu else "[] (no liquid candidates — strongly consider NO_TRADE)",
            positions=json.dumps(positions) if positions else "[]",
            equity=(account or {}).get("equity", 0.0),
            cash=(account or {}).get("cash", 0.0),
            daily_pnl=daily_pnl,
            pos_count=len(positions),
            max_pos=SETTINGS.max_open_positions,
        )
        resp = self.client.messages.create(
            model=SETTINGS.llm_model,
            max_tokens=1024,
            # NOTE: temperature omitted — Sonnet 5 rejects non-default values (400).
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        text = resp.content[0].text.strip()
        # strip code fences if present
        if text.startswith("```"):
            text = text.split("```")[1].removeprefix("json").strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return TradeProposal(action="NO_TRADE", thesis=f"unparseable LLM output: {text[:200]}")
        data.pop("spread", None)  # menu-only policy: custom legs disabled by default
        return TradeProposal(**data)
