"""Order state machine (Master Plan v2 §6.1) — timeout never implies rejection.

PENDING_SUBMIT → SUBMITTED → (PARTIALLY_FILLED) → FILLED
                ↘ UNKNOWN → (reconcile by client_order_id) → SUBMITTED | CANCELED | REJECTED

UNKNOWN after a timeout/502: we NEVER blindly resubmit. Instead we query the
broker by client_order_id; only a confirmed absence lets us submit a fresh
idempotent order. Duplicate client_order_id is rejected by Alpaca (422), which
is itself a safe signal that the original order exists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import json


class OrderState(str, Enum):
    PENDING_SUBMIT = "pending_submit"
    SUBMITTED = "submitted"
    UNKNOWN = "unknown"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


ALLOWED_TRANSITIONS: dict[OrderState, set[OrderState]] = {
    OrderState.PENDING_SUBMIT: {OrderState.SUBMITTED, OrderState.UNKNOWN, OrderState.REJECTED},
    OrderState.SUBMITTED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED, OrderState.UNKNOWN},
    OrderState.UNKNOWN: {OrderState.SUBMITTED, OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED},
    OrderState.PARTIALLY_FILLED: {OrderState.FILLED, OrderState.CANCELED, OrderState.UNKNOWN},
    OrderState.FILLED: set(),
    OrderState.CANCELED: set(),
    OrderState.REJECTED: set(),
}

# Alpaca order status → local state
ALPACA_STATUS_MAP = {
    "new": OrderState.SUBMITTED,
    "accepted": OrderState.SUBMITTED,
    "pending_new": OrderState.SUBMITTED,
    "accepted_for_bidding": OrderState.SUBMITTED,
    "held": OrderState.SUBMITTED,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    "filled": OrderState.FILLED,
    "done_for_day": OrderState.CANCELED,
    "canceled": OrderState.CANCELED,
    "expired": OrderState.CANCELED,
    "replaced": OrderState.CANCELED,  # we never request replace; treat as closed
    "pending_cancel": OrderState.UNKNOWN,
    "pending_replace": OrderState.UNKNOWN,
    "stopped": OrderState.CANCELED,
    "rejected": OrderState.REJECTED,
    "suspended": OrderState.REJECTED,
    "calc": OrderState.SUBMITTED,
}


def map_status(broker_status: str | None) -> OrderState:
    return ALPACA_STATUS_MAP.get(str(broker_status or "").lower(), OrderState.UNKNOWN)


def can_transition(current: OrderState, nxt: OrderState) -> bool:
    return nxt in ALLOWED_TRANSITIONS.get(current, set())


class OrderStateMachine:
    """File-persisted registry of client_order_id → state transitions."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path("./data/order_states.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.orders: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.orders = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self.orders = {}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.orders, indent=0), encoding="utf-8")

    def register(self, client_order_id: str) -> OrderState:
        self.orders[client_order_id] = {
            "state": OrderState.PENDING_SUBMIT.value,
            "history": [{"ts": datetime.now(timezone.utc).isoformat(), "state": OrderState.PENDING_SUBMIT.value}],
        }
        self._save()
        return OrderState.PENDING_SUBMIT

    def transition(self, client_order_id: str, nxt: OrderState, note: str = "") -> bool:
        rec = self.orders.get(client_order_id)
        if not rec:
            self.register(client_order_id)
            rec = self.orders[client_order_id]
        cur = OrderState(rec["state"])
        if cur is nxt:
            return True
        if not can_transition(cur, nxt):
            # unknown is always a legal observer state; otherwise refuse regression
            if nxt is not OrderState.UNKNOWN:
                return False
        rec["state"] = nxt.value
        rec.setdefault("history", []).append({"ts": datetime.now(timezone.utc).isoformat(), "state": nxt.value, "note": note})
        self._save()
        return True

    def get(self, client_order_id: str) -> OrderState | None:
        rec = self.orders.get(client_order_id)
        return OrderState(rec["state"]) if rec else None

    def on_timeout(self, client_order_id: str) -> OrderState:
        """Network failure during submit: mark UNKNOWN. Never resubmit blind."""
        rec = self.orders.get(client_order_id)
        if rec and OrderState(rec["state"]) is OrderState.PENDING_SUBMIT:
            self.transition(client_order_id, OrderState.UNKNOWN, "submit timeout — reconcile before retry")
        return self.get(client_order_id) or OrderState.UNKNOWN

    def unresolved(self) -> list[str]:
        return [k for k, v in self.orders.items() if v["state"] in (OrderState.UNKNOWN.value, OrderState.PENDING_SUBMIT.value)]
