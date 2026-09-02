"""Market data via alpaca-py: equity bars/quotes + options chain snapshots.

One snapshot per cycle is the single source of truth (documented weakness:
real-time vs historical endpoints can disagree). Everything downstream reads
from the Snapshot object captured here, and the raw snapshot is audit-logged.

Fixes CONFIRMED review findings:
- TimeDay/TimeMinute don't exist in alpaca-py 0.44.0 -> TimeFrame(1, Day) etc.
- TradingClient import restored (was lost in an edit)
- open_interest lives on the contracts endpoint, NOT snapshots -> chain rows
  merge snapshot quotes/greeks with contract OI (2 calls, cached per cycle)
- snapshot audit event no longer embeds huge bar arrays (day-level summary only)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.models import Position
from alpaca.trading.requests import GetOptionContractsRequest

from .audit import AuditLog
from .config import SETTINGS
from .models import cycle_id, utcnow

TF_DAY = TimeFrame(1, TimeFrameUnit.Day)
TF_5MIN = TimeFrame(5, TimeFrameUnit.Minute)


@dataclass
class Snapshot:
    cycle: str
    ts: str
    underliers: dict[str, dict] = field(default_factory=dict)  # symbol -> spot/bars/quote
    chains: dict[str, list[dict]] = field(default_factory=dict)  # symbol -> option row dicts
    clock: dict | None = None
    account: dict | None = None
    positions: list[dict] = field(default_factory=list)


class MarketData:
    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        self.stock_hist = StockHistoricalDataClient(SETTINGS.alpaca_api_key, SETTINGS.alpaca_api_secret)
        self.option_hist = OptionHistoricalDataClient(SETTINGS.alpaca_api_key, SETTINGS.alpaca_api_secret)
        self.trading = TradingClient(SETTINGS.alpaca_api_key, SETTINGS.alpaca_api_secret, paper=True)

    def capture(self) -> Snapshot:
        cyc = cycle_id()
        snap = Snapshot(cycle=cyc, ts=utcnow().isoformat())
        req_symbols = list(SETTINGS.underliers)

        # clock + account + positions (broker state is truth)
        try:
            clock = self.trading.get_clock()
            snap.clock = {
                "is_open": bool(clock.is_open),
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
                "status": str(getattr(acct, "status", "")),
                "options_trading_level": str(getattr(acct, "options_trading_level", "")),
            }
            snap.positions = [self._pos_dict(p) for p in self.trading.get_all_positions()]
        except Exception as e:  # noqa: BLE001 — log and continue; kill-check aborts safely
            snap.account = {"error": str(e)}

        end = utcnow()
        for sym in req_symbols:
            try:
                quote = self.stock_hist.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=sym, feed=SETTINGS.data_feed)
                )
                q = quote[sym]
                bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
                spot = (bid + ask) / 2 if bid and ask else ask or bid
                bars = self.stock_hist.get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=sym,
                        timeframe=TF_DAY,
                        start=end - timedelta(days=40),
                        end=end,
                        feed=SETTINGS.data_feed,
                    )
                )[sym]
                closes = [float(b.close) for b in bars]
                intraday = self.stock_hist.get_stock_bars(
                    StockBarsRequest(
                        symbol_or_symbols=sym,
                        timeframe=TF_5MIN,
                        start=end - timedelta(days=2),
                        end=end,
                        feed=SETTINGS.data_feed,
                    )
                )[sym]
                day_highs = [float(b.high) for b in intraday[-78:]] or ([closes[-1]] if closes else [spot])
                day_lows = [float(b.low) for b in intraday[-78:]] or ([closes[-1]] if closes else [spot])
                snap.underliers[sym] = {
                    "spot": round(spot or 0, 2),
                    "last_close": closes[-1] if closes else spot,
                    "closes_40d": closes,
                    "day_high": max(day_highs),
                    "day_low": min(day_lows),
                }
            except Exception as e:  # noqa: BLE001
                snap.underliers[sym] = {"error": str(e)}

        # options chains: snapshots (quotes/greeks/IV) + contracts (open interest)
        for sym in req_symbols:
            try:
                chain = self.option_hist.get_option_chain(OptionChainRequest(symbol_or_symbols=sym))
                rows = chain[sym] if isinstance(chain, dict) and sym in chain else []
                oi_map: dict[str, int] = {}
                vol_map: dict[str, int] = {}
                try:
                    contracts = self.trading.get_option_contracts(
                        GetOptionContractsRequest(underlying_symbols=[sym], limit=500)
                    )
                    for c in contracts.option_contracts if hasattr(contracts, "option_contracts") else contracts:
                        oi_map[str(c.symbol)] = int(getattr(c, "open_interest", 0) or 0)
                        vol_map[str(c.symbol)] = int(getattr(c, "volume", 0) or 0)
                except Exception as e:  # noqa: BLE001 — OI is best-effort; liquidity gate degrades
                    self.audit.write(cyc, "contracts_oi_error", underlier=sym, error=str(e))
                out_rows = []
                for c in rows:
                    sym_opt = str(c.symbol)
                    cp = "call" if (getattr(c, "type", None) == "call" or sym_opt[-8] == "C") else "put"
                    out_rows.append(
                        {
                            "symbol": sym_opt,
                            "strike": float(c.strike_price),
                            "expiry": str(c.expiration_date),
                            "type": cp,
                            "bid": float(c.bid_price) if c.bid_price is not None else 0.0,
                            "ask": float(c.ask_price) if c.ask_price is not None else 0.0,
                            "open_interest": oi_map.get(sym_opt, 0),
                            "volume": vol_map.get(sym_opt, int(getattr(c, "volume", 0) or 0)),
                            "greeks_delta": getattr(getattr(c, "greeks", None), "delta", None),
                            "implied_vol": getattr(c, "implied_volatility", None),
                        }
                    )
                snap.chains[sym] = out_rows
            except Exception as e:  # noqa: BLE001
                snap.chains[sym] = [{"error": str(e)}]

        # audit: log a compact snapshot summary, not megabytes of bars
        self.audit.write(
            cyc,
            "snapshot",
            underliers={k: {kk: vv for kk, vv in v.items() if kk != "closes_40d"} for k, v in snap.underliers.items()},
            chain_sizes={k: len(v) for k, v in snap.chains.items()},
            clock=snap.clock,
            account=snap.account,
        )
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
