"""Тесты напоминаний об экспирации фьючерсов (без сети)."""
from __future__ import annotations

from datetime import date

from futures_expiry_watch import build_reminders, parse_expiry


def test_parse_expiry_ok():
    assert parse_expiry("BRZ6     # Нефть Brent          до 02.12.2026") == date(2026, 12, 2)


def test_parse_expiry_none():
    assert parse_expiry("BRZ6") is None


def test_build_reminders_within_3_days():
    today = date(2026, 11, 26)
    lines = [
        "BRZ6     # Нефть Brent          до 28.11.2026",  # через 2 дн.
        "NGZ6     # Природный газ (NG)   до 30.12.2026",  # через 34 дн. -> раньше не должно
        "GDZ6     # Золото (USD)         до 16.12.2026",  # через 20 дн. -> раньше не должно
    ]
    rem = build_reminders(today, lines)
    assert len(rem) == 1
    assert "28.11.2026" in rem[0]
    assert "BRZ6" in rem[0]


def test_build_reminders_expiry_today_or_past_window():
    today = date(2026, 11, 26)
    lines = ["T@RTSX".replace("@RTSX", "") + "   # Тест          до 26.11.2026"]  # сегодня
    rem = build_reminders(today, lines)
    assert len(rem) == 1
    assert "сегодня" in rem[0]


def test_build_reminders_position_on_expiring_contract():
    today = date(2026, 11, 26)
    lines = ["BRZ6     # Нефть Brent          до 27.11.2026"]
    positions = [{"symbol": "BRZ6@RTSX", "quantity": {"value": "1"}}]
    rem = build_reminders(today, lines, positions=positions)
    assert len(rem) == 2
    assert "Позиция BRZ6" in rem[1]
