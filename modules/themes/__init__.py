"""
Themes module — a relative-strength tracker for user-defined investment themes.

Each theme is a curated basket of stocks (see `store.py`). We build an
equal-weight total-return index per theme and feed those synthetic indices
through the *existing* RS engines — so the page is mostly wiring:

  * theme ranking 0-99 + movers  → `rankings.compute_rankings(..., close=panel)`
  * theme RRG rotation + calls    → `rrg.compute_rrg(..., close=panel)`
  * per-theme constituent leaders → computed off the same fetched closes

All of it runs off ONE cached price fetch (`signal._fetch_close`). The theme
index is the mean of constituent daily returns, compounded — mean-of-returns
(not mean-of-prices) so a high-priced name can't dominate, and `skipna` lets a
constituent contribute only from its inception.

Routes:
  GET  /themes.html            → themes.html
  GET  /api/themes             → ranking + RRG + constituents + theme defs
  GET  /api/themes/summary     → hub badge (leading theme)
  POST /api/themes/save        → create/update a theme
  POST /api/themes/delete      → delete a theme
"""

from datetime import datetime
from pathlib import Path

import pandas as pd

from modules import Response
from modules.rrg import signal, compute_rrg
from modules.rankings import compute_rankings, _ret, _rel, flag_stats_for
from . import store as themes_store

_MODULE_DIR = Path(__file__).resolve().parent
BENCHMARK   = "SPY"


# ---------------------------------------------------------------------------
# Equal-weight theme index + the assembled view
# ---------------------------------------------------------------------------

def _theme_key(theme):
    return f"T{theme['id']}"


def _build_panel(themes, close, benchmark):
    """Synthetic close panel: one equal-weight theme-index column per theme
    (key 'T<id>') + the real benchmark column. Returns (panel, keys, name_map)."""
    cols, name_map, keys = {}, {}, []
    for t in themes:
        cons = [s for s in t["symbols"] if s in close.columns and close[s].notna().any()]
        if not cons:
            continue
        rets = close[cons].pct_change()
        idx  = (1 + rets.mean(axis=1).fillna(0.0)).cumprod() * 100  # EW total-return index
        if idx.dropna().empty:
            continue
        key = _theme_key(t)
        cols[key]      = idx
        name_map[key]  = t["name"]
        keys.append(key)
    panel = pd.DataFrame(cols)
    if not panel.empty and benchmark in close.columns:
        panel[benchmark] = close[benchmark]
    return panel, keys, name_map


def _constituent_leaders(themes, close, benchmark):
    """{theme_id: [{symbol, price, chg_pct, rs_1m, rs_3m}]} ranked by 1m RS."""
    bench = close[benchmark] if benchmark in close.columns else None
    out = {}
    for t in themes:
        rows = []
        for s in t["symbols"]:
            if s not in close.columns:
                continue
            px = close[s].dropna()
            if px.empty:
                continue
            chg = _ret(close[s], 1)
            rows.append({
                "symbol":  s,
                "price":   round(float(px.iloc[-1]), 2),
                "chg_pct": round(chg, 2) if chg is not None else None,
                "rs_1m":   _rel(_ret(close[s], 21), _ret(bench, 21)) if bench is not None else None,
                "rs_3m":   _rel(_ret(close[s], 63), _ret(bench, 63)) if bench is not None else None,
            })
        rows.sort(key=lambda r: (r["rs_1m"] is not None, r["rs_1m"] or 0), reverse=True)
        out[str(t["id"])] = rows
    # enrich every constituent with its flag win-rate + current volume exhaustion
    # (off-universe tickers simply get blanks — fail-soft)
    stats = flag_stats_for([r["symbol"] for rows in out.values() for r in rows])
    for rows in out.values():
        for r in rows:
            r.update(stats.get(r["symbol"], {}))
    return out


def _apply_names(rows, name_map):
    """Overwrite synthetic 'T<id>' tickers with real theme names + ids."""
    for r in rows:
        key = r.get("ticker")
        if key in name_map:
            r["name"] = name_map[key]
            r["id"]   = int(key[1:])


def compute_theme_view(timeframe="daily", tail=6, benchmark=BENCHMARK):
    """Assemble the full themes payload off one cached daily fetch (+ a weekly
    fetch only when the RRG is on the weekly lens)."""
    interval = "1wk" if timeframe == "weekly" else "1d"
    all_themes = themes_store.list_themes()
    themes = [t for t in all_themes if t["symbols"]]

    empty = {"date": None, "benchmark": benchmark,
             "ranking": {"sectors": [], "rank_up_daily": [], "rank_down_daily": [],
                         "rank_up_weekly": [], "rank_down_weekly": []},
             "rrg": {"sectors": {}, "best": None}, "leaders": {}, "themes": all_themes}
    if not themes:
        return empty

    all_syms   = sorted({s for t in themes for s in t["symbols"]})
    daily_close = signal._fetch_close(all_syms + [benchmark], "1d", signal.PERIOD)

    panel_d, keys, name_map = _build_panel(themes, daily_close, benchmark)
    if not keys:
        return empty

    # Ranking + movers (always daily, like the sector rankings page)
    ranking = compute_rankings(tickers=keys, benchmark=benchmark, close=panel_d)
    _apply_names(ranking["sectors"], name_map)
    for mv in ("rank_up_daily", "rank_down_daily", "rank_up_weekly", "rank_down_weekly"):
        _apply_names(ranking[mv], name_map)

    # Constituent leaders (daily)
    leaders = _constituent_leaders(themes, daily_close, benchmark)

    # RRG (honors the daily/weekly toggle)
    if interval == "1wk":
        wk_close = signal._fetch_close(all_syms + [benchmark], "1wk", signal.PERIOD)
        panel_r, keys_r, name_map_r = _build_panel(themes, wk_close, benchmark)
    else:
        panel_r, keys_r, name_map_r = panel_d, keys, name_map

    rrg_res = compute_rrg(keys_r, benchmark, interval, tail=tail, close=panel_r)
    sectors = {}
    for key, d in rrg_res.get("sectors", {}).items():
        d["name"] = name_map_r.get(key, key)
        d["id"]   = int(key[1:])
        sectors[key] = d
    rrg_res["sectors"] = sectors
    if rrg_res.get("best"):
        bk = rrg_res["best"]["ticker"]
        rrg_res["best"]["name"] = name_map_r.get(bk, bk)

    return {
        "date":      ranking.get("date"),
        "benchmark": benchmark,
        "ranking":   ranking,
        "rrg":       rrg_res,
        "leaders":   leaders,
        "themes":    all_themes,
    }


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _handle_index(req):
    with open(_MODULE_DIR / "themes.html") as f:
        return Response.html(f.read())


def _handle_themes(req):
    timeframe = req.qs.get("timeframe", ["daily"])[0]
    try:
        tail = max(3, min(int(req.qs.get("tail", ["6"])[0]), 14))
    except (TypeError, ValueError):
        tail = 6
    try:
        result = compute_theme_view(timeframe, tail)
        result["timeframe"] = timeframe
        result["tail"]      = tail
        result["updated"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return Response.json(result)
    except Exception as e:
        return Response.error(str(e))


def _handle_save(req):
    try:
        body = req.json_body() or {}
    except Exception:
        body = {}
    try:
        tid = themes_store.save_theme(
            name=body.get("name", ""),
            symbols=body.get("symbols") or [],
            description=body.get("description", "") or "",
            theme_id=body.get("id"),
        )
        return Response.json({"ok": True, "id": tid})
    except Exception as e:
        return Response.error(str(e), 400)


def _handle_delete(req):
    try:
        body = req.json_body() or {}
    except Exception:
        body = {}
    tid = body.get("id")
    if not tid:
        return Response.error("id required", 400)
    themes_store.delete_theme(tid)
    return Response.json({"ok": True})


def _handle_summary(req):
    try:
        secs = compute_theme_view("daily", 6)["ranking"]["sectors"]
        top  = secs[0] if secs else None
        if not top or top.get("rank") is None:
            return Response.json({"status": "neutral", "text": "no data"})
        return Response.json({
            "status": "healthy",
            "text":   f"{top['name']} · rank {top['rank']}",
        })
    except Exception as e:
        return Response.error(str(e))


def register_routes(router):
    themes_store.init_db()        # creates tables + seeds the 6 built-in themes
    router.get("/themes.html",        _handle_index)
    router.get("/api/themes",         _handle_themes)
    router.get("/api/themes/summary", _handle_summary)
    router.post("/api/themes/save",   _handle_save)
    router.post("/api/themes/delete", _handle_delete)
