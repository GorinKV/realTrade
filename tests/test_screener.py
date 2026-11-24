"""Тесты скоринга/плана скринера (без сети)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import screener

MSK = ZoneInfo("Europe/Moscow")


def _row(**kw):
    row = {
        "SECID": "TEST",
        "LAST": 100.0,
        "HIGH": 103.0,
        "LOW": 99.0,
        "OPEN": 101.0,
        "PREVPRICE": 100.0,
        "LASTTOPREVPRICE": 0.0,
        "VALTODAY": 100_000_000.0,
        "VALTODAY_RUR": 100_000_000.0,
        "NUMTRADES": 2000,
        "LOTSIZE": 1,
        "TRADINGSTATUS": "T",
    }
    row.update(kw)
    return row


def _stats(**kw):
    stats = {"avg_turnover": 50_000_000.0, "atr_pct": 3.0, "sma": 99.0, "sessions": 14}
    stats.update(kw)
    return stats


def test_day_change_pct_prefers_last_to_prev_price():
    assert screener.day_change_pct(_row(LASTTOPREVPRICE=-1.5)) == -1.5
    assert screener.day_change_pct(_row(LASTTOPREVPRICE=None, LAST=110.0, PREVPRICE=100.0)) == pytest.approx(10.0)
    assert screener.day_change_pct(_row(LASTTOPREVPRICE=None, LAST=None)) == 0.0


def test_pos_in_range_bounds():
    assert screener.pos_in_range(10, 20, 10) == 0.0
    assert screener.pos_in_range(10, 20, 20) == 1.0
    assert screener.pos_in_range(10, 20, 15) == 0.5
    assert screener.pos_in_range(10, 10, 10) is None


def test_session_fraction_main_session():
    assert screener.session_fraction(datetime(2026, 11, 24, 10, 0, tzinfo=MSK)) == 0.05
    assert screener.session_fraction(datetime(2026, 11, 24, 18, 40, tzinfo=MSK)) == 1.0
    mid = screener.session_fraction(datetime(2026, 11, 24, 14, 20, tzinfo=MSK))
    assert 0.45 < mid < 0.55


def test_score_rewards_relative_volume():
    low = screener.score_candidate(_row(VALTODAY_RUR=30_000_000.0), _stats(), 0.5)
    high = screener.score_candidate(_row(VALTODAY_RUR=300_000_000.0), _stats(), 0.5)
    assert high > low


def test_score_direction_uses_close_position_in_range():
    row = _row(LAST=102.9)  # near high
    long_score = screener.score_candidate(row, _stats(), 0.5, direction=1)
    short_score = screener.score_candidate(row, _stats(), 0.5, direction=-1)
    assert long_score > short_score


def test_classify_breakout_pullback_breakdown():
    assert screener.classify(_row(LAST=102.9, LASTTOPREVPRICE=2.0), _stats(sma=99.0)) == "breakout"
    assert screener.classify(_row(LAST=100.5, LASTTOPREVPRICE=1.0), _stats(sma=95.0)) == "pullback"
    assert screener.classify(_row(LAST=99.1, LASTTOPREVPRICE=-2.0), _stats(sma=102.0)) == "breakdown"
    assert screener.classify(_row(LAST=None), _stats()) == "range"


def test_size_position_respects_risk_and_cash():
    # риск 0.5% от 100 000 = 500 ₽; риск на акцию 10 ₽ -> 50 акций
    assert screener.size_position(100.0, 90.0, equity=100_000, risk_pct=0.5, lotsize=10, cash=1_000_000) == 5
    # кэша хватает только на 2 лота
    assert screener.size_position(100.0, 90.0, equity=100_000, risk_pct=0.5, lotsize=10, cash=2_000) == 1
    assert screener.size_position(100.0, 100.0, equity=100_000, risk_pct=0.5, lotsize=1, cash=10_000) == 0


def test_trade_plan_geometry_and_cost_gate():
    row = _row(LAST=102.9, HIGH=103.0, LOW=99.0, LASTTOPREVPRICE=2.0)
    plan = screener.trade_plan(row, _stats(atr_pct=3.0), equity=100_000, cash=100_000)
    assert plan["setup"] == "breakout"
    assert plan["entry"] > 103.0
    assert plan["stop"] < plan["entry"]
    assert plan["target"] > plan["entry"]
    assert plan["rr"] > 1.5
    assert plan["cost_ok"] is True

    low_vol = screener.trade_plan(_row(LAST=100.0), _stats(atr_pct=0.2), equity=100_000, cash=100_000)
    assert low_vol["cost_ok"] is False


def test_screen_filters_liquidity_and_builds_weak_list():
    universe = [
        _row(SECID="GOOD", LAST=100.0, LASTTOPREVPRICE=2.5, HIGH=101.0, VALTODAY_RUR=500_000_000.0),
        _row(SECID="TINY", LAST=100.0, VALTODAY_RUR=100_000.0),
        _row(SECID="DOWN", LAST=99.0, LASTTOPREVPRICE=-3.0, LOW=98.5, HIGH=103.0),
    ]
    history = {
        "GOOD": [
            {"TRADEDATE": f"2026-11-{d:02d}", "VALUE": 100_000_000, "HIGH": 103, "LOW": 99, "CLOSE": 101}
            for d in range(10, 22)
        ],
        "DOWN": [
            {"TRADEDATE": f"2026-11-{d:02d}", "VALUE": 100_000_000, "HIGH": 103, "LOW": 99, "CLOSE": 101}
            for d in range(10, 22)
        ],
    }
    result = screener.screen(
        equity=100_000,
        cash=100_000,
        top=10,
        now=datetime(2026, 11, 24, 12, 0, tzinfo=MSK),
        universe=universe,
        history=history,
    )
    tickers = [c["ticker"] for c in result["candidates"]]
    weak = [w["ticker"] for w in result["weak"]]
    assert "GOOD" in tickers
    assert "TINY" not in tickers
    assert "DOWN" in weak
    assert result["market"]["advancers"] >= 1
    assert result["market"]["decliners"] >= 1
