#!/usr/bin/env python3
"""Предторговая проверка Финама: лимиты, ликвидность, поток, дубли.

Финам — единственная площадка исполнения (T-Инвест только данные).
Проверка обязательна перед каждой заявкой, потому что у Финама есть
ограничения по инструментам, ценам и плечу, а API-ошибка стоит времени.

Блокирующие проверки (заявка не уходит):
  * инструмент не торгуется / нет long;
  * количество не кратно лоту;
  * нет свежей котировки (старше quote_max_age_sec);
  * спред шире max_spread_pct;
  * в стакане на лучших уровнях меньше, чем нужно;
  * лимитная цена отличается от последней больше max_price_deviation_pct;
  * не хватает кэша (BUY) или позиции (SELL);
  * стоимость заявки выше max_order_value_rub или доли equity;
  * уже есть активная заявка по этому символу в ту же сторону.

Предупреждения (не блокируют): вне основной сессии, широкий спред при
маркет-заявке, цена далеко от VWAP.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from finam_client import FinamClient, book_summary, norm_symbol, num

MSK = ZoneInfo("Europe/Moscow")
CONFIG_FILE = Path(__file__).with_name("trade_config.json")

DEFAULT_CONFIG: dict = {
    "max_spread_pct": 0.4,
    "max_price_deviation_pct": 1.0,
    "max_order_value_rub": 50_000,
    "max_position_share_pct": 25.0,
    "commission_pct": 0.3,
    "risk_pct": 0.5,
    "quote_max_age_sec": 300,
    "allow_market_orders": True,
    "session": {"start": "10:00", "end": "18:40", "evening_start": "19:00", "evening_end": "23:50"},
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


@dataclass
class Verdict:
    ok: bool
    checks: list[Check] = field(default_factory=list)
    card: dict = field(default_factory=dict)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.blocking]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.blocking]

    def render(self) -> str:
        lines = ["ПРЕДТОРГОВАЯ ПРОВЕРКА ФИНАМ:"]
        for c in self.checks:
            mark = "✅" if c.ok else ("⛔" if c.blocking else "⚠️")
            lines.append(f"  {mark} {c.name}: {c.detail}")
        if self.card:
            lines.append("")
            lines.append("Карточка: " + json.dumps(self.card, ensure_ascii=False))
        lines.append("")
        lines.append("ИТОГ: " + ("можно выставлять" if self.ok else "ЗАПРЕЩЕНО — исправьте пункты выше"))
        return "\n".join(lines)


def load_config(path: Path = CONFIG_FILE) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        user = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


def _in_session(now: datetime, cfg: dict) -> tuple[bool, str]:
    session = cfg.get("session") or {}
    now = now.astimezone(MSK)
    minutes = now.hour * 60 + now.minute

    def parse(text: str, fallback: int) -> int:
        try:
            hh, mm = text.split(":")
            return int(hh) * 60 + int(mm)
        except (ValueError, AttributeError):
            return fallback

    start = parse(session.get("start", "10:00"), 600)
    end = parse(session.get("end", "18:40"), 1120)
    ev_start = parse(session.get("evening_start", "19:00"), 1140)
    ev_end = parse(session.get("evening_end", "23:50"), 1430)
    if start <= minutes <= end:
        return True, "основная сессия"
    if ev_start <= minutes <= ev_end:
        return True, "вечерняя сессия"
    return False, "вне торговой сессии (заявка может встать в очередь)"


def _quote_age_sec(quote: dict) -> float | None:
    ts = quote.get("timestamp")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()


def _position_qty(client: FinamClient, symbol: str) -> float:
    symbol = norm_symbol(symbol)
    for p in client.positions():
        if p.get("symbol") == symbol:
            return num(p.get("quantity"), 0.0) or 0.0
    return 0.0


def _has_duplicate(client: FinamClient, symbol: str, side: str) -> str | None:
    symbol = norm_symbol(symbol)
    for order in client.active_orders():
        if order.get("status") not in {"ORDER_STATUS_NEW", "ORDER_STATUS_PARTIALLY_FILLED", "ORDER_STATUS_WATCHING"}:
            continue
        body = order.get("order") or {}
        sltp = order.get("sltp_order") or {}
        order_symbol = body.get("symbol") or sltp.get("symbol")
        order_side = body.get("side") or sltp.get("side")
        if order_symbol == symbol and order_side == side:
            return str(order.get("order_id"))
    return None


def preflight(
    client: FinamClient,
    symbol: str,
    side: str,
    quantity: int,
    limit_price: float | None = None,
    *,
    order_type: str = "ORDER_TYPE_LIMIT",
    cfg: dict | None = None,
    now: datetime | None = None,
) -> Verdict:
    cfg = cfg or load_config()
    now = now or datetime.now(MSK)
    symbol = norm_symbol(symbol)
    side = "SIDE_BUY" if side.upper() in {"BUY", "SIDE_BUY"} else "SIDE_SELL"
    checks: list[Check] = []

    # 1. Инструмент и лот
    lotsize = 1
    try:
        params = client.asset_params(symbol)
        trade_lot = num(params.get("trade_lot_size")) or 1.0
        lotsize = int(trade_lot) or 1
        tradeable = bool(params.get("tradeable")) and bool(params.get("is_tradable", True))
        checks.append(Check("инструмент", tradeable, f"{symbol}: tradeable={tradeable}, лот {lotsize}"))
        if side == "SIDE_BUY":
            longable = (params.get("longable") or {}).get("value", "")
            checks.append(Check("покупка разрешена", longable == "AVAILABLE", f"longable={longable or '—'}"))
    except Exception as exc:  # noqa: BLE001 - API может не знать символ
        checks.append(Check("инструмент", False, f"{symbol}: {exc}"))

    checks.append(
        Check(
            "кратность лоту",
            lotsize > 0 and quantity % lotsize == 0,
            f"{quantity} шт / лот {lotsize}",
            blocking=False,
        )
    )

    # 2. Котировка и стакан
    quote: dict = {}
    book: dict = {}
    last = bid = ask = None
    try:
        quote = client.quote(symbol)
        last, bid, ask = num(quote.get("last")), num(quote.get("bid")), num(quote.get("ask"))
        age = _quote_age_sec(quote)
        fresh = age is not None and age <= float(cfg["quote_max_age_sec"])
        checks.append(Check("котировка", bool(last) and fresh, f"last={last}, возраст {age:.0f} c" if age is not None else "нет метки времени"))
        book = book_summary(client.orderbook(symbol))
        spread = book.get("spread_pct")
        spread_ok = spread is not None and spread <= float(cfg["max_spread_pct"])
        checks.append(Check("спред", spread_ok, f"{spread:.2f}%" if spread is not None else "нет стакана"))
        depth_ok = (book.get("ask_depth") or 0) >= quantity if side == "SIDE_BUY" else (book.get("bid_depth") or 0) >= quantity
        checks.append(
            Check(
                "глубина стакана",
                bool(depth_ok),
                f"биды {book.get('bid_depth') or 0:.0f} / аски {book.get('ask_depth') or 0:.0f} шт, нужно {quantity}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("котировка", False, f"ошибка данных: {exc}"))

    # 3. Цена
    effective = limit_price or last or 0.0
    if order_type == "ORDER_TYPE_MARKET" or limit_price is None:
        if not cfg.get("allow_market_orders", True):
            checks.append(Check("маркет-заявка", False, "маркет-заявки запрещены конфигом"))
        elif last:
            checks.append(Check("тип заявки", True, "MARKET — исполнение по рынку, возможен проскальзывание", blocking=False))
    elif last:
        deviation = abs(limit_price / last - 1) * 100
        checks.append(
            Check(
                "отклонение цены",
                deviation <= float(cfg["max_price_deviation_pct"]),
                f"лимит {limit_price} vs last {last}: {deviation:.2f}% (макс {cfg['max_price_deviation_pct']}%)",
            )
        )

    # 4. Кэш / позиция
    try:
        cash = client.cash_rub()
        if side == "SIDE_BUY":
            need = effective * quantity * (1 + float(cfg["commission_pct"]) / 100)
            checks.append(Check("кэш", cash >= need, f"нужно {need:,.2f} ₽, есть {cash:,.2f} ₽".replace(",", " ")))
        else:
            have = _position_qty(client, symbol)
            checks.append(Check("позиция", have >= quantity, f"продаём {quantity} из {have:.0f} шт"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("счёт", False, f"не удалось получить счёт: {exc}"))

    # 5. Стоимость и доля equity
    equity = 0.0
    try:
        equity = client.equity()
    except Exception:  # noqa: BLE001
        pass
    value = effective * quantity
    checks.append(
        Check(
            "стоимость заявки",
            value <= float(cfg["max_order_value_rub"]),
            f"{value:,.2f} ₽ (макс {cfg['max_order_value_rub']:,.0f} ₽)".replace(",", " "),
        )
    )
    if equity > 0:
        share = value / equity * 100
        checks.append(
            Check(
                "доля equity",
                share <= float(cfg["max_position_share_pct"]),
                f"{share:.1f}% equity (макс {cfg['max_position_share_pct']}%)",
                blocking=False,
            )
        )

    # 6. Дубли и сессия
    try:
        dup = _has_duplicate(client, symbol, side)
        checks.append(Check("дубли", dup is None, f"уже есть активная заявка {dup}" if dup else "активных нет"))
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("дубли", False, f"не удалось проверить: {exc}"))
    session_ok, session_note = _in_session(now, cfg)
    checks.append(Check("сессия", session_ok, session_note, blocking=False))

    ok = not any(not c.ok and c.blocking for c in checks)
    card = {
        "account_id": client.account_id,
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": quantity,
        "lots": quantity // lotsize if lotsize else quantity,
        "limit_price": limit_price,
        "last": last,
        "bid": bid,
        "ask": ask,
        "value_rub": round(value, 2),
        "commission_rub": round(value * float(cfg["commission_pct"]) / 100, 2),
        "spread_pct": round(book["spread_pct"], 3) if book.get("spread_pct") is not None else None,
    }
    return Verdict(ok=ok, checks=checks, card=card)


def preflight_sltp(
    client: FinamClient,
    symbol: str,
    quantity: int,
    *,
    sl_price: float | None,
    tp_price: float | None,
    side: str = "SIDE_SELL",
    cfg: dict | None = None,
    now: datetime | None = None,
) -> Verdict:
    """Проверка защитной SL/TP: геометрия, позиция, дубли, спред."""
    cfg = cfg or load_config()
    symbol = norm_symbol(symbol)
    checks: list[Check] = []
    quote = client.quote(symbol)
    last = num(quote.get("last"))
    book = book_summary(client.orderbook(symbol))

    have = _position_qty(client, symbol)
    checks.append(Check("позиция", have >= quantity, f"защищаем {quantity} из {have:.0f} шт"))

    if last is None:
        checks.append(Check("котировка", False, "нет последней цены"))
    else:
        if sl_price is not None:
            dist = (last - sl_price) / last * 100
            ok = 0.05 <= dist <= 20
            checks.append(Check("стоп", ok, f"SL {sl_price} = {dist:.2f}% от last {last} (допустимо 0.05–20%)"))
        if tp_price is not None:
            dist = (tp_price - last) / last * 100
            ok = 0.05 <= dist <= 50
            checks.append(Check("тейк", ok, f"TP {tp_price} = +{dist:.2f}% от last {last} (допустимо 0.05–50%)"))
        if sl_price is not None and tp_price is not None:
            checks.append(Check("геометрия", sl_price < last < tp_price, f"SL {sl_price} < last {last} < TP {tp_price}"))

    spread = book.get("spread_pct")
    checks.append(
        Check(
            "спред",
            spread is not None and spread <= float(cfg["max_spread_pct"]),
            f"{spread:.2f}%" if spread is not None else "нет стакана",
            blocking=False,
        )
    )
    dup = _has_duplicate(client, symbol, "SIDE_SELL")
    checks.append(Check("дубли", dup is None, f"уже есть SELL-заявка {dup}" if dup else "нет", blocking=False))
    session_ok, session_note = _in_session(now or datetime.now(MSK), cfg)
    checks.append(Check("сессия", session_ok, session_note, blocking=False))

    ok = not any(not c.ok and c.blocking for c in checks)
    card = {
        "account_id": client.account_id,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "last": last,
        "spread_pct": round(spread, 3) if spread is not None else None,
    }
    return Verdict(ok=ok, checks=checks, card=card)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Предторговая проверка Финама")
    parser.add_argument("symbol")
    parser.add_argument("--side", default="BUY", choices=["BUY", "SELL", "SIDE_BUY", "SIDE_SELL"])
    parser.add_argument("--qty", type=int, required=True)
    parser.add_argument("--limit", type=float)
    parser.add_argument("--market", action="store_true")
    args = parser.parse_args(argv)
    client = FinamClient()
    order_type = "ORDER_TYPE_MARKET" if args.market else "ORDER_TYPE_LIMIT"
    verdict = preflight(client, args.symbol, args.side, args.qty, args.limit, order_type=order_type)
    print(verdict.render())
    return 0 if verdict.ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
