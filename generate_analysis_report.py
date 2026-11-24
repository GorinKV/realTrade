#!/usr/bin/env python3
"""Часовой анализ портфеля (realTrade).

Данные через локальный сервис https://127.0.0.1:9443:
  - /api/moex/day/summary  — дневной свод (open/high/low/close/change, объём)
  - /api/tinkoff/orderbook — стакан LQDT (поллер торгует LQDT только вручную)

"В списке активов" (in play) — динамический: из дневного свода берутся тикеры
с объёмом выше порога или с большим дневным Δ (алгоритм поиска in play),
объединенные с watchlist.

Результат: /opt/app/scripts/analysis/analysis_latest.md + analysis_log.txt,
и отправляется в Telegram (токен и chat_id из /opt/app/telegram.local.json).
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

WATCHLIST = ["LQDT", "ROSN", "VKCO", "FLOT", "SBER", "X5", "FIXR", "SMLT", "POSI"]
# алгоритм in play: объём >= VOL_MIN ИЛИ |Δдень| >= CHG_MIN
VOL_MIN = 20_000
CHG_MIN = 0.5
MAX_IN_PLAY = 8


def get(path: str, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    try:
        with urllib.request.urlopen(url, context=CTX, timeout=15) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def in_play_tickers(summary: dict, watchlist: list[str]) -> list[str]:
    """Динамический список «в списке активов» (in play)."""
    rows = [p for p in summary.get("prices", []) if p.get("ticker")]
    inplay = [
        p["ticker"] for p in rows
        if p.get("volume", 0) >= VOL_MIN or abs(p.get("change_day_pct", 0)) >= CHG_MIN
    ]
    # сортируем по объёму вниз, добавляем watchlist, ограничиваем
    inplay.sort(key=lambda t: -next((p["volume"] for p in rows if p["ticker"] == t), 0))
    seen: set[str] = set()
    out = [t for t in inplay + watchlist if not (t in seen or seen.add(t))][:MAX_IN_PLAY]
    return out


def send_telegram(text: str) -> None:
    try:
        conf = json.loads(Path("/opt/app/telegram.local.json").read_text())
        tok, chat = conf.get("bot_token", ""), conf.get("chat_id", "")
    except Exception:
        return
    if not tok or not chat:
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
    except Exception:
        pass


def main() -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    summary = get("/api/moex/day/summary") or {}
    days = {p["ticker"]: p for p in summary.get("prices", []) if p.get("ticker")}
    tickers = in_play_tickers(summary, WATCHLIST)

    lines: list[str] = []
    for t in tickers:
        d = days.get(t)
        if not d:
            lines.append(f"- **{t}** — данные дневного свода недоступны")
            continue
        lines.append(
            f"- **{t}** {d.get('close', '?'):>7} (open {d.get('open', '?')}, "
            f"H {d.get('high', '?')}, L {d.get('low', '?')})  "
            f"Δдень {d.get('change_day_pct', 0):+.2f}%, V {d.get('volume', 0):,}"
        )

    ob = get("/api/tinkoff/orderbook", ticker="LQDT", depth=6)
    if ob and ob.get("bids"):
        lines.append(
            "LQDT стакан: " +
            "биды " + str([(b["price"], b["quantity"]) for b in ob["bids"][:3]]) +
            " аски " + str([(a["price"], a["quantity"]) for a in ob["asks"][:3]])
        )
    tr = get("/api/tinkoff/trades", ticker="LQDT", minutes=60)
    if tr:
        lines.append(f"LQDT за 60м: {tr.get('total_trades', 0)} сделок, объём {tr.get('total_volume', 0):,}")

    report = f"## {now}\n\n" + "\n".join(lines) + "\n"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "analysis_latest.md").write_text(report, encoding="utf-8")
    with (OUT / "analysis_log.txt").open("a", encoding="utf-8") as fh:
        fh.write(report + "\n")
    print(report)
    send_telegram(f"📊 <b>Анализ портфеля {now}</b>\n\n" + "\n".join(l.replace("**", "") for l in lines[:8]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
