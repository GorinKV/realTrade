#!/usr/bin/env python3
"""Поиск признаков айсберг-заявок и «роботов» в стаканах T-Инвест.

Сигнал: уровень, размер которого сильно выделяется от соседей
(> 1.5x максимум соседних уровней) — может быть размещённым «снаружи»
или постоянно обновляемым. Для контроля позиций и стопов — физика книги.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import marketdata


def icebergs(sym: str) -> list[str]:
    out = []
    ob = marketdata.orderbook(sym, depth=10)
    if not ob:
        return out
    for side in ("bids", "asks"):
        levels = ob.get(side) or []
        for i, lev in enumerate(levels):
            q = lev.get("quantity") or 0
            others = [l.get("quantity") or 0 for j, l in enumerate(levels) if j != i]
            if not others:
                continue
            ref = max(others)
            if q > max(ref * 1.5, 1) and q > max(others[:1] or [0]) * 2:
                where = f"{'бид' if side == 'bids' else 'аск'} #{i+1}"
                out.append(f"{where} {sym}: {q:.0f} шт @ {lev.get('price')} (соседи max {ref:.0f})")
    return out


def main() -> None:
    found = False
    for path in ["/opt/app/poll_tickers.txt", "/opt/app/poll_tickers2.txt", "/opt/app/poll_futures.txt"]:
        p = Path(path)
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            sym = line.split("#")[0].strip().upper()
            if not sym or sym.startswith("-"):
                continue
            rows = icebergs(sym)
            for r in rows:
                print(r)
                found = True
    if not found:
        print("айсбергов/выделяющихся уровней не видно")


if __name__ == "__main__":
    main()
