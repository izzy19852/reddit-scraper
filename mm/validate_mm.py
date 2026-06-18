"""
validate_mm.py — the kill-test's own kill-test.

Before trusting the engine on real captures, prove it can tell *benign* flow
(uninformed order flow — where market-making prints the quoted spread) from
*toxic* flow (informed order flow — where you bleed despite an identical quoted
spread). We generate both with known properties and confirm the engine:

  * sees the SAME quoted half-spread in both cases, and
  * separates them entirely via the ADVERSE-SELECTION column, flipping the net
    edge from positive (EDGE) to negative (DEAD).

Run:  python validate_mm.py
"""

from __future__ import annotations

import numpy as np

from mm_core import BookEvent, Trade, simulate, summarize


# --------------------------------------------------------------------------- #
# Synthetic tape generator
# --------------------------------------------------------------------------- #
def make_tape(n_steps=20000, mid0=20000.0, half_spread_bps=2.0,
              sigma=0.6, trade_prob=0.5, toxicity=0.0, horizon_steps=5,
              top_size=100.0, trade_size=8.0, informed_frac=1.0, seed=0):
    """
    Generate a recorded tape.

    toxicity > 0 makes a fraction (`informed_frac`) of trades *informed*: after an
    informed taker buy the mid drifts UP and after an informed taker sell it drifts
    DOWN over the next `horizon_steps` (so a maker who just sold/bought is
    immediately on the wrong side). Larger orders are more likely informed, which
    is why queue position bites in reality: at the front you fill on small
    uninformed orders (cheap), at the back you wait through them and only get caught
    by the big informed sweep (toxic). toxicity == 0 is a fully benign world: side
    is independent of subsequent mid moves.
    """
    rng = np.random.default_rng(seed)
    half = mid0 * half_spread_bps / 1e4

    book_events, trades = [], []
    mid = mid0
    dt = 1.0
    # Active informed-drift contributions: list of [steps_remaining, direction]
    active = []

    for i in range(n_steps):
        ts = i * dt
        bid = mid - half
        ask = mid + half
        book_events.append(BookEvent(ts, bid, ask, top_size, top_size))

        if rng.random() < trade_prob:
            is_buy = rng.random() < 0.5
            side = "buy" if is_buy else "sell"
            price = ask if is_buy else bid
            # Continuous size distribution; larger orders are more likely informed
            # (and push the mid harder) — this is what makes queue position bite.
            size = rng.exponential(trade_size)
            p_inf = informed_frac * size / (size + trade_size) if toxicity > 0 else 0.0
            informed = rng.random() < p_inf
            trades.append(Trade(ts + 0.25, price, size, side))
            if informed:
                mag = min(size / trade_size, 3.0)
                active.append([horizon_steps, (1.0 if is_buy else -1.0) * mag])

        # Advance mid: random walk + sum of active informed drifts
        drift = 0.0
        if active:
            drift = toxicity * half * sum(d for _, d in active)
            for c in active:
                c[0] -= 1
            active = [c for c in active if c[0] > 0]
        mid = mid + sigma * rng.standard_normal() + drift

    return book_events, trades


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _verdict(net):
    return "EDGE" if net > 0 else "DEAD"


def _report(label, summary, rebate):
    print(f"\n{label}")
    print(f"  fills            : {summary['n_fills']}")
    print(f"  eff half-spread  : {summary['eff_half_bps']:+.3f} bps")
    print(f"  adverse selection: {summary['adverse_bps']:+.3f} bps")
    print(f"  realized spread  : {summary['realized_bps']:+.3f} bps")
    print(f"  + maker rebate   : {rebate:+.3f} bps")
    print(f"  NET EDGE         : {summary['net_bps']:+.3f} bps  -> {_verdict(summary['net_bps'])}")


def main():
    rebate = 0.5          # bps maker rebate
    horizon_s = 5.0       # markout horizon (matches horizon_steps below)
    queue_frac = 0.0      # front-of-queue for a clean, high-sample validation

    # Identical quoted spread (2.0 bps half) in both worlds; only toxicity differs.
    benign_b, benign_t = make_tape(toxicity=0.0, horizon_steps=5, seed=1)
    toxic_b, toxic_t = make_tape(toxicity=0.95, horizon_steps=5, seed=1)

    benign = summarize(simulate(benign_b, benign_t, horizon_s, queue_frac), rebate)
    toxic = summarize(simulate(toxic_b, toxic_t, horizon_s, queue_frac), rebate)

    print("=" * 64)
    print("ENGINE VALIDATION — benign vs toxic flow, identical quoted spread")
    print("=" * 64)
    _report("BENIGN  (uninformed flow)", benign, rebate)
    _report("TOXIC   (informed flow)", toxic, rebate)
    print("\n" + "-" * 64)

    # --- Assertions: the engine must separate the two worlds ---------------- #
    # 1. Same quoted/effective half-spread (the book looks identical).
    assert abs(benign["eff_half_bps"] - toxic["eff_half_bps"]) < 0.25, (
        "effective half-spread should match across benign/toxic")

    # 2. Benign flow has ~zero adverse selection; toxic flow has large positive.
    assert abs(benign["adverse_bps"]) < 0.5, (
        f"benign adverse selection should be ~0, got {benign['adverse_bps']:.3f}")
    assert toxic["adverse_bps"] > 2.0, (
        f"toxic adverse selection should be large+, got {toxic['adverse_bps']:.3f}")

    # 3. The verdict flips: benign is an EDGE, toxic is DEAD.
    assert bool(benign["net_bps"] > 0), "benign flow should net positive (EDGE)"
    assert bool(toxic["net_bps"] < 0), "toxic flow should net negative (DEAD)"

    print("PASS — engine separates benign flow (EDGE) from toxic flow (DEAD)")
    print("       via adverse selection, at an identical quoted spread.")
    print("-" * 64)


if __name__ == "__main__":
    main()
