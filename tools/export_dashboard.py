"""Export dashboard JSON from today's audit log + current snapshot.

Run inside the codespace; commits are pushed from the runbook.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from veritas.audit import AuditLog
from veritas.config import SETTINGS


def main() -> None:
    audit = AuditLog(SETTINGS.audit_dir)
    out_dir = SETTINGS.dashboard_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    events = audit.read_day()
    by_stage = defaultdict(list)
    for e in events:
        by_stage[e["stage"]].append(e)

    # headline metrics
    pnl_events = by_stage.get("pnl_context", [])
    equity = pnl_events[-1]["payload"].get("equity") if pnl_events else None
    daily_pnl = pnl_events[-1]["payload"].get("daily_pnl") if pnl_events else 0.0
    if equity is None:
        # loop hasn't run yet today — fall back to the live account so the
        # dashboard never shows "null equity" to a visitor
        try:
            from alpaca.trading.client import TradingClient

            tc = TradingClient(SETTINGS.alpaca_api_key, SETTINGS.alpaca_api_secret, paper=True)
            acct = tc.get_account()
            equity = float(acct.equity)
            daily_pnl = round(equity - float(acct.last_equity or equity), 2)
        except Exception:  # noqa: BLE001 — dashboard must export even if the API is down
            pass

    executions = [
        e for e in by_stage.get("execution", [])
        if e["payload"].get("submit_result", {}).get("ok")
        or e["payload"].get("action") == "dry_run_skipped"
    ]
    risk_blocks = [
        e for e in by_stage.get("risk", [])
        if not e["payload"].get("verdict", {}).get("approved", True)
    ]

    decisions = []
    # pair proposal → validation → risk by CYCLE id, never positionally:
    # risk events exist only for validation-passing cycles, so a positional
    # zip pairs a NO_TRADE proposal with a later cycle's verdict (confirmed defect)
    val_by_cycle = {e["cycle"]: e for e in by_stage.get("validation", [])}
    risk_by_cycle = {e["cycle"]: e for e in by_stage.get("risk", [])}
    for prop in by_stage.get("llm_proposal", []):
        cyc = prop["cycle"]
        val = val_by_cycle.get(cyc)
        risk = risk_by_cycle.get(cyc)
        decisions.append(
            {
                "ts": prop["ts"],
                "cycle": cyc,
                "action": prop["payload"].get("proposal", {}).get("action"),
                "thesis": prop["payload"].get("proposal", {}).get("thesis", "")[:300],
                "confidence": prop["payload"].get("proposal", {}).get("confidence"),
                "validation": val["payload"].get("report", {}).get("passed") if val else None,
                "risk_approved": risk["payload"].get("verdict", {}).get("approved") if risk else None,
                "risk_failures": risk["payload"].get("verdict", {}).get("failures", []) if risk else [],
            }
        )

    state = {
        "updated_at": events[-1]["ts"] if events else None,
        "equity": equity,
        "daily_pnl": daily_pnl,
        "trades_today": len(executions),
        "risk_blocks_today": len(risk_blocks),
        "decisions": decisions[-30:],
        "n_events_today": len(events),
    }
    (out_dir / "state.json").write_text(json.dumps(state, indent=1, default=str))
    print(json.dumps({k: v for k, v in state.items() if k != "decisions"}, indent=1, default=str))


if __name__ == "__main__":
    main()
