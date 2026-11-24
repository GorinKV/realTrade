"""Тесты трейлинг-стопа (без сети)."""
from __future__ import annotations

from datetime import datetime, timezone

import trailing
from trailing import find_sl_order, next_stop, round_step, tick


def test_round_step():
    assert round_step(89.534, 0.01) == 89.53
    assert round_step(89.535, 0.01) == 89.54
    assert round_step(3626.66, 0.1) == 3626.7
    assert round_step(2.119689, 0.0001) == 2.1197


def test_next_stop_example_from_user():
    # цена 90 -> стоп 89.7 при отступе 0.3
    assert next_stop(90.0, 0.3, 0.01) == 89.7
    assert next_stop(90.5, 0.3, 0.01) == 90.2


def test_find_sl_order_ignores_tp_only_and_other_symbols():
    orders = [
        {"order_id": "1", "sltp_order": {"symbol": "BRZ6@RTSX", "quantity_sl": {"value": "0"}, "sl_price": {"value": "0"}}},
        {"order_id": "2", "sltp_order": {"symbol": "FLOT@MISX", "quantity_sl": {"value": "40"}, "sl_price": {"value": "76.74"}}},
        {"order_id": "3", "sltp_order": {"symbol": "BRZ6@RTSX", "quantity_sl": {"value": "1"}, "sl_price": {"value": "88.69"}}},
    ]
    found = find_sl_order(orders, "BRZ6@RTSX")
    assert found is not None and found["order_id"] == "3"
    assert find_sl_order(orders, "SBER") is None


class FakeClient:
    def __init__(self, last=90.0, position=1.0, sl=None):
        self.last = last
        self.position = position
        self.orders = []
        self.canceled = []
        self.placed = []
        if sl is not None:
            self.orders.append(
                {
                    "order_id": "old",
                    "status": "ORDER_STATUS_WATCHING",
                    "sltp_order": {
                        "symbol": "BRZ6@RTSX",
                        "quantity_sl": {"value": "1"},
                        "sl_price": {"value": str(sl)},
                    },
                }
            )

    def quote(self, symbol):
        return {"last": {"value": str(self.last)}, "timestamp": datetime.now(timezone.utc).isoformat()}

    def positions(self):
        return [{"symbol": "BRZ6@RTSX", "quantity": {"value": str(self.position)}}] if self.position else []

    def active_orders(self):
        return list(self.orders)

    def cancel_order(self, order_id):
        self.canceled.append(order_id)
        self.orders = [o for o in self.orders if o["order_id"] != order_id]
        return {"order_id": order_id, "status": "ORDER_STATUS_DISABLED"}

    def place_sltp(self, symbol, side, quantity, *, sl_price=None, tp_price=None, comment="", **kw):
        self.placed.append({"symbol": symbol, "quantity": quantity, "sl": sl_price})
        order = {
            "order_id": f"new{len(self.placed)}",
            "status": "ORDER_STATUS_WATCHING",
            "sltp_order": {
                "symbol": "BRZ6@RTSX",
                "quantity_sl": {"value": str(quantity)},
                "sl_price": {"value": str(sl_price)},
            },
        }
        self.orders.append(order)
        return order


def test_tick_moves_stop_up_and_cancels_old(tmp_path):
    client = FakeClient(last=90.0, sl=88.69)
    state = tmp_path / "state.json"
    result = tick(client, "BRZ6@RTSX", 1, 0.3, step=0.01, state_file=state, log=lambda *_: None)
    assert result["status"] == "moved"
    assert result["sl"] == 89.7
    assert client.canceled == ["old"]
    assert client.placed == [{"symbol": "BRZ6@RTSX", "quantity": 1, "sl": 89.7}]


def test_tick_never_moves_stop_down(tmp_path):
    client = FakeClient(last=89.6, sl=89.7)  # цена упала, стоп выше расчётного
    state = tmp_path / "state.json"
    result = tick(client, "BRZ6@RTSX", 1, 0.3, step=0.01, state_file=state, log=lambda *_: None)
    assert result["status"] == "hold"
    assert result["sl"] == 89.7
    assert client.canceled == []


def test_tick_moves_only_after_min_move(tmp_path):
    """Стоп сдвигается после прохода 10 пп (0.1), а не на каждом шаге."""
    client = FakeClient(last=89.75, sl=89.39)  # расчётный 89.45: всего +0.06
    state = tmp_path / "state.json"
    result = tick(client, "BRZ6@RTSX", 1, 0.3, step=0.01, min_move=0.1,
                  state_file=state, log=lambda *_: None)
    assert result["status"] == "hold"
    assert result["sl"] == 89.39
    assert client.placed == []

    client.last = 89.80  # best 89.80 -> расчётный 89.50: +0.11 от стопа
    result = tick(client, "BRZ6@RTSX", 1, 0.3, step=0.01, min_move=0.1,
                  state_file=state, log=lambda *_: None)
    assert result["status"] == "moved"
    assert result["sl"] == 89.5


def test_tick_remembers_best_price_between_ticks(tmp_path):
    client = FakeClient(last=90.0, sl=88.69)
    state = tmp_path / "state.json"
    tick(client, "BRZ6@RTSX", 1, 0.3, step=0.01, state_file=state, log=lambda *_: None)
    client.last = 89.8  # откат
    result = tick(client, "BRZ6@RTSX", 1, 0.3, step=0.01, state_file=state, log=lambda *_: None)
    assert result["status"] == "hold"
    assert result["sl"] == 89.7
    assert result["best"] == 90.0


def test_tick_stops_when_position_closed(tmp_path):
    client = FakeClient(position=0.0, sl=89.7)
    state = tmp_path / "state.json"
    result = tick(client, "BRZ6@RTSX", 1, 0.3, step=0.01, state_file=state, log=lambda *_: None)
    assert result["status"] == "closed"
    assert trailing.load_state(state, "BRZ6@RTSX")["closed"] is True
