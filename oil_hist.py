#!/usr/bin/env python3
"""Недельная хронология движения нефтяных фьючерсов (BRZ6, NGZ6) с точками входа."""
from finam_client import FinamClient, num


def main() -> None:
    c = FinamClient()
    for sym in ("BRZ6@RTSX", "NGZ6@RTSX"):
        bars = c.bars(sym, timeframe="TIME_FRAME_D",
                      start="2026-10-25T00:00:00Z", end="2026-11-26T00:00:00Z")
        print(f"== {sym} (последние 10 дней)")
        for b in (bars or [])[-10:]:
            d = (b.get("begin") or "")[:10]
            o, h, l, cl = (num(b.get(k)) for k in ("open", "high", "low", "close"))
            v = num(b.get("volume"))
            if not cl:
                continue
            print(f"   {d}  O {o:.4f} H {h:.4f} L {l:.4f} C {cl:.4f}  V {v:,.0f}")


if __name__ == "__main__":
    main()
