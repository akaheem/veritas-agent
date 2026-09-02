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

    executions = [
        e for e in by_stage.get("execution", []) if e["payload"].get("submit_result", {}).get("ok")
    ]
    risk_blocks = [
        e for e in by_stage.get("risk", [])
        if not e["payload"].get("verdict", {}).get("approved", True)
    ]

    decisions = []
    for prop, val, risk in zip(
        by_stage.get("llm_proposal", []),
        by_stage.get("validation", []),
        by_stage.get("risk", []),
    ):
        decisions.append(
            {
                "ts": prop["ts"],
                "action": prop["payload"].get("proposal", {}).get("action"),
                "thesis": prop["payload"].get("proposal", {}).get("thesis", "")[:300],
                "confidence": prop["payload"].get("proposal", {}).get("confidence"),
                "validation": val["payload"].get("report", {}).get("passed"),
                "risk_approved": risk["payload"].get("verdict", {}).get("approved"),
                "risk_failures": risk["payload"].get("verdict", {}).get("failures", []),
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
