"""Unit-тесты retry-логики в TInvestAdapter (после патча adapter.py)."""
from __future__ import annotations

import grpc
import pytest

from tinvest_mcp.adapter import _is_transient, _stable
from tinvest_mcp.sdk import RequestError


def test_is_transient_unavailable():
    assert _is_transient(RequestError(grpc.StatusCode.UNAVAILABLE, "tcp handshaker shutdown", None))
    assert _is_transient(RequestError(grpc.StatusCode.DEADLINE_EXCEEDED, "deadline", None))
    assert not _is_transient(RequestError(grpc.StatusCode.UNKNOWN, "boom", None))
    assert not _is_transient(ValueError("x"))


def test_stable_retries_transient_then_ok():
    calls = {"n": 0}

    class Fake:
        @_stable
        def flaky(self):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RequestError(grpc.StatusCode.UNAVAILABLE, "tcp handshaker shutdown", None)
            return "ok"

    assert Fake().flaky() == "ok"
    assert calls["n"] == 3


def test_stable_reraises_non_transient():
    calls = {"n": 0}

    class Fake:
        @_stable
        def boom(self):
            calls["n"] += 1
            raise ValueError("not transient")

    with pytest.raises(ValueError):
        Fake().boom()
    assert calls["n"] == 1
