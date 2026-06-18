"""
The LLM layer — domain "subagents" + the master synthesizer, run on the user's
Claude SUBSCRIPTION via the local `claude` CLI in headless mode (NOT the metered
API: no ANTHROPIC_API_KEY, no per-token cost, no new dependency).

Design contract (settled with the user): MATH DECIDES, LLM EXPLAINS. The
deterministic combiner (`combiner.py`) produces the score/stance/longs/avoids; the
LLM here only narrates that result and surfaces cross-domain agreement/conflict —
it is anchored to the number and must not invent its own. Every call is fail-soft:
if the CLI is missing / not logged in / errors / times out, we fall back to a
deterministic TEMPLATE so the brief always renders (the $0 / offline mode).

CLI invocation (headless): the prompt is piped on STDIN (avoids arg-length limits),
and we ask for `--output-format json` so we can pull a clean `result` field:

    claude -p --output-format json --model <m> [--append-system-prompt <sys>]

Model names are CLI aliases (`haiku`/`opus`), overridable via env so they track new
releases without a code change.
"""

import json
import os
import shutil
import subprocess

# Cheap fan-out model for the per-domain one-liners; the judgment-heavy synthesis
# model for the single master call. Aliases resolve to the current releases
# (Opus 4.8 / Haiku 4.5); override with full IDs via env if needed.
DOMAIN_MODEL = os.environ.get("HARNESS_DOMAIN_MODEL", "haiku")
MASTER_MODEL = os.environ.get("HARNESS_MASTER_MODEL", "opus")

_CLI_TIMEOUT = int(os.environ.get("HARNESS_CLI_TIMEOUT", "150"))

_SYSTEM = (
    "You are the lead market strategist for a personal trading desk. You explain what is "
    "happening in PLAIN ENGLISH — like a sharp portfolio manager talking to a smart friend, "
    "not a quant writing a research note. Translate the technical signals (market breadth, "
    "sector rotation, relative-strength rankings, options flow, and the growth/inflation "
    "regime) into what they MEAN for positioning and sizing. A deterministic engine has "
    "already decided the stance, the composite score, the growth/inflation regime "
    "probabilities, and the confluence long/avoid lists — anchor everything to those "
    "numbers and never invent a different call. Within that anchor you have room to "
    "interpret: connect the dots across signals, surface a non-obvious insight or two, and "
    "be specific (name the sectors and tickers). The edge is CONFLUENCE across signals, not "
    "any single one. Be decisive and concrete, use everyday language, avoid jargon, and "
    "skip boilerplate disclaimers (keep only the event-risk note if one is flagged)."
)


def _binary():
    return os.environ.get("HARNESS_CLAUDE_BIN") or shutil.which("claude")


def available():
    """True if LLM narration is possible (binary present and not disabled)."""
    if os.environ.get("HARNESS_LLM", "1") == "0":
        return False
    return bool(_binary())


def claude_cli(prompt, model, system=_SYSTEM, timeout=_CLI_TIMEOUT):
    """One headless `claude` call on the subscription. Returns the assistant text,
    or None on any failure (missing binary, non-zero exit, timeout, bad JSON)."""
    binary = _binary()
    if not binary or os.environ.get("HARNESS_LLM", "1") == "0":
        return None
    cmd = [binary, "-p", "--output-format", "json", "--model", model]
    if system:
        cmd += ["--append-system-prompt", system]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    out = proc.stdout.strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return out                      # some CLI builds print raw text — use it
    if isinstance(data, dict):
        if data.get("is_error"):
            return None
        return data.get("result") or None
    return None


# ---------------------------------------------------------------------------
# Domain subagents — one cheap rationale per vote
# ---------------------------------------------------------------------------

def domain_rationale(vote):
    """A 1-2 sentence read of a single domain's vote (cheap Haiku call), or a
    deterministic template when the LLM is unavailable."""
    if not vote.get("ok"):
        return vote.get("note") or "no data"
    if not available():
        return _template_rationale(vote)
    prompt = (
        f"Domain: {vote['domain']} (horizon {vote['horizon']}).\n"
        f"Vote: direction {vote['direction']:+d}, conviction {vote['conviction']}/100, "
        f"weight {vote['weight']}.\n"
        f"Factors: {json.dumps(vote.get('factors'))}\n"
        f"Detail: {json.dumps(vote.get('detail'), default=str)[:1200]}\n\n"
        "In ONE or TWO sentences, state what this module is signaling right now and "
        "why. No preamble."
    )
    return claude_cli(prompt, DOMAIN_MODEL) or _template_rationale(vote)


def _template_rationale(vote):
    d = {1: "bullish", -1: "bearish", 0: "neutral"}[vote["direction"]]
    facs = ", ".join(f"{l}" for l, _ in (vote.get("factors") or [])[:3])
    return f"{vote['domain']}: {d} (conviction {vote['conviction']})" + (f" — {facs}" if facs else "")


# ---------------------------------------------------------------------------
# Master synthesizer — one brief, anchored to the combiner result
# ---------------------------------------------------------------------------

def _macro_detail(votes):
    """The macro vote's regime + caution indicators (for the brief payload)."""
    for v in votes:
        if v.get("domain") == "macro" and v.get("ok"):
            return v.get("detail") or {}
    return {}


def master_brief(votes, combined, regime, rotation):
    """The unified daily brief (one Opus call), or a deterministic template when the
    LLM is unavailable. Anchored to `combined` — explains it, never overrides it."""
    if not available():
        return _template_brief(votes, combined), False
    mac = _macro_detail(votes)
    payload = {
        "composite_score": combined["score"],
        "posture": combined["posture"],
        "stance": combined["stance"],
        "breadth_regime": regime, "rotation": rotation,
        "macro_regime": mac.get("regime"),
        "market_health": mac.get("health"),
        "caution_indicators": mac.get("caution"),
        "factor_breakdown": combined["factors"],
        "confluence_longs": combined["longs"],
        "confluence_avoids": combined["avoids"],
        "votes": [{k: v[k] for k in ("domain", "direction", "conviction", "weight",
                                     "horizon", "ok", "factors")} for v in votes],
    }
    prompt = (
        "Here is today's market state as JSON. A deterministic engine already set the "
        "composite score, the stance, and the growth/inflation regime probabilities — "
        "anchor your brief to them and don't contradict them.\n\n"
        f"{json.dumps(payload, default=str)}\n\n"
        "Write a plain-English market brief in GitHub-flavored markdown that a smart "
        "non-quant could follow. Structure it like this:\n"
        "- Start with a short, decisive HEADLINE as a markdown heading (e.g. "
        "`### Today is a hold-and-stay-selective day`).\n"
        "- Then 3-5 SHORT paragraphs (no section labels): what's actually happening today "
        "and what it means; what the CONCENTRATE-vs-ROTATE stance says about sizing this "
        "week; the growth/inflation regime in everyday terms (what backdrop we're in and "
        "what plays in it); and the specific confluence longs/avoids — name the sectors and "
        "any tickers, with a one-line WHY each.\n"
        "- Surface ONE or TWO non-obvious expert insights (a rotation quietly starting, a "
        "complacency tell, a divergence) — interpret, don't just list.\n"
        "- Add an event-risk line ONLY if the news vote flags an imminent print.\n"
        "Keep it tight (~250-320 words), confident, and jargon-free. The edge is confluence "
        "across signals, not any single one."
    )
    text = claude_cli(prompt, MASTER_MODEL)
    if text:
        return text, True
    return _template_brief(votes, combined), False


def _template_brief(votes, combined):
    """Deterministic plain-markdown brief from the combiner output — the $0/offline
    fallback. Readable on its own; the LLM version just adds prose."""
    L = []
    L.append(f"**Stance: {combined['stance']}** · composite **{combined['score']:+.0f}** "
             f"({combined['posture']}) · regime {combined.get('regime') or '—'} · "
             f"rotation {combined.get('rotation') or '—'}")
    mac = _macro_detail(votes).get("regime") or {}
    if mac:
        L.append("")
        L.append(f"**Macro backdrop:** {mac.get('regime')} ({mac.get('confidence')}% confidence, "
                 f"shift risk {mac.get('shift_risk')}) — {mac.get('playbook')}")
    L.append("")
    if combined["factors"]:
        drivers = ", ".join(f"{d} ({a:+.0f})" for d, a in combined["factors"][:5])
        L.append(f"**Drivers:** {drivers}")
    if combined["longs"]:
        L.append("**Confluence longs:** " + ", ".join(
            f"{x['ticker']} ({x['call']}{', rank ' + str(x['rank']) if x.get('rank') is not None else ''})"
            for x in combined["longs"]))
    if combined["avoids"]:
        L.append("**Avoids:** " + ", ".join(
            f"{x['ticker']} ({x['call']})" for x in combined["avoids"]))
    news = next((v for v in votes if v["domain"] == "news" and v.get("ok")), None)
    if news and (news.get("detail") or {}).get("flag"):
        L.append(f"**Event risk:** {news['detail'].get('note')}")
    L.append("")
    L.append("_LLM narration unavailable — deterministic brief from the combiner._")
    return "\n".join(L)
