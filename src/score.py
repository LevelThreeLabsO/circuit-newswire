"""Relevance scoring: additive keyword points across four axes.

Terms carry points, points sum, and a per-source threshold admits the story. No AI.

The shape of this is the whole design, and it came out of reading 269 of The Circuit's
own headlines. Geography is NOT the beat. Of eleven Egypt stories they published, ten
carry a business or technology term — a tire factory, an IMF payout, a minerals survey,
an airport terminal, a startup fund, Cairo stocks. Egypt never appears as Egypt; it
appears as a venue for capital. So:

    geography            2 points — cannot clear the threshold alone
    named entity         3 points — a company or fund IS business by definition
    named principal      3 points — MBS, MBZ, Tahnoun, Al-Rumayyan, Al Mubarak
    sector               2 points — business, ventures, technology, science
    conflict economics   3 points — Hormuz, rerouting, war-risk insurance
    money scale          1 point  — $, billion, million, percentages

At the default threshold of 4, "Saudi Arabia arrests cleric" scores 2 and dies, while
"Egypt seeks bids to build a $500 million tire factory" scores 5. A named entity needs
just one companion signal, so "ADNOC hires Squarepoint's Roulon to lead trading arm"
clears at 5.

Two mechanisms carried over from the JI original: a per-source threshold, for throttling
a high-volume outlet without removing it; and a byline bypass, so a named reporter's work
surfaces regardless of score.

And the limitation, stated plainly rather than discovered later: this is literal word
matching. It cannot tell how central a subject is — one passing mention scores the same
as a whole article — it cannot tell reporting from commentary about reporting, and it
cannot catch a story that avoids its vocabulary. Every gap in the original was closed by
adding another word after a miss. `judge` in scoring.yaml is the dormant seam where a
language model scoring relevance by meaning would go instead; see `judge_hook()`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_FILE = Path(__file__).resolve().parent.parent / "scoring.yaml"

# Term boundaries that tolerate the punctuation in real names: "e&", "AI", "Ma'aden",
# "2PointZero". A plain \b breaks on the ampersand and would match "ai" inside "Dubai".
_LEFT = r"(?<![A-Za-z0-9])"
# A trailing plural is allowed, so the word lists can stay singular. Without this,
# "investments" silently failed to match "investment" — a whole class of quiet misses
# that looked like vocabulary gaps and was really a matcher bug.
_RIGHT = r"(?:s|es)?(?![A-Za-z0-9])"


@dataclass
class Verdict:
    score: int
    axes: list[str]
    matched: list[str]
    vetoed: str | None = None
    bypass: str | None = None

    @property
    def admitted(self) -> bool:
        return self.vetoed is None and (self.bypass is not None or self.score > 0)


def _compile(terms: list[str]) -> re.Pattern:
    parts = [_LEFT + re.escape(str(t).strip()) + _RIGHT for t in terms if str(t).strip()]
    return re.compile("|".join(parts), re.IGNORECASE) if parts else re.compile(r"(?!x)x")


class Scorer:
    def __init__(self, config: dict | None = None):
        self.config = config if config is not None else yaml.safe_load(CONFIG_FILE.read_text())
        self.default_threshold = int(self.config.get("default_threshold", 4))
        self.axes: list[tuple[str, int, re.Pattern]] = []
        for axis in self.config.get("axes", []):
            self.axes.append((axis["name"], int(axis["points"]), _compile(axis.get("terms", []))))
        # Money scale is a pattern, not a word list — "$1.2bn", "20%", "billion".
        self.money = re.compile(self.config.get("money_pattern", r"\$|\bbillion\b|\bmillion\b|\d+%"),
                                re.IGNORECASE)
        self.money_points = int(self.config.get("money_points", 1))
        self.noise = _compile(self.config.get("noise", []))
        self.bylines = [b.lower() for b in self.config.get("byline_bypass", [])]
        # Section routing, from the same vocabulary the scorer already reads.
        cats = self.config.get("categories", {})
        self._energy = _compile(cats.get("energy", []))
        self._tech = _compile(cats.get("tech", []))
        self._mena = _compile(cats.get("wider_mena", []))
        self._gcc = _compile(cats.get("gcc", []))

    # ---- the gate -----------------------------------------------------------

    def score(self, title: str, body: str = "", author: str = "") -> Verdict:
        """Score one item.

        Title and body are scored together, which is how the original worked and is the
        right call for a keyword system: a headline alone is often too terse to carry
        two axes ("ADNOC's XRG targets US, Latin America" needs the teaser to know it is
        an expansion story).
        """
        text = f" {title} {body} ".replace("’", "'")

        noise_hit = self.noise.search(title)  # veto reads the headline only, not the teaser
        if noise_hit:
            return Verdict(0, [], [], vetoed=noise_hit.group(0))

        total = 0
        axes_hit: list[str] = []
        matched: list[str] = []
        for name, points, pattern in self.axes:
            hits = pattern.findall(text)
            if hits:
                total += points
                axes_hit.append(name)
                matched += [h if isinstance(h, str) else h[0] for h in hits[:3]]
        if self.money.search(text):
            total += self.money_points
            axes_hit.append("money")

        bypass = self._byline(author)
        # Dedupe matched terms, preserving order, and keep the list short enough to log.
        seen: dict[str, None] = {}
        for m in matched:
            seen.setdefault(m.strip().lower(), None)
        return Verdict(total, axes_hit, list(seen)[:6], bypass=bypass)

    def threshold_for(self, source_threshold: int | None) -> int:
        return int(source_threshold) if source_threshold is not None else self.default_threshold

    def admits(self, verdict: Verdict, source_threshold: int | None = None) -> bool:
        if verdict.vetoed:
            return False
        if verdict.bypass:
            return True
        return verdict.score >= self.threshold_for(source_threshold)

    def _byline(self, author: str) -> str | None:
        a = (author or "").lower()
        if not a:
            return None
        return next((b for b in self.bylines if b in a), None)

    # ---- categorisation -----------------------------------------------------

    def categorize(self, title: str, body: str = "") -> str:
        """Which digest section a story belongs in.

        Derived from the story, not from its source: the same outlet files a data-center
        deal and a tanker rerouting, and the reader wants those in different places. The
        order is a priority — a story about rerouting crude away from Hormuz is energy
        news first, wherever it happens.
        """
        text = f" {title} {body} "
        if self._energy.search(text):
            return "energy"
        if self._tech.search(text):
            return "tech"
        # Wider MENA only counts as such when no Gulf state is in the story at all;
        # a Saudi investment in Egypt is Gulf business, not Egyptian business.
        if self._mena.search(text) and not self._gcc.search(text):
            return "mena"
        return "gulf"

    # ---- the dormant seam ---------------------------------------------------

    def judge_hook(self, title: str, body: str) -> None:
        """Where a meaning-based relevance judge would go.

        Deliberately unimplemented. `judge: true` in scoring.yaml is refused loudly
        rather than silently ignored, so nobody can believe an AI pass is running when
        it is not. The pattern to copy when the word list starts feeling like a
        treadmill is `ji-govt-watcher/src/classifier.py`: Gemini 2.5 Flash on the free
        tier, structured verdict, no Anthropic key, no per-item cost worth measuring.
        """
        raise NotImplementedError(
            "scoring.yaml sets judge: true, but no judge is implemented. "
            "Set it back to false, or port ji-govt-watcher/src/classifier.py."
        )
