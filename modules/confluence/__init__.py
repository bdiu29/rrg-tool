"""
Confluence factor library — the shared, pure-leaf layer at the BOTTOM of the
dependency order.

Each factor is a self-contained script that reads a symbol's own price (and, where
relevant, volume) and emits a signal that other modules fold into a decision:

  * the RRG conviction engine (`rrg.signal._conviction`) sums these factors into a
    signed score that drives the rotation call;
  * the options-flow scoring engine reads them as Rule-6 structure/discount context.

Factors here are PURE (numpy/pandas only, no I/O, no upward imports) so the layer
can sit beneath every module without a cycle — the fetch/orchestration that feeds
them prices lives in the *consumer* (e.g. `signal.exhaustion_for` / the flow poller),
exactly as the original `flags`/`exhaustion` leaves were used.

Current factors:
  * `flags`          — bull/bear flag continuation (price+volume)
  * `exhaustion`     — buyer/seller volume climax (topping/bottoming)
  * `volume_profile` — volume-at-price: POC / value area / HVN-LVN + profile shape

Adding a factor = drop a `<name>.py` exposing a pure `current(...)` read and a
`contribution(read, ...) -> (signed_amount, label)`, then register it in `FACTORS`.
"""

from modules.confluence import flags, exhaustion, volume_profile

# Registry of pure factors a combiner can discover/iterate. `contribution` turns a
# factor's `current(...)` read into a signed amount (+bull / −bear) + a label, in the
# same convention `_conviction` uses; `weight` is the theory-fixed scale applied by
# the consumer. The wave-family factors (golden pocket / Elliott / divergence) are
# NOT here — they share one ZigZag pass and live with the wave engine.
FACTORS = {
    "volume_profile": volume_profile,
}

__all__ = ["flags", "exhaustion", "volume_profile", "FACTORS"]
