"""Fail-closed execution adapter for StandX Perps."""
from __future__ import annotations

import threading
from decimal import Decimal

from src.common.comparison import depth_price
from src.common.market_data import age_seconds, utc_now
from src.execution.journal import OrderJournal
from src.execution.orders import decimal, direction, fresh_quote, unknown
from .client import StandXClient


class StandXExecutionAdapter:
    venue = "standx"
    requires_page_slippage = False
    live_blocker = "StandX 需要钱包鉴权、账户余额、费率和终态回执核验"

    def __init__(self, client: StandXClient, journal: OrderJournal, account: str,
                 enabled: bool = False, poll_seconds: float = 8):
        self.client, self.journal, self.account = client, journal, account
        self.enabled, self.poll_seconds = enabled, poll_seconds
        self.lock = threading.RLock()
        self.verified_fee_bps = None

    @property
    def live_ready(self):
        return bool(self.enabled and self.client.token and self.verified_fee_bps is not None)

    @staticmethod
    def _symbol(symbol):
        return StandXClient.symbol_name(symbol)

    def size_decimals(self, symbol):
        info = self.client.query_symbol_info(symbol)
        value = info.get("qty_tick_decimals", info.get("size_decimals"))
        return int(value) if value is not None else 6

    def get_market_price(self, symbol):
        row = self.client.query_symbol_market(symbol)
        return float(row.get("mark_price") or row.get("mid_price") or row.get("last_price"))

    def get_sizing_book(self, symbol):
        book = self.client.query_depth_book(symbol)
        def levels(values):
            result = []
            for item in values:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    result.append({"price": float(item[0]), "size": float(item[1])})
            return result
        return {"symbol": symbol, "fetched_at": utc_now(),
                "bids": sorted(levels(book["bids"]), key=lambda x: x["price"], reverse=True),
                "asks": sorted(levels(book["asks"]), key=lambda x: x["price"])}

    def get_executable_quote(self, symbol, quantity):
        if not self.live_ready:
            raise ValueError(self.live_blocker)
        book = self.get_sizing_book(symbol)
        return self.quote_from_sizing_book(symbol, quantity, book)

    def quote_from_sizing_book(self, symbol, quantity, book):
        """Build a size-bound quote locally; freshness and depth remain required."""
        if not self.live_ready:
            raise ValueError(self.live_blocker)
        age = age_seconds(book["fetched_at"])
        if age is None or not 0 <= age <= 5:
            raise ValueError("StandX 深度过期")
        buy, sell = depth_price(book["asks"], float(quantity)), depth_price(book["bids"], float(quantity))
        if not buy["sufficient"] or not sell["sufficient"]:
            raise ValueError("StandX 可见深度不足")
        return {"symbol": symbol, "size": quantity, "bid": sell["worst_price"], "ask": buy["worst_price"],
                "bid_vwap": sell["vwap"], "ask_vwap": buy["vwap"], "updated_at": book["fetched_at"],
                "executable": True, "instrument_type": "perpetual", "fee_bps": self.verified_fee_bps, "sizing_book": book}

    def validate_order(self, symbol, quantity, reduce_only=False, side=None):
        if not self.live_ready:
            raise ValueError(self.live_blocker)
        info = self.client.query_symbol_info(symbol)
        market_price = self.get_market_price(symbol)
        minimum = info.get("min_order_qty", info.get("min_base_amount"))
        if not reduce_only and minimum is not None and decimal(quantity, True) < decimal(minimum, True):
            raise ValueError("StandX 未满足最小下单数量")
        balance = self.client.query_balance()
        available = balance.get("cross_available", balance.get("available"))
        if not reduce_only and available is not None and decimal(available, True) < decimal(quantity, True) * decimal(market_price, True):
            raise ValueError("StandX 可用保证金不足")
        positions = [p for p in self.client.query_positions(symbol) if decimal(p.get("qty", p.get("position", "0")), True) != 0]
        if positions and not reduce_only:
            raise ValueError("StandX 当前市场已有持仓，禁止叠加")

    def validate_quote_order(self, symbol, quantity, side, quote, slippage_bps):
        fresh_quote(quote, symbol, quantity)
        return True

    def place_order(self, symbol, side, size, price, slippage_bps, client_order_id, quote, reduce_only=False):
        if not self.live_ready:
            return {"success": False, "confirmed": True, "status": "rejected", "filled_size": 0,
                    "error": self.live_blocker}
        with self.lock:
            fresh_quote(quote, symbol, size)
            self.validate_order(symbol, size, reduce_only=reduce_only, side=side)
            row, new = self.journal.reserve(self.venue, self.account, client_order_id,
                                            {"symbol": symbol, "side": direction(side), "size": str(size),
                                             "price": str(price), "reduce_only": reduce_only})
            if not new:
                return __import__('json').loads(row["result"]) if row["result"] else unknown("提交意图已存在，禁止重发")
            try:
                result = self.client.place_limit_ioc(self._symbol(symbol), direction(side), str(size), str(price),
                                                     reduce_only=reduce_only, cl_ord_id=client_order_id,
                                                     reconcile_seconds=self.poll_seconds)
            except Exception:
                result = unknown("StandX 订单状态未核对，禁止重发")
            self.journal.finish(row["seq"], result)
            return result

    def close_position(self, symbol, side, size, client_order_id, reduce_only=True, slippage_bps=10):
        if not reduce_only:
            raise ValueError("StandX 平仓必须只减仓")
        opposite = "sell" if direction(side) == "buy" else "buy"
        quote = self.get_executable_quote(symbol, size)
        return self.place_order(symbol, opposite, size, quote["ask" if opposite == "buy" else "bid"],
                                slippage_bps, client_order_id, quote, reduce_only=True)

    def assert_flat(self, symbol):
        return not [p for p in self.client.query_positions(symbol) if decimal(p.get("qty", p.get("position", "0")), True) != 0]
