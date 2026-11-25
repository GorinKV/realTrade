#!/usr/bin/env python3
"""Контроль открытых сделок: текущие цены, PnL и наличие защитных заявок.

Каждый час (автоматизация). Смотрит /accounts/{id}/orders и позиции,
печатает карточку по каждой позиции. Если позиция без SL/TP — пишет
«РИСК: без защиты».

Трейлинг для фьючерсов Финама из trailing.py (BRZ6, ручные — add_trail_table
в часовом отчёте).
"""
from finam_client import FinamClient, num


def main() -> None:
    c = FinamClient()
    active = {o.get("sltp_order", {}).get("symbol"): o for o in c.active_orders() if o.get("sltp_order")}
    positions = c.positions()
    print(f"## Контроль позиций {c.account_id}")
    for p in positions or []:
        sym = p.get("symbol") or ""
        qty = num(p.get("quantity"))
        if not qty:
            continue
        s = active.get(sym)
        sltp = (s or {}).get("sltp_order") or {}
        sl = num(sltp.get("sl_price")) or None
        tp = num(sltp.get("tp_price")) or None
        protected = f"SL {sl}" + (f" / TP {tp}" if tp else "")
        print(f"{sym}: {qty:+.0f} шт, last {num(p.get('current_price')):.4f}, PnL {num(p.get('unrealized_pnl'), 0):+.2f} ₽, {protected if sl else 'РИСК: без SL/TP'}")
    for sym, o in active.items():
        if not any(p.get("symbol") == sym for p in positions or []):
            print(f"{sym}: позиции нет, статус {o.get('status')}")


if __name__ == "__main__":
    main()
