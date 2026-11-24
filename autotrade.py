#!/usr/bin/env python3
"""Правило автоторговли: не больше 3 сделок в день и нагрузка < 80%.

После каждого часового анализа:
  1. считаем исполненные сегодня сделки и нагрузку на депозит
     (стоимость позиций / equity, а также маржа / equity — берём максимум);
  2. если сделок < 3 и нагрузка < 80% — выбираем «самую надёжную и прибыльную»
     бумагу из кандидатов скринера;
  3. покупаем 1 лот лимитом по лучшему аску (после pretrade-проверки);
  4. если заявка исполнилась — сразу выставляем SL/TP из плана.

Надёжность = score скринера + подтверждение потоком T-Invest − штраф за спред.
Прибыльность = ожидаемый ход до цели (move_pct) и R:R.
Поток «против» — кандидат отбрасывается.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from finam_client import FinamClient, book_summary, norm_symbol, num
from pretrade import load_config, preflight, preflight_sltp

MSK = ZoneInfo("Europe/Moscow")
JOURNAL = Path(__file__).with_name("trades_journal.jsonl")

AUTOTRADE_DEFAULTS: dict = {
    "enabled": True,
    "max_trades_per_day": 3,
    "max_deposit_load_pct": 80.0,
    "size_pct_of_equity": 10.0,
    "max_trades_per_symbol_per_day": 1,
    "min_score": 45.0,
    "max_spread_pct": 0.25,
    "min_move_pct": 1.5,
    "require_flow_not_against": True,
    "with_protection": True,
}


def autotrade_cfg(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    return {**AUTOTRADE_DEFAULTS, **(cfg.get("autotrade") or {})}


def _today(now: datetime | None = None) -> str:
    return (now or datetime.now(MSK)).astimezone(MSK).date().isoformat()


def count_today_trades(orders: list[dict], now: datetime | None = None) -> int:
    """Исполненные сегодня заявки (executed_quantity > 0, дата по MSK)."""
    today = _today(now)
    count = 0
    for order in orders:
        if (num(order.get("executed_quantity"), 0.0) or 0.0) <= 0:
            continue
        ts = order.get("transact_at") or order.get("accept_at") or ""
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(MSK)
        except ValueError:
            continue
        if dt.date().isoformat() == today:
            count += 1
    return count


def today_traded_symbols(orders: list[dict], now: datetime | None = None) -> set[str]:
    """Символы, по которым сегодня уже были исполненные сделки."""
    today = _today(now)
    out: set[str] = set()
    for order in orders:
        if (num(order.get("executed_quantity"), 0.0) or 0.0) <= 0:
            continue
        ts = order.get("transact_at") or order.get("accept_at") or ""
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(MSK)
        except ValueError:
            continue
        if dt.date().isoformat() != today:
            continue
        body = order.get("order") or {}
        symbol = body.get("symbol") or (order.get("sltp_order") or {}).get("symbol")
        if symbol:
            out.add(symbol)
    return out


def deposit_load_pct(account: dict) -> tuple[float, dict]:
    """Нагрузка на депозит: max(позиции/equity, маржа/equity), %."""
    equity = num(account.get("equity"), 0.0) or 0.0
    invested = 0.0
    for p in account.get("positions") or []:
        qty = num(p.get("quantity"), 0.0) or 0.0
        price = num(p.get("current_price")) or num(p.get("average_price")) or 0.0
        invested += qty * price
    margin = num((account.get("portfolio_mc") or {}).get("initial_margin"), 0.0) or 0.0
    if equity <= 0:
        return 0.0, {"invested": invested, "margin": margin, "equity": equity}
    pct = max(invested / equity * 100, margin / equity * 100)
    return round(pct, 2), {"invested": round(invested, 2), "margin": round(margin, 2), "equity": equity}


def size_lots(
    equity: float,
    price: float,
    lotsize: int,
    pct: float,
    cash: float,
    max_order_value: float | None = None,
) -> int:
    """Сколько целых лотов купить на pct% депозита (не больше кэша и лимита)."""
    if price <= 0 or lotsize <= 0 or equity <= 0:
        return 0
    budget = equity * pct / 100
    if max_order_value is not None:
        budget = min(budget, max_order_value)
    budget = min(budget, cash * 0.95)
    return max(int(budget // (price * lotsize)), 0)


def pick_best(candidates: list[dict], cfg: dict) -> dict | None:
    """Самая надёжная и прибыльная: score + поток + спред + ожидаемый ход."""
    best: dict | None = None
    best_rank = float("-inf")
    for cand in candidates:
        plan = cand.get("plan") or {}
        flow = cand.get("flow")
        if float(cand.get("score", 0)) < cfg["min_score"]:
            continue
        if cfg["require_flow_not_against"] and flow and flow.get("against"):
            continue
        spread = cand.get("spread_pct")
        if spread is None or spread > cfg["max_spread_pct"]:
            continue
        if float(plan.get("move_pct", 0)) < cfg["min_move_pct"]:
            continue
        if not plan.get("cost_ok", True):
            continue
        reliability = float(cand.get("score", 0))
        if flow and flow.get("confirmed"):
            reliability += 10
        reliability -= float(spread) * 20
        profit = min(float(plan.get("move_pct", 0)) * 2, 20) + min(float(plan.get("rr", 0)) * 2, 6)
        rank = reliability * 0.6 + profit * 0.4
        cand["_rank"] = round(rank, 2)
        if rank > best_rank:
            best, best_rank = cand, rank
    return best


def check_rule(client: FinamClient, cfg: dict, now: datetime | None = None) -> dict:
    account = client.account()
    trades = count_today_trades(client.orders(), now)
    load, detail = deposit_load_pct(account)
    allowed = (
        bool(cfg["enabled"])
        and trades < int(cfg["max_trades_per_day"])
        and load < float(cfg["max_deposit_load_pct"])
    )
    if not cfg["enabled"]:
        reason = "автоторговля выключена"
    elif trades >= int(cfg["max_trades_per_day"]):
        reason = f"сделок сегодня {trades} ≥ {cfg['max_trades_per_day']}"
    elif load >= float(cfg["max_deposit_load_pct"]):
        reason = f"нагрузка {load}% ≥ {cfg['max_deposit_load_pct']}%"
    else:
        reason = f"сделок {trades} < {cfg['max_trades_per_day']}, нагрузка {load}% < {cfg['max_deposit_load_pct']}%"
    return {
        "allowed": allowed,
        "reason": reason,
        "trades_today": trades,
        "load_pct": load,
        "load_detail": detail,
    }


def _journal(entry: dict) -> None:
    with JOURNAL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def enrich_candidates(client: FinamClient, candidates: list[dict], max_spread: float = 0.4) -> list[dict]:
    """Реальные bid/ask/стакан Finam + поток T-Invest; широкий спред — отбрасываем."""
    import marketdata

    out: list[dict] = []
    for cand in candidates:
        try:
            quote = client.quote(cand["ticker"])
            book = book_summary(client.orderbook(cand["ticker"]))
        except Exception:  # noqa: BLE001 - без стакана кандидат остаётся с пометкой
            cand["spread_pct"] = None
            out.append(cand)
            continue
        cand["finam_last"] = num(quote.get("last"))
        cand["finam_bid"] = num(quote.get("bid"))
        cand["finam_ask"] = num(quote.get("ask"))
        cand["spread_pct"] = round(book["spread_pct"], 3) if book["spread_pct"] is not None else None
        cand["bid_depth"] = round(book["bid_depth"], 0)
        if cand["spread_pct"] is not None and cand["spread_pct"] > max_spread:
            cand["reject"] = f"спред {cand['spread_pct']:.2f}% > {max_spread}%"
        marketdata.apply_flow(cand, 1 if cand.get("chg", 0) >= 0 else -1)
        out.append(cand)
    return [c for c in out if not c.get("reject")]


def _entry_price(client: FinamClient, symbol: str, fallback: float) -> float:
    try:
        book = book_summary(client.orderbook(symbol))
        if book.get("best_ask"):
            return float(book["best_ask"])
    except Exception:  # noqa: BLE001
        pass
    return fallback


def execute(client: FinamClient, cand: dict, cfg: dict, now: datetime | None = None) -> dict:
    """Купить лучшего кандидата на size_pct_of_equity % депозита; затем SL/TP."""
    lotsize = int(cand.get("lotsize") or 1)
    symbol = norm_symbol(cand["ticker"])
    price = _entry_price(client, symbol, float(cand.get("last") or 0.0))
    plan = cand.get("plan") or {}

    account = client.account()
    equity = num(account.get("equity"), 0.0) or 0.0
    cash = client.cash_rub()
    trades_cfg = load_config()
    lots = size_lots(
        equity,
        price,
        lotsize,
        float(cfg["size_pct_of_equity"]),
        cash,
        float(trades_cfg.get("max_order_value_rub", 1e12)),
    )
    if lots <= 0:
        result = {
            "status": "skipped",
            "reason": f"{cfg['size_pct_of_equity']}% депозита ({equity * float(cfg['size_pct_of_equity']) / 100:.0f} ₽) "
                      f"не хватает на лот {symbol} по {price}",
        }
        _journal({"ts": datetime.now(MSK).isoformat(), "action": "autotrade_skipped", **result})
        return result
    quantity = lots * lotsize
    value = quantity * price
    load_after, _ = deposit_load_pct(account)
    if load_after + value / equity * 100 >= float(cfg["max_deposit_load_pct"]):
        result = {"status": "skipped", "reason": f"после сделки нагрузка превысит {cfg['max_deposit_load_pct']}%"}
        _journal({"ts": datetime.now(MSK).isoformat(), "action": "autotrade_skipped", **result})
        return result

    verdict = preflight(
        client,
        symbol,
        "BUY",
        quantity,
        round(price, 4),
        order_type="ORDER_TYPE_LIMIT",
        now=now,
    )
    if not verdict.ok:
        result = {"status": "blocked", "reason": [c.detail for c in verdict.failures], "card": verdict.card}
        _journal({"ts": datetime.now(MSK).isoformat(), "action": "autotrade_blocked", **result})
        return result

    order = client.place_order(
        symbol, "BUY", quantity, limit_price=round(price, 4), comment="autotrade 1 lot"
    )
    status = order.get("status", "")
    filled = (num(order.get("executed_quantity"), 0.0) or 0.0) >= quantity or status == "ORDER_STATUS_FILLED"
    result: dict = {
        "status": "placed",
        "symbol": symbol,
        "quantity": quantity,
        "lots": lots,
        "size_pct": cfg["size_pct_of_equity"],
        "limit_price": round(price, 4),
        "order_id": order.get("order_id"),
        "order_status": status,
        "filled": filled,
        "score": cand.get("score"),
        "rank": cand.get("_rank"),
        "plan": plan,
        "flow": cand.get("flow"),
    }

    if filled and cfg.get("with_protection", True):
        sl, tp = plan.get("stop"), plan.get("target")
        if sl and tp:
            sverdict = preflight_sltp(client, symbol, quantity, sl_price=sl, tp_price=tp, now=now)
            if sverdict.ok:
                sltp = client.place_sltp(symbol, "SELL", quantity, sl_price=sl, tp_price=tp, comment="autotrade")
                result["sltp"] = {"order_id": sltp.get("order_id"), "status": sltp.get("status"), "sl": sl, "tp": tp}
            else:
                result["sltp"] = {"error": [c.detail for c in sverdict.failures]}
        else:
            result["sltp"] = {"error": "в плане нет стопа/цели"}
    elif not filled:
        result["sltp"] = {"error": "заявка не исполнена — защиту выставить после фила"}

    _journal({"ts": datetime.now(MSK).isoformat(), "action": "autotrade", **result})
    return result


def maybe_trade(client: FinamClient, candidates: list[dict], cfg: dict | None = None, now: datetime | None = None) -> dict:
    """Проверить правило и, если можно, совершить одну сделку 1 лотом."""
    cfg = autotrade_cfg(cfg)
    state = check_rule(client, cfg, now)
    if not state["allowed"]:
        return {"traded": False, "state": state}

    traded_today = today_traded_symbols(client.orders(), now)
    limit_per_symbol = int(cfg.get("max_trades_per_symbol_per_day", 1))
    if limit_per_symbol > 0:
        candidates = [c for c in candidates if norm_symbol(c["ticker"]) not in traded_today]
    best = pick_best(candidates, cfg)
    if best is None:
        return {"traded": False, "state": state, "reason": "нет кандидатов, проходящих критерии надёжности"}
    result = execute(client, best, cfg, now)
    return {"traded": result.get("status") == "placed", "state": state, "pick": best.get("ticker"), "result": result}


def render(result: dict) -> str:
    state = result.get("state") or {}
    lines = [
        f"Автоторговля: {'сделка' if result.get('traded') else 'пропуск'} — {state.get('reason', '')}",
        f"  сделок сегодня: {state.get('trades_today')}, нагрузка: {state.get('load_pct')}%",
    ]
    if result.get("pick"):
        lines.append(f"  выбран: {result['pick']}")
    if not result.get("traded") and result.get("reason"):
        lines.append(f"  причина: {result['reason']}")
    res = result.get("result") or {}
    if res.get("order_id"):
        lines.append(
            f"  заявка {res['order_id']}: {res.get('symbol')} x{res.get('quantity')} "
            f"({res.get('lots')} лот, ~{res.get('size_pct')}% депозита) @{res.get('limit_price')} "
            f"({res.get('order_status')})"
        )
    if res.get("sltp"):
        sltp = res["sltp"]
        if sltp.get("order_id"):
            lines.append(f"  защита {sltp['order_id']}: SL {sltp.get('sl')} / TP {sltp.get('tp')}")
        elif sltp.get("error"):
            lines.append(f"  защита: {sltp['error']}")
    if res.get("reason"):
        lines.append(f"  пропуск: {res['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Автоторговля по правилу 3 сделки / 80% нагрузки")
    parser.add_argument("--dry-run", action="store_true", help="только показать решение")
    parser.add_argument("--top", type=int, default=8)
    args = parser.parse_args(argv)

    import screener

    client = FinamClient()
    account = client.account()
    equity = num(account.get("equity"), 0.0) or 0.0
    cash = client.cash_rub()
    result_screen = screener.screen(equity=equity, cash=cash, top=args.top)
    result_screen["candidates"] = enrich_candidates(client, result_screen["candidates"])
    result_screen["candidates"].sort(key=lambda c: c["score"], reverse=True)
    cfg = autotrade_cfg()
    state = check_rule(client, cfg)
    print(render({"traded": False, "state": state}))
    best = pick_best(result_screen["candidates"], cfg)
    if best:
        print(f"кандидат: {best['ticker']} score={best['score']} rank={best.get('_rank')} "
              f"ход {best['plan']['move_pct']}% спред {best.get('spread_pct')}%")
    else:
        print("кандидатов нет")
    if args.dry_run:
        return 0
    result = maybe_trade(client, result_screen["candidates"], cfg)
    print(render(result))
    return 0 if result.get("traded") or not result.get("result") else 3


if __name__ == "__main__":
    raise SystemExit(main())
