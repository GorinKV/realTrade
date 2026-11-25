#!/usr/bin/env python3
"""Поиск плотностей стаканов T-Invest (локальный сервис 127.0.0.1:9443).

По всем наблюдаемым тикерам (poll_tickers + poll_futures из /opt/app):
выводит верх стакана, разброс по уровням и признак глубоких провалов.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import marketdata


def levels(items):
    return [(l["price"], l["quantity"]) for l in items or []][:3]


def main() -> None:
    files = ["/opt/app/poll_tickers.txt", "/opt/app/poll_futures.txt"]
    found = False
    for path in files:
        p = Path(path)
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            sym = line.split("#")[0].strip().upper()
            if not sym or sym.startswith("-"):
                continue
            ob = marketdata.orderbook(sym, depth=3)
            if not ob:
                continue
            bids, asks = levels(ob.get("bids")), levels(ob.get("asks"))
            if not bids or not asks:
                continue
            b_tot = sum(q for _, q in bids)
            a_tot = sum(q for _, q in asks)
            big = f"  ПОТОК: {b_tot:+.0f} бид / {a_tot:+.0f} аск"
            print(f"{sym}: бид {bids} аск {asks}{big}")
            found = True
    if not found:
        print("плотностей нет (сервис недоступен)")


if __name__ == "__main__":
    main()
