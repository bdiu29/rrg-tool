# Schwab — Account Positions

[← Back to main README](../../README.md)

Connect your Schwab brokerage account to see your positions alongside daily RRG sector rotation signals. Each holding gets a **BUY / HOLD / SELL / WATCH / AVOID** flag derived from its sector ETF's current rotation signal.

Open at **http://localhost:8000/schwab.html**. Requires Schwab developer credentials in `.env` (see [main README setup](../../README.md#one-time-setup)).

---

## Connecting your account

The OAuth flow uses a URL-paste pattern (Schwab redirects to a local address that won't load — that's expected):

1. Open **http://localhost:8000/schwab.html**.
2. Click **Open Schwab Login** — a new tab opens on Schwab's authorization page.
3. Log in and approve access. Schwab redirects to `https://127.0.0.1`, which **won't load** — that's normal.
4. Copy the full URL from your browser's address bar (it contains `?code=…`).
5. Paste it into the field on the page and click **Connect Account**.

Your access token is saved to `.env` and refreshed automatically on every positions call. Refresh tokens last 7 days of inactivity; if yours expires, just reconnect. **Disconnect** clears the saved tokens and drops you back to the connect flow, so you can re-authenticate or switch accounts.

> No passwords are ever stored — only OAuth tokens, and only in your local `.env`.

---

## What you see

| Column | Description |
|---|---|
| Symbol / Description | The position's ticker and name |
| Qty | Number of shares held |
| Mkt Value | Current market value |
| Day P&L | Today's unrealized gain/loss for the position |
| Open P&L | Total unrealized gain/loss since purchase |
| Sector | The SPDR sector ETF the holding maps to |
| Signal | Sector's daily RRG heading arrow + quadrant |
| **Action** | **BUY / HOLD / SELL / WATCH / AVOID** |

Hover the Signal cell to read the full rationale behind the call. Non-equity positions (options, bonds, money market) show `—` for sector and signal — they don't map to a sector ETF.

If the [Screener module](../screener/README.md) is running, alert dots appear next to symbols with active pump/dump alerts (red = dump, green = pump), so a position flashing a warning is visible right on this page.

### Buttons

- **Refresh** — re-fetch your positions and recalculate signals.
- **Export CSV** — download your current positions table as a `.csv` (includes all columns).
- **Disconnect** — clear saved tokens and return to the connect flow.

---

## How signals are derived

Each equity/ETF position is mapped to its GICS **sector** via yfinance, then to the corresponding SPDR sector ETF (`SECTOR_ETF_MAP`). That ETF's current daily RRG call (from the [RRG module](../rrg/README.md)'s `compute_rrg`) becomes the position's signal, and the call maps to an action:

| RRG call | Action |
|---|---|
| ROTATE IN | BUY |
| HOLD | HOLD |
| ROTATE OUT | SELL |
| AVOID | AVOID |
| WATCH | WATCH |

Sector lookups are cached in memory for the lifetime of the server process.

---

## Notes & integration

- **OAuth ownership:** this module owns the Schwab OAuth flow and token storage. Other modules reuse the authenticated session — `breadth` and `screener` import `get_access_token()` for market data, and `screener` also uses `get_position_symbols()` (the focus list for alerts) and `SECTOR_ETF_MAP`.
- **`.env` keys:** `SCHWAB_CLIENT_ID`, `SCHWAB_CLIENT_SECRET`, `SCHWAB_URI` (set up once), plus `SCHWAB_ACCESS_TOKEN` / `SCHWAB_REFRESH_TOKEN` / `SCHWAB_TOKEN_EXPIRY` (written automatically by the OAuth flow). The redirect URI registered with your Schwab app must be `https://127.0.0.1`.
