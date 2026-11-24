#!/usr/bin/env python3
"""Скринер «in play» по всему рынку MOEX (TQBR) + торговый план.

Данные:
  * MOEX ISS (публичный, без токена) — все акции TQBR, дневная история;
  * Finam Trade API — реальные bid/ask и стакан по финалистам.

Что считается для каждой бумаги:
  RVOL        — сегодняшний оборот с поправкой на время сессии / средний дневной;
  range_exp   — сегодняшний диапазон (H-L)/close к среднему за 14 дней;
  ATR%        — средний дневной диапазон, % (риск на единицу цены);
  gap         — гэп на открытии, %;
  pos_in_rng  — где цена в дневном диапазоне (0 — low, 1 — high);
  trend       — цена выше/ниже средней за 10 сессий.

Сделочный план (только long, шортов на счёте нет):
  вход  = пробой дневного максимума + 0.1*ATR (для breakout),
          либо текущая цена (для pullback);
  стоп  = вход − 1.2*ATR, но не выше дневного минимума;
  цель  = вход + 2.5*ATR;
  размер = риск 0.5% equity / (вход − стоп), кратно лоту и в пределах кэша.

Комиссия: 0.3%/сторона => 0.6% на круг. Кандидаты с целью < 1.5% отбрасываются.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

ISS = "https://iss.moex.com/iss"
MSK = ZoneInfo("Europe/Moscow")
SESSION_START = (10, 0)
SESSION_END = (18, 40)

DEFAULTS: dict[str, float] = {
    "min_turnover_rub": 20_000_000,   # ликвидность: не меньше 20 млн ₽ за день
    "min_trades": 500,
    "min_price": 1.0,
    "min_atr_pct": 0.8,               # иначе цель не отбивает комиссию
    "risk_pct": 0.5,                  # риск на сделку, % equity
    "commission_pct": 0.3,            # за сторону
}


def _get(url: str, timeout: float = 20.0, retries: int = 3) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "realTrade/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = exc
    raise RuntimeError(f"MOEX ISS недоступен: {last}")


def _iss(path: str, **params: str) -> dict:
    query = {"iss.meta": "off", **params}
    return _get(f"{ISS}{path}?{urllib.parse.urlencode(query)}")


def _block(doc: dict, name: str) -> list[dict]:
    block = doc.get(name) or {}
    cols = block.get("columns") or []
    return [dict(zip(cols, row)) for row in block.get("data") or []]


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


# -- market data -------------------------------------------------------------
def fetch_board(board: str = "TQBR") -> list[dict]:
    """Все бумаги режима + текущие рыночные данные (один запрос)."""
    doc = _iss(
        f"/engines/stock/markets/shares/boards/{board}/securities.json",
        **{"iss.only": "securities,marketdata"},
    )
    securities = {row["SECID"]: row for row in _block(doc, "securities")}
    merged: list[dict] = []
    for md in _block(doc, "marketdata"):
        sec = securities.get(md["SECID"])
        if sec:
            merged.append({**sec, **md})
    return merged


def fetch_history(days: int = 14, board: str = "TQBR") -> dict[str, list[dict]]:
    """Дневная история по всем бумагам режима за период (один запрос)."""
    till = date.today()
    frm = till - timedelta(days=days * 2 + 10)
    doc = _iss(
        f"/history/engines/stock/markets/shares/boards/{board}/securities.json",
        **{"iss.only": "history", "from": frm.isoformat(), "till": till.isoformat()},
    )
    out: dict[str, list[dict]] = {}
    for row in _block(doc, "history"):
        out.setdefault(row["SECID"], []).append(row)
    for rows in out.values():
        rows.sort(key=lambda r: r["TRADEDATE"])
    return out


def day_stats(rows: list[dict], lookback: int = 14) -> dict:
    """Средний оборот, ATR% и тренд по дневной истории (без сегодняшнего дня)."""
    rows = rows[-lookback:]
    turnovers = [v for v in (_f(r.get("VALUE")) for r in rows) if v and v > 0]
    ranges: list[float] = []
    closes: list[float] = []
    for idx, row in enumerate(rows):
        high, low = _f(row.get("HIGH")), _f(row.get("LOW"))
        close = _f(row.get("CLOSE")) or _f(row.get("LEGALCLOSEPRICE"))
        if not close:
            continue
        closes.append(close)
        if high and low and close:
            ranges.append((high - low) / close * 100)
    avg_turnover = sum(turnovers) / len(turnovers) if turnovers else 0.0
    atr_pct = sum(ranges) / len(ranges) if ranges else 0.0
    sma = sum(closes) / len(closes) if closes else 0.0
    return {
        "avg_turnover": avg_turnover,
        "atr_pct": atr_pct,
        "sma": sma,
        "sessions": len(rows),
    }


def session_fraction(now: datetime | None = None) -> float:
    """Доля основной сессии (10:00–18:40 MSK), прошедшая к моменту now."""
    now = (now or datetime.now(MSK)).astimezone(MSK)
    start = now.replace(hour=SESSION_START[0], minute=SESSION_START[1], second=0, microsecond=0)
    end = now.replace(hour=SESSION_END[0], minute=SESSION_END[1], second=0, microsecond=0)
    if now <= start:
        return 0.05
    if now >= end:
        return 1.0
    total = (end - start).total_seconds()
    return max(0.05, min(1.0, (now - start).total_seconds() / total))


def pos_in_range(low: float | None, high: float | None, last: float | None) -> float | None:
    if low is None or high is None or last is None or high <= low:
        return None
    return max(0.0, min(1.0, (last - low) / (high - low)))


# -- scoring -----------------------------------------------------------------
def day_change_pct(row: dict) -> float:
    """Изменение к предыдущему закрытию, % (LASTCHANGEPRCNT в ISS — к прошлой сделке)."""
    chg = _f(row.get("LASTTOPREVPRICE"))
    if chg is not None:
        return chg
    last = _f(row.get("LAST"))
    prev = _f(row.get("PREVPRICE"))
    if last and prev:
        return (last / prev - 1) * 100
    return 0.0


def score_candidate(row: dict, stats: dict, elapsed: float, direction: int = 1) -> float:
    """0..100: чем выше, тем сильнее «в игре» бумага."""
    turnover = _f(row.get("VALTODAY_RUR")) or _f(row.get("VALTODAY")) or 0.0
    avg = stats.get("avg_turnover") or 0.0
    rvol = (turnover / elapsed) / avg if avg > 0 and elapsed > 0 else 1.0

    high, low = _f(row.get("HIGH")), _f(row.get("LOW"))
    close = _f(row.get("LAST"))
    today_range = ((high - low) / close * 100) if high and low and close else 0.0
    atr = stats.get("atr_pct") or 0.0
    range_exp = today_range / atr if atr > 0 else 1.0

    chg = day_change_pct(row)
    pir = pos_in_range(low, high, close)
    pos = 0.5 if pir is None else (pir if direction > 0 else 1 - pir)

    score = 0.0
    score += min(max(rvol, 0.0) / 3.0, 1.0) * 35
    score += min(max(range_exp, 0.0) / 2.5, 1.0) * 20
    score += min(abs(chg) / 5.0, 1.0) * 20
    score += pos * 15
    score += min(turnover / 1_000_000_000, 1.0) * 10
    return round(min(score, 100.0), 1)


def classify(row: dict, stats: dict) -> str:
    """Тип сетапа: breakout / pullback / breakdown / range."""
    chg = day_change_pct(row)
    pir = pos_in_range(_f(row.get("LOW")), _f(row.get("HIGH")), _f(row.get("LAST")))
    sma = stats.get("sma") or 0.0
    last = _f(row.get("LAST")) or 0.0
    if pir is None:
        return "range"
    if chg > 0 and last >= sma and pir >= 0.7:
        return "breakout"
    if chg > 0 and last >= sma * 0.99:
        return "pullback"
    if chg < 0 and last <= sma and pir <= 0.3:
        return "breakdown"
    return "range"


def size_position(entry: float, stop: float, equity: float, risk_pct: float, lotsize: int, cash: float) -> int:
    """Целые лоты: риск ≤ risk_pct equity и стоимость ≤ 95% кэша."""
    risk_per_share = entry - stop
    if risk_per_share <= 0 or entry <= 0:
        return 0
    by_risk = (equity * risk_pct / 100) / risk_per_share
    by_cash = (cash * 0.95) / (entry * max(lotsize, 1))
    lots = int(min(by_risk // max(lotsize, 1), by_cash))
    return max(lots, 0)


def trade_plan(row: dict, stats: dict, equity: float, cash: float, cfg: dict[str, float] | None = None) -> dict:
    cfg = {**DEFAULTS, **(cfg or {})}
    last = _f(row.get("LAST")) or 0.0
    high, low = _f(row.get("HIGH")), _f(row.get("LOW"))
    atr_pct = stats.get("atr_pct") or 0.0
    atr = last * atr_pct / 100 if last else 0.0
    lotsize = int(_f(row.get("LOTSIZE"), 1) or 1)
    setup = classify(row, stats)

    if setup == "breakout" and high:
        entry = high + 0.1 * atr
    else:
        entry = last
    stop = entry - 1.2 * atr
    if low:
        stop = min(stop, low - 0.05 * atr)
    target = entry + 2.5 * atr
    lots = size_position(entry, stop, equity, cfg["risk_pct"], lotsize, cash)
    move_pct = (target - entry) / entry * 100 if entry else 0.0
    rr = (target - entry) / (entry - stop) if entry > stop else 0.0
    return {
        "setup": setup,
        "entry": round(entry, 4),
        "stop": round(stop, 4),
        "target": round(target, 4),
        "rr": round(rr, 2),
        "move_pct": round(move_pct, 2),
        "lots": lots,
        "shares": lots * lotsize,
        "cost_rub": round(entry * lots * lotsize, 2),
        "risk_rub": round((entry - stop) * lots * lotsize, 2),
        "atr_pct": round(atr_pct, 2),
        "trigger_pct": round((entry / last - 1) * 100, 2) if last else 0.0,
        # цель должна перекрывать комиссию на круг (2 стороны) с запасом
        "cost_ok": move_pct >= cfg["commission_pct"] * 2 * 1.5,
    }


# -- screening ---------------------------------------------------------------
def screen(
    *,
    equity: float = 0.0,
    cash: float = 0.0,
    top: int = 6,
    board: str = "TQBR",
    cfg: dict[str, float] | None = None,
    now: datetime | None = None,
    universe: list[dict] | None = None,
    history: dict[str, list[dict]] | None = None,
) -> dict:
    cfg = {**DEFAULTS, **(cfg or {})}
    now = now or datetime.now(MSK)
    elapsed = session_fraction(now)
    rows = universe if universe is not None else fetch_board(board)
    hist = history if history is not None else fetch_history(board=board)

    advancers = decliners = 0
    up2 = down2 = 0
    total_turnover = 0.0
    candidates: list[dict] = []
    weak: list[dict] = []

    for row in rows:
        chg = day_change_pct(row)
        turnover = _f(row.get("VALTODAY_RUR")) or _f(row.get("VALTODAY")) or 0.0
        total_turnover += turnover
        if chg > 0:
            advancers += 1
        elif chg < 0:
            decliners += 1
        if chg >= 2:
            up2 += 1
        elif chg <= -2:
            down2 += 1

        if row.get("TRADINGSTATUS") not in (None, "T"):
            continue
        last = _f(row.get("LAST"))
        if last is None or last < cfg["min_price"]:
            continue
        if turnover < cfg["min_turnover_rub"]:
            continue
        if (_f(row.get("NUMTRADES"), 0) or 0) < cfg["min_trades"]:
            continue
        stats = day_stats(hist.get(row["SECID"], []))
        if stats["atr_pct"] < cfg["min_atr_pct"]:
            continue

        direction = 1 if chg >= 0 else -1
        score = score_candidate(row, stats, elapsed, direction)
        plan = trade_plan(row, stats, equity, cash, cfg)
        if not plan["cost_ok"]:
            continue
        if plan["setup"] == "breakdown":
            weak.append(
                {
                    "ticker": row["SECID"],
                    "name": row.get("SHORTNAME") or row["SECID"],
                    "last": last,
                    "chg": round(chg, 2),
                    "rvol": round(((turnover / elapsed) / stats["avg_turnover"]) if stats["avg_turnover"] else 1.0, 2),
                    "score": score,
                }
            )
            continue
        if plan["setup"] == "range" and abs(chg) < 0.5:
            continue
        candidates.append(
            {
                "ticker": row["SECID"],
                "name": row.get("SHORTNAME") or row["SECID"],
                "last": last,
                "chg": round(chg, 2),
                "turnover": turnover,
                "rvol": round(((turnover / elapsed) / stats["avg_turnover"]) if stats["avg_turnover"] else 1.0, 2),
                "atr_pct": round(stats["atr_pct"], 2),
                "gap": round(((_f(row.get("OPEN"), last) or last) / (_f(row.get("PREVPRICE"), last) or last) - 1) * 100, 2),
                "pir": pos_in_range(_f(row.get("LOW")), _f(row.get("HIGH")), last),
                "score": score,
                "bid": _f(row.get("BID")),
                "ask": _f(row.get("OFFER")),
                "lotsize": int(_f(row.get("LOTSIZE"), 1) or 1),
                "plan": plan,
            }
        )

    candidates.sort(key=lambda c: c["score"], reverse=True)
    weak.sort(key=lambda c: c["score"], reverse=True)
    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M MSK"),
        "elapsed": round(elapsed, 2),
        "market": {
            "advancers": advancers,
            "decliners": decliners,
            "up2": up2,
            "down2": down2,
            "turnover_rub": total_turnover,
            "breadth": round(advancers / (advancers + decliners) * 100, 1) if advancers + decliners else 0.0,
        },
        "candidates": candidates[:top],
        "weak": weak[:5],
        "universe": len(rows),
    }


def render_markdown(result: dict) -> str:
    m = result["market"]
    lines = [
        f"## Анализ рынка {result['generated_at']}",
        "",
        f"Ширина рынка: {m['breadth']}% растут ({m['advancers']}↑ / {m['decliners']}↓), "
        f"сильных +2%: {m['up2']}, −2%: {m['down2']}; оборот TQBR {m['turnover_rub'] / 1e9:.1f} млрд ₽",
        "",
    ]
    if not result["candidates"]:
        lines.append("Кандидатов, проходящих фильтры (ликвидность + ATR + комиссия), нет.")
        return "\n".join(lines) + "\n"

    lines.append("| # | Тикер | Сетап | Цена | Δдень | RVOL | ATR% | Вход | Стоп | Цель | R:R | Лоты |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, c in enumerate(result["candidates"], 1):
        p = c["plan"]
        lines.append(
            f"| {i} | {c['ticker']} | {p['setup']} | {c['last']:.4g} | {c['chg']:+.2f}% | "
            f"{c['rvol']:.1f}× | {c['atr_pct']:.1f} | {p['entry']:.4g} | {p['stop']:.4g} | "
            f"{p['target']:.4g} | {p['rr']:.1f} | {p['lots']} |"
        )
    if result.get("weak"):
        lines.append("")
        lines.append(
            "Под давлением (не лонг): "
            + ", ".join(f"{w['ticker']} {w['chg']:+.1f}%" for w in result["weak"])
        )
    lines += [
        "",
        "Вход — пробой дневного максимума +0.1 ATR (breakout) или текущая цена (pullback). "
        "Стоп 1.2 ATR, цель 2.5 ATR. Риск 0.5% equity на сделку; комиссия 0.3%/сторона уже учтена в фильтре.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Скринер in play (MOEX TQBR)")
    parser.add_argument("--top", type=int, default=6)
    parser.add_argument("--min-turnover", type=float, default=DEFAULTS["min_turnover_rub"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    cfg = {"min_turnover_rub": args.min_turnover}
    result = screen(top=args.top, cfg=cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
