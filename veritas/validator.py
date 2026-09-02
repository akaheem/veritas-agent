"""Deterministic validator — recomputes every LLM-adjacent number from raw legs.

Documented weakness: LLMs hallucinate numerics (2026 LLM-trading evaluation
studies). Policy: the proposal's contracts count is honored, but ALL economics
are recomputed from leg bid/ask captured in the same snapshot. Custom legs are
checked for existence in the snapshot; anything unknown is rejected.
"""

from __future__ import annotations

from .config import SETTINGS
from .models import SpreadCandidate, TradeProposal, ValidationReport
from .spreads import execution_reality_haircut, spread_math


def validate(proposal: TradeProposal, candidates: list[SpreadCandidate]) -> ValidationReport:
    checks: dict[str, bool] = {}
    failures: list[str] = []

    if proposal.action != "PROPOSE":
        return ValidationReport(passed=True, checks={"no_trade": True})

    spread: SpreadCandidate | None = None
    if proposal.candidate_index is not None:
        if 0 <= proposal.candidate_index < len(candidates):
            spread = candidates[proposal.candidate_index]
            checks["menu_index_valid"] = True
        else:
            failures.append(f"candidate_index {proposal.candidate_index} out of range (menu size {len(candidates)})")
            checks["menu_index_valid"] = False
    elif proposal.spread is not None:
        checks["custom_legs_accepted"] = False
        failures.append("custom legs disabled in competition config; menu-only")
    else:
        failures.append("PROPOSE without candidate_index or spread")

    if spread is None:
        return ValidationReport(passed=False, checks=checks, failures=failures)

    contracts = max(1, min(int(proposal.contracts), SETTINGS.max_contracts_per_leg))
    m = spread_math(
        spread.legs[0].strike,
        spread.legs[1].strike,
        (spread.legs[0].bid + spread.legs[0].ask) / 2,
        (spread.legs[1].bid + spread.legs[1].ask) / 2,
        spread.kind.split("_")[1],
        spread.spot,
        contracts,
    )
    adj_credit, adj_max_loss = execution_reality_haircut(
        m["credit"], m["width"], contracts, SETTINGS.slippage_per_leg, SETTINGS.slippage_ratio
    )
    corrected = spread.model_copy(
        update={
            "contracts": contracts,
            "credit": m["credit"],
            "width": m["width"],
            "max_loss": m["max_loss"],
            "breakeven": m["breakeven"],
            "edge_ratio": m["edge_ratio"],
            "adjusted_credit": adj_credit,
            "adjusted_max_loss": adj_max_loss,
        }
    )

    if m["credit"] <= 0:
        failures.append("recomputed credit <= 0")
    if m["edge_ratio"] < SETTINGS.min_edge_ratio:
        failures.append(f"edge ratio {m['edge_ratio']} below floor {SETTINGS.min_edge_ratio}")
    if corrected.adjusted_credit <= 0:
        failures.append("credit does not survive execution-reality haircut")
    checks["credit_positive"] = m["credit"] > 0
    checks["edge_ratio_ok"] = m["edge_ratio"] >= SETTINGS.min_edge_ratio
    checks["survives_haircut"] = corrected.adjusted_credit > 0
    checks["dte_window"] = SETTINGS.dte_min <= spread.dte <= SETTINGS.dte_max
    if not checks["dte_window"]:
        failures.append(f"DTE {spread.dte} outside [{SETTINGS.dte_min},{SETTINGS.dte_max}]")

    return ValidationReport(
        passed=not failures, checks=checks, failures=failures, corrected=corrected
    )
