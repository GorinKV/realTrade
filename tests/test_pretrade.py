"""Тесты предторговой проверки Finam на фейковом клиенте (без сети)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pretrade

MSK = ZoneInfo("Europe/Moscow")


def _quote(last=100.0, bid=99.9, ask=100.1):
    return {
        "last": {"value": str(last)},
        "bid": {"value": str(bid)},
        "ask": {"value": str(ask)},
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _book(bid=99.9, ask=100.1, size=1000):
    return [
        {"price": {"value": str(bid)}, "buy_size": {"value": str(size)}, "sell_size": {"value": "0"}},
        {"price": {"value": str(ask)}, "buy_size": {"value": "0"}, "sell_size": {"value": str(size)}},
    ]


class FakeClient:
    account_id = "950034"

    def __init__(
        self,
        *,
        quote=None,
        book=None,
        cash=100_000.0,
        equity=100_000.0,
        positions=None,
        orders=None,
        longable="AVAILABLE",
        tradeable=True,
    ):
        self._quote = quote or _quote()
        self._book = book or _book()
        self._cash = cash
        self._equity = equity
        self._positions = positions or []
        self._orders = orders or []
        self._longable = longable
        self._tradeable = tradeable

    def asset_params(self, symbol):
        return {
            "tradeable": self._tradeable,
            "is_tradable": self._tradeable,
            "longable": {"value": self._longable},
            "trade_lot_size": "1",
        }

    def quote(self, symbol):
        return self._quote

    def orderbook(self, symbol):
        return self._book

    def cash_rub(self):
        return self._cash

    def equity(self):
        return self._equity

    def positions(self):
        return self._positions

    def active_orders(self):
        return self._orders


def test_preflight_passes_for_reasonable_limit_order():
    verdict = pretrade.preflight(FakeClient(), "SBER", "BUY", 10, 100.1)
    assert verdict.ok, verdict.render()
    assert verdict.card["symbol"] == "SBER@MISX"
    assert verdict.card["commission_rub"] > 0


def test_preflight_blocks_insufficient_cash():
    verdict = pretrade.preflight(FakeClient(cash=100.0), "SBER", "BUY", 10, 100.1)
    assert not verdict.ok
    assert any(c.name == "кэш" for c in verdict.failures)


def test_preflight_blocks_wide_spread():
    wide = _book(bid=97.0, ask=103.0)
    verdict = pretrade.preflight(FakeClient(book=wide), "SBER", "BUY", 1, 100.0)
    assert not verdict.ok
    assert any(c.name == "спред" for c in verdict.failures)


def test_preflight_blocks_price_deviation():
    verdict = pretrade.preflight(FakeClient(), "SBER", "BUY", 1, 105.0)
    assert not verdict.ok
    assert any(c.name == "отклонение цены" for c in verdict.failures)


def test_preflight_blocks_duplicate_order():
    orders = [{"order_id": "42", "status": "ORDER_STATUS_NEW", "order": {"symbol": "SBER@MISX", "side": "SIDE_BUY"}}]
    verdict = pretrade.preflight(FakeClient(orders=orders), "SBER", "BUY", 1, 100.0)
    assert not verdict.ok
    assert any(c.name == "дубли" for c in verdict.failures)


def test_preflight_blocks_when_not_longable():
    verdict = pretrade.preflight(FakeClient(longable="NOT_AVAILABLE"), "SBER", "BUY", 1, 100.0)
    assert not verdict.ok
    assert any(c.name == "покупка разрешена" for c in verdict.failures)


def test_preflight_blocks_sell_without_position():
    verdict = pretrade.preflight(FakeClient(), "SBER", "SELL", 10, None, order_type="ORDER_TYPE_MARKET")
    assert not verdict.ok
    assert any(c.name == "позиция" for c in verdict.failures)


def test_preflight_allows_sell_with_position():
    positions = [{"symbol": "SBER@MISX", "quantity": {"value": "20"}}]
    verdict = pretrade.preflight(
        FakeClient(positions=positions), "SBER", "SELL", 10, None, order_type="ORDER_TYPE_MARKET"
    )
    assert verdict.ok, verdict.render()


def test_preflight_sltp_geometry_ok():
    positions = [{"symbol": "SBER@MISX", "quantity": {"value": "20"}}]
    verdict = pretrade.preflight_sltp(
        FakeClient(positions=positions), "SBER", 10, sl_price=95.0, tp_price=110.0
    )
    assert verdict.ok, verdict.render()


def test_preflight_sltp_bad_geometry():
    positions = [{"symbol": "SBER@MISX", "quantity": {"value": "20"}}]
    verdict = pretrade.preflight_sltp(
        FakeClient(positions=positions), "SBER", 10, sl_price=105.0, tp_price=110.0
    )
    assert not verdict.ok
    assert any(c.name == "геометрия" for c in verdict.failures)


def test_session_window():
    cfg = pretrade.load_config()
    inside, _ = pretrade._in_session(datetime(2026, 11, 24, 12, 0, tzinfo=MSK), cfg)
    evening, _ = pretrade._in_session(datetime(2026, 11, 24, 20, 0, tzinfo=MSK), cfg)
    night, _ = pretrade._in_session(datetime(2026, 11, 24, 3, 0, tzinfo=MSK), cfg)
    assert inside and evening and not night
