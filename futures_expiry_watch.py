#!/usr/bin/env python3
"""Напоминание об окончании фьючерсов (правило из trade_config.json:
3 дня заранее, утром и вечером — в 10:00 и 17:00 МСК).

Читает экспирирующиеся контракты из /opt/app/poll_futures.txt и шлёт
в Telegram, когда до экспирации ≤ 3 дней.
"""
import json
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
POLL = Path("/opt/app/poll_futures.txt")


def tg_send(text: str) -> None:
    conf = json.loads(Path("/opt/app/telegram.local.json").read_text())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{conf['bot_token']}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": conf["chat_id"], "text": text}).encode(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def parse_expiry(text: str):
    for part in text.split():
        if len(part) == 10 and part[2] == "." and part[5] == ".":
            try:
                return date(int(part[6:]), int(part[3:5]), int(part[:2]))
            except ValueError:
                pass
    return None


def build_reminders(today: date, lines: list[str], positions: list[dict] | None = None) -> list[str]:
    """Напоминания: экспирации в окне (≤3 дня и не дальше дня экспирации) +
    позиции, открытые на контрактах с истекающим сроком."""
    exp_map: dict[str, date] = {}
    reminders: list[str] = []
    for line in lines:
        exp = parse_expiry(line)
        if not exp:
            continue
        days = (exp - today).days
        if days < -1 or days > 3:
            continue
        sym = line.split()[0]
        exp_map[sym] = exp
        reminders.append(f"⏳ {line} — экспирирует {exp.isoformat()} ({'сегодня' if days <= 0 else f'через {days} дн.'})")
    for p in positions or []:
        sym = (p.get("symbol") or "").upper().split("@")[0]
        exp = exp_map.get(sym)
        if exp and (exp - today).days <= 3:
            reminders.append(f"⚠️ Позиция {sym} открыта на контракт с истекающим сроком {exp.isoformat()} — требуется ролл/закрытие.")
    return reminders


def main() -> None:
    if not POLL.exists():
        print("файл не найден", POLL)
        return
    today = datetime.now(MSK).date()
    lines: list[str] = []
    for raw in POLL.read_text().splitlines():
        line = raw.split("#")[0].strip().upper()
        if not line or line.startswith("-"):
            continue
        exp = parse_expiry(line)
        if exp and (exp - today).days <= 3 and (exp - today).days >= -1:
            lines.append(line)
    reminders = build_reminders(today, lines)
    if reminders:
        print("\n".join(reminders))
        tg_send("⚠️ Напоминание об экспирации фьючерсов:\n" + "\n".join(reminders))
    else:
        print("экспирации скоро нет")


if __name__ == "__main__":
    main()
