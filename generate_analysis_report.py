#!/usr/bin/env python3
"""Часовой анализ рынка и портфеля (realTrade).

1. Портфель Finam (счёт 950034): equity, кэш, позиции, защитные SL/TP.
2. Скринер «in play» по всему TQBR (MOEX ISS): RVOL, расширение диапазона,
   ATR, гэп, тренд — с готовым планом входа/стопа/цели.
3. Проверка финалистов по реальному стакану Finam (спред, глубина).
4. Результат: /opt/app/scripts/analysis/analysis_latest.md + лог + Telegram.

Запуск:  python generate_analysis_report.py [--no-telegram] [--top N]
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autotrade  # noqa: E402
import screener  # noqa: E402
from finam_client import FinamClient, book_summary, num  # noqa: E402

OUT = Path("/opt/app/scripts/analysis")
TELEGRAM_CONF = Path("/opt/app/telegram.local.json")
MSK = ZoneInfo("Europe/Moscow")
MAX_SPREAD_PCT = 0.4


def telegram_send(text: str) -> bool:
    try:
        conf = json.loads(TELEGRAM_CONF.read_text(encoding="utf-8"))
        token, chat = conf.get("bot_token", ""), conf.get("chat_id", "")
    except (OSError, ValueError):
        return False
    if not token or not chat:
        return False
    body = urllib.parse.urlencode(
        {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - уведомление не должно ломать отчёт
        return False


def portfolio_block(client: FinamClient) -> tuple[list[str], dict]:
    acc = client.account()
    equity = num(acc.get("equity"), 0.0) or 0.0
    cash = client.cash_rub()
    positions = []
    for p in acc.get("positions") or []:
        positions.append(
            {
                "symbol": p.get("symbol"),
                "qty": num(p.get("quantity"), 0.0) or 0.0,
                "avg": num(p.get("average_price")),
                "last": num(p.get("current_price")),
                "pnl": num(p.get("unrealized_pnl"), 0.0) or 0.0,
            }
        )

    covered: dict[str, float] = {}
    pending: list[dict] = []
    orders_error = None
    try:
        active = client.active_orders()
    except Exception as exc:  # noqa: BLE001 - 503 на /orders не должен ломать отчёт
        active = []
        orders_error = exc
    if orders_error is not None:
        lines_orders_warning = f"⚠️ активные заявки недоступны ({orders_error}) — статус защиты не проверен"
    else:
        lines_orders_warning = None
    for order in active:
        sltp = order.get("sltp_order") or {}
        if sltp:
            symbol = sltp.get("symbol", "")
            covered[symbol] = covered.get(symbol, 0.0) + (num(sltp.get("quantity_sl"), 0.0) or 0.0)
            pending.append(
                {
                    "order_id": order.get("order_id"),
                    "symbol": symbol,
                    "status": order.get("status"),
                    "sl": num(sltp.get("sl_price")),
                    "tp": num(sltp.get("tp_price")),
                    "qty_sl": num(sltp.get("quantity_sl")),
                    "qty_tp": num(sltp.get("quantity_tp")),
                }
            )

    lines = [
        *([lines_orders_warning] if lines_orders_warning else []),
        f"Портфель Finam {client.account_id}: equity {equity:,.2f} ₽, кэш {cash:,.2f} ₽"
        .replace(",", " ")
        .replace("₽  кэш", "₽, кэш"),
    ]
    for p in positions:
        lines.append(
            f"  {p['symbol']}: {p['qty']:.0f} шт, средняя {p['avg']}, последняя {p['last']}, PnL {p['pnl']:+.2f} ₽"
        )
    if not positions:
        lines.append("  позиций нет")
    for deal in pending:
        lines.append(
            f"  защита {deal['symbol']}: SL {deal['sl']} / TP {deal['tp']} "
            f"(SL {deal['qty_sl']:.0f} шт, TP {deal['qty_tp']:.0f} шт), статус {deal['status']}"
        )
    for p in positions:
        if orders_error is not None:
            break
        if covered.get(p["symbol"], 0.0) < p["qty"]:
            lines.append(
                f"  ⚠️ {p['symbol']}: без стопа {p['qty'] - covered.get(p['symbol'], 0.0):.0f} шт из {p['qty']:.0f}"
            )
    return lines, {"equity": equity, "cash": cash, "positions": positions, "protection": pending}


def enrich_with_finam(client: FinamClient, candidates: list[dict]) -> list[dict]:
    """Реальные bid/ask Finam + поток T-Invest (общая логика в autotrade)."""
    return autotrade.enrich_candidates(client, candidates, MAX_SPREAD_PCT)


def render(result: dict, port_lines: list[str], portfolio: dict, trade_text: str | None = None) -> str:
    lines = [f"## {result['generated_at']}", ""]
    lines += port_lines
    if trade_text:
        lines += ["", trade_text]
    market = result["market"]
    lines += [
        "",
        f"Рынок: ширина {market['breadth']}% ({market['advancers']}↑ / {market['decliners']}↓), "
        f"+2%: {market['up2']}, −2%: {market['down2']}, оборот {market['turnover_rub'] / 1e9:.1f} млрд ₽",
        "",
    ]
    if not result["candidates"]:
        lines.append("Кандидатов нет: ни одна бумага не проходит фильтры ликвидности/ATR/комиссии.")
        return "\n".join(lines) + "\n"

    lines += [
        "| # | Тикер | Сетап | Цена | Δдень | RVOL | ATR% | Вход | Стоп | Цель | R:R | Лоты | Спред | Поток T |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, c in enumerate(result["candidates"], 1):
        p = c["plan"]
        spread = f"{c['spread_pct']:.2f}%" if c.get("spread_pct") is not None else "—"
        flow = c.get("flow")
        flow_txt = f"{flow['bias']:+.0f} ({flow['note']})" if flow else "—"
        lines.append(
            f"| {i} | {c['ticker']} | {p['setup']} | {c['last']:.4g} | {c['chg']:+.2f}% | "
            f"{c['rvol']:.1f}× | {c['atr_pct']:.1f} | {p['entry']:.4g} | {p['stop']:.4g} | "
            f"{p['target']:.4g} | {p['rr']:.1f} | {p['lots']} | {spread} | {flow_txt} |"
        )
    lines += [
        "",
        "Вход: пробой дневного максимума +0.1 ATR (breakout) либо текущая цена (pullback). "
        "Стоп 1.2 ATR, цель 2.5 ATR. Риск 0.5% equity; лоты посчитаны от equity и кэша Finam.",
        "Данные: MOEX ISS (весь рынок) + T-Invest (стакан/лента). Исполнение и проверка перед сделкой: Финам.",
    ]
    return "\n".join(lines) + "\n"


def render_telegram(result: dict, port_lines: list[str], portfolio: dict, trade_text: str | None = None) -> str:
    market = result["market"]
    parts = [
        f"📊 <b>Анализ {result['generated_at']}</b>",
        "",
        *[html.escape(line) for line in port_lines],
        "",
        *([html.escape(trade_text), ""] if trade_text else []),
        f"Рынок: ширина {market['breadth']}% ({market['advancers']}↑/{market['decliners']}↓), "
        f"оборот {market['turnover_rub'] / 1e9:.1f} млрд ₽",
        "",
    ]
    if not result["candidates"]:
        parts.append("Кандидатов нет — фильтры ликвидности/ATR не пройдены.")
        return "\n".join(parts)

    parts.append("<b>В игре (long):</b>")
    for i, c in enumerate(result["candidates"], 1):
        p = c["plan"]
        spread = f", спред {c['spread_pct']:.2f}%" if c.get("spread_pct") is not None else ""
        flow = c.get("flow")
        flow_txt = f", поток {flow['bias']:+.0f}" if flow else ""
        parts.append(
            f"{i}. <b>{c['ticker']}</b> {c['last']:.4g} ({c['chg']:+.2f}%), "
            f"RVOL {c['rvol']:.1f}×, ATR {c['atr_pct']:.1f}%{spread}{flow_txt}\n"
            f"   вход {p['entry']:.4g} / стоп {p['stop']:.4g} / цель {p['target']:.4g} "
            f"(R:R {p['rr']:.1f}, {p['lots']} лот)"
        )
    parts += ["", "<i>Риск 0.5% equity; комиссия 0.3%/сторона учтена.</i>"]
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Часовой анализ рынка и портфеля")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--no-autotrade", action="store_true", help="только анализ, без сделки")
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args()

    client = FinamClient(timeout=args.timeout)
    port_lines, portfolio = portfolio_block(client)
    result = screener.screen(equity=portfolio["equity"], cash=portfolio["cash"], top=args.top + 4)
    result["candidates"] = enrich_with_finam(client, result["candidates"])
    result["candidates"].sort(key=lambda c: c["score"], reverse=True)
    result["candidates"] = result["candidates"][: args.top]

    trade_text = None
    if not args.no_autotrade:
        try:
            trade_result = autotrade.maybe_trade(client, result["candidates"])
            trade_text = autotrade.render(trade_result)
        except Exception as exc:  # noqa: BLE001 - торговля не должна ломать анализ
            trade_text = f"Автоторговля: пропуск — не удалось получить состояние счёта/заявок ({exc})"
        print(trade_text)

    report = render(result, port_lines, portfolio, trade_text)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "analysis_latest.md").write_text(report, encoding="utf-8")
    with (OUT / "analysis_log.txt").open("a", encoding="utf-8") as fh:
        fh.write(report + "\n" + "-" * 40 + "\n")
    print(report)

    if not args.no_telegram:
        ok = telegram_send(render_telegram(result, port_lines, portfolio, trade_text))
        print(f"telegram: {'отправлено' if ok else 'не отправлено'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
