"""Тесты стабильности MCP-сервера (stdio) и retry в TInvestAdapter.

Тесты работают с реальным окружением T-Инвестиций (токены из ~/.config/tinvest-mcp/).
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time

import pytest

MCP_BIN = "/home/kvg/tinvest-mcp/.venv/bin/tinvest-mcp"
MCP_ENV_FILE = "/home/kvg/.config/tinvest-mcp/.env"
MCP_CONFIG = "/home/kvg/.config/tinvest-mcp/config.toml"
MCP_STATE_DIR = "/home/kvg/.local/state/tinvest-mcp"


def _mcp_call(name: str, args: dict, timeout: float = 90, retries: int = 4):
    """Вызвать MCP через stdio: initialize → tools/call."""
    env = dict(os.environ)
    env.update({
        "TINVEST_MCP_CONFIG": MCP_CONFIG,
        "TINVEST_MCP_ENV_FILE": MCP_ENV_FILE,
        "TINVEST_MCP_STATE_DIR": MCP_STATE_DIR,
    })
    proc = subprocess.Popen(
        [MCP_BIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env,
    )
    q: queue.Queue[str] = queue.Queue()

    def reader():
        for line in proc.stdout or []:
            if line.strip():
                q.put(line.strip())

    threading.Thread(target=reader, daemon=True).start()

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv(t=30):
        end = time.time() + t
        while time.time() < end:
            try:
                return json.loads(q.get(timeout=max(0.3, end - time.time())))
            except queue.Empty:
                return None
        return None

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "test", "version": "1"}}})
    assert recv(), "MCP initialize timeout"
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    time.sleep(0.5)

    err = None
    for attempt in range(retries):
        send({"jsonrpc": "2.0", "id": 100 + attempt, "method": "tools/call",
              "params": {"name": name, "arguments": args}})
        resp = recv(timeout)
        if resp is None:
            err = "timeout"
            continue
        if "error" in resp:
            err = resp["error"].get("message", str(resp["error"]))
            continue
        text = resp["result"]["content"][0]["text"]
        if "UNAVAILABLE" in text or "handshaker" in text or "not connected" in text.lower():
            err = text[:200]
            time.sleep(2)
            continue
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        return json.loads(text) if text else {}
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    pytest.fail(f"MCP call {name} failed after {retries} retries: {err}")


def test_mcp_get_accounts_stable():
    accounts = _mcp_call("get_accounts", {})
    assert accounts, "get_accounts пустой ответ"
    assert "2188481036" in [a.get("id") for a in accounts]


def test_mcp_get_market_snapshot_vkco():
    uid = "b71bd174-c72c-41b0-a66f-5f9073e0d1f5"
    snap = _mcp_call("get_market_snapshot", {"instrument_uid": uid})
    assert snap.get("last_price"), f"нет last_price: {snap}"
    assert snap.get("best_bid"), f"нет best_bid: {snap}"
    assert snap.get("best_ask"), f"нет best_ask: {snap}"
