"""
External delivery for flow alerts — reuses the screener's Discord/SMTP senders
(single implementation of the channel transport), with flow-specific formatting and
its own routing: flow alerts go to the channels listed in the `flow_channels` setting
(plus the in-app `flow_alert` table, which is implicit). Fail-soft like the screener.
"""

from modules.screener import notify as _scr
from modules.flow import store

KIND_EMOJI = {"bull": "🟢", "bear": "🔴", "info": "🔵"}


def configured():
    return _scr.configured()


def _line(a):
    return f"{KIND_EMOJI.get(a.get('kind'), '•')} {a['message']}"


def dispatch(new_alerts):
    """Route newly inserted flow alerts to the configured `flow_channels`. One batched
    message per channel; channel errors are swallowed (recorded in the screener
    sender's last-error state), never raised into the poller."""
    if not new_alerts:
        return
    channels = set(store.get_setting("flow_channels", []) or [])
    avail = configured()
    use = [ch for ch in channels if avail.get(ch)]
    if not use:
        return

    text = "\n".join(_line(a) for a in new_alerts)
    delivered = set()
    if "discord" in use:
        ok, _ = _scr.send_discord("📈 Unusual options flow\n" + text)
        if ok:
            delivered.add("discord")
    if "email" in use:
        n = len(new_alerts)
        ok, _ = _scr.send_email(f"Options flow: {n} alert{'s' if n != 1 else ''}", text)
        if ok:
            delivered.add("email")
    if delivered:
        store.mark_delivered([a["id"] for a in new_alerts if a.get("id")], sorted(delivered))
