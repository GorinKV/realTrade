#!/usr/bin/env python3
"""Дневной/часовой анализ отслеживаемых активов (LQDT и прочие).

Через локальный сервис https://127.0.0.1:9443:
  - /api/moex/day/summary  — дневные уровни (open/high/low/close/change, объём)
  - /api/tinkoff/orderbook — стакан (если есть поллер)
  - /api/tinkoff/trades    — краткие торги за N минут

Пишет: /opt/app/scripts/analysis/analysis_latest.md + analysis_log.txt, печатает в stdout.
"""
from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
BASE = "https://127.0.0.1:9443"
OUT = Path("/opt/app/scripts/analysis")
TICKERS = ["LQDT", "ROSN", "VKCO", "FLOT", "SBER", "X5", "FIXR"]


def get(path: str, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        with urllib.request.urlopen(url, context=CTX, timeout=15) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def main() -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    summary = get("/api/moex/day/summary")
    day = {p["ticker"]: p for p in (summary or {}).get("prices", []) if p.get("ticker")}
    for t in TICKERS:
        d = day.get(t)
        if not d:
            lines.append(f"- **{t}** — данные дневного свода недоступны")
            continue
        lines.append(
            f"- **{t}** {d.get('close','?'):>7}  (open {d.get('open','?')}, "
            f"H {d.get('high','?')}, L {d.get('low','?')})  "
            f"Δдень {d.get('change_pct', 0):+.2f}%, V {d.get('volume', 0):,}"
        )
    ob = get("/api/tinkoff/orderbook", ticker="LQDT", depth=6)
    if ob and ob.get("bids"):
        top_b = [(b["price"], b["quantity"]) for b in ob["bids"][:3]]
        top_a = [(a["price"], a["quantity"]) for a in ob["asks"][:3]]
        lines.append(f"- LQDT стакан: биды {top_b}, аски {top_a}")
    else:
        lines.append("- LQDT стакан: данные недоступны (поллер не ведёт за МОЕКС)")
    tr = get("/api/tinkoff/trades", ticker="LQDT", minutes=60)
    if tr:
        lines.append(
            f"- LQDT за 60м: {tr.get('total_trades', 0)} сделок, "
            f"объём {tr.get('total_volume', 0):,}"
        )
    report = f"## {now}\n\n" + "\n".join(lines) + "\n"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "analysis_latest.md").write_text(report, encoding="utf-8")
    with (OUT / "analysis_log.txt").open("a", encoding="utf-8") as fh:
        fh.write(report + "\n")
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
