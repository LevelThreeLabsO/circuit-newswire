"""The three-times-daily briefing: five stories an editor should not miss.

Different job from the newswire. The newswire answers "is this on the beat?" — a filter,
and a keyword list does it well. This answers "of the forty things we sent since dawn,
which five actually matter?" That is a judgment about weight and novelty, and word
counting cannot make it.

Two stages, deliberately:

1. **Rank deterministically** on evidence the newswire already collected — how many other
   outlets filed the same story, the size of the number in the headline, whether a
   sovereign fund or national champion is named, how strongly it scored, whether the
   outlet is one The Circuit actually cites. This is defensible, debuggable, and produces
   a sane answer with no model involved.

2. **Let a model choose five from the top of that ranking**, for distinctness and news
   weight, and write one line on why each matters. Rank order alone will happily pick
   four Hormuz stories; a model will notice they are one story and reach further down.

If the model is unavailable or answers badly, the deterministic top five ships instead.
The briefing is never blocked on an API, and it never posts a five-item list it cannot
justify — a fallback selection says so in the message rather than pretending.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SHORTLIST = 15          # how many the model chooses from
WANTED = 5
# gemini-2.5-flash is closed to new API keys — a fresh key gets 404 NOT_FOUND with a
# pointer to this model. Verified working against the real key before deploying.
MODEL = "gemini-3.6-flash"

# Outlets The Circuit actually relies on, from counting every outbound link in 300 of
# their posts. A story these broke carries more weight for this desk than the same story
# picked up elsewhere.
TIER_ONE = {
    "bloomberg", "reuters", "the national", "agbi", "ft", "financial times",
    "wall street journal", "wsj", "the new york times", "zawya", "arab news",
}

# Named capital: the actors whose involvement makes a story matter to this beat.
BIG_ACTORS = re.compile(
    r"\b(PIF|Public Investment Fund|Mubadala|ADIA|ADQ|QIA|Qatar Investment Authority|KIA|"
    r"MGX|G42|Lunate|Sanabil|IHC|Investcorp|ADNOC|XRG|Aramco|QatarEnergy|SABIC|Ma'?aden|"
    r"Masdar|ACWA|TAQA|DP World|AD Ports|NEOM|Qiddiya|Diriyah|Red Sea Global|Emaar|"
    r"Aldar|stc|e&|Etihad Rail|Riyadh Air|Tadawul|MBS|Mohammed bin Salman|MBZ|"
    r"bin Zayed|Tahnoo?n|Al-?Rumayyan|Al Mubarak)\b",
    re.IGNORECASE,
)

_MONEY = re.compile(r"\$\s?([\d.,]+)\s*(bn|billion|b\b|m\b|million|trillion|tn)?", re.IGNORECASE)


def money_weight(title: str) -> float:
    """Rough magnitude of the largest figure in the headline, in billions.

    A $30bn sovereign commitment and a $30m seed round are not the same story, and the
    newswire's scorer treats both as one 'money' point. Capped so a single enormous
    number cannot dominate the ranking on its own.
    """
    best = 0.0
    for amount, unit in _MONEY.findall(title):
        try:
            value = float(amount.replace(",", ""))
        except ValueError:
            continue
        unit = (unit or "").lower()
        if unit.startswith(("bn", "b", "billion")):
            value *= 1.0
        elif unit.startswith(("tn", "trillion")):
            value *= 1000.0
        elif unit.startswith(("m", "million")):
            value /= 1000.0
        else:
            value /= 1_000_000.0     # a bare figure is not billions
        best = max(best, value)
    return min(best, 50.0)


def rank(entries: list[dict]) -> list[dict]:
    """Order the period's stories by weight. Highest first."""
    scored = []
    for e in entries:
        if e.get("kind") == "brief":
            continue
        title = e.get("title") or ""
        weight = 0.0
        # Corroboration is the strongest signal available and the only one that is not a
        # guess: each point is another newsroom independently deciding this was worth
        # filing within the same three hours.
        weight += 3.0 * int(e.get("corroboration", 0))
        weight += 1.5 * len(BIG_ACTORS.findall(title))
        weight += min(money_weight(title), 10.0) * 0.4
        weight += 0.5 * max(0, int(e.get("score", 0)) - 4)
        if (e.get("outlet") or "").lower() in TIER_ONE:
            weight += 1.0
        # Conflict economics has been the spine of this beat all year — Hormuz transits,
        # rerouting, war-risk premiums.
        if "conflict_econ" in (e.get("axes") or []):
            weight += 1.0
        item = dict(e)
        item["weight"] = round(weight, 2)
        scored.append(item)
    return sorted(scored, key=lambda e: (-e["weight"], e.get("at", "")))


SYSTEM_PROMPT = """You are briefing the editors of The Circuit, a publication covering \
business, finance, energy and technology in the Gulf and wider Middle East.

You will be given headlines their newswire posted since the last briefing, already ranked \
by a crude weighting. Choose the FIVE an editor must not miss, and order them by \
importance.

Choose for:
- Consequence. A sovereign fund taking a stake, a national champion restructuring, a \
policy or regulatory change that moves money, a disruption to trade routes or energy flows.
- Distinctness. Do not pick five versions of one story. If three headlines are all about \
Hormuz shipping, pick the strongest and use the other slots for different subjects.
- Novelty. Prefer a development over a restatement, and a decision over a comment about a \
decision.

Avoid: routine market wraps, index moves, gold prices, scheduled data releases, conference \
announcements, vendor press releases, and anything whose only claim is that a number changed.

For each chosen story write ONE sentence on why it matters to this desk.

This sentence must NOT restate the headline. Assume the reader has read it. Say what the \
development implies, changes or reveals — the second-order point an editor would make in \
a news meeting. Examples of the difference:

  headline: "Egypt targets $3bn from new bond issuances"
  bad  (restatement): "Egypt is tapping debt markets for $3 billion to meet funding needs."
  good (consequence): "First test of investor appetite for Egyptian paper since the IMF \
review, and the pricing will set the floor for the region's other deficit borrowers."

  headline: "Adnoc keeps loading LNG on tankers despite Hormuz disruption"
  bad  (restatement): "Adnoc is maintaining LNG exports despite disruption."
  good (consequence): "Cuts against the assumption that Gulf gas exports have stalled, \
and suggests buyers are still accepting Hormuz transit risk."

Do not invent detail that is not in the headline: no deal sizes, dates, names or causes \
that are not there. Where the implication is genuinely uncertain, say what it would tell \
you rather than asserting an outcome. No adjectives beyond what the facts carry.

Return the index numbers as given, not a rewritten list."""


class Choice:
    def __init__(self, index: int, why: str):
        self.index = index
        self.why = why


def _gemini_available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def choose(shortlist: list[dict]) -> tuple[list[dict], str]:
    """Pick five from the shortlist. Returns (chosen, how) — 'gemini' or 'ranked'."""
    if len(shortlist) <= WANTED:
        return shortlist, "ranked"
    if not _gemini_available():
        return shortlist[:WANTED], "ranked"
    try:
        chosen = _choose_with_gemini(shortlist)
    except Exception as e:  # noqa: BLE001 — a briefing must never fail on an API
        print(f"  ! model selection failed ({type(e).__name__}: {e}); using the ranking")
        return shortlist[:WANTED], "ranked"
    if not chosen:
        return shortlist[:WANTED], "ranked"
    return chosen, "gemini"


def _choose_with_gemini(shortlist: list[dict]) -> list[dict]:
    from google import genai
    from google.genai import types

    lines = []
    for i, e in enumerate(shortlist):
        corr = int(e.get("corroboration", 0))
        also = f", also filed by {corr} other outlet{'s' if corr != 1 else ''}" if corr else ""
        lines.append(f"[{i}] {e.get('title')} — {e.get('outlet')}{also}")

    schema = {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "why": {"type": "string"},
                    },
                    "required": ["index", "why"],
                },
            }
        },
        "required": ["picks"],
    }

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"])
    response = client.models.generate_content(
        model=MODEL,
        contents="\n".join(lines),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=schema,
            # 4000, not 1200, and thinking held low. gemini-3.6-flash is a thinking
            # model: reasoning tokens count against max_output_tokens, and at 1200 it
            # spent 1,149 of them thinking and 47 answering, so the JSON came back
            # truncated mid-string and every run silently fell back to ranking. Measured
            # need with thinking_level=low is ~230 output tokens.
            max_output_tokens=4000,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        ),
    )
    payload = json.loads((response.text or "{}").strip())

    out: list[dict] = []
    seen: set[int] = set()
    # Walk EVERY pick, not the first five. Validation rejects hallucinated and repeated
    # indices, and slicing to five before filtering meant each rejection silently shrank
    # the briefing — a test with two bad picks produced a four-item "five to check".
    for pick in payload.get("picks", []):
        if len(out) >= WANTED:
            break
        idx = pick.get("index")
        if not isinstance(idx, int) or not (0 <= idx < len(shortlist)) or idx in seen:
            continue
        seen.add(idx)
        entry = dict(shortlist[idx])
        entry["why"] = (pick.get("why") or "").strip()
        out.append(entry)

    # Top up from the ranking if the model returned too few usable picks, so a thin
    # answer degrades to the deterministic order rather than a short list.
    for entry in shortlist:
        if len(out) >= WANTED:
            break
        if any(e.get("url") == entry.get("url") for e in out):
            continue
        out.append(dict(entry))
    return out


def _escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# The desk is Eastern; the runner is UTC. `.astimezone()` with no argument converts to the
# MACHINE's local time, so this labelled the 6am edition "Midday" and midday "Evening" in
# the cloud — and the bug was invisible locally, because a Mac in New York gives the right
# answer. Name the zone explicitly.
EASTERN = ZoneInfo("America/New_York")


def edition_label(when: datetime | None = None) -> str:
    hour = (when or datetime.now(timezone.utc)).astimezone(EASTERN).hour
    if hour < 11:
        return "Morning"
    if hour < 16:
        return "Midday"
    return "Evening"


def format_brief(chosen: list[dict], how: str, period_hours: float, total: int,
                 when: datetime | None = None) -> str:
    """The Slack message. Plain, and honest about how the five were chosen."""
    count = len(chosen)
    header = (f"*{edition_label(when)} briefing* — {count} to check from "
              f"{total} stor{'y' if total == 1 else 'ies'} in the last {period_hours:.0f}h")
    lines = [header, ""]
    for n, e in enumerate(chosen, 1):
        title = _escape(e.get("title") or "")
        url = e.get("url") or ""
        outlet = _escape(e.get("outlet") or "")
        head = f"*{n}. <{url}|{title}>*" if url else f"*{n}. {title}*"
        corr = int(e.get("corroboration", 0))
        tail = f" · also filed by {corr} other outlet{'s' if corr != 1 else ''}" if corr else ""
        lines.append(f"{head} — {outlet}{tail}")
        why = (e.get("why") or "").strip()
        if why:
            lines.append(f"_{_escape(why)}_")
        lines.append("")
    if how == "ranked":
        # Say so. A list assembled by weighting alone may contain two versions of one
        # story, and the reader should know which kind of list they are holding.
        lines.append("_Selected by ranking only — editorial pass unavailable this run._")
    return "\n".join(lines).strip()
