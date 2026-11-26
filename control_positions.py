#!/usr/bin/env python3
"""Контроль открытых сделок: текущие цены, PnL и наличие защитных заявок.

Каждый час (автоматизация). Смотрит /accounts/{id}/orders и позиции,
печатает карточку по каждой позиции. Если позиция без SL/TP — пишет
«РИСК: без защиты».

Правила (от пользователя):
  - стопы ставятся на акциях (лонги);
  - при прибыли > 1% SL переносится в безубыток (вход);
  - далее каждый 1% прибыли стоп трейлится на 1% прибыли выше входа.
"""
from finam_client import FinamClient, norm_symbol, num


def trail_longs(c: FinamClient) -> None:
    for p in c.positions() or []:
        qty = num(p.get("quantity"))
        if not qty or qty < 0:  # только лонги
            continue
        sym = norm_symbol(p.get("symbol") or "")
        entry = num(p.get("average_price"))
        last = num(p.get("current_price"))
        pnl = num(p.get("unrealized_pnl"), 0.0) or 0.0
        if not entry or not pnl or not last:
            continue
        profit_pct = (last / entry - 1.0) * 100
        if profit_pct < 1.0:
            continue
        target_sl = entry * (1.0 + max(0.0, profit_pct - 1.0) / 100.0)
        # SL выше входа для лонга отвергает API (там нет защиты) — ставим на вход
        if target_sl >= entry:
            # API отвергает SL выше входа для лонга — ставим на entry − 0.05
            target_sl = entry - 0.05
        if last:
            target_sl = min(target_sl, last - 0.001)  # API требует SL ниже цены
        if not target_sl or target_sl >= last:
            continue
        active = {o.get("sltp_order", {}).get("symbol"): o for o in c.active_orders() if o.get("sltp_order")}
        s = active.get(sym) or {}
        sltp = s.get("sltp_order") or {}
        cur_sl = num(sltp.get("sl_price")) or 0.0
        if cur_sl and cur_sl >= target_sl - 0.001:
            continue
        if s.get("order_id"):
            c.cancel_order(s["order_id"])
        placed = False
        for margin in (0.05, 0.1, 0.2, 0.3):
            cand = entry - margin
            if cand >= last:
                continue
            try:
                c.place_sltp(sym, "SIDE_SELL", int(qty), sl_price=cand, comment="trail 1%/1%")
                print(f"{sym}: SL {cand:.4f} (PnL +{profit_pct:.2f}%), margin {margin}")
                placed = True
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
        if not placed:
            print(f"{sym}: не удалось выставить SL ({exc})")


def main() -> None:
    c = FinamClient()
    trail_longs(c)
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
        if not any(p.get("symbol") == sym and num(p.get("quantity")) for p in positions or []):
            oid = o.get("order_id")
            try:
                c.cancel_order(oid)
                print(f"{sym}: позиции нет — осиротевший SLTP {oid} снят (защита от шорта)")
            except Exception as exc:  # noqa: BLE001
                print(f"{sym}: позиции нет, снять SLTP {oid} не удалось: {exc}")


if __name__ == "__main__":
    main()
