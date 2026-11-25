#!/usr/bin/env python3
"""Нефтяной скринер: BRZ6 (Brent), NGZ6 (WTI) — структура стакана, хронология,
входы по правилам книги и дневному H/L.

Отправляет свод в Telegram (секреты — /opt/app/telegram.local.json).
"""
import json
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

from finam_client import FinamClient, num


def tg_send(text: str) -> None:
    conf = json.loads(Path("/opt/app/telegram.local.json").read_text())
    url = f"https://api.telegram.org/bot{conf['bot_token']}/sendMessage"
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode({"chat_id": conf["chat_id"], "text": text}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def qty(l: dict) -> float:
    return num(l.get("quantity")) or num(l.get("size")) or num(l.get("sell_size")) or num(l.get("buy_size")) or 0.0


def screen(c: FinamClient, sym: str) -> str:
    q = c.quote(sym) or {}
    last, high, low = (num(q.get(k)) for k in ("last", "high", "low"))
    ob = c.orderbook(sym) or []
    bids = [l for l in ob if num(l.get("price")) and num(l.get("price")) <= last]
    asks = [l for l in ob if l not in bids]
    lines = [f"== {sym}"]
    if last and high and low:
        lines.append(f"last {last:.4f} | H {high:.4f} ({(high - last) / last * 100:+.2f}% от last) | "
                     f"L {low:.4f} ({(last - low) / last * 100:+.2f})")
    if bids:
        top = ", ".join(f"{num(l['price']):.4f}x{qty(l):.0f}" + ("⛁" if l.get("is_density") else "")
                        for l in bids[:4])
        lines.append(f"биды: {top}")
    if asks:
        top = ", ".join(f"{num(l['price']):.4f}x{qty(l):.0f}" + ("⛁" if l.get("is_density") else "")
                        for l in asks[:4])
        lines.append(f"аски: {top}")
    bars = c.bars(sym, timeframe="TIME_FRAME_D", start="2026-10-25T00:00:00Z", end="2026-11-26T00:00:00Z")
    closes = [(str(b.get("timestamp", ""))[:10], num(b.get("close"))) for b in (bars or []) if num(b.get("close"))]
    closes = closes[-6:]
    if closes:
        lines.append("последние дни: " + ", ".join(f"{d[-2:]}:{c:.2f}" for d, c in closes))
    return "\n".join(lines)


def main() -> None:
    c = FinamClient()
    parts = [f"⛽ нефть, {date.today().isoformat()}"]
    for sym in ("BRZ6@RTSX", "NGZ6@RTSX"):
        parts.append(screen(c, sym))
    text = "\n".join(parts)
    print(text)
    tg_send(text)


if __name__ == "__main__":
    main()
