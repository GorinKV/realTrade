"""Тесты правила автоторговли (без сети)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import autotrade
from autotrade import (
    autotrade_cfg,
    check_rule,
    count_today_trades,
    deposit_load_pct,
    pick_best,
    size_lots,
)

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=MSK)


def _order(executed="1", ts="2026-11-24T09:00:00Z"):
    return {"executed_quantity": {"value": executed}, "transact_at": ts}


def test_count_today_trades_uses_msk_date_and_executed_qty():
    orders = [
        _order("1", "2026-11-24T09:00:00Z"),          # сегодня
        _order("2", "2026-11-23T20:00:00Z"),          # вчера по MSK
        _order("0", "2026-11-24T10:00:00Z"),          # не исполнена
        _order("0.5", "2026-11-23T21:30:00Z"),        # 00:30 MSK сегодня
    ]
    assert count_today_trades(orders, NOW) == 2


def test_today_traded_symbols():
    orders = [
        {"executed_quantity": {"value": "1"}, "transact_at": "2026-11-24T09:00:00Z",
         "order": {"symbol": "FLOT@MISX"}},
        {"executed_quantity": {"value": "2"}, "transact_at": "2026-11-23T09:00:00Z",
         "order": {"symbol": "SBER@MISX"}},
        {"executed_quantity": {"value": "0"}, "transact_at": "2026-11-24T10:00:00Z",
         "order": {"symbol": "ASTR@MISX"}},
    ]
    assert autotrade.today_traded_symbols(orders, NOW) == {"FLOT@MISX"}


def test_deposit_load_uses_positions_and_margin():
    account = {
        "equity": {"value": "200000"},
        "positions": [{"quantity": {"value": "1000"}, "current_price": {"value": "100"}}],
        "portfolio_mc": {"initial_margin": {"value": "30000"}},
    }
    pct, detail = deposit_load_pct(account)
    assert pct == 50.0
    assert detail["invested"] == 100000


def test_size_lots_ten_percent_of_equity():
    # 10% от 35 329 = 3 532.9; лот 10 шт по 80.9 = 809 -> 4 лота
    assert size_lots(35_329, 80.9, 10, 10.0, 32_000) == 4
    # кэш ограничивает: 1 000 ₽ -> 1 лот
    assert size_lots(35_329, 80.9, 10, 10.0, 1_000) == 1
    # лимит стоимости заявки
    assert size_lots(35_329, 80.9, 10, 10.0, 32_000, max_order_value=1_000) == 1
    # лот дороже 10% депозита
    assert size_lots(35_329, 5_000, 1, 10.0, 32_000) == 0


def _cand(ticker="AAA", score=70, spread=0.1, move=4.0, rr=2.0, flow=None):
    return {
        "ticker": ticker,
        "score": score,
        "spread_pct": spread,
        "plan": {"move_pct": move, "rr": rr, "cost_ok": True},
        "flow": flow,
    }


def test_pick_best_excludes_against_flow_and_wide_spread():
    candidates = [
        _cand("AGAINST", score=95, flow={"bias": -60, "against": True, "confirmed": False}),
        _cand("WIDE", score=95, spread=0.9),
        _cand("LOWSCORE", score=10),
        _cand("TINYMOVE", score=80, move=0.4),
        _cand("OK", score=60, flow={"bias": 30, "against": False, "confirmed": True}),
    ]
    best = pick_best(candidates, autotrade_cfg())
    assert best is not None and best["ticker"] == "OK"


def test_pick_best_prefers_confirmed_flow_and_profit():
    a = _cand("A", score=65, move=3.0, flow={"bias": 20, "against": False, "confirmed": True})
    b = _cand("B", score=65, move=3.0, flow={"bias": 0, "against": False, "confirmed": False})
    cfg = autotrade_cfg()
    pick_best([a, b], cfg)
    assert a["_rank"] > b["_rank"]


class FakeClient:
    account_id = "950034"

    def __init__(self, trades=1, load_equity=200000.0, invested=20000.0):
        self._orders = [_order("1", "2026-11-24T09:00:00Z") for _ in range(trades)]
        self._equity = load_equity
        self._invested = invested

    def account(self):
        qty = self._invested / 100 if self._invested else 0
        return {
            "equity": {"value": str(self._equity)},
            "positions": [{"quantity": {"value": str(qty)}, "current_price": {"value": "100"}}] if qty else [],
            "portfolio_mc": {"initial_margin": {"value": "0"}},
        }

    def orders(self):
        return self._orders


def test_check_rule_allows_under_limits():
    state = check_rule(FakeClient(trades=1), autotrade_cfg(), NOW)
    assert state["allowed"] is True
    assert state["trades_today"] == 1
    assert state["load_pct"] == 10.0


def test_check_rule_blocks_three_trades():
    state = check_rule(FakeClient(trades=3), autotrade_cfg(), NOW)
    assert state["allowed"] is False
    assert "сделок" in state["reason"]


def test_check_rule_blocks_high_load():
    state = check_rule(FakeClient(trades=1, load_equity=100000, invested=85000), autotrade_cfg(), NOW)
    assert state["allowed"] is False
    assert "нагрузка" in state["reason"]


def test_check_rule_respects_enabled_flag():
    cfg = autotrade_cfg()
    cfg["enabled"] = False
    state = check_rule(FakeClient(trades=0), cfg, NOW)
    assert state["allowed"] is False
    assert "выключена" in state["reason"]
