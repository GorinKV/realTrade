#!/usr/bin/env python3
"""Мост Telegram → агент: отдаёт новые сообщения пользователя по HTTP.

GET /pending  -> {"messages": [{"id": N, "text": "..."}], "count": N}
GET /health   -> {"ok": true}
GET /positions -> портфель + сводка защиты (для Mini App)
GET /market   -> топ кандидатов скринера + рыночная сводка (для Mini App)
GET /         -> Mini App: index.html из /opt/app/miniapp
GET /app/*    -> статика Mini App из /opt/app/miniapp

Токен и chat_id читаются из /opt/app/telegram.local.json (на диск, не в код
автоматизации). Состояние offset — в tg_bridge_state.json рядом со скриптом.
Слушает только 127.0.0.1:8798, наружу не открыт.

Бот получает только сообщения, отправленные ему лично (или в чат, где он
участник). Свои исходящие сообщения бот в getUpdates не видит — петли нет.
"""
from __future__ import annotations

import json
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from finam_client import FinamClient, norm_symbol, num

CONF = Path("/opt/app/telegram.local.json")
STATE = Path(__file__).with_name("tg_bridge_state.json")
HOST, PORT = "127.0.0.1", 8798
STATIC_DIR = Path("/opt/app/miniapp")


def _conf() -> dict:
    return json.loads(CONF.read_text(encoding="utf-8"))


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, TypeError):
        return {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def poll() -> list[dict]:
    conf = _conf()
    token = conf["bot_token"]
    allowed_chat = str(conf["chat_id"])
    state = load_state()
    offset = int(state.get("offset") or 0)
    recent = list(state.get("recent") or [])
    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=0&limit=50"
    if offset:
        url += f"&offset={offset}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read())

    fresh: list[dict] = []
    max_id = offset
    for update in data.get("result") or []:
        try:
            update_id = int(update.get("update_id") or 0)
        except (TypeError, ValueError):
            continue
        max_id = max(max_id, update_id)
        msg = update.get("message") or update.get("edited_message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        sender = msg.get("from") or {}
        if str(chat_id) != allowed_chat or sender.get("is_bot"):
            continue
        text = (msg.get("text") or "").strip()
        if text:
            fresh.append({"id": update_id, "text": text})

    if max_id != offset:
        offset = max_id + 1
    if fresh:
        recent = (recent + fresh)[-50:]
    save_state({"offset": offset, "recent": recent})
    return recent


def market_payload(top: int = 8) -> dict:
    """Данные для Mini App: топ кандидатов скринера + рыночная сводка."""
    from screener import screen

    c = FinamClient()
    res = screen(equity=c.equity(), cash=c.cash_rub(), top=top)
    return {
        "generated_at": res["generated_at"],
        "market": res["market"],
        "candidates": res["candidates"],
    }


def positions_payload() -> dict:
    """Данные для Mini App: портфель + сводка защиты (безубыток / прибыль / минус)."""
    c = FinamClient()
    acc = c.account()
    equity = num(acc.get("equity"), 0.0) or 0.0
    cash = c.cash_rub()
    sl_by_symbol: dict[str, float] = {}
    for o in c.active_orders():
        sl = o.get("sltp_order") or {}
        sym = sl.get("symbol") or ""
        if sym:
            sl_by_symbol[sym] = num(sl.get("sl_price"))
    positions: list[dict] = []
    stats = {"total": 0, "breakeven": 0, "protected": 0, "plus_plain": 0, "minus": 0}
    for p in acc.get("positions") or []:
        qty = num(p.get("quantity"), 0.0) or 0.0
        if not qty:
            continue
        sym = norm_symbol(p.get("symbol") or "")
        entry = num(p.get("average_price"))
        last = num(p.get("current_price"))
        sl = sl_by_symbol.get(p.get("symbol"))
        ratio = (sl / entry - 1.0) if (sl and entry) else None
        if ratio is not None and ratio >= 0.009:
            cat = "protected"
        elif ratio is not None and ratio >= -0.001:
            cat = "breakeven"
        elif last < entry:
            cat = "minus"
        else:
            cat = "plus_plain"
        stats["total"] += 1
        stats[cat] += 1
        positions.append(
            {
                "symbol": sym,
                "qty": qty,
                "avg": entry,
                "last": last,
                "pnl": num(p.get("unrealized_pnl"), 0.0) or 0.0,
                "sl": sl,
                "category": cat,
            }
        )
    return {
        "account_id": c.account_id,
        "equity": equity,
        "cash": cash,
        "positions": positions,
        "stats": stats,
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, rel: str) -> bool:
        target = (STATIC_DIR / rel.lstrip("/")).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return False
        if not target.is_file():
            return False
        body = target.read_bytes()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self) -> None:  # noqa: N802 - API http.server
        path = self.path.split("?", 1)[0]
        if self.path.startswith("/health"):
            self._json({"ok": True})
            return
        if self.path.startswith("/pending"):
            try:
                messages = poll()
                after = 0
                if "?" in self.path:
                    query = self.path.split("?", 1)[1]
                    for part in query.split("&"):
                        if part.startswith("after="):
                            try:
                                after = int(part[6:])
                            except ValueError:
                                after = 0
                messages = [m for m in messages if int(m.get("id") or 0) > after]
                self._json({"messages": messages, "count": len(messages)})
            except Exception as exc:  # noqa: BLE001 - мост не должен падать
                self._json({"messages": [], "error": f"{type(exc).__name__}: {exc}"}, 502)
            return
        if path == "/positions":
            try:
                self._json(positions_payload())
            except Exception as exc:  # noqa: BLE001 - UI не должен ломаться из-за бэкенда
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 502)
            return
        if path == "/market":
            try:
                self._json(market_payload())
            except Exception as exc:  # noqa: BLE001 - UI не должен ломаться из-за бэкенда
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 502)
            return
        rel = path.lstrip("/")
        if rel.startswith("app/"):
            rel = rel[4:]
        elif rel == "app":
            rel = "index.html"
        elif not rel:
            rel = "index.html"
        if self._static(rel):
            return
        self._json({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args) -> None:  # тише в журнале
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"tg_bridge: http://{HOST}:{PORT}/pending", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
