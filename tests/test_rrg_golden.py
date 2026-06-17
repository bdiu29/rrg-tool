"""
Golden-master / characterization test for the RRG wave engine.

This is the safety net for the Stage-2 refactor that moves the cohesive wave family
(ZigZag / Elliott / Fibonacci / divergence / ABC / MTF) out of `rrg.signal` into
`confluence.wave`. It pins the FULL `compute_rrg` output (both intervals, including
the multi-timeframe blend) on a deterministic synthetic price panel to a committed
fixture, and asserts byte-identical output. A pure code move must not change a single
value; any drift fails here with a diff.

Determinism is achieved offline by injecting a seeded synthetic panel through a
patched `signal._fetch_close` (so even the MTF path, which normally fetches, is
exercised deterministically) and stubbing the live-only refinements (regime /
rotation / exhaustion / flag win rate / volume profile) to neutral values.

Regenerate the fixture intentionally (only when behavior is *meant* to change):
    /usr/bin/python3 -c "import tests.test_rrg_golden as t; t.regenerate()"
"""

import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from modules.rrg import signal, compute_rrg, BENCHMARK

_FIX = Path(__file__).parent / "fixtures" / "rrg_golden.json"
_TICKERS = ["AAA", "BBB", "CCC"]


def _panel(seed=7, n=820):
    """A deterministic multi-ticker close panel with varied wave structure (an
    uptrend with swings, a downtrend, a choppy range) so most engine branches run."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2021-01-01", periods=n)
    t = np.arange(n)

    def s(drift, amp, period, noise, base=100.0):
        return base + drift * t + amp * np.sin(2 * np.pi * t / period) + np.cumsum(rng.normal(0, noise, n))

    return pd.DataFrame({
        "AAA": s(0.05, 6, 80, 0.4),
        "BBB": s(-0.03, 5, 60, 0.4),
        "CCC": s(0.0, 8, 100, 0.5),
        "SPY": s(0.02, 3, 120, 0.3),
    }, index=idx)


def _compute_all():
    """compute_rrg for both intervals on the synthetic panel, fully deterministic:
    fetch + live refinements patched out. Runs the real-fetch code path (close=None)
    so the MTF blend executes."""
    panel = _panel()
    keep = ("_fetch_close", "current_regime", "rotation_regime",
            "exhaustion_for", "flag_win_rates_for", "volume_profile_for",
            "accumulation_for")
    orig = {k: getattr(signal, k) for k in keep}
    try:
        signal._fetch_close = lambda symbols, interval, period=signal.PERIOD: \
            panel[[c for c in symbols if c in panel.columns]]
        signal.current_regime = lambda: None
        signal.rotation_regime = lambda: None
        signal.exhaustion_for = lambda syms, period=None: {}
        signal.flag_win_rates_for = lambda syms, period=None: {}
        signal.volume_profile_for = lambda syms, period=None: {}
        signal.accumulation_for = lambda syms, period=None: {}
        signal._PRICE_CACHE.clear()
        out = {iv: compute_rrg(_TICKERS, BENCHMARK, iv, tail=6) for iv in ("1d", "1wk")}
    finally:
        for k, v in orig.items():
            setattr(signal, k, v)
        signal._PRICE_CACHE.clear()
    # round-trip through canonical JSON so the comparison is value-based, not object-based
    return json.loads(json.dumps(out, sort_keys=True))


class TestRRGGolden(unittest.TestCase):
    def test_compute_rrg_is_byte_identical_to_fixture(self):
        self.maxDiff = None
        self.assertTrue(_FIX.exists(),
                        "fixture missing — run tests.test_rrg_golden.regenerate()")
        self.assertEqual(_compute_all(), json.loads(_FIX.read_text()))


def regenerate():
    _FIX.parent.mkdir(parents=True, exist_ok=True)
    _FIX.write_text(json.dumps(_compute_all(), sort_keys=True, indent=1))
    print(f"wrote {_FIX}")


if __name__ == "__main__":
    unittest.main()
