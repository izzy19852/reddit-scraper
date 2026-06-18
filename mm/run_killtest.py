"""
run_killtest.py — multi-pair verdict runner.

Point it at a directory of recorded captures (one JSONL per pair, produced by
capture_stub.py) and it prints, per pair:

    gross half-spread | adverse selection | realized spread | net edge @ queue pos

It evaluates net edge at several queue positions. The pessimistic column
(`net@q0.25`) assumes a quarter of the displayed book trades ahead of you — a
pair only counts as a survivor if it stays positive *there*. Front-of-queue
(q0.00) numbers are how every retail MM backtest lies to itself.

Usage
-----
    # Real captures:
    python run_killtest.py --dir captures --rebate 0.0 --horizon 5

    # No capture yet? Generate a synthetic spectrum and see the table shape:
    python run_killtest.py --demo --rebate 0.5
"""

from __future__ import annotations

import argparse
import glob
import os
import tempfile

from mm_core import load_tape, simulate, summarize


QUEUE_POSITIONS = [0.0, 0.10, 0.25]


def _verdict(net):
    return "EDGE" if net > 0 else "DEAD"


def run_pair(name, book_events, trades, horizon, rebate, fee):
    base = summarize(simulate(book_events, trades, horizon, queue_frac=0.0), rebate, fee)
    nets = {}
    last = base
    for q in QUEUE_POSITIONS:
        s = summarize(simulate(book_events, trades, horizon, queue_frac=q), rebate, fee)
        nets[q] = s
        last = s
    return base, nets


def print_table(rows, rebate, fee, horizon):
    print()
    print(f"REALIZED-SPREAD KILL-TEST   (horizon={horizon}s  rebate={rebate:+.2f}bps  fee={fee:.2f}bps)")
    print("=" * 100)
    hdr = (f"{'pair':<14}{'fills':>7}{'gross½':>9}{'adverse':>9}{'realized':>10}"
           f"{'net@q0.00':>11}{'net@q0.10':>11}{'net@q0.25':>11}  verdict")
    print(hdr)
    print("-" * 100)
    survivors = []
    for name, base, nets in rows:
        if base["n_fills"] == 0:
            print(f"{name:<14}{'0':>7}   (no fills — empty or too-short capture)")
            continue
        q25 = nets[0.25]
        verdict = _verdict(q25["net_bps"])
        if verdict == "EDGE":
            survivors.append(name)
        print(f"{name:<14}{base['n_fills']:>7}"
              f"{base['eff_half_bps']:>9.3f}{base['adverse_bps']:>9.3f}"
              f"{base['realized_bps']:>10.3f}"
              f"{nets[0.0]['net_bps']:>11.3f}{nets[0.10]['net_bps']:>11.3f}"
              f"{nets[0.25]['net_bps']:>11.3f}  {verdict}")
    print("-" * 100)
    if survivors:
        print(f"SURVIVORS @ q0.25 (back of queue): {', '.join(survivors)}")
        print("  -> necessary, not sufficient. Next step is paper-quoting live; the public")
        print("     tape can't see your true queue position or model your own cancels.")
    else:
        print("NO SURVIVORS @ q0.25 — a clean, capital-free kill. If the spread can't beat")
        print("  adverse selection on the recorded tape, it won't from the slower real queue.")
    print("=" * 100)


# --------------------------------------------------------------------------- #
# Demo: generate a synthetic spectrum (large-cap trap -> mid-liquidity survivor)
# --------------------------------------------------------------------------- #
def _make_demo_captures(dirpath):
    """A spectrum: tight+toxic large-cap, two mid-liquidity, one thin/junk."""
    import json
    from validate_mm import make_tape

    specs = [
        # name,      half_bps, toxicity, sigma, trade_prob, informed_frac, seed
        ("BTCUSDT",   0.4,      0.9,      0.8,   0.7,        0.35,          11),  # large-cap trap
        ("MIDCAP_A",  3.5,      0.5,      0.5,   0.4,        0.12,          12),  # survives q0 dies q0.25
        ("MIDCAP_B",  2.6,      0.7,      0.5,   0.4,        0.20,          13),  # borderline
        ("THINJUNK",  8.0,      1.6,      0.4,   0.2,        0.50,          14),  # wide but toxic
    ]
    paths = []
    for name, half_bps, tox, sigma, tp, ifrac, seed in specs:
        be, tr = make_tape(n_steps=8000, half_spread_bps=half_bps, toxicity=tox,
                           sigma=sigma, trade_prob=tp, horizon_steps=5,
                           informed_frac=ifrac, seed=seed)
        path = os.path.join(dirpath, f"{name}.jsonl")
        with open(path, "w") as fh:
            for b in be:
                fh.write(json.dumps({"type": "book", "ts": b.ts, "bid": b.bid,
                                     "ask": b.ask, "bid_size": b.bid_size,
                                     "ask_size": b.ask_size}) + "\n")
            for t in tr:
                fh.write(json.dumps({"type": "trade", "ts": t.ts, "price": t.price,
                                     "size": t.size, "side": t.side}) + "\n")
        paths.append(path)
    return paths


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help="directory of *.jsonl captures (one per pair)")
    ap.add_argument("--horizon", type=float, default=5.0, help="markout horizon (s)")
    ap.add_argument("--rebate", type=float, default=0.0, help="maker rebate (bps)")
    ap.add_argument("--fee", type=float, default=0.0, help="maker fee (bps), if any")
    ap.add_argument("--demo", action="store_true",
                    help="generate a synthetic spectrum and run on it (no network)")
    args = ap.parse_args()

    tmp = None
    if args.demo:
        tmp = tempfile.mkdtemp(prefix="mm_demo_")
        _make_demo_captures(tmp)
        directory = tmp
        print(f"[demo] synthetic captures written to {directory}")
    else:
        if not args.dir:
            ap.error("provide --dir <captures> or --demo")
        directory = args.dir

    files = sorted(glob.glob(os.path.join(directory, "*.jsonl")))
    if not files:
        ap.error(f"no *.jsonl captures found in {directory}")

    rows = []
    for path in files:
        name = os.path.splitext(os.path.basename(path))[0]
        book_events, trades = load_tape(path)
        base, nets = run_pair(name, book_events, trades, args.horizon,
                              args.rebate, args.fee)
        rows.append((name, base, nets))

    print_table(rows, args.rebate, args.fee, args.horizon)


if __name__ == "__main__":
    main()
