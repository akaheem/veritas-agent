"""Execution layer — routes orders through the Alpaca MCP Server (hackathon req).

Pattern: long-lived stdio MCP session (official `mcp` SDK — ClientSession +
stdio_client, the API shipped in mcp 1.29.x), explicit env allow-list,
client_order_id idempotency on every order, retry w/ backoff on 5xx/timeout,
and a 5-min reconciliation loop where broker state is truth.

Multi-leg policy (documented weakness: replace_order fails on multi-leg):
NEVER replace an order. Cancel + place fresh with a new client_order_id.
Master Plan v2 §6: timeout ⇒ UNKNOWN state — find_order_by_client_id
reconciles before any resubmission.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .audit import AuditLog
from .config import SETTINGS
from .models import SpreadCandidate, utcnow

SERVER_CMD = "alpaca-mcp-server"  # pip-installed in the codespace venv (v2.3.1)
TOOLSETS_ENV = {"ALPACA_TOOLSETS": "account,trading,options-data,stock-data,assets"}
CALL_TIMEOUT = timedelta(seconds=45)  # hard cap: a hung subprocess stalls nothing


class McpBroker:
    """Thin async wrapper. Holds one MCP session open for the whole loop."""

    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        self._session: ClientSession | None = None
        self._streams = None
        self._params = StdioServerParameters(
            command=SERVER_CMD,
            args=[],
            env={
                "ALPACA_API_KEY": SETTINGS.alpaca_api_key,
                "ALPACA_SECRET_KEY": SETTINGS.alpaca_api_secret,
                "ALPACA_PAPER_TRADE": "true",
                **TOOLSETS_ENV,
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/home/vscode",
            },
        )

    async def __aenter__(self) -> "McpBroker":
        self._streams = stdio_client(self._params)
        read, write = await self._streams.__aenter__()
        self._session = ClientSession(read, write, read_timeout_seconds=CALL_TIMEOUT)
        await self._session.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            try:
                await self._session.__aexit__(*exc)
            except Exception:  # noqa: BLE001
                pass
        if self._streams:
            try:
                await self._streams.__aexit__(*exc)
            except Exception:  # noqa: BLE001
                pass

    async def call(self, tool: str, args: dict, retries: int = 3) -> dict:
        """Call an MCP tool with backoff. Returns structured content dict.
        Tool errors come back as results (is_error), not exceptions."""
        if not self._session:
            raise RuntimeError("MCP session not open")
        delay = 2.0
        last_err = None
        for attempt in range(retries):
            try:
                result = await self._session.call_tool(tool, args)
                structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None) or {}
                data = structured.get("result", structured)
                if getattr(result, "isError", False) or getattr(result, "is_error", False):
                    # genuine tool-level error: retry only for transient-looking messages
                    text = str(getattr(result, "content", ""))
                    if any(k in text.lower() for k in ("rate limit", "502", "timeout", "temporarily")):
                        last_err = text
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
                    return {"ok": False, "error": text, "tool": tool}
                self.audit.write("mcp", f"call:{tool}", args=_redact(args), ok=True)
                return {"ok": True, "data": data}
            except Exception as e:  # noqa: BLE001 — transport level: timeout/5xx/connection
                last_err = f"{type(e).__name__}: {e}"
                self.audit.write("mcp", f"call_error:{tool}", error=last_err, attempt=attempt)
                await asyncio.sleep(delay)
                delay *= 2
        return {"ok": False, "error": f"retries exhausted: {last_err}", "tool": tool}

    # ---------- account / state ----------
    async def get_account(self) -> dict:
        r = await self.call("get_account_info", {})
        return r.get("data", {}) if r["ok"] else {"error": r.get("error")}

    async def get_positions(self) -> list[dict]:
        r = await self.call("get_all_positions", {})
        if not r["ok"]:
            return []
        d = r.get("data")
        return d if isinstance(d, list) else []

    async def get_activities(self) -> list[dict]:
        r = await self.call("get_account_activities", {})
        d = r.get("data") if r["ok"] else []
        return d if isinstance(d, list) else []

    # ---------- orders ----------
    async def open_credit_spread(self, spread: SpreadCandidate, idem_tag: str, credit_ratio: float | None = None) -> dict:
        """Open a 2-leg credit spread as a single mleg order. limit_price = NET CREDIT (negative).

        Research-verified conventions (docs.alpaca.markets, alpaca-mcp-server v2.3.1):
        - mleg limit_price: positive = debit, NEGATIVE = credit received
        - legs passed as a REAL JSON array (issue #97 only bites clients that stringify)
        - position_intent required per leg; order_class auto-inferred from legs
        - Master Plan v2 §8: entry price is ADAPTIVE — credit_ratio comes from the
          Execution Confidence Score (0.90 normal / 0.80 reduced); the fixed
          entry_credit_buffer is only a fallback.
        """
        ratio = credit_ratio if credit_ratio is not None else SETTINGS.entry_credit_buffer
        legs = []
        for leg in spread.legs:
            legs.append(
                {
                    "symbol": leg.symbol,
                    "ratio_qty": str(leg.ratio_qty),
                    "side": leg.side,
                    "position_intent": "sell_to_open" if leg.side == "sell" else "buy_to_open",
                }
            )
        net_credit = -(spread.credit * ratio)  # negative = credit
        args = {
            "qty": str(spread.contracts),
            "type": "limit",
            "time_in_force": "day",
            "limit_price": f"{net_credit:.2f}",
            "client_order_id": f"veritas-open-{idem_tag}-{uuid.uuid4().hex[:8]}",
            "legs": legs,
        }
        self.audit.write("mcp", "submit_open", args=_redact(args),
                         credit_ratio=ratio, mid_credit=spread.credit)
        return await self.call("place_option_order", args)

    async def close_credit_spread(self, spread: SpreadCandidate, debit_limit: float, idem_tag: str) -> dict:
        """Close by reversing the legs. debit_limit = max price we pay to close (positive)."""
        legs = []
        for leg in spread.legs:
            legs.append(
                {
                    "symbol": leg.symbol,
                    "ratio_qty": str(leg.ratio_qty),
                    "side": "buy" if leg.side == "sell" else "sell",
                    "position_intent": "buy_to_close" if leg.side == "sell" else "sell_to_close",
                }
            )
        args = {
            "qty": str(spread.contracts),
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(round(debit_limit, 2)),
            "client_order_id": f"veritas-close-{idem_tag}-{uuid.uuid4().hex[:8]}",
            "legs": legs,
        }
        self.audit.write("mcp", "submit_close", args=_redact(args))
        return await self.call("place_option_order", args)

    async def cancel(self, order_id: str) -> dict:
        return await self.call("cancel_order_by_id", {"order_id": order_id})

    async def get_orders(self, status: str = "open") -> list[dict]:
        r = await self.call("get_orders", {"status": status})
        d = r.get("data") if r["ok"] else []
        return d if isinstance(d, list) else []

    async def find_order_by_client_id(self, client_order_id: str) -> dict | None:
        """UNKNOWN-state reconciliation: query by client_order_id (Master Plan v2 §6.2)."""
        r = await self.call("get_order_by_client_id", {"client_order_id": client_order_id})
        return r.get("data") if r.get("ok") else None

    # ---------- working-order registry (gates must see accepted-but-unfilled risk) ----------
    def _reg_path(self):
        from pathlib import Path

        return Path("./data/working_orders.json")

    def _load_reg(self) -> dict:
        p = self._reg_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _save_reg(self, reg: dict) -> None:
        p = self._reg_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(reg, indent=0), encoding="utf-8")

    async def remember_working(self, order_id: str, tag: str, spread) -> None:
        reg = self._load_reg()
        reg[order_id] = {
            "tag": tag,
            "adjusted_max_loss": getattr(spread, "adjusted_max_loss", 0.0),
            "submitted_at": utcnow().isoformat(),
        }
        self._save_reg(reg)

    async def get_open_veritas_orders(self) -> list[dict]:
        """Working veritas orders + persisted heat for the risk gates."""
        reg = self._load_reg()
        open_orders = await self.get_orders("open")
        out = []
        for o in open_orders:
            oid = str(o.get("id", ""))
            if oid in reg or str(o.get("client_order_id", "")).startswith("veritas-"):
                out.append({
                    "id": oid,
                    "tag": reg.get(oid, {}).get("tag"),
                    "adjusted_max_loss": reg.get(oid, {}).get("adjusted_max_loss", 0.0),
                })
        # prune registry entries no longer working
        open_ids = {str(o.get("id")) for o in open_orders}
        stale = [k for k in reg if k not in open_ids]
        if stale:
            for k in stale:
                reg.pop(k, None)
            self._save_reg(reg)
        return out

    async def cancel_all_working(self) -> int:
        """Cancel every working veritas order (kill/EOD: never let a stale fill
        open a position after flatten). Returns count of cancel attempts."""
        n = 0
        for o in await self.get_open_veritas_orders():
            if o.get("id"):
                r = await self.cancel(o["id"])
                n += 1 if r.get("ok") else 0
        self.audit.write("mcp", "cancel_all_working", cancelled=n)
        return n


def _redact(args: dict) -> dict:
    """Audit-log args without any sensitive material (none expected, belt+braces)."""
    return {k: v for k, v in args.items() if k not in ("key", "secret")}


def _redact_unused(*_a) -> None:  # placeholder to keep imports honest
    return None
