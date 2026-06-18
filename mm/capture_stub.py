"""
capture_stub.py — record a live order-book + trade tape to JSONL.

Run this on YOUR VM (the one with exchange connectivity — a sandbox usually
can't reach exchanges). It connects to a venue's public websocket and writes one
JSON object per line in the exact format run_killtest.py / mm_core.load_tape
expect:

    {"type":"book", "ts":<sec>, "bid":..,"ask":..,"bid_size":..,"ask_size":..}
    {"type":"trade","ts":<sec>, "price":..,"size":..,"side":"buy"|"sell"}

`side` is the TAKER (aggressor) side: "buy" lifts the ask, "sell" hits the bid.

Default venue is Coinbase (public, no key needed; US-accessible). Binance is also
wired up (deeper books, but blocks US IPs). To test the large-cap hypothesis
honestly, capture a SPECTRUM — not just BTC. Grab a large-cap, a few mid-liquidity
names, and a couple of thin ones, ~2 hours each, into one folder, then run
run_killtest.py over it.

Symbols: Coinbase uses dash-form USD pairs (BTC-USD); Binance uses btcusdt.

Examples
--------
    pip install -r requirements.txt
    python capture_stub.py --venue coinbase --symbol BTC-USD --minutes 120 --out captures/BTC-USD.jsonl
    python capture_stub.py --venue coinbase --symbol ARB-USD --minutes 120 --out captures/ARB-USD.jsonl
    python capture_stub.py --venue binance  --symbol btcusdt --minutes 120 --out captures/BTCUSDT.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time


BINANCE_WS = "wss://stream.binance.com:9443/stream?streams={streams}"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"


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


async def capture_coinbase(symbol: str, minutes: float, out_path: str,
                           flush_every: int = 200):
    """Capture top-of-book + trades from Coinbase Exchange (public, no key).

    Uses the `ticker` channel, which is unauthenticated and emits — on every
    trade — both the current best bid/ask (+sizes) and the trade itself. Product
    ids are dash-form USD pairs, e.g. BTC-USD, ETH-USD, SOL-USD.

    IMPORTANT — taker side. Coinbase's `side` field is the MAKER's side, not the
    aggressor's. Our engine wants the taker (aggressor) side, so we invert it:
        maker side "sell" -> a resting ask was lifted -> taker BUY
        maker side "buy"  -> a resting bid was hit    -> taker SELL
    run_killtest prints a side-label sanity check so you can confirm this came out
    right on real data (taker buys should print at/above mid).
    """
    import websockets  # imported here so --help works without the dep installed

    product = symbol.upper()
    sub = json.dumps({"type": "subscribe",
                      "product_ids": [product], "channels": ["ticker"]})

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    deadline = time.time() + minutes * 60.0
    n = 0
    last_trade_id = None  # baseline set from the initial snapshot; skip phantom trade

    print(f"[capture] {product} -> {out_path}  for {minutes:.0f} min")
    print(f"[capture] {COINBASE_WS}  (ticker channel)")

    with open(out_path, "w") as fh:
        async for ws in websockets.connect(COINBASE_WS, ping_interval=15,
                                           ping_timeout=20):
            try:
                await ws.send(sub)
                while time.time() < deadline:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    msg = json.loads(raw)
                    if msg.get("type") != "ticker":
                        continue
                    ts = time.time()

                    try:
                        bid = float(msg["best_bid"])
                        ask = float(msg["best_ask"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    # sizes were added later; fall back to a nominal 1.0 if absent
                    bid_size = float(msg.get("best_bid_size") or 1.0)
                    ask_size = float(msg.get("best_ask_size") or 1.0)
                    fh.write(json.dumps({
                        "type": "book", "ts": ts, "bid": bid, "ask": ask,
                        "bid_size": bid_size, "ask_size": ask_size}) + "\n")
                    n += 1

                    tid = msg.get("trade_id")
                    if tid is not None and tid != last_trade_id:
                        if last_trade_id is not None:  # not the opening snapshot
                            taker = "buy" if msg.get("side") == "sell" else "sell"
                            size = float(msg.get("last_size") or 0.0)
                            if size > 0:
                                fh.write(json.dumps({
                                    "type": "trade", "ts": ts,
                                    "price": float(msg["price"]), "size": size,
                                    "side": taker}) + "\n")
                                n += 1
                        last_trade_id = tid

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


def capture(venue: str, symbol: str, minutes: float, out_path: str):
    """Return the capture coroutine for the chosen venue."""
    if venue == "binance":
        return capture_binance(symbol, minutes, out_path)
    if venue == "coinbase":
        return capture_coinbase(symbol, minutes, out_path)
    raise ValueError(f"unknown venue: {venue}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", required=True,
                    help="binance: btcusdt | coinbase: BTC-USD")
    ap.add_argument("--minutes", type=float, default=120.0, help="capture duration")
    ap.add_argument("--out", required=True, help="output .jsonl path")
    ap.add_argument("--venue", default="coinbase", choices=["coinbase", "binance"],
                    help="exchange to capture from")
    args = ap.parse_args()

    asyncio.run(capture(args.venue, args.symbol, args.minutes, args.out))


if __name__ == "__main__":
    main()
