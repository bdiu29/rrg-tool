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
    "You are the master analyst of a personal market-intelligence harness. Eight "
    "data modules (breadth/regime, sector RRG, RS rankings, CANSLIM growth, options "
    "flow, news/event-risk, screener, themes) each cast a signed vote. A DETERMINISTIC "
    "combiner has already summed those votes into a composite score, a CONCENTRATE-vs-"
    "ROTATE stance, and confluence long/avoid lists. Your job is to EXPLAIN that result "
    "in plain, decisive prose — never to contradict the number or invent a different "
    "call. Edge comes from CONFLUENCE across modules, not any single signal; call out "
    "where modules agree and where they conflict. Be concise and concrete. No preamble, "
    "no disclaimers beyond the event-risk note."
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

def master_brief(votes, combined, regime, rotation):
    """The unified daily brief (one Opus call), or a deterministic template when the
    LLM is unavailable. Anchored to `combined` — explains it, never overrides it."""
    if not available():
        return _template_brief(votes, combined), False
    payload = {
        "composite_score": combined["score"],
        "posture": combined["posture"],
        "stance": combined["stance"],
        "regime": regime, "rotation": rotation,
        "factor_breakdown": combined["factors"],
        "confluence_longs": combined["longs"],
        "confluence_avoids": combined["avoids"],
        "votes": [{k: v[k] for k in ("domain", "direction", "conviction", "weight",
                                     "horizon", "ok", "factors")} for v in votes],
    }
    prompt = (
        "Here is today's harness state as JSON. The composite score, posture and "
        "stance were computed deterministically — anchor your brief to them.\n\n"
        f"{json.dumps(payload, default=str)}\n\n"
        "Write a concise market brief in GitHub-flavored markdown with these sections:\n"
        "1. **Stance** — CONCENTRATE vs ROTATE and what it means for sizing this week.\n"
        "2. **Why** — the 2-4 votes driving the composite; name agreements and conflicts.\n"
        "3. **Confluence longs / avoids** — the sectors the longs/avoids lists name, with the WHY.\n"
        "4. **Event risk** — only if the news vote flags an imminent print.\n"
        "Keep it tight (under ~250 words). Remember: confluence across modules is the edge; "
        "no single signal has standalone alpha in a concentration regime."
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
