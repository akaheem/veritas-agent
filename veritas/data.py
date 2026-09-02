"""Market data via alpaca-py: equity bars/quotes + options chain snapshots.

One snapshot per cycle is the single source of truth (documented weakness:
real-time vs historical endpoints can disagree). Everything downstream reads
from the Snapshot object captured here, and the raw snapshot is audit-logged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    OptionLatestQuoteRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeDay, TimeMinute
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Position

from .audit import AuditLog
from .config import SETTINGS
from .models import cycle_id, utcnow


@dataclass
class Snapshot:
    cycle: str
    ts: str
    underliers: dict[str, dict] = field(default_factory=dict)  # symbol -> spot/bars/quote
    chains: dict[str, list[dict]] = field(default_factory=dict)  # symbol -> option snapshot dicts
    clock: dict | None = None
    account: dict | None = None
    positions: list[dict] = field(default_factory=list)


class MarketData:
    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        self.stock_hist = StockHistoricalDataClient(
            SETTINGS.alpaca_api_key, SETTINGS.alpaca_api_secret, url_override=None
        )
        self.option_hist = OptionHistoricalDataClient(SETTINGS.alpaca_api_key, SETTINGS.alpaca_api_secret)
        self.trading = TradingClient(
            SETTINGS.alpaca_api_key, SETTINGS.alpaca_api_secret, paper=True
        )

    def capture(self) -> Snapshot:
        cyc = cycle_id()
        snap = Snapshot(cycle=cyc, ts=utcnow().isoformat())
        req_symbols = list(SETTINGS.underliers)

        # clock + account (broker state is truth)
        try:
            clock = self.trading.get_clock()
            snap.clock = {
                "is_open": clock.is_open,
                "next_open": str(clock.next_open),
                "next_close": str(clock.next_close),
                "timestamp": str(clock.timestamp),
            }
            acct = self.trading.get_account()
            snap.account = {
                "equity": float(acct.equity),
                "cash": float(acct.cash),
                "buying_power": float(acct.buying_power),
                "last_equity": float(acct.last_equity),
                "daytrade_count": getattr(acct, "daytrade_count", None),
            }
            snap.positions = [
                self._pos_dict(p) for p in self.trading.get_all_positions()
            ]
        except Exception as e:  # noqa: BLE001 — log and continue; reconciliation will catch up
            snap.account = {"error": str(e)}

        for sym in req_symbols:
            try:
                quote = self.stock_hist.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=sym, feed=SETTINGS.data_feed)
                )
                q = quote[sym]
                spot = (float(q.bid_price) + float(q.ask_price)) / 2 or float(q.ask_price)
                end = utcnow()
                start = end - __import__("datetime").timedelta(days=40)
                bars = self.stock_hist.get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=sym,
                        timeframe=TimeDay,
                        start=start,
                        end=end,
                        feed=SETTINGS.data_feed,
                    )
                )[sym]
                closes = [float(b.close) for b in bars]
                last = float(bars[-1].close) if closes else spot
                intraday = self.stock_hist.get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=sym,
                        timeframe=TimeMinute(5),
                        start=end - __import__("datetime").timedelta(days=2),
                        end=end,
                        feed=SETTINGS.data_feed,
                    )
                )[sym]
                day_highs = [float(b.high) for b in intraday[-78:]] or [last]
                day_lows = [float(b.low) for b in intraday[-78:]] or [last]
                snap.underliers[sym] = {
                    "spot": round(spot, 2),
                    "last_close": last,
                    "closes_40d": closes,
                    "day_high": max(day_highs),
                    "day_low": min(day_lows),
                }
            except Exception as e:  # noqa: BLE001
                snap.underliers[sym] = {"error": str(e)}

        # options chains
        for sym in req_symbols:
            try:
                chain_req = OptionChainRequest(symbol_or_symbols=sym)
                chain = self.option_hist.get_option_chain(chain_req)
                rows = []
                for c in chain[sym] if isinstance(chain, dict) else chain:
                    rows.append(
                        {
                            "symbol": c.symbol,
                            "strike": float(c.strike_price),
                            "expiry": str(c.expiration_date),
                            "type": c.type if hasattr(c, "type") else ("call" if c.symbol[-8] == "C" else "put"),
                            "bid": float(c.bid_price) if c.bid_price is not None else 0.0,
                            "ask": float(c.ask_price) if c.ask_price is not None else 0.0,
                            "open_interest": int(c.open_interest or 0),
                            "volume": int(c.volume or 0),
                            "greeks_delta": getattr(getattr(c, "greeks", None), "delta", None),
                            "implied_vol": getattr(c, "implied_volatility", None),
                        }
                    )
                snap.chains[sym] = rows
            except Exception as e:  # noqa: BLE001
                snap.chains[sym] = [{"error": str(e)}]

        self.audit.write(cyc, "snapshot", snapshot=snap.__dict__ | {"chains": "elided"})
        return snap

    @staticmethod
    def _pos_dict(p: Position) -> dict:
        return {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "side": str(p.side),
            "avg_entry": float(p.avg_entry_price),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
            "asset_class": str(getattr(p, "asset_class", "us_equity")),
        }
