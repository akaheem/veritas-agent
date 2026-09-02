"""Append-only JSONL audit log. Every stage of every cycle lands here.

Design goal (2026 agentic-trading survey): results must be independently
replayable. One file per trading day: logs/audit-YYYY-MM-DD.jsonl
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import AuditEvent, utcnow


class AuditLog:
    def __init__(self, audit_dir: Path) -> None:
        self.audit_dir = audit_dir
        self.audit_dir.mkdir(parents=True, exist_ok=True)

    def _path(self) -> Path:
        return self.audit_dir / f"audit-{utcnow().strftime('%Y-%m-%d')}.jsonl"

    def write(self, cycle: str, stage: str, **payload) -> None:
        event = AuditEvent(ts=utcnow().isoformat(), cycle=cycle, stage=stage, payload=payload)
        with self._path().open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")

    def read_day(self, day: str | None = None) -> list[dict]:
        p = self.audit_dir / f"audit-{day or utcnow().strftime('%Y-%m-%d')}.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
