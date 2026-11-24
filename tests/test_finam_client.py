"""Тесты клиента Finam: парсинг, символы, стакан, ретраи, JWT."""
from __future__ import annotations

import json

import pytest

import finam_client
from finam_client import FinamClient, FinamError, book_summary, load_env, norm_symbol, num


def test_norm_symbol():
    assert norm_symbol("sber") == "SBER@MISX"
    assert norm_symbol("SBER@MISX") == "SBER@MISX"
    assert norm_symbol("LQDT", mic="TQBR") == "LQDT@TQBR"


def test_num_unwraps_value():
    assert num({"value": "2.1411"}) == pytest.approx(2.1411)
    assert num(None) is None
    assert num({"value": ""}, 7.0) == 7.0
    assert num("3.5") == 3.5


def test_load_env(tmp_path):
    path = tmp_path / "x.env"
    path.write_text("# comment\nA=1\nB='two'\nC=\"three\"\nbroken\n")
    env = load_env(path)
    assert env == {"A": "1", "B": "two", "C": "three"}


def test_book_summary_bids_asks_and_spread():
    rows = [
        {"price": {"value": "99.9"}, "buy_size": {"value": "10"}, "sell_size": {"value": "0"}},
        {"price": {"value": "100.1"}, "buy_size": {"value": "0"}, "sell_size": {"value": "20"}},
        {"price": {"value": "99.8"}, "buy_size": {"value": "5"}, "sell_size": {"value": "0"}},
    ]
    summary = book_summary(rows)
    assert summary["best_bid"] == 99.9
    assert summary["best_ask"] == 100.1
    assert summary["bid_depth"] == 15
    assert summary["ask_depth"] == 20
    assert summary["spread_pct"] == pytest.approx((100.1 - 99.9) / 100.0 * 100, rel=1e-3)


def test_client_retries_network_error(monkeypatch):
    attempts = {"n": 0}

    def fake_http(self, method, path, body, token):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise FinamError(None, "network: ConnectionResetError")
        return 200, json.dumps({"ok": True})

    monkeypatch.setattr(FinamClient, "authenticate", lambda self, force=False: "token")
    monkeypatch.setattr(FinamClient, "_http", fake_http)
    monkeypatch.setattr(finam_client.time, "sleep", lambda *_: None)
    client = FinamClient(secret="x")
    assert client.request("GET", "/test") == {"ok": True}
    assert attempts["n"] == 3


def test_client_reauths_on_401(monkeypatch):
    calls = {"auth": 0, "http": 0}

    def fake_auth(self, force=False):
        if not hasattr(self, "_tok") or force:
            calls["auth"] += 1
            self._tok = f"t{calls['auth']}"
        return self._tok

    def fake_http(self, method, path, body, token):
        calls["http"] += 1
        if calls["http"] == 1:
            return 401, json.dumps({"message": "token expired"})
        return 200, json.dumps({"token_used": token})

    monkeypatch.setattr(FinamClient, "authenticate", fake_auth)
    monkeypatch.setattr(FinamClient, "_http", fake_http)
    client = FinamClient(secret="x")
    assert client.request("GET", "/test") == {"token_used": "t2"}


def test_client_raises_on_400(monkeypatch):
    monkeypatch.setattr(FinamClient, "authenticate", lambda self, force=False: "token")
    monkeypatch.setattr(FinamClient, "_http", lambda self, m, p, b, t: (400, json.dumps({"message": "bad price"})))
    client = FinamClient(secret="x")
    with pytest.raises(FinamError) as exc:
        client.request("POST", "/orders", {})
    assert exc.value.status == 400
    assert "bad price" in exc.value.message


def test_place_order_body(monkeypatch):
    captured = {}

    def fake_request(self, method, path, body=None, **kw):
        captured.update({"method": method, "path": path, "body": body})
        return {"order_id": "1", "status": "ORDER_STATUS_NEW"}

    monkeypatch.setattr(FinamClient, "request", fake_request)
    client = FinamClient(secret="x", account_id="950034")
    client.place_order("sber", "BUY", 2, limit_price=288.55)
    assert captured["path"] == "/accounts/950034/orders"
    assert captured["body"]["symbol"] == "SBER@MISX"
    assert captured["body"]["side"] == "SIDE_BUY"
    assert captured["body"]["type"] == "ORDER_TYPE_LIMIT"
    assert captured["body"]["limit_price"]["value"] == "288.550000"


def test_place_sltp_body(monkeypatch):
    captured = {}

    def fake_request(self, method, path, body=None, **kw):
        captured.update({"path": path, "body": body})
        return {"order_id": "35593478", "status": "ORDER_STATUS_WATCHING"}

    monkeypatch.setattr(FinamClient, "request", fake_request)
    client = FinamClient(secret="x", account_id="950034")
    client.place_sltp("LQDT", "SELL", 1500, sl_price=2.09, tp_price=2.35)
    assert captured["path"] == "/accounts/950034/sltp-orders"
    body = captured["body"]
    assert body["quantity_sl"]["value"] == "1500"
    assert body["sl_price"]["value"] == "2.090000"
    assert body["tp_price"]["value"] == "2.350000"
    assert body["valid_before"] == "VALID_BEFORE_GOOD_TILL_CANCEL"
