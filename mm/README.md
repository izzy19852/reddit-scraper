# Realized-Spread Kill-Test

A capital-free way to decide whether a crypto pair is worth market-making, from
**recorded** order-book data alone. You never risk a dollar: you post passive
*shadow* quotes against a recorded tape and ask one question — after my quote
gets filled, which way does the price go?

That single question is **adverse selection**, and it's the whole game.

```
realized_spread = effective_half_spread − adverse_selection (+ maker_rebate)
```

- **Positive** → the spread overpays for the toxicity. There's an edge.
- **Negative** → the flow is too informed. Dead, no matter how wide the quote.

The quoted spread you see in the book means **nothing** on its own. A pair shows
a wide spread *because* it's thin/toxic; a tight spread *because* it's crowded
and fast. You can't get the wide spread without the risk. The MM's entire job is
finding pairs where the spread overpays for the toxicity — and that's an
empirical question you *measure*, not assume.

## Files

| file | role |
|------|------|
| `mm_core.py`       | shadow-quote simulator + markout (realized-spread) engine |
| `capture_stub.py`  | live websocket capture → JSONL (one pair) |
| `run_killtest.py`  | multi-pair verdict table (gross / adverse / realized / net@queue) |
| `run_live_test.py` | **one command**: capture real data → run kill-test → log findings |
| `validate_mm.py`   | proof the engine separates benign flow from toxic flow |

## Step 0 — install (needs Python 3.9+)

```bash
cd mm
python -m pip install -r requirements.txt
```

On Windows use `python`, on most Mac/Linux setups use `python3`. That's the only
substitution you'll make below.

## Step 1 — prove the tool works, offline (no internet, ~10 seconds)

```bash
python validate_mm.py                          # benign EDGE vs toxic DEAD
python run_killtest.py --demo --rebate 0.5     # see the verdict table on synthetic data
```

This is a unit test of the *measuring instrument*. It confirms the engine reads
adverse selection correctly when the answer is known. It says **nothing** about
whether any real pair is profitable — that's Step 2.

## Step 2 — the real test (one command, needs internet)

Defaults to **Coinbase** (public feed, no API key, US-accessible). Run this on
your laptop or a VM — anywhere that can reach the exchange. It captures a spectrum
of real pairs at once, runs the kill-test, and writes a findings log.

```bash
# Smoke test first — proves your capture works in ~3 minutes:
python run_live_test.py --minutes 3

# Then a real read (informed flow needs time to show up):
python run_live_test.py --minutes 120 \
    --symbols BTC-USD,ETH-USD,SOL-USD,ARB-USD,OP-USD,LINK-USD \
    --rebate 0.0 --horizon 5
```

Coinbase product ids are dash-form USD pairs (`BTC-USD`). Set `--rebate` to
**your venue's actual maker rebate in bps** (0 if none). The findings land in
`captures/findings_<timestamp>.md` — send me that file.

To use Binance instead (deeper books, but blocks US IPs), add
`--venue binance` and use `btcusdt`-style symbols.

### Side-label sanity check (important for Coinbase)

Coinbase reports the *maker's* side, not the aggressor's. The capture inverts it,
and the kill-test prints a **SIDE-LABEL SANITY** block: taker buys should print
at/above mid, sells at/below. If a pair shows `!! LIKELY INVERTED`, the taker side
is backwards (which would turn toxicity into a fake edge) — tell me and it's a
one-line fix. Healthy real data sits around 0.8+.

### Doing it by hand instead (if you prefer)

```bash
# Capture pairs one at a time…
python capture_stub.py --venue coinbase --symbol BTC-USD --minutes 120 --out captures/BTC-USD.jsonl
python capture_stub.py --venue coinbase --symbol ARB-USD --minutes 120 --out captures/ARB-USD.jsonl
# …then score the folder:
python run_killtest.py --dir captures --rebate 0.0 --horizon 5 --log captures/findings.txt
```

`net@q0.25` assumes a quarter of the displayed book trades **ahead** of you. A
pair only counts as a survivor if it stays positive *there*. Front-of-queue
(`net@q0.00`) numbers are how every retail MM backtest lies to itself.

## The large-cap prediction (so you can check the tool against reality)

**BTC/USDT will show a near-zero half-spread and heavy adverse selection — net
negative, exactly the trap.** That's the large-cap hypothesis being falsified by
data instead of by assertion. The interesting rows are the *mid-liquidity*
pairs, where the half-spread is wide enough that it *might* overpay for the
toxicity. If even those go negative at q0.25, the whole thing is dead for you — a
clean, cheap kill. If one or two survive, that's the first real signal you've
found a corner the machines haven't bothered with.

## Honest boundary (the engine's main blind spot)

Capturing the public tape **can't see your true queue position or model your own
cancels**. So an "edge" here is *necessary, not sufficient* — the next step after
a survivor is paper-quoting it live. But a **dead** result here is definitive: if
the spread can't beat adverse selection on the recorded tape, it certainly won't
with you standing in the slower real queue.
