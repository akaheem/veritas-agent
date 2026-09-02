"""Watchdog check: is the agent alive and fresh?

Writes .watchdog_run_cycle if the last committed dashboard state is stale
(>25 min old) during market hours — the workflow then re-runs one cycle.
Never opens new positions itself (VERITAS_KILL_MODE=halt_new is set by the
workflow env).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from veritas.config import NY_TZ, SETTINGS


def main() -> None:
    now = datetime.now(ZoneInfo(NY_TZ))
    # only act during market hours
    if now.weekday() >= 5 or not ((9, 30) <= (now.hour, now.minute) <= (15, 45)):
        print("outside market hours — nothing to do")
        return

    state_path = SETTINGS.dashboard_dir / "state.json"
    stale = True
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            updated = datetime.fromisoformat(state["updated_at"])
            age = datetime.now(timezone.utc) - updated
            stale = age > timedelta(minutes=25)
            print(f"last state update: {age.total_seconds()/60:.0f} min ago")
        except Exception as e:  # noqa: BLE001
            print(f"state unreadable: {e}")
    else:
        print("no state file yet")

    if stale:
        print("STALE — requesting one recovery cycle")
        (state_path.parent.parent / ".watchdog_run_cycle").write_text("1")
    else:
        print("fresh — agent alive")


if __name__ == "__main__":
    sys.exit(main())
