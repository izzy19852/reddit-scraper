"""
mm_core.py — Shadow-quote simulator + markout (realized-spread) engine.

The whole market-making question reduces to one number:

    realized_spread = effective_half_spread - adverse_selection (+ maker_rebate)

You *capture* the effective half-spread every time a resting quote is filled.
You *pay* adverse selection when the mid keeps moving in the taker's direction
after the fill (you sold right before it went up, or bought right before it went
down). The quoted spread you see in the book tells you nothing on its own; what
the price does *after* you are filled tells you everything.

This module measures that from a *recorded* tape (order-book updates + trades),
without risking a cent. You post passive shadow quotes at the top of book, let
recorded market orders walk the queue, and mark every simulated fill out to a
fixed horizon.

Conventions
-----------
Taker side semantics (the aggressor):
    'buy'  = taker buy  = lifts the ASK = fills OUR resting ask  (we SELL, go short)
    'sell' = taker sell = hits the BID  = fills OUR resting bid  (we BUY,  go long)

Per fill, with mid m0 at fill time and mid mD at fill_time + horizon, and
s = +1 for our-sell (ask filled), s = -1 for our-buy (bid filled):

    effective_half = s * (price - m0)          # what we captured vs mid
    realized_half  = s * (price - mD)          # what survived the markout
    adverse        = s * (mD - m0)             # = effective_half - realized_half

All reported in basis points of m0.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import List, Dict, Optional


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class BookEvent:
    ts: float          # seconds (epoch or relative)
    bid: float
    ask: float
    bid_size: float
    ask_size: float

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)


@dataclass
class Trade:
    ts: float
    price: float
    size: float
    side: str          # 'buy' (taker buy) or 'sell' (taker sell)


@dataclass
class Fill:
    ts: float
    our_side: str      # 'sell' (ask filled) or 'buy' (bid filled)
    price: float
    size: float
    mid0: float
    midD: float
    eff_bps: float
    realized_bps: float
    adverse_bps: float


# --------------------------------------------------------------------------- #
# Markout helper
# --------------------------------------------------------------------------- #
class _MidCurve:
    """Step function: mid at or before a given time (last known book state)."""

    def __init__(self, book_events: List[BookEvent]):
        self._ts = [b.ts for b in book_events]
        self._mid = [b.mid for b in book_events]

    def at(self, t: float) -> Optional[float]:
        if not self._ts:
            return None
        idx = bisect.bisect_right(self._ts, t) - 1
        if idx < 0:
            idx = 0
        return self._mid[idx]


# --------------------------------------------------------------------------- #
# Quote-level queue state
# --------------------------------------------------------------------------- #
@dataclass
class _QuoteState:
    price: float
    q_ahead: float     # volume that must trade at this level before we fill


# --------------------------------------------------------------------------- #
# Simulator
# --------------------------------------------------------------------------- #
def simulate(
    book_events: List[BookEvent],
    trades: List[Trade],
    horizon_s: float,
    queue_frac: float,
    quote_size: float = 1.0,
) -> List[Fill]:
    """
    Walk the recorded tape with a passive shadow quote resting at top-of-book on
    both sides.

    queue_frac models our queue position as a fraction of the displayed size that
    must trade before us:
        0.0  -> front of queue (optimistic)
        0.25 -> a quarter of the book ahead of us (pessimistic / realistic for a
                slow retail maker)

    When the best price at a side changes we cancel-replace (rejoin the new level
    at `queue_frac` back). Size-only changes leave our queue position untouched
    (new size joins behind us).
    """
    if not book_events:
        return []

    mid_curve = _MidCurve(book_events)

    # Merge streams in time order; on ties, apply the book update before the trade
    # so a trade always sees the freshest book.
    merged = []
    for b in book_events:
        merged.append((b.ts, 0, b))
    for t in trades:
        merged.append((t.ts, 1, t))
    merged.sort(key=lambda x: (x[0], x[1]))

    cur: Optional[BookEvent] = None
    ask_q: Optional[_QuoteState] = None
    bid_q: Optional[_QuoteState] = None
    fills: List[Fill] = []

    for _, kind, ev in merged:
        if kind == 0:  # book update
            cur = ev
            # (Re)join ask side
            if ask_q is None or ask_q.price != cur.ask:
                ask_q = _QuoteState(cur.ask, queue_frac * cur.ask_size)
            # (Re)join bid side
            if bid_q is None or bid_q.price != cur.bid:
                bid_q = _QuoteState(cur.bid, queue_frac * cur.bid_size)
            continue

        # trade
        if cur is None:
            continue
        tr: Trade = ev

        if tr.side == "buy" and ask_q is not None and tr.price >= ask_q.price:
            # taker buy walks our ask queue
            ask_q.q_ahead -= tr.size
            if ask_q.q_ahead <= 0:
                fills.append(_mark(tr.ts, "sell", ask_q.price, quote_size,
                                   cur.mid, mid_curve, horizon_s))
                # rejoin behind the freshly cleared level
                ask_q = _QuoteState(cur.ask, queue_frac * cur.ask_size)

        elif tr.side == "sell" and bid_q is not None and tr.price <= bid_q.price:
            # taker sell walks our bid queue
            bid_q.q_ahead -= tr.size
            if bid_q.q_ahead <= 0:
                fills.append(_mark(tr.ts, "buy", bid_q.price, quote_size,
                                   cur.mid, mid_curve, horizon_s))
                bid_q = _QuoteState(cur.bid, queue_frac * cur.bid_size)

    return fills


def _mark(ts, our_side, price, size, mid0, mid_curve: _MidCurve, horizon_s) -> Fill:
    midD = mid_curve.at(ts + horizon_s)
    if midD is None:
        midD = mid0
    s = 1.0 if our_side == "sell" else -1.0
    eff = s * (price - mid0) / mid0 * 1e4
    realized = s * (price - midD) / mid0 * 1e4
    adverse = eff - realized
    return Fill(ts, our_side, price, size, mid0, midD, eff, realized, adverse)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def summarize(fills: List[Fill], rebate_bps: float = 0.0,
              fee_bps: float = 0.0) -> Dict[str, float]:
    """Volume-weighted aggregate of a fill list, plus net edge after fees/rebate."""
    n = len(fills)
    if n == 0:
        return {
            "n_fills": 0, "volume": 0.0,
            "eff_half_bps": float("nan"),
            "adverse_bps": float("nan"),
            "realized_bps": float("nan"),
            "net_bps": float("nan"),
        }
    vol = sum(f.size for f in fills)
    w = lambda key: sum(getattr(f, key) * f.size for f in fills) / vol
    eff = w("eff_bps")
    adv = w("adverse_bps")
    realized = w("realized_bps")
    net = realized + rebate_bps - fee_bps
    return {
        "n_fills": n,
        "volume": vol,
        "eff_half_bps": eff,
        "adverse_bps": adv,
        "realized_bps": realized,
        "net_bps": net,
    }


# --------------------------------------------------------------------------- #
# Tape loading (JSONL produced by capture_stub.py)
# --------------------------------------------------------------------------- #
def load_tape(path: str):
    """Load a JSONL capture into (book_events, trades).

    Each line is one JSON object with a "type" of "book" or "trade".
    """
    import json

    book_events: List[BookEvent] = []
    trades: List[Trade] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            t = rec.get("type")
            if t == "book":
                book_events.append(BookEvent(
                    ts=float(rec["ts"]),
                    bid=float(rec["bid"]), ask=float(rec["ask"]),
                    bid_size=float(rec["bid_size"]),
                    ask_size=float(rec["ask_size"]),
                ))
            elif t == "trade":
                trades.append(Trade(
                    ts=float(rec["ts"]),
                    price=float(rec["price"]),
                    size=float(rec["size"]),
                    side=str(rec["side"]),
                ))
    book_events.sort(key=lambda b: b.ts)
    trades.sort(key=lambda t: t.ts)
    return book_events, trades
