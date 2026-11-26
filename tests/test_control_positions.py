"""Тесты контроля позиций: трейлинг лонгов (без сети)."""
from __future__ import annotations

import control_positions


class FakeClient:
    def __init__(self, positions, orders=None):
        self.positions_list = positions
        self.orders = orders or []
        self.canceled = []
        self.placed = []

    def positions(self):
        return self.positions_list

    def active_orders(self):
        return [o for o in self.orders if o.get("sltp_order")]

    def cancel_order(self, order_id):
        self.canceled.append(order_id)
        self.orders = [o for o in self.orders if o.get("order_id") != order_id]
        return {"order_id": order_id, "status": "ORDER_STATUS_DISABLED"}

    def place_sltp(self, symbol, side, quantity, *, sl_price=None, tp_price=None, comment="", **kw):
        self.placed.append({"symbol": symbol, "quantity": quantity, "sl": sl_price, "comment": comment})
        order = {
            "order_id": f"new{len(self.placed)}",
            "status": "ORDER_STATUS_WATCHING",
            "sltp_order": {
                "symbol": symbol,
                "quantity_sl": {"value": str(quantity)},
                "sl_price": {"value": str(sl_price)},
            },
        }
        self.orders.append(order)
        return order


def _pos(symbol, qty, entry, last, pnl):
    return {
        "symbol": symbol,
        "quantity": {"value": str(qty)},
        "average_price": {"value": str(entry)},
        "current_price": {"value": str(last)},
        "unrealized_pnl": {"value": str(pnl)},
    }


def test_trail_profit_above_3_uses_aggressive_stop():
    """Прибыль > 3% -> стоп в 0.5% от цены (агрессивный режим)."""
    client = FakeClient(positions=[_pos("T@MISX", 4, 275.06, 285.0, 39.76)])
    control_positions.trail_longs(client)
    assert len(client.placed) == 1
    assert client.placed[0]["sl"] == 283.575  # 285.0 * (1 - 0.005)
    assert client.placed[0]["comment"].startswith("trail aggressive")


def test_trail_profit_between_1_and_3_uses_classic():
    """Прибыль в (1%, 3%] -> классический трейлинг (стоп = вход * (1 + прибыль - 1))."""
    client = FakeClient(positions=[_pos("T@MISX", 4, 100.0, 102.0, 8.0)])
    control_positions.trail_longs(client)
    assert len(client.placed) == 1
    assert client.placed[0]["sl"] == 101.0
    assert client.placed[0]["comment"] == "trail 1%/1%"


def test_trail_profit_below_1_no_placement():
    client = FakeClient(positions=[_pos("T@MISX", 4, 275.06, 274.0, -4.0)])
    control_positions.trail_longs(client)
    assert client.placed == []
    assert client.canceled == []


def test_trail_params_defaults_and_config(monkeypatch):
    monkeypatch.setattr(control_positions, "load_config", lambda: {})
    assert control_positions._trail_params() == (3.0, 0.005)
    cfg = {"trailing": {"stocks": {"aggressive_from_profit_pct": 2.5, "aggressive_trail_frac": 0.004}}}
    assert control_positions._trail_params(cfg) == (2.5, 0.004)
