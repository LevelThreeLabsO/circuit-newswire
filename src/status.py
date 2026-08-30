"""Run state, written every run and readable from outside the machine.

This module exists FIRST, before any feed logic, and that ordering is the single most
important decision in this project. The JI Newswire it descends from had no way to
report its own state: when it broke, every diagnosis was an inference from what was
missing in Slack, three consecutive diagnoses were wrong, and the real cause was found
minutes after a status endpoint finally existed.

There is no server here, and none is needed. `status.json` is committed to the repo on
every run, so on a public repo the raw URL *is* the status endpoint:

    https://raw.githubusercontent.com/LevelThreeLabsO/circuit-newswire/main/status.json

What it records, per run: when the run started and finished, how long it took, what each
source returned, how many items survived each gate, whether Slack accepted the post, and
the last error with its traceback. Plus two things that only mean anything across runs —
`consecutive_empty` and `consecutive_parse_fail` per source, which are how a silently
dead feed gets caught. A dead feed returns HTTP 200 and serves a web page; nothing else
in the pipeline will ever notice.
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime, timezone
from pathlib import Path

STATUS_FILE = Path(__file__).resolve().parent.parent / "status.json"

# A source silent for this many HOURS is reported. Counting runs was wrong twice over:
# the count assumed a 15-minute cadence while the cloud loop polls every five minutes, so
# it fired after 16 hours instead of 48 — and it flagged sources that are merely
# low-volume. Semafor legitimately went 33 hours between stories, and Intelligence Online
# publishes a few times a week. Hours are what the sentence in the channel claims anyway.
DEAD_SOURCE_HOURS = 72


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict:
    """Previous status, or a blank one. Never raises — a corrupt status file must not be
    able to stop a run, since its whole job is to report on runs."""
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


class Run:
    """Accumulates one run's facts, then writes them out.

    Carries forward the cross-run counters from the previous status file, so per-source
    health survives the single-shot cloud runs the same way dedup state does.
    """

    def __init__(self) -> None:
        prev = load()
        self.prev = prev
        self.started = _now()
        self.t0 = datetime.now(timezone.utc)
        self.sources: dict[str, dict] = {}
        self.counts: dict[str, int] = {}
        self.posted = 0
        self.delivered: bool | None = None
        self.error: str | None = None
        # Set only when a health alert is actually delivered, so the cooldown below is
        # never consumed by an alert nobody received.
        self.alerted = False
        # Cross-run counters, keyed by source.
        self._streak_empty: dict[str, int] = dict(prev.get("consecutive_empty", {}))
        self._streak_parse: dict[str, int] = dict(prev.get("consecutive_parse_fail", {}))
        # When each source last returned anything, for time-based staleness.
        self._last_item: dict[str, str] = dict(prev.get("last_item_at", {}))

    # ---- per-source outcomes -------------------------------------------------

    def source_ok(self, key: str, fetched: int, in_window: int) -> None:
        self.sources[key] = {"status": "ok", "fetched": fetched, "in_window": in_window}
        self._streak_parse[key] = 0
        self._streak_empty[key] = 0 if fetched else self._streak_empty.get(key, 0) + 1
        if fetched:
            self._last_item[key] = _now()

    def source_parse_fail(self, key: str, detail: str) -> None:
        """A feed that answered but could not be parsed.

        Deliberately distinct from `fetched: 0`. Fitch, Argaam, ADX, Treasury, OPEC and
        the World Bank all serve real feeds that a strict XML parser rejects — if that
        collapsed into "returned nothing", a formatting bug and a dead outlet would look
        identical in the status file.
        """
        self.sources[key] = {"status": "parse_fail", "detail": detail[:300]}
        self._streak_parse[key] = self._streak_parse.get(key, 0) + 1

    def source_error(self, key: str, exc: BaseException) -> None:
        self.sources[key] = {"status": "error", "detail": f"{type(exc).__name__}: {exc}"[:300]}
        self._streak_empty[key] = self._streak_empty.get(key, 0) + 1

    # ---- gate counts --------------------------------------------------------

    def gate(self, name: str, n: int) -> None:
        self.counts[name] = n

    def failed(self, exc: BaseException) -> None:
        self.error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        self.error_traceback = traceback.format_exc()[-2000:]

    # ---- health -------------------------------------------------------------

    def stale_sources(self, hours: int = DEAD_SOURCE_HOURS) -> list[str]:
        """Sources silent long enough that a dead feed is likelier than a quiet desk.

        This is the check that would have caught JPost's eighteen dead days. It reports
        only sources we have actually watched go quiet — a source with no recorded
        history is not evidence of anything.
        """
        bad = []
        for key in self.sources:
            last = self.hours_since_key("last_item_at", key)
            if last is not None and last >= hours:
                bad.append(key)
        return sorted(set(bad))

    def hours_since_key(self, field: str, key: str) -> float | None:
        stamp = (self.prev.get(field) or {}).get(key)
        if not stamp:
            return None
        try:
            then = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        return (datetime.now(timezone.utc) - then).total_seconds() / 3600

    def hours_since(self, field: str) -> float | None:
        last = self.prev.get(field)
        if not last:
            return None
        try:
            then = datetime.fromisoformat(last)
        except ValueError:
            return None
        return (datetime.now(timezone.utc) - then).total_seconds() / 3600

    def hours_since_alert(self) -> float | None:
        return self.hours_since("last_alert_at")

    def hours_since_post(self) -> float | None:
        last = self.prev.get("last_posted_at")
        if not last:
            return None
        try:
            then = datetime.fromisoformat(last)
        except ValueError:
            return None
        return (datetime.now(timezone.utc) - then).total_seconds() / 3600

    # ---- persist ------------------------------------------------------------

    def write(self) -> dict:
        finished = datetime.now(timezone.utc)
        last_posted = self.prev.get("last_posted_at")
        if self.posted and self.delivered:
            last_posted = _now()
        # WITHOUT THIS THE ALARM SPAMS. The cloud run polls every five minutes, so an
        # alert with no cooldown posts twelve identical messages an hour. That happened
        # in production on 30 August: this stamp was written, lost in a stash during a
        # push conflict, and the alarm shipped without it.
        last_alert = _now() if self.alerted else self.prev.get("last_alert_at")
        doc = {
            "started_at": self.started,
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_seconds": round((finished - self.t0).total_seconds(), 1),
            # The dispatch interval. Run duration crossing it means runs overlap, which
            # is how the original went from 30-second runs to dying against a ceiling.
            "dispatch_interval_seconds": 900,
            "items": self.counts,
            "posted": self.posted,
            "delivered": self.delivered,
            "last_posted_at": last_posted,
            "last_alert_at": last_alert,
            "sources": self.sources,
            "consecutive_empty": self._streak_empty,
            "consecutive_parse_fail": self._streak_parse,
            "last_item_at": self._last_item,
            "stale_sources": self.stale_sources(),
            "error": self.error,
            "error_traceback": getattr(self, "error_traceback", None),
        }
        STATUS_FILE.write_text(json.dumps(doc, indent=2) + "\n")
        return doc


def summarize(doc: dict) -> str:
    """Human-readable status, for `poll.py --status`."""
    if not doc:
        return "No status recorded yet — the watcher has not run."
    lines = [
        f"last run   {doc.get('started_at')} → {doc.get('finished_at')}",
        f"duration   {doc.get('duration_seconds')}s (dispatch every "
        f"{doc.get('dispatch_interval_seconds', 900) // 60} min)",
        f"items      {doc.get('items')}",
        f"posted     {doc.get('posted')}  delivered={doc.get('delivered')}",
        f"last post  {doc.get('last_posted_at') or 'never'}",
    ]
    if doc.get("stale_sources"):
        lines.append(f"STALE      {', '.join(doc['stale_sources'])}")
    if doc.get("error"):
        lines.append(f"ERROR      {doc['error']}")
    bad = {k: v for k, v in (doc.get("sources") or {}).items() if v.get("status") != "ok"}
    if bad:
        lines.append("unhealthy sources:")
        lines += [f"  {k:22} {v.get('status')}: {v.get('detail', '')[:80]}" for k, v in bad.items()]
    return "\n".join(lines)
