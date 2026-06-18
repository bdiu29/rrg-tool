"""
AI-assist chat (Phase 4) — the "manual trading" mode's grounded assistant.

Answers the user's questions about their watchlist / picks / paper book / regime, but
ANCHORED to the deterministic signals the harness already computes (the project's
math-decides / LLM-explains contract): we assemble a compact JSON of the current state
and instruct the model to answer ONLY from it, and to say so when something isn't there.
No new signal is invented here — it's a read-out, not an oracle.

Runs on the Claude subscription via `agents.claude_cli` (no API key); fail-soft → a
clear "assistant unavailable" message, never an exception.
"""

import json

from modules.harness import agents

_CHAT_SYSTEM = (
    "You are the assistant inside a personal market-intelligence harness. A CONTEXT "
    "block of deterministic signals (a regime-arbitrated combiner decision, per-domain "
    "votes, the user's watchlist trade suggestions with impulse/hold/stop, and the paper "
    "books) is provided with each question. Rules: (1) Answer ONLY from the CONTEXT — if "
    "the data needed isn't there, say so plainly and suggest the user run the relevant "
    "panel. (2) You EXPLAIN the deterministic read; you never override it or invent "
    "numbers/prices. (3) Be concise and concrete; reference specific tickers, scores, and "
    "stops from the CONTEXT. (4) This is not investment advice and the user trades "
    "manually — frame setups, risks, and the stop, not commands."
)

_MAX_HISTORY = 6        # last N prior turns carried for continuity


def _trim(obj, n=2000):
    s = json.dumps(obj, default=str)
    return s if len(s) <= n else s[:n] + "…"


def build_context():
    """Compact, fail-soft snapshot of the deterministic state for grounding. Each piece
    degrades to None/[] independently so the chat works as modules come online."""
    ctx = {}
    try:
        from modules.harness import get_brief
        b = get_brief(force=False)
        c = b.get("combined", {})
        ctx["decision"] = {k: c.get(k) for k in ("stance", "posture", "score", "regime",
                                                 "rotation")}
        ctx["longs"] = [{"ticker": x.get("ticker"), "call": x.get("call"),
                         "score": x.get("score")} for x in (c.get("longs") or [])][:5]
        ctx["avoids"] = [x.get("ticker") for x in (c.get("avoids") or [])][:5]
        ctx["votes"] = [{"domain": v.get("domain"), "direction": v.get("direction"),
                         "conviction": v.get("conviction")}
                        for v in (b.get("votes") or []) if v.get("ok")]
    except Exception:
        pass
    try:
        from modules.harness import picks
        sug = picks.cached_suggestions()
        if sug:
            ctx["suggestions"] = [{k: s.get(k) for k in ("symbol", "pick", "impulse",
                                   "hold", "tradeable", "stop", "why")}
                                  for s in sug.get("suggestions", [])[:15]]
        else:
            ctx["suggestions_note"] = "not computed yet — run Get Suggestions for live picks"
    except Exception:
        pass
    try:
        from modules.harness import paper, store
        ctx["paper"] = paper.state()
        ctx["watchlist"] = store.get_watchlist()
    except Exception:
        pass
    return ctx


def answer(message, history=None):
    """Grounded reply to a user message. Returns {answer, grounded, llm_used}."""
    message = (message or "").strip()
    if not message:
        return {"answer": "Ask a question about your watchlist, picks, the paper book, or "
                          "the regime.", "grounded": True, "llm_used": False}
    if not agents.available():
        return {"answer": "The AI assistant isn't available — the local `claude` CLI isn't "
                          "logged in (or HARNESS_LLM=0). The deterministic panels above "
                          "still work; this chat needs the Claude subscription CLI.",
                "grounded": True, "llm_used": False}

    ctx = build_context()
    convo = ""
    for turn in (history or [])[-_MAX_HISTORY:]:
        role = "User" if turn.get("role") == "user" else "Assistant"
        convo += f"{role}: {turn.get('content', '')}\n"

    prompt = (f"CONTEXT (deterministic signals, as-of now):\n{_trim(ctx, 6000)}\n\n"
              + (f"Earlier in this conversation:\n{convo}\n" if convo else "")
              + f"User question: {message}\n\nAnswer grounded in the CONTEXT.")
    out = agents.claude_cli(prompt, agents.MASTER_MODEL, system=_CHAT_SYSTEM)
    if not out:
        return {"answer": "The assistant timed out or returned nothing — try again, or "
                          "ask something simpler.", "grounded": True, "llm_used": False}
    return {"answer": out, "grounded": True, "llm_used": True}
