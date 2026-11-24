#!/usr/bin/env python3
"""Трейлинг-стоп Финама: подтягивает SL за ценой с заданным отступом.

Пример: BRZ6, отступ 0.3 пункта (30 пп при шаге 0.01):
цена 90.0 -> стоп 89.70; цена 90.5 -> стоп 90.20. Вниз стоп не двигается.

Логика одного такта:
  1. берём последнюю цену и позицию;
  2. best = максимум из истории и текущей цены;
  3. целевой стоп = best - trail (округление к шагу цены);
  4. если целевой стоп выше текущего — отменяем старый SL/TP и выставляем
     новый SL (только стоп, без тейка);
  5. состояние (best, текущий стоп, id заявки) хранится в trailing_state.json.

Запуск раз в минуту (автоматизация) или циклом:
  python trailing.py BRZ6 --qty 1 --trail 0.3 --once
  python trailing.py BRZ6 --qty 1 --trail 0.3 --interval 15
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from finam_client import FinamClient, norm_symbol, num

MSK = ZoneInfo("Europe/Moscow")
STATE_FILE = Path(__file__).with_name("trailing_state.json")


def resolve_symbol(client: FinamClient, raw: str) -> str:
    """BRZ6 -> BRZ6@RTSX, SBER -> SBER@MISX (фьючерсы живут на RTSX)."""
    raw = raw.strip().upper()
    if "@" in raw:
        return raw
    for mic in ("MISX", "RTSX"):
        symbol = f"{raw}@{mic}"
        try:
            client.asset_params(symbol)
            return symbol
        except Exception:  # noqa: BLE001 - пробуем следующую площадку
            continue
    return norm_symbol(raw)


def round_step(value: float, step: float) -> float:
    """Округлить цену к шагу инструмента (0.01, 0.1 и т.п.)."""
    if step <= 0:
        return round(value, 6)
    d = Decimal(str(value)) / Decimal(str(step))
    return float(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP) * Decimal(str(step)))


def next_stop(best: float, trail: float, step: float) -> float:
    """Стоп на trail ниже лучшей цены, выровненный по шагу."""
    return round_step(best - trail, step)


def find_sl_order(orders: list[dict], symbol: str) -> dict | None:
    """Активная SL/TP-заявка по символу с непустым стопом."""
    symbol = norm_symbol(symbol)
    for order in orders:
        sltp = order.get("sltp_order") or {}
        if sltp.get("symbol") != symbol:
            continue
        if (num(sltp.get("quantity_sl"), 0.0) or 0.0) <= 0:
            continue
        return order
    return None


def position_qty(client: FinamClient, symbol: str) -> float:
    symbol = norm_symbol(symbol)
    for p in client.positions():
        if p.get("symbol") == symbol:
            return num(p.get("quantity"), 0.0) or 0.0
    return 0.0


def load_state(path: Path, symbol: str) -> dict:
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if state.get("symbol") != symbol:
        state = {"symbol": symbol}
    return state


def save_state(path: Path, state: dict) -> None:
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def tick(
    client: FinamClient,
    symbol: str,
    quantity: int,
    trail: float,
    *,
    step: float = 0.01,
    state_file: Path = STATE_FILE,
    log=print,
) -> dict:
    symbol = norm_symbol(symbol)
    state = load_state(state_file, symbol)

    have = position_qty(client, symbol)
    if have < quantity:
        state.update({"closed": True, "updated": datetime.now(MSK).isoformat()})
        save_state(state_file, state)
        log(f"{symbol}: позиции {have:.0f} < {quantity} — трейлинг остановлен")
        return {"status": "closed", "position": have}

    quote = client.quote(symbol)
    last = num(quote.get("last"))
    if not last:
        log(f"{symbol}: нет последней цены")
        return {"status": "no_quote"}

    best = max(float(state.get("best") or 0.0), float(last))
    desired = next_stop(best, trail, step)
    current = find_sl_order(client.active_orders(), symbol)
    current_sl = num((current or {}).get("sltp_order", {}).get("sl_price"))

    state["best"] = best
    state["trail"] = trail
    state["quantity"] = quantity
    state["updated"] = datetime.now(MSK).isoformat()

    if current and current_sl is not None and current_sl >= desired - step / 2:
        state["sl"] = current_sl
        state["order_id"] = current.get("order_id")
        save_state(state_file, state)
        log(f"{symbol}: last {last}, best {best}, стоп {current_sl} — без изменений")
        return {"status": "hold", "last": last, "best": best, "sl": current_sl}

    # Сначала снимаем старый стоп, потом ставим новый: два SL одновременно
    # могут продать позицию дважды.
    if current:
        client.cancel_order(current["order_id"])
        log(f"{symbol}: снят старый стоп {current.get('order_id')} ({current_sl})")

    placed = client.place_sltp(symbol, "SELL", quantity, sl_price=desired, comment=f"trailing {trail}")
    sltp = placed.get("sltp_order") or {}
    state.update(
        {
            "sl": desired,
            "order_id": placed.get("order_id"),
            "best_at_place": best,
            "closed": False,
        }
    )
    save_state(state_file, state)
    log(
        f"{symbol}: last {last}, best {best} -> стоп {desired} "
        f"(заявка {placed.get('order_id')}, {placed.get('status')})"
    )
    return {
        "status": "moved",
        "last": last,
        "best": best,
        "sl": desired,
        "order_id": placed.get("order_id"),
        "order_status": placed.get("status"),
        "sl_price": num(sltp.get("sl_price")),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Трейлинг-стоп Финама")
    parser.add_argument("symbol")
    parser.add_argument("--qty", type=int, required=True)
    parser.add_argument("--trail", type=float, required=True, help="отступ стопа в пунктах цены (0.3 = 30 пп)")
    parser.add_argument("--step", type=float, default=0.01, help="шаг цены инструмента")
    parser.add_argument("--interval", type=float, default=15.0, help="секунд между тактами в режиме цикла")
    parser.add_argument("--once", action="store_true", help="один такт (для автоматизации)")
    parser.add_argument("--state", default=str(STATE_FILE))
    args = parser.parse_args(argv)

    client = FinamClient()
    symbol = resolve_symbol(client, args.symbol)
    state_file = Path(args.state)

    def run_once() -> dict:
        return tick(
            client,
            symbol,
            args.qty,
            args.trail,
            step=args.step,
            state_file=state_file,
        )

    if args.once:
        result = run_once()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("status") in {"moved", "hold", "closed"} else 1

    print(f"Трейлинг {symbol}: отступ {args.trail}, шаг {args.step}, интервал {args.interval} c. Ctrl+C — стоп.")
    while True:
        try:
            result = run_once()
        except Exception as exc:  # noqa: BLE001 - цикл не должен падать
            print(f"{datetime.now(MSK):%H:%M:%S} ошибка: {type(exc).__name__}: {exc}")
            result = {"status": "error"}
        if result.get("status") == "closed":
            return 0
        time.sleep(max(args.interval, 5.0))


if __name__ == "__main__":
    raise SystemExit(main())
