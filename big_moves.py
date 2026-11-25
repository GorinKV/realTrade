#!/usr/bin/env python3
"""Анализ больших движений: самые движимые бумаги дня по дневному ходу.

MOEX ISS — рыночные данные. Не торгуем и не исполняем здесь:
для этого generate_analysis_report (Финам).
"""
from screener import _f, day_change_pct, fetch_board


def main() -> None:
    rows = fetch_board()
    out = []
    for row in rows:
        chg = day_change_pct(row)
        if chg is None or chg == 0.0:
            continue
        last = _f(row.get("LAST")) or 0.0
        turnover = _f(row.get("VALTODAY_RUR")) or 0.0
        out.append((abs(chg), row.get("SECID") or "?", last, chg, turnover))
    out.sort(reverse=True)
    for _, secid, last, chg, turnover in out[:12]:
        print(f"{secid:8s} LAST={last:>12.4f}  ΔДЕНЬ {chg:+8.2f}%  оборот {turnover/1e9:7.3f} млрд ₽")


if __name__ == "__main__":
    main()
