"""Тесты эндпоинтов Mini App (tg_bridge): портфель + рынок, без сети."""
from __future__ import annotations

import tg_bridge
import screener


class FakeFinam:
    account_id = "950034"

    def account(self) -> dict:
        return {
            "equity": {"value": "32711.64"},
            "cash": [{"currency_code": "RUB", "units": "10710", "nanos": 600000000}],
            "positions": [
                {
                    "symbol": "BANEP@MISX",
                    "quantity": {"value": "4"},
                    "average_price": {"value": "752.5"},
                    "current_price": {"value": "760.5"},
                    "unrealized_pnl": {"value": "32"},
                },
                {
                    "symbol": "T@MISX",
                    "quantity": {"value": "0"},
                    "average_price": {"value": "0"},
                    "current_price": {"value": "280.5"},
                    "unrealized_pnl": {"value": "0"},
                },
            ],
        }

    def cash_rub(self) -> float:
        return 10710.6

    def active_orders(self) -> list[dict]:
        return [
            {
                "order_id": "813",
                "status": "ORDER_STATUS_WATCHING",
                "sltp_order": {
                    "symbol": "BANEP@MISX",
                    "quantity_sl": {"value": "4"},
                    "sl_price": {"value": "756.5"},
                },
            },
        ]


def test_positions_payload(monkeypatch):
    monkeypatch.setattr(tg_bridge, "FinamClient", lambda: FakeFinam())
    d = tg_bridge.positions_payload()
    assert d["account_id"] == "950034"
    assert d["equity"] == 32711.64
    assert len(d["positions"]) == 1  # нулевой остаток T@MISX отфильтрован
    p = d["positions"][0]
    assert p["symbol"] == "BANEP@MISX"
    assert p["category"] == "breakeven"  # SL 756.5 >= entry 752.5
    assert d["stats"]["total"] == 1
    assert d["stats"]["breakeven"] == 1


def test_market_payload(monkeypatch):
    def fake_screen(*, equity: float = 0.0, cash: float = 0.0, top: int = 6, **kw):
        return {
            "generated_at": "2026-11-26 12:33 MSK",
            "market": {"breadth": 52.7, "turnover_rub": 27703976590.0, "advancers": 207, "decliners": 186},
            "candidates": [
                {
                    "ticker": "EUTR",
                    "name": "ЕвроТранс",
                    "last": 6.3,
                    "chg": 5.88,
                    "rvol": 2.89,
                    "atr_pct": 1.2,
                    "plan": {"setup": "breakout", "entry": 6.4, "stop": 5.9, "target": 7.6, "rr": 2.1, "lots": 3},
                },
            ],
        }

    monkeypatch.setattr(screener, "screen", fake_screen)
    d = tg_bridge.market_payload(top=5)
    assert d["market"]["breadth"] == 52.7
    assert len(d["candidates"]) == 1
    assert d["candidates"][0]["ticker"] == "EUTR"
