#!/usr/bin/env python3
"""Мост Telegram → агент: отдаёт новые сообщения пользователя по HTTP.

GET /pending  -> {"messages": [{"id": N, "text": "..."}], "count": N}
GET /health   -> {"ok": true}

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

CONF = Path("/opt/app/telegram.local.json")
STATE = Path(__file__).with_name("tg_bridge_state.json")
HOST, PORT = "127.0.0.1", 8798


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


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - API http.server
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
        self._json({"error": "not found"}, 404)

    def log_message(self, fmt: str, *args) -> None:  # тише в журнале
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"tg_bridge: http://{HOST}:{PORT}/pending", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
