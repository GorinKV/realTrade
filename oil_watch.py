#!/usr/bin/env python3
"""Анализ движений по нефтяным фьючерсам (BRZ6 — Brent, NGZ6 — WTI).

Поиск точек входа: структура стакана (разброс по уровням), дистанция
последней цены до дневного H/L, торговая активность.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import marketdata
from finam_client import FinamClient, num


def levels(rows):
    out = []
    for r in rows or []:
        p, q = num(r.get("price")), num(r.get("quantity"))
        if p is None:
            continue
        out.append((p, q or 0, r.get("is_density") or False, r.get("ratio")))
    return out[:6]


def main() -> None:
    c = FinamClient()
    for sym in ("BRZ6@RTSX", "NGZ6@RTSX"):
        q = c.quote(sym) or {}
        last = num(q.get("last"))
        high = num(q.get("high"))
        low = num(q.get("low"))
        vol = num(q.get("volume"))
        print(f"== {sym}")
        if last:
            print(f"   last {last:.4f}   H {high}   L {low}")
            if high and low and last:
                print(f"   дистанция до H: +{(high - last) / last * 100:.2f}%   "
                      f"до L: {(last - low) / last * 100:.2f}%   vol {vol:,.0f}")
        ob = marketdata.orderbook(sym.split("@")[0], depth=6) or {}
        bids = levels(ob.get("bids"))
        asks = levels(ob.get("asks"))
        if bids:
            print("   биды:", ", ".join(f"{p:.4f}x{q:.0f}" + ("*плотн" if d else "") for p, q, d, _ in bids))
        if asks:
            print("   аски:", ", ".join(f"{p:.4f}x{q:.0f}" + ("*плотн" if d else "") for p, q, d, _ in asks))
        tr = marketdata.trades(sym.split("@")[0]) or {}
        if tr.get("delta") is not None and tr.get("total_volume"):
            v = int(tr["total_volume"])
            print(f"   поток(лента): delta {num(tr.get('delta')):+.0f} / vol {v:,.0f} "
                  f"({num(tr.get('delta')) / v * 100:+.2f}%)")


if __name__ == "__main__":
    main()
