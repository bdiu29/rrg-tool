# Confluence — Shared Factor Library

[← Back to main README](../../README.md)

A **routeless** shared library at the *bottom* of the project's dependency order. Unlike every other module, Confluence has no page and no routes — it's a collection of **pure leaves** (numpy/pandas only, no I/O, no imports of anything above it) that other modules fold into a decision. Each factor reads a symbol's own price (and where relevant volume) and emits a **signed contribution**: the [RRG](../rrg/README.md) conviction engine sums them into its rotation-call score; the [Flow](../flow/README.md) tool reads them as Rule-6 context; the [Screener](../screener/README.md) and [CANSLIM](../canslim/README.md) modules read their vectorized panels.

This generalizes the original `flags` / `exhaustion` pattern (which used to live in `rrg/`) into one organized, extensible home.

---

## The factor contract

Every leaf exposes the same shape:

```python
read = current(...)                  # a pure head-bar read (dict of the latest state)
amount, label = contribution(read)   # a signed value in ≈[-1, 1] + a human label
panels(...)                          # (optional) the vectorized full-history version
```

The consumer scales the signed `amount` by a **theory-fixed weight** and adds it up. `__init__.py` keeps a `FACTORS` registry so consumers can discover/iterate the leaves. **Adding a factor = drop a `<name>.py` + register it.**

A leaf has **no I/O**: the fetch/orchestration that feeds it prices stays in the *consumer* (e.g. `rrg.signal.exhaustion_for` / `volume_profile_for` / `accumulation_for` use the RRG's cached OHLC fetch), so the leaf stays pure and cycle-free.

---

## The leaves

| Leaf | What it measures | Signed read | Consumed by |
|---|---|---|---|
| **`flags.py`** | Bull / bear flag continuation patterns | bull + / bear − | RRG conviction, Screener field, flag backtest |
| **`exhaustion.py`** | A one-bar volume *climax* into a new high/low closing weak/strong | selling climax + / buying climax − | RRG conviction, Screener field |
| **`volume_profile.py`** | Volume-at-price over a trailing window → POC / VAH / VAL / HVN / LVN + a D/P/b/B/trend shape | discount + bottoming `b` = bullish; premium + topping `P` = bearish | RRG conviction, Flow Rule-6 |
| **`accumulation.py`** | U/D volume ratio + Chaikin A/D divergence + accum/distrib days → an **A–E** rating (the institutional buy/sell footprint) | A strong accumulation + … E heavy distribution − | RRG conviction, Screener `ad_rating`, Flow Rule-6, CANSLIM (S) |
| **`institutional.py`** | Institutional %-held + holder-count QoQ trend → a sponsorship score (reads *ownership numbers*, not OHLCV) | funds adding + / distributing − | CANSLIM (I) only — meaningless for ETFs, so **not** wired into RRG |
| **`wave.py`** | The cohesive **wave engine** on the RS line — significant-swing ZigZag, golden-pocket vs shallow retrace depth, wave-3/5 Fibonacci extensions, the ABC corrective family, dual RSI divergence | (drives the RRG calls, not a single scalar) | RRG (`signal` is the consumer) |

### `flags` & `exhaustion` — moved, not changed

These two leaves were **moved here verbatim from `rrg/`** so there's a single source of truth shared by the RRG conviction engine, the Screener's vectorized panels (`flag` / `exhaustion` fields), and the standalone flag backtest. Only the import path changed (`modules.confluence.flags` / `exhaustion`).

### `volume_profile` — value area as a signed factor

Volume-at-price over a trailing window (each bar's volume spread across its high–low into bins — the standard bar-based approximation, no tick tape needed). It derives the **POC** (point of control), the **VAH/VAL** 70% value-area band (below VAL = discount, above VAH = premium), HVN/LVN nodes, and a **shape** (`D` balanced / `P` fat top / `b` fat bottom / `B` double distribution / `trend` flat). `contribution()` grades to `[−1, 1]` — a discount price under a bottoming `b` profile is bullish; a premium price under a topping `P` is bearish.

### `accumulation` — the standing institutional footprint

The accumulation/distribution read that `volume_profile` (price *location*) and `exhaustion` (one-bar *climax*) don't capture. Three tells over a trailing window, blended: the **U/D volume ratio** (Σ up-close-day volume ÷ Σ down-close-day volume — IBD's metric; >1 accumulation), the **Chaikin A/D line** direction vs price (a divergence = selling into strength, or its mirror), and **accumulation/distribution days** (a close in the top/bottom of range on heavy volume). Output is graded to an **A–E rating**.

### `institutional` — CANSLIM's "I"

The odd one out: it reads **ownership numbers** (institutional %-held + holder count, fetched by the consumer via yfinance), not OHLCV, so the leaf takes those numbers as inputs and stays pure. The signal is mostly the **quarter-over-quarter change** (funds adding vs distributing) with a small level band. It's **forward-accumulating** — the QoQ delta only emerges once the store holds two dated reads. Consumed by CANSLIM only.

### `wave` — the RRG's wave engine

Unlike the single-factor leaves, the wave engine's pieces share **one no-lookahead ZigZag pass**, so they travel together (it is *not* in the `FACTORS` registry). It was extracted **verbatim** from `rrg.signal`; `rrg.signal` remains the consumer (it keeps the price-fetch orchestration, the multi-timeframe blend, the `_conviction` combiner, and `_rotation_call`). See the [RRG README](../rrg/README.md) for how the wave structure becomes a rotation call.

---

## No lookahead, pinned by a golden master

The wave-engine extraction is guarded by **`tests/test_rrg_golden.py`**, which pins the full `compute_rrg` output (both intervals, including the MTF blend) on a deterministic synthetic panel to a committed fixture and asserts byte-identical output. A pure move/refactor must not change a value; regenerate the fixture only when behavior is *meant* to change.

---

## Files

| File | Role |
|---|---|
| `__init__.py` | the `FACTORS` registry + the `current()` → `contribution()` contract |
| `flags.py` | bull/bear flag detection core (moved from `rrg/`) |
| `exhaustion.py` | volume buyer/seller exhaustion (moved from `rrg/`) |
| `volume_profile.py` | volume-at-price → POC/VAH/VAL/shape + signed contribution |
| `accumulation.py` | U/D volume + A/D divergence → A–E accumulation rating |
| `institutional.py` | ownership %-held + holder-count trend → sponsorship score |
| `wave.py` | the ZigZag/Elliott/Fib/RSI-divergence/ABC wave engine on the RS line |

Unit tests: [`tests/test_confluence_volume_profile.py`](../../tests/test_confluence_volume_profile.py), [`tests/test_confluence_accumulation.py`](../../tests/test_confluence_accumulation.py), [`tests/test_confluence_institutional.py`](../../tests/test_confluence_institutional.py), and the wave engine via [`tests/test_rrg_golden.py`](../../tests/test_rrg_golden.py) / [`tests/test_rrg_flags.py`](../../tests/test_rrg_flags.py).

> Educational only. These factors are *inputs* to a decision, not signals on their own — they're meant to be combined and confirmed with price trend.
