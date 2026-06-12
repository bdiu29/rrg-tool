"""
External alert delivery: Discord webhook and SMTP email, routed per-watchlist.

In-app delivery is implicit (alerts table). A channel only ever sees alerts
that were NEWLY inserted this pass (the UNIQUE constraint in the store is the
dedupe), batched into one message per channel per pass. Every send is
fail-soft: a channel error is recorded, never raised into the poller.

.env keys:
  DISCORD_WEBHOOK_URL
  SMTP_HOST  SMTP_PORT (default 465, SSL)  SMTP_USER  SMTP_PASS
  ALERT_EMAIL_TO (defaults to SMTP_USER)
"""

import smtplib
from email.message import EmailMessage

import requests

from modules.schwab import _read_env
from modules.screener import store

KIND_EMOJI = {"pump": "🟢", "dump": "🔴", "info": "🔵"}

DISCORD_CHAR_LIMIT = 1900   # under Discord's 2000 hard cap

_last_error = {"discord": None, "email": None}


def configured():
    env = _read_env()
    return {
        "discord": bool(env.get("DISCORD_WEBHOOK_URL")),
        "email":   bool(env.get("SMTP_HOST") and env.get("SMTP_USER")
                        and env.get("SMTP_PASS")),
    }


def last_errors():
    return dict(_last_error)


def send_discord(text):
    """→ (ok, error)"""
    env = _read_env()
    url = env.get("DISCORD_WEBHOOK_URL")
    if not url:
        return False, "DISCORD_WEBHOOK_URL not set"
    try:
        r = requests.post(url, json={"content": text[:DISCORD_CHAR_LIMIT]},
                          timeout=10)
        r.raise_for_status()
        _last_error["discord"] = None
        return True, None
    except Exception as e:
        _last_error["discord"] = str(e)
        return False, str(e)


def send_email(subject, body):
    """→ (ok, error)"""
    env  = _read_env()
    host = env.get("SMTP_HOST")
    user = env.get("SMTP_USER")
    pw   = env.get("SMTP_PASS")
    if not (host and user and pw):
        return False, "SMTP_HOST/SMTP_USER/SMTP_PASS not set"
    to = env.get("ALERT_EMAIL_TO") or user
    try:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, user, to
        msg.set_content(body)
        with smtplib.SMTP_SSL(host, int(env.get("SMTP_PORT", 465)), timeout=15) as s:
            s.login(user, pw)
            s.send_message(msg)
        _last_error["email"] = None
        return True, None
    except Exception as e:
        _last_error["email"] = str(e)
        return False, str(e)


def send_test(channel):
    text = "Screener test alert — channel routing works."
    if channel == "discord":
        return send_discord(f"🛠️ {text}")
    if channel == "email":
        return send_email("Screener test alert", text)
    return False, f"unknown channel '{channel}'"


def _format_line(alert):
    emoji = KIND_EMOJI.get(alert["kind"], "•")
    return f"{emoji} {alert['kind'].upper()} {alert['symbol']} — {alert['message']}"


def dispatch(new_alerts, position_symbols=()):
    """Route newly inserted alerts to their external channels.

    new_alerts: [{id, symbol, kind, rule_key, message, ...}]
    Route per symbol = union of channels across watchlists holding it, plus
    the positions-channels setting for held symbols. One batched message per
    channel per call.
    """
    if not new_alerts:
        return
    avail = configured()
    routes = store.channels_for_symbols(sorted({a["symbol"] for a in new_alerts}))
    pos_channels = store.get_positions_channels()

    per_channel = {"discord": [], "email": []}
    for a in new_alerts:
        chans = set(routes.get(a["symbol"], set()))
        if a["symbol"] in position_symbols:
            chans |= pos_channels
        for ch in chans:
            if ch in per_channel and avail.get(ch):
                per_channel[ch].append(a)

    delivered = {}   # alert id → set of channels that succeeded
    if per_channel["discord"]:
        text = "\n".join(_format_line(a) for a in per_channel["discord"])
        ok, _err = send_discord(text)
        if ok:
            for a in per_channel["discord"]:
                delivered.setdefault(a["id"], set()).add("discord")
    if per_channel["email"]:
        body = "\n".join(_format_line(a) for a in per_channel["email"])
        n    = len(per_channel["email"])
        ok, _err = send_email(f"Screener: {n} alert{'s' if n != 1 else ''}", body)
        if ok:
            for a in per_channel["email"]:
                delivered.setdefault(a["id"], set()).add("email")

    groups = {}
    for aid, chans in delivered.items():
        groups.setdefault(frozenset(chans), []).append(aid)
    for chans, ids in groups.items():
        store.mark_delivered(ids, sorted(chans))
