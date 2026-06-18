"""
run_live_test.py — one command: capture REAL data, then run the kill-test and
log the findings.

This is the real test. It must run on a machine with internet that can reach the
exchange (your laptop or a VM) — NOT inside a walled sandbox. It:

  1. Connects to the venue's public websockets (default: Coinbase) and records a
     spectrum of pairs CONCURRENTLY for the same window (large-cap + mid + thin).
  2. Runs the realized-spread kill-test on what it recorded, including a
     side-label sanity check (Coinbase reports the maker side, which we invert).
  3. Writes a timestamped findings report (the verdict table + run metadata) so
     you have a durable log of what the data said.

Quick smoke test (prove the plumbing in ~3 minutes):
    python run_live_test.py --minutes 3

A real read (recommended — informed flow needs time to show up):
    python run_live_test.py --minutes 120 \
        --symbols btcusdt,ethusdt,solusdt,arbusdt,opusdt,linkusdt \
        --rebate 0.0 --horizon 5

Findings land in:  captures/findings_<UTC timestamp>.md
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os

from capture_stub import capture
from run_killtest import evaluate_dir, format_table


# A default spectrum: do NOT pre-pick large-cap. One mega-cap, some mid-liquidity,
# a couple of thinner names — let the data sort them. (Coinbase dash-form USD pairs.)
DEFAULT_SYMBOLS = "BTC-USD,ETH-USD,SOL-USD,ARB-USD,OP-USD,LINK-USD"


async def _capture_all(venue, symbols, minutes, outdir):
    os.makedirs(outdir, exist_ok=True)
    tasks = []
    for s in symbols:
        out = os.path.join(outdir, f"{s.upper()}.jsonl")
        tasks.append(capture(venue, s, minutes, out))
    await asyncio.gather(*tasks)


def _capture_summary(outdir, symbols):
    lines = []
    for s in symbols:
        path = os.path.join(outdir, f"{s.upper()}.jsonl")
        if os.path.exists(path):
            with open(path) as fh:
                n = sum(1 for _ in fh)
            kb = os.path.getsize(path) / 1024
            lines.append(f"  {s.upper():<12} {n:>8} records  ({kb:,.0f} KB)")
        else:
            lines.append(f"  {s.upper():<12} (no file written)")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venue", default="coinbase", choices=["coinbase", "binance"],
                    help="exchange to capture from (default: coinbase)")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated symbols (default: a spectrum for the venue)")
    ap.add_argument("--minutes", type=float, default=120.0,
                    help="capture window per pair (concurrent). Try 3 for a smoke test.")
    ap.add_argument("--dir", default="captures", help="output directory")
    ap.add_argument("--rebate", type=float, default=0.0, help="your maker rebate (bps)")
    ap.add_argument("--fee", type=float, default=0.0, help="your maker fee (bps), if any")
    ap.add_argument("--horizon", type=float, default=5.0, help="markout horizon (s)")
    args = ap.parse_args()

    default_syms = (DEFAULT_SYMBOLS if args.venue == "coinbase"
                    else "btcusdt,ethusdt,solusdt,arbusdt,opusdt,linkusdt")
    raw_syms = args.symbols if args.symbols else default_syms
    symbols = [s.strip() for s in raw_syms.split(",") if s.strip()]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print("=" * 72)
    print("LIVE REALIZED-SPREAD KILL-TEST")
    print("=" * 72)
    print(f"venue    : {args.venue} (public)")
    print(f"symbols  : {', '.join(s.upper() for s in symbols)}")
    print(f"window   : {args.minutes:.0f} min (concurrent)")
    print(f"rebate   : {args.rebate:+.2f} bps   horizon: {args.horizon:.0f}s")
    print("=" * 72)
    print("Capturing… (Ctrl-C stops early; whatever was recorded is still usable)\n")

    try:
        asyncio.run(_capture_all(args.venue, symbols, args.minutes, args.dir))
    except KeyboardInterrupt:
        print("\n[interrupted] analyzing what was captured so far…")

    rows = evaluate_dir(args.dir, args.horizon, args.rebate, args.fee)
    table = format_table(rows, args.rebate, args.fee, args.horizon)
    print(table)

    # --- durable findings log ---------------------------------------------- #
    report_path = os.path.join(args.dir, f"findings_{stamp}.md")
    header = [
        f"# Realized-spread kill-test findings — {stamp}",
        "",
        f"- venue: {args.venue} (public)",
        f"- symbols: {', '.join(s.upper() for s in symbols)}",
        f"- window: {args.minutes:.0f} min per pair (concurrent)",
        f"- rebate: {args.rebate:+.2f} bps   fee: {args.fee:.2f} bps   horizon: {args.horizon:.0f}s",
        "",
        "## Capture volume",
        "",
        "```",
        _capture_summary(args.dir, symbols),
        "```",
        "",
        "## Verdict table",
        "",
        "```",
        table,
        "```",
        "",
        "## How to read this",
        "",
        "- `gross½`  = half-spread you captured at fill (before markout).",
        "- `adverse` = how far the mid moved against you after the fill. THE signal.",
        "- `realized` = gross − adverse. What actually survives.",
        "- `net@qX`  = realized + rebate − fee, at queue position X "
        "(fraction of the book ahead of you).",
        "",
        "A pair only counts as a survivor if `net@q0.25` (back of queue) is positive. "
        "Even then it is *necessary, not sufficient* — the public tape can't see your "
        "true queue position. A NEGATIVE result is definitive; a POSITIVE one means "
        "\"worth paper-trading next,\" not a green light.",
        "",
        "Check the SIDE-LABEL SANITY block first. If a pair reads LIKELY INVERTED, its "
        "verdict is meaningless until the taker side is fixed — Coinbase reports the "
        "maker side and we invert it, so this is the thing to confirm on real data.",
        "",
        "Watch for thin-sample noise: a pair with very few `fills` (say < ~500) is not "
        "a verdict, it's a coin flip. Capture longer.",
    ]
    with open(report_path, "w") as fh:
        fh.write("\n".join(header) + "\n")

    print(f"\n[findings] written to {report_path}")
    print("[findings] send me that file (or paste the table) and I'll help you read it.")


if __name__ == "__main__":
    main()
