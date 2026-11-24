#!/usr/bin/env python3
"""Клиент Finam Trade API (REST, только stdlib).

Проверено вживую на счёте 950034:

  GET  /v1/accounts/{id}                        — equity, cash, позиции
  GET  /v1/accounts/{id}/orders                 — заявки (включая SL/TP, статусы)
  GET  /v1/accounts/{id}/orders/{order_id}      — одна заявка
  POST /v1/accounts/{id}/orders                 — MARKET / LIMIT
  DEL  /v1/accounts/{id}/orders/{order_id}      — отмена (в т.ч. SL/TP)
  POST /v1/accounts/{id}/sltp-orders            — SL/TP (статус ORDER_STATUS_WATCHING)
  GET  /v1/instruments/{symbol}/quotes/latest   — bid/ask/last/volume
  GET  /v1/instruments/{symbol}/orderbook       — стакан
  GET  /v1/instruments/{symbol}/trades/latest   — последние сделки
  GET  /v1/instruments/{symbol}/bars            — свечи (TIME_FRAME_D/M5/...)

CLI (заявки — только с явным --yes):

  python finam_client.py account
  python finam_client.py quote SBER
  python finam_client.py book SBER
  python finam_client.py orders [--all]
  python finam_client.py buy  SBER --qty 1 --limit 288.50 --yes
  python finam_client.py sell SBER --qty 1 --market --yes
  python finam_client.py sltp SBER --qty 1 --sl 280 --tp 300 --yes
  python finam_client.py cancel 35593478 --yes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "https://api.finam.ru/v1"
ENV_FILE = Path(__file__).with_name("finam.env")
DEFAULT_ACCOUNT = "950034"
DEFAULT_MIC = "MISX"

ACTIVE_STATUSES = {
    "ORDER_STATUS_NEW",
    "ORDER_STATUS_PARTIALLY_FILLED",
    "ORDER_STATUS_WATCHING",
    "ORDER_STATUS_PENDING",
}

RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_ATTEMPTS = 3


class FinamError(RuntimeError):
    """Ошибка Finam API (HTTP-код + сообщение)."""

    def __init__(self, status: int | None, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"{status}: {message}" if status else message)


def load_env(path: Path | str = ENV_FILE) -> dict[str, str]:
    """Простой парсер KEY=VALUE (без export и кавычек)."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def norm_symbol(symbol: str, mic: str = DEFAULT_MIC) -> str:
    """SBER -> SBER@MISX; SBER@MISX остаётся как есть."""
    symbol = symbol.strip().upper()
    if "@" in symbol:
        return symbol
    return f"{symbol}@{mic}"


def num(obj: Any, default: float | None = None) -> float | None:
    """Достать число из {"value": "..."} или вернуть как есть."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        obj = obj.get("value")
    if obj is None or obj == "":
        return default
    try:
        return float(obj)
    except (TypeError, ValueError):
        return default


def fmt_money(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}".replace(",", " ")


class FinamClient:
    def __init__(
        self,
        secret: str | None = None,
        account_id: str | None = None,
        base_url: str = BASE_URL,
        env_file: Path | str = ENV_FILE,
        timeout: float = 25.0,
    ) -> None:
        env = load_env(env_file)
        self.secret = secret or os.environ.get("FINAM_SECRET_TOKEN") or env.get("FINAM_SECRET_TOKEN", "")
        self.account_id = (
            account_id or os.environ.get("FINAM_ACCOUNT_ID") or env.get("FINAM_ACCOUNT_ID") or DEFAULT_ACCOUNT
        )
        self.base_url = (os.environ.get("FINAM_BASE_URL") or base_url).rstrip("/")
        self.timeout = timeout
        self._token: str | None = None

    # -- transport ---------------------------------------------------------
    def _http(self, method: str, path: str, body: dict | None, token: str | None) -> tuple[int, str]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FinamError(None, f"network: {type(exc).__name__}: {exc}") from exc

    def authenticate(self, force: bool = False) -> str:
        if self._token and not force:
            return self._token
        if not self.secret:
            raise FinamError(None, "FINAM_SECRET_TOKEN не задан")
        status, text = self._http("POST", "/sessions", {"secret": self.secret}, None)
        if status != 200:
            raise FinamError(status, _error_text(text))
        token = json.loads(text).get("token", "")
        if not token:
            raise FinamError(status, "в ответе /sessions нет token")
        self._token = token
        return token

    def request(self, method: str, path: str, body: dict | None = None, *, auth: bool = True) -> Any:
        """HTTP-запрос с повторами на сетевых сбоях/429/5xx и обновлением JWT на 401."""
        last: FinamError | None = None
        for attempt in range(RETRY_ATTEMPTS):
            token = self.authenticate() if auth else None
            try:
                status, text = self._http(method, path, body, token)
            except FinamError as exc:
                last = exc
                time.sleep(0.5 * (attempt + 1))
                continue
            if status == 401 and auth and attempt == 0:
                self.authenticate(force=True)
                continue
            if status in RETRY_STATUSES:
                last = FinamError(status, _error_text(text))
                time.sleep(0.5 * (attempt + 1))
                continue
            if status >= 400:
                raise FinamError(status, _error_text(text))
            return json.loads(text) if text.strip() else {}
        raise last or FinamError(None, "запрос не удался")

    # -- account -----------------------------------------------------------
    def account(self) -> dict:
        return self.request("GET", f"/accounts/{self.account_id}")

    def equity(self) -> float:
        return num(self.account().get("equity"), 0.0) or 0.0

    def positions(self) -> list[dict]:
        return list(self.account().get("positions") or [])

    def cash_rub(self) -> float:
        total = 0.0
        for row in self.account().get("cash") or []:
            if row.get("currency_code") == "RUB":
                total += float(row.get("units", 0)) + float(row.get("nanos", 0)) / 1e9
        return total

    # -- orders ------------------------------------------------------------
    def orders(self) -> list[dict]:
        return list(self.request("GET", f"/accounts/{self.account_id}/orders").get("orders") or [])

    def active_orders(self) -> list[dict]:
        return [o for o in self.orders() if o.get("status") in ACTIVE_STATUSES]

    def order(self, order_id: str) -> dict:
        return self.request("GET", f"/accounts/{self.account_id}/orders/{order_id}")

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        *,
        order_type: str = "ORDER_TYPE_LIMIT",
        limit_price: float | None = None,
        time_in_force: str = "TIME_IN_FORCE_DAY",
        comment: str = "",
    ) -> dict:
        if order_type == "ORDER_TYPE_LIMIT" and limit_price is None:
            raise ValueError("для лимитной заявки нужен limit_price")
        body: dict[str, Any] = {
            "symbol": norm_symbol(symbol),
            "quantity": {"value": str(quantity)},
            "side": "SIDE_BUY" if side.upper() in {"BUY", "SIDE_BUY"} else "SIDE_SELL",
            "type": order_type,
            "time_in_force": time_in_force,
            "comment": comment[:128],
        }
        if limit_price is not None:
            body["limit_price"] = {"value": f"{limit_price:.6f}"}
        return self.request("POST", f"/accounts/{self.account_id}/orders", body)

    def cancel_order(self, order_id: str) -> dict:
        return self.request("DELETE", f"/accounts/{self.account_id}/orders/{order_id}")

    def place_sltp(
        self,
        symbol: str,
        side: str,
        quantity: int,
        *,
        sl_price: float | None = None,
        tp_price: float | None = None,
        valid_before: str = "VALID_BEFORE_GOOD_TILL_CANCEL",
        comment: str = "",
    ) -> dict:
        if sl_price is None and tp_price is None:
            raise ValueError("нужен хотя бы один из sl_price/tp_price")
        body: dict[str, Any] = {
            "symbol": norm_symbol(symbol),
            "side": "SIDE_BUY" if side.upper() in {"BUY", "SIDE_BUY"} else "SIDE_SELL",
            "valid_before": valid_before,
            "comment": comment[:128],
        }
        if sl_price is not None:
            body["quantity_sl"] = {"value": str(quantity)}
            body["sl_price"] = {"value": f"{sl_price:.6f}"}
        if tp_price is not None:
            body["quantity_tp"] = {"value": str(quantity)}
            body["tp_price"] = {"value": f"{tp_price:.6f}"}
        return self.request("POST", f"/accounts/{self.account_id}/sltp-orders", body)

    # -- market data -------------------------------------------------------
    def asset(self, symbol: str) -> dict:
        return self.request("GET", f"/assets/{norm_symbol(symbol)}?account_id={self.account_id}")

    def asset_params(self, symbol: str) -> dict:
        return self.request("GET", f"/assets/{norm_symbol(symbol)}/params?account_id={self.account_id}")

    def quote(self, symbol: str) -> dict:
        return self.request("GET", f"/instruments/{norm_symbol(symbol)}/quotes/latest").get("quote") or {}

    def orderbook(self, symbol: str) -> list[dict]:
        return list(self.request("GET", f"/instruments/{norm_symbol(symbol)}/orderbook").get("orderbook", {}).get("rows") or [])

    def trades(self, symbol: str) -> list[dict]:
        return list(self.request("GET", f"/instruments/{norm_symbol(symbol)}/trades/latest").get("trades") or [])

    def bars(self, symbol: str, timeframe: str = "TIME_FRAME_D", start: str | None = None, end: str | None = None) -> list[dict]:
        query: dict[str, str] = {"timeframe": timeframe}
        if start:
            query["interval.start_time"] = start
        if end:
            query["interval.end_time"] = end
        path = f"/instruments/{norm_symbol(symbol)}/bars?{urllib.parse.urlencode(query)}"
        return list(self.request("GET", path).get("bars") or [])


def _error_text(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return text[:300]
    msg = data.get("message") or data.get("detail") or text
    details = data.get("details")
    if details:
        msg = f"{msg} {details}"
    return str(msg)[:500]


def book_summary(rows: list[dict]) -> dict:
    """Собрать биды/аски из строк стакана Finam (buy_size/sell_size)."""
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for row in rows:
        price = num(row.get("price"))
        if price is None:
            continue
        buy = num(row.get("buy_size"), 0.0) or 0.0
        sell = num(row.get("sell_size"), 0.0) or 0.0
        if buy > 0:
            bids.append((price, buy))
        if sell > 0:
            asks.append((price, sell))
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    spread_pct = None
    if best_bid and best_ask:
        mid = (best_bid + best_ask) / 2
        spread_pct = (best_ask - best_bid) / mid * 100 if mid else None
    return {
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_pct": spread_pct,
        "bid_depth": sum(q for _, q in bids[:5]),
        "ask_depth": sum(q for _, q in asks[:5]),
    }


# -- CLI ---------------------------------------------------------------------
def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _require_yes(args: argparse.Namespace, card: dict) -> None:
    print("КАРТОЧКА ЗАЯВКИ:")
    _print(card)
    if not args.yes:
        print("\nЭто предпросмотр. Для исполнения повторите с --yes")
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finam Trade API (REST)")
    parser.add_argument("--account", default=None, help=f"счёт (по умолчанию {DEFAULT_ACCOUNT})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("account", help="equity, cash, позиции")
    for name in ("quote", "book", "trades"):
        p = sub.add_parser(name)
        p.add_argument("symbol")
    p = sub.add_parser("orders")
    p.add_argument("--all", action="store_true", help="включая исполненные/отменённые")

    p = sub.add_parser("buy")
    p.add_argument("symbol")
    p.add_argument("--qty", type=int, required=True)
    p.add_argument("--limit", type=float)
    p.add_argument("--market", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true", help="игнорировать блокирующие проверки pretrade")

    p = sub.add_parser("sell")
    p.add_argument("symbol")
    p.add_argument("--qty", type=int, required=True)
    p.add_argument("--limit", type=float)
    p.add_argument("--market", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true", help="игнорировать блокирующие проверки pretrade")

    p = sub.add_parser("sltp")
    p.add_argument("symbol")
    p.add_argument("--qty", type=int, required=True)
    p.add_argument("--sl", type=float)
    p.add_argument("--tp", type=float)
    p.add_argument("--side", default="SIDE_SELL", choices=["SIDE_SELL", "SIDE_BUY"])
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true", help="игнорировать блокирующие проверки pretrade")

    p = sub.add_parser("cancel")
    p.add_argument("order_id")
    p.add_argument("--yes", action="store_true")

    args = parser.parse_args(argv)
    client = FinamClient(account_id=args.account)

    if args.cmd == "account":
        acc = client.account()
        _print(
            {
                "account_id": acc.get("account_id"),
                "equity": num(acc.get("equity")),
                "cash_rub": client.cash_rub(),
                "positions": [
                    {
                        "symbol": p.get("symbol"),
                        "quantity": num(p.get("quantity")),
                        "avg": num(p.get("average_price")),
                        "last": num(p.get("current_price")),
                        "pnl": num(p.get("unrealized_pnl")),
                    }
                    for p in acc.get("positions") or []
                ],
                "active_orders": [
                    {"order_id": o.get("order_id"), "status": o.get("status"), "symbol": o.get("order", {}).get("symbol")}
                    for o in client.active_orders()
                ],
            }
        )
        return 0

    if args.cmd == "quote":
        _print(client.quote(args.symbol))
        return 0

    if args.cmd == "book":
        _print(book_summary(client.orderbook(args.symbol)))
        return 0

    if args.cmd == "trades":
        _print(client.trades(args.symbol)[:20])
        return 0

    if args.cmd == "orders":
        rows = client.orders() if args.all else client.active_orders()
        _print(rows)
        return 0

    if args.cmd in {"buy", "sell"}:
        from pretrade import preflight

        side = "SIDE_BUY" if args.cmd == "buy" else "SIDE_SELL"
        order_type = "ORDER_TYPE_MARKET" if args.market or args.limit is None else "ORDER_TYPE_LIMIT"
        verdict = preflight(client, args.symbol, side, args.qty, args.limit, order_type=order_type)
        print(verdict.render())
        if not verdict.ok and not args.force:
            print("\nЗаявка не отправлена: есть блокирующие замечания (--force — только осознанно).")
            return 3
        card = verdict.card
        _require_yes(args, card)
        _print(
            client.place_order(
                args.symbol, side, args.qty, order_type=order_type, limit_price=args.limit, comment="realTrade"
            )
        )
        return 0

    if args.cmd == "sltp":
        from pretrade import preflight_sltp

        verdict = preflight_sltp(
            client, args.symbol, args.qty, sl_price=args.sl, tp_price=args.tp, side=args.side
        )
        print(verdict.render())
        if not verdict.ok and not args.force:
            print("\nSL/TP не выставлены: есть блокирующие замечания (--force — только осознанно).")
            return 3
        _require_yes(args, verdict.card)
        _print(client.place_sltp(args.symbol, args.side, args.qty, sl_price=args.sl, tp_price=args.tp, comment="realTrade"))
        return 0

    if args.cmd == "cancel":
        _require_yes(args, {"order_id": args.order_id})
        _print(client.cancel_order(args.order_id))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
