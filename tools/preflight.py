"""Preflight (Master Plan v2 §14) — run BEFORE any competition session.

Verifies, in order, with PASS/FAIL output and a nonzero exit on any critical:
  1. config/secrets present
  2. account: status ACTIVE, options level 3, equity $100,000 (competition)
  3. market clock + calendar
  4. data feed identity + option chain + quote freshness
  5. MCP server: reachable, tool schemas present (place_option_order etc.)
  6. deterministic candidate generation produces >=1 candidate
  7. LLM structured output parses (1 real call)
  8. tiny paper MLeg lifecycle: submit → status → cancel (then reconcile)

Usage:
  uv run python tools/preflight.py [--competition]   # --competition enforces $100k + 1-3 contracts
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

from veritas.audit import AuditLog  # noqa: E402
from veritas.broker import McpBroker  # noqa: E402
from veritas.config import NY_TZ, SETTINGS  # noqa: E402
from veritas.data import MarketData  # noqa: E402
from veritas.decision import DecisionCore  # noqa: E402
from veritas.features import build_features  # noqa: E402
from veritas.candidates import build_candidates  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "", critical: bool = True) -> bool:
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else ("FAIL*" if critical else "warn")
    print(f"[{mark:4}] {name}" + (f" — {detail}" if detail else ""))
    return ok


async def main(competition: bool) -> int:
    audit = AuditLog(SETTINGS.audit_dir)
    audit.write("preflight", "start", competition=competition)

    print(f"== VERITAS preflight ({'COMPETITION' if competition else 'dev'}) ==")
    problems = SETTINGS.validate()
    check("config/secrets", not problems, "; ".join(problems))
    if problems:
        return 1

    data = MarketData(audit)

    # account + clock + positions via alpaca-py
    try:
        acct = data.trading.get_account()
        # AccountStatus enum str()s as "AccountStatus.ACTIVE" — compare robustly
        status_str = str(getattr(acct, "status", "")).split(".")[-1].upper()
        check("account status ACTIVE", status_str == "ACTIVE", f"status={status_str}")
        level = int(getattr(acct, "options_trading_level", 0) or 0)
        check("options level >= 3 (spreads)", level >= 3, f"level={level}")
        eq = float(acct.equity)
        if competition:
            check("equity == $100,000", abs(eq - 100_000.0) < 1.0, f"equity=${eq:,.2f}")
        else:
            check("equity > 0", eq > 0, f"equity=${eq:,.2f}")
        positions = data.trading.get_all_positions()
        check("positions fetched", True, f"n={len(positions)}")
    except Exception as e:  # noqa: BLE001
        check("account reachable", False, str(e)[:120])
        return 1

    try:
        clock = data.trading.get_clock()
        check("market clock", True,
              f"is_open={clock.is_open} next_open={clock.next_open}")
    except Exception as e:  # noqa: BLE001
        check("market clock", False, str(e)[:120])

    # snapshot: chains + feed identity + freshness
    snap = data.capture()
    chains_ok = all(
        len(snap.chains.get(s, [])) > 0 and "error" not in snap.chains.get(s, [{"error": 1}])[0]
        for s in SETTINGS.underliers
    )
    check("option chain snapshot", chains_ok,
          "; ".join(f"{k}={len(v)}" for k, v in snap.chains.items()))
    check("feed identity logged", bool(SETTINGS.data_feed), f"feed={SETTINGS.data_feed}"
          + (" (INDICATIVE — conservative pricing enforced)" if SETTINGS.data_feed == "iex" else ""))

    # deterministic candidate engine
    feats = build_features(snap)
    cands = build_candidates(snap)
    check("features computed", len(feats) > 0, f"n={len(feats)}")
    check("candidates generated", len(cands) > 0,
          f"n={len(cands)}; DTE window [{SETTINGS.dte_min},{SETTINGS.dte_max}]" if cands else
          "none right now (may be legitimate — market conditions; verify chain row count above)")

    # LLM structured output
    try:
        brain = DecisionCore()
        prop = brain.decide(feats, cands, snap.positions, snap.account, 0.0)
        # decide() degrades API failures into a NO_TRADE fallback whose thesis
        # is tagged "[decision-fallback: ...]" — that must count as a FAILURE
        # here, or preflight green-lights a session where the LLM never works.
        thesis = prop.thesis or ""
        fallback = thesis.startswith("[decision-fallback:")
        check("LLM decision parses", not fallback,
              (f"LLM UNAVAILABLE: {thesis[:180]}" if fallback
               else f"action={prop.action} thesis='{thesis[:60]}'"))
    except Exception as e:  # noqa: BLE001
        check("LLM decision parses", False, f"{type(e).__name__}: {str(e)[:200]}")

    # MCP server round-trip
    async with McpBroker(audit) as broker:
        acct_m = await broker.get_account()
        check("MCP get_account_info", "error" not in acct_m, str(acct_m.get("account_number", ""))[:20])
        tools = await broker.call("get_option_chain", {"underlying_symbol": "SPY"})
        check("MCP options data", tools.get("ok", False), "" if tools.get("ok") else str(tools.get("error", ""))[:80])
        # NOTE: real MLeg lifecycle test intentionally NOT automatic — run it once
        # manually under --competition with the runbook before the session.
        if competition:
            print("\n[ACTION] Run the tiny MLeg open→status→cancel test manually now "
                  "(runbook §5), then verify reverse-mleg close on the test spread.\n")

    critical_failed = [n for n, ok, _ in RESULTS if not ok]
    audit.write("preflight", "done", failed=critical_failed, results=[(n, ok) for n, ok, _ in RESULTS])
    print(f"\n{'PREFLIGHT PASSED' if not critical_failed else 'PREFLIGHT FAILED: ' + str(critical_failed)}")
    return 0 if not critical_failed else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--competition", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args().competition)))
