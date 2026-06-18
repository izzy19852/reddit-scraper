"""
capture_stub.py — record a live order-book + trade tape to JSONL.

Run this on YOUR VM (the one with exchange connectivity — a sandbox usually
can't reach exchanges). It connects to a venue's public websocket and writes one
JSON object per line in the exact format run_killtest.py / mm_core.load_tape
expect:

    {"type":"book", "ts":<sec>, "bid":..,"ask":..,"bid_size":..,"ask_size":..}
    {"type":"trade","ts":<sec>, "price":..,"size":..,"side":"buy"|"sell"}

`side` is the TAKER (aggressor) side: "buy" lifts the ask, "sell" hits the bid.

Default venue is Binance spot (public, no key needed). To test the large-cap
hypothesis honestly, capture a SPECTRUM — not just BTC. Grab a large-cap, a few
mid-liquidity names, and a couple of thin ones, ~2 hours each, into one folder,
then run run_killtest.py over it.

Examples
--------
    pip install -r requirements.txt
    python capture_stub.py --symbol btcusdt   --minutes 120 --out captures/BTCUSDT.jsonl
    python capture_stub.py --symbol arbusdt    --minutes 120 --out captures/ARBUSDT.jsonl
    python capture_stub.py --symbol some_thin   --minutes 120 --out captures/THIN.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time


BINANCE_WS = "wss://stream.binance.com:9443/stream?streams={streams}"


async def capture_binance(symbol: str, minutes: float, out_path: str,
                          flush_every: int = 200):
    """Capture bookTicker (best bid/ask + sizes) and trade streams from Binance."""
    import websockets  # imported here so --help works without the dep installed

    sym = symbol.lower()
    streams = f"{sym}@bookTicker/{sym}@trade"
    url = BINANCE_WS.format(streams=streams)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    deadline = time.time() + minutes * 60.0
    n = 0

    print(f"[capture] {symbol} -> {out_path}  for {minutes:.0f} min")
    print(f"[capture] {url}")

    with open(out_path, "w") as fh:
        async for ws in websockets.connect(url, ping_interval=15, ping_timeout=20):
            try:
                while time.time() < deadline:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    msg = json.loads(raw)
                    stream = msg.get("stream", "")
                    d = msg.get("data", {})
                    ts = time.time()

                    if stream.endswith("@bookTicker"):
                        rec = {
                            "type": "book", "ts": ts,
                            "bid": float(d["b"]), "ask": float(d["a"]),
                            "bid_size": float(d["B"]), "ask_size": float(d["A"]),
                        }
                    elif stream.endswith("@trade"):
                        # m == True  -> buyer is the maker -> taker SOLD -> "sell"
                        # m == False -> buyer is the taker -> taker BOUGHT -> "buy"
                        side = "sell" if d.get("m") else "buy"
                        rec = {
                            "type": "trade", "ts": ts,
                            "price": float(d["p"]), "size": float(d["q"]),
                            "side": side,
                        }
                    else:
                        continue

                    fh.write(json.dumps(rec) + "\n")
                    n += 1
                    if n % flush_every == 0:
                        fh.flush()
                break  # deadline reached
            except asyncio.TimeoutError:
                print("[capture] recv timeout; reconnecting…")
                continue
            except Exception as e:  # noqa: BLE001 — keep capturing across blips
                print(f"[capture] error: {e!r}; reconnecting…")
                if time.time() >= deadline:
                    break
                await asyncio.sleep(2)
                continue

    print(f"[capture] done: {n} records -> {out_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", required=True, help="e.g. btcusdt, arbusdt")
    ap.add_argument("--minutes", type=float, default=120.0, help="capture duration")
    ap.add_argument("--out", required=True, help="output .jsonl path")
    ap.add_argument("--venue", default="binance", choices=["binance"],
                    help="exchange (only binance wired up in this stub)")
    args = ap.parse_args()

    if args.venue == "binance":
        asyncio.run(capture_binance(args.symbol, args.minutes, args.out))


if __name__ == "__main__":
    main()
