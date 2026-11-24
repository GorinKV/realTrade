#!/usr/bin/env python3
"""Интрадей-данные T-Invest (локальный сервис) — ТОЛЬКО для анализа.

T-Банк не используется для сделок (дорого, gRPC нестабилен), но его поток
данных — стакан с плотностями и лента сделок — самый информативный для
подтверждения входа. Исполнение всегда в Финаме.

Локальный сервис на 127.0.0.1:9443 (сертификат самоподписанный):
  GET /api/tinkoff/orderbook?ticker=FLOT&depth=10
  GET /api/tinkoff/trades?ticker=FLOT&minutes=60
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE = "https://127.0.0.1:9443"
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def _get(path: str, timeout: float = 8.0) -> dict | None:
    try:
        with urllib.request.urlopen(BASE + path, context=_CTX, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def orderbook(ticker: str, depth: int = 10) -> dict | None:
    query = urllib.parse.urlencode({"ticker": ticker, "depth": depth})
    return _get(f"/api/tinkoff/orderbook?{query}")


def trades(ticker: str, minutes: int = 60) -> dict | None:
    query = urllib.parse.urlencode({"ticker": ticker, "minutes": minutes})
    return _get(f"/api/tinkoff/trades?{query}")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def flow_signal(ticker: str, direction: int = 1) -> dict | None:
    """Свести стакан + ленту в оценку -100..100 (знак = сторона потока).

    direction=1 — ищем подтверждение лонга, -1 — слабость/шорт.
    None — данных нет (например, LQDT@MISX не ведётся поллером).
    """
    ob = orderbook(ticker)
    tr = trades(ticker)
    if not ob and not tr:
        return None

    imbalance = None
    best_bid = best_ask = None
    if ob:
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None
        raw = ob.get("imbalance")
        if raw is None and ob.get("bid_total") and ob.get("ask_total"):
            total = ob["bid_total"] + ob["ask_total"]
            raw = (ob["bid_total"] - ob["ask_total"]) / total if total else 0.0
        imbalance = float(raw) if raw is not None else None

    delta_share = None
    vwap = None
    volume = 0
    large = 0
    if tr:
        volume = int(tr.get("total_volume") or 0)
        vwap = tr.get("vwap")
        if volume:
            delta_share = float(tr.get("delta") or 0) / volume
        large = len(tr.get("large_prints") or [])

    bias = 0.0
    if imbalance is not None:
        bias += _clamp(imbalance * 100 * 0.6, -30, 30)
    if delta_share is not None:
        bias += _clamp(delta_share * 100, -40, 40)
    bias = round(_clamp(bias, -100, 100), 1)

    confirmed = bias >= 15 if direction > 0 else bias <= -15
    against = bias <= -15 if direction > 0 else bias >= 15
    if confirmed:
        note = "поток подтверждает"
    elif against:
        note = "поток против"
    else:
        note = "поток нейтральный"

    return {
        "ticker": ticker,
        "imbalance": round(imbalance, 3) if imbalance is not None else None,
        "delta_share": round(delta_share, 3) if delta_share is not None else None,
        "vwap": vwap,
        "volume": volume,
        "large_prints": large,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bias": bias,
        "confirmed": confirmed,
        "against": against,
        "note": note,
    }


def apply_flow(candidate: dict, direction: int = 1) -> dict:
    """Добавить поток T-Invest к кандидату и скорректировать score."""
    signal = flow_signal(candidate["ticker"], direction)
    if signal is None:
        candidate["flow"] = None
        return candidate
    candidate["flow"] = signal
    candidate["score"] = round(_clamp(candidate["score"] + signal["bias"] * 0.15, 0, 100), 1)
    return candidate


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Поток T-Invest по тикеру")
    parser.add_argument("ticker")
    parser.add_argument("--direction", type=int, default=1, choices=[1, -1])
    args = parser.parse_args(argv)
    signal = flow_signal(args.ticker, args.direction)
    print(json.dumps(signal, ensure_ascii=False, indent=2) if signal else "данных нет")
    return 0 if signal else 1


if __name__ == "__main__":
    raise SystemExit(main())
