"""
JSON condition engine for the screener — pure pandas, unit-tested.

A screen is a list of conditions [{field, op, value}] ANDed together.
NaN never matches any condition (TradingView semantics): a symbol with no
market cap fails every market-cap condition rather than slipping through
a `!=` or `<` comparison.

FIELDS is the single registry driving validation here AND the frontend's
field dropdown (served via /api/screener/fields) — add a field once.
"""

import numpy as np
import pandas as pd

# kind: num → all comparison ops; str → ==, !=, in
FIELDS = {
    "close":              {"label": "Last price",        "kind": "num"},
    "chg_pct":            {"label": "Change %",          "kind": "num"},
    "gap_pct":            {"label": "Gap %",             "kind": "num"},
    "volume":             {"label": "Volume",            "kind": "num"},
    "vol_chg_pct":        {"label": "Volume change %",   "kind": "num"},
    "avg_vol_10d":        {"label": "Avg volume (10d)",  "kind": "num"},
    "rvol_10d":           {"label": "Rel volume (10d)",  "kind": "num"},
    "rsi14":              {"label": "RSI (14)",          "kind": "num"},
    "atr_pct":            {"label": "ATR % of price",    "kind": "num"},
    "rs_1m_pct":          {"label": "RS vs SPY 1m %",    "kind": "num"},
    "rs_3m_pct":          {"label": "RS vs SPY 3m %",    "kind": "num"},
    "pct_from_52w_high":  {"label": "% off 52w high",    "kind": "num"},
    "pct_from_52w_low":   {"label": "% above 52w low",   "kind": "num"},
    "price_vs_sma20_pct": {"label": "Price vs SMA20 %",  "kind": "num"},
    "price_vs_sma50_pct": {"label": "Price vs SMA50 %",  "kind": "num"},
    "price_vs_sma150_pct": {"label": "Price vs SMA150 %", "kind": "num"},
    "price_vs_sma200_pct": {"label": "Price vs SMA200 %", "kind": "num"},
    "price_vs_ema5_pct":  {"label": "Price vs EMA5 %",   "kind": "num"},
    "price_vs_ema10_pct": {"label": "Price vs EMA10 %",  "kind": "num"},
    "price_vs_ema20_pct": {"label": "Price vs EMA20 %",  "kind": "num"},
    "price_vs_ema50_pct": {"label": "Price vs EMA50 %",  "kind": "num"},
    "price_vs_ema100_pct": {"label": "Price vs EMA100 %", "kind": "num"},
    "price_vs_ema200_pct": {"label": "Price vs EMA200 %", "kind": "num"},
    "gp_retrace":         {"label": "Golden-pocket retrace (0-1)", "kind": "num"},
    "gp_in_pocket":       {"label": "In golden pocket (1/0)",      "kind": "num"},
    "gp_approaching":     {"label": "Approaching golden pocket (1/0)", "kind": "num"},
    "gp_direction":       {"label": "Golden-pocket direction", "kind": "str"},
    "flag":               {"label": "Flag pattern (bull/bear/none)", "kind": "str"},
    "exhaustion":         {"label": "Volume exhaustion (buyer/seller/none)", "kind": "str"},
    "market_cap":         {"label": "Market cap",        "kind": "num"},
    "pe_ratio":           {"label": "P/E ratio",         "kind": "num"},
    "div_yield":          {"label": "Dividend yield %",  "kind": "num"},
    "beta":               {"label": "Beta",              "kind": "num"},
    "days_to_earnings":   {"label": "Days to earnings",  "kind": "num"},
    "sector":             {"label": "Sector",            "kind": "str"},
    "sector_etf":         {"label": "Sector ETF",        "kind": "str"},
    "rrg_call":           {"label": "Sector RRG call",   "kind": "str"},
}

NUM_OPS = (">", ">=", "<", "<=", "==", "!=", "between")
STR_OPS = ("==", "!=", "in")
OPS     = sorted(set(NUM_OPS) | set(STR_OPS))


def validate_conditions(conditions):
    """→ list of error strings (empty = valid)."""
    errors = []
    if not isinstance(conditions, list):
        return ["conditions must be a list"]
    for i, c in enumerate(conditions):
        tag = f"condition {i + 1}"
        if not isinstance(c, dict):
            errors.append(f"{tag}: not an object")
            continue
        field, op, value = c.get("field"), c.get("op"), c.get("value")
        spec = FIELDS.get(field)
        if spec is None:
            errors.append(f"{tag}: unknown field '{field}'")
            continue
        valid_ops = NUM_OPS if spec["kind"] == "num" else STR_OPS
        if op not in valid_ops:
            errors.append(f"{tag}: op '{op}' not valid for {field}")
            continue
        if op == "between":
            if not (isinstance(value, (list, tuple)) and len(value) == 2):
                errors.append(f"{tag}: 'between' needs [lo, hi]")
        elif op == "in":
            if not (isinstance(value, (list, tuple)) and value):
                errors.append(f"{tag}: 'in' needs a non-empty list")
        elif spec["kind"] == "num" and not isinstance(value, (int, float)):
            errors.append(f"{tag}: numeric value required")
    return errors


def derive_scan_columns(df, today=None):
    """Add scan-time derived columns: price-vs-SMA stretches and
    days_to_earnings (from an earnings_date column, if present)."""
    df = df.copy()
    for n in (20, 50, 150, 200):
        sma = df.get(f"sma{n}")
        if sma is not None:
            df[f"price_vs_sma{n}_pct"] = (df["close"] / sma - 1) * 100
    for n in (5, 10, 20, 50, 100, 200):
        ema = df.get(f"ema{n}")
        if ema is not None:
            df[f"price_vs_ema{n}_pct"] = (df["close"] / ema - 1) * 100
    if "earnings_date" in df.columns:
        today = pd.Timestamp(today or pd.Timestamp.now().normalize())
        ed = pd.to_datetime(df["earnings_date"], errors="coerce")
        df["days_to_earnings"] = (ed - today).dt.days.astype("float")
    return df


def apply_filters(df, conditions):
    """AND of all condition masks; rows with NaN in a filtered field never
    match. Unknown fields simply match nothing (mask of False)."""
    if df.empty or not conditions:
        return df
    mask = pd.Series(True, index=df.index)
    for c in conditions:
        field, op, value = c.get("field"), c.get("op"), c.get("value")
        if field not in df.columns:
            mask &= False
            continue
        s = df[field]
        notna = s.notna()
        if op == ">":
            m = s > value
        elif op == ">=":
            m = s >= value
        elif op == "<":
            m = s < value
        elif op == "<=":
            m = s <= value
        elif op == "==":
            m = s == value
        elif op == "!=":
            m = s != value
        elif op == "between":
            lo, hi = value
            m = (s >= lo) & (s <= hi)
        elif op == "in":
            m = s.isin(list(value))
        else:
            m = pd.Series(False, index=df.index)
        mask &= m.fillna(False) & notna
    return df[mask]
