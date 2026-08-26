"""Persistent state: what we have posted, what we have seen, what we posted recently.

Descended from `ji-govt-watcher/src/state.py` — the state file lives in the repo and the
Actions run commits it back, so dedup survives across single-shot cloud runs. All git
work is best-effort: outside a repo (local dry-runs) it no-ops and just uses the file.

Three stores, each answering a different question:

  seen        {hash: iso}  — already posted. Keyed by BOTH a URL hash and a headline
                             hash for every item, because outlets revise a story and
                             reissue it on a new link; URL-only matching posted one WSJ
                             piece three times.
  first_seen  {hash: iso}  — when an item with no usable publish date was first sighted.
                             Undated items pass the freshness gate forever otherwise,
                             which means they repost forever.
  titles      [{...}]      — recently posted headlines, for cross-outlet dedup: four
                             outlets filing their own version of the same story.

Keys are hashes, never URLs. A Google News search URL runs 330 characters, and the
original's store — keyed on full URLs against a 9KB ceiling — filled after 26 items and
then silently failed to save, so every run started amnesiac.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent.parent / "watcher_state.json"
REPO = STATE_FILE.parent

SEEN_DAYS = 7
FIRST_SEEN_DAYS = 7
TITLE_HOURS = 3          # cross-outlet window; see dedup.py for why it is this short
TITLE_MAX = 150

GIT_ID = [
    "-c", "user.name=github-actions[bot]",
    "-c", "user.email=github-actions[bot]@users.noreply.github.com",
]


def key_hash(value: str) -> str:
    """Short stable key. 16 hex chars is ~1e-10 collision risk at our volumes."""
    return hashlib.sha1(value.strip().lower().encode("utf-8")).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def blank() -> dict:
    return {"seen": {}, "first_seen": {}, "titles": []}


def _coerce(doc: object) -> dict:
    if not isinstance(doc, dict):
        return blank()
    out = blank()
    for k in ("seen", "first_seen"):
        v = doc.get(k)
        if isinstance(v, dict):
            out[k] = {str(a): str(b) for a, b in v.items()}
    v = doc.get("titles")
    if isinstance(v, list):
        out["titles"] = [t for t in v if isinstance(t, dict) and t.get("words")]
    return out


def exists() -> bool:
    return STATE_FILE.exists()


def load() -> dict:
    if not STATE_FILE.exists():
        return blank()
    try:
        return _coerce(json.loads(STATE_FILE.read_text()))
    except Exception:
        return blank()


def prune(state: dict) -> dict:
    """Drop what has aged out. The freshness gate is the primary guard against
    reprocessing old items; this just stops the file growing without bound."""
    now = _now()
    for store, days in (("seen", SEEN_DAYS), ("first_seen", FIRST_SEEN_DAYS)):
        cutoff = now - timedelta(days=days)
        state[store] = {
            k: v for k, v in state[store].items()
            if (_parse_iso(v) or now) >= cutoff
        }
    tcut = now - timedelta(hours=TITLE_HOURS)
    titles = [t for t in state["titles"] if (_parse_iso(t.get("at", "")) or now) >= tcut]
    state["titles"] = titles[-TITLE_MAX:]
    return state


def merge(a: dict, b: dict) -> dict:
    """Union two states, keeping the earliest timestamp per key.

    Earliest, not latest, on purpose: `first_seen` means first, and for `seen` the older
    stamp is the one that ages the entry out correctly.
    """
    out = blank()
    for store in ("seen", "first_seen"):
        out[store] = dict(a.get(store, {}))
        for k, v in b.get(store, {}).items():
            if k not in out[store] or v < out[store][k]:
                out[store][k] = v
    combined = {(t.get("at"), t.get("words")): t for t in a.get("titles", []) + b.get("titles", [])}
    out["titles"] = sorted(combined.values(), key=lambda t: t.get("at", ""))[-TITLE_MAX:]
    return out


# ---- claiming ---------------------------------------------------------------

def claim(state: dict, keys: list[str]) -> None:
    """Mark keys as posted.

    Called BEFORE the digest is sent, never after. Marking afterwards means two
    overlapping runs both read an empty store, both build the same list, and both send
    it — and if delivery then fails, marking beforehand is what keeps a whole day of
    coverage from being consumed rather than queued (the caller rolls back on failure).
    """
    stamp = _iso(_now())
    for k in keys:
        state["seen"].setdefault(k, stamp)


def unclaim(state: dict, keys: list[str]) -> None:
    """Give keys back after a delivery failure, so the next run resends them."""
    for k in keys:
        state["seen"].pop(k, None)


def stamp_first_seen(state: dict, key: str) -> datetime:
    """Record (or read back) when an undated item was first sighted."""
    existing = _parse_iso(state["first_seen"].get(key, ""))
    if existing:
        return existing
    now = _now()
    state["first_seen"][key] = _iso(now)
    return now


def remember_title(state: dict, words: list[str], outlet: str) -> None:
    state["titles"].append({"words": sorted(words), "at": _iso(_now()), "outlet": outlet})


# ---- git ---------------------------------------------------------------------

def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=30
    )


def _is_git_repo() -> bool:
    return _git("rev-parse", "--is-inside-work-tree").returncode == 0


def save(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(prune(state), indent=1, sort_keys=True) + "\n")


def latest() -> dict:
    """Freshest committed state from origin, unioned with local, so overlapping runs
    don't repost. Falls back to the local file outside a git repo."""
    local = load()
    if not _is_git_repo():
        return local
    _git("fetch", "-q", "origin", "main")
    r = _git("show", "origin/main:watcher_state.json")
    if r.returncode != 0:
        return local
    try:
        return merge(local, _coerce(json.loads(r.stdout)))
    except Exception:
        return local


def record(state: dict, files: tuple[str, ...] = ("watcher_state.json", "status.json")) -> None:
    """Persist locally and, inside a git repo, commit + push. Retries on a concurrent
    push race by re-merging against the new origin."""
    save(state)
    if not _is_git_repo():
        return
    for _ in range(5):
        _git("fetch", "-q", "origin", "main")
        remote = _git("show", "origin/main:watcher_state.json")
        merged = state
        if remote.returncode == 0:
            try:
                merged = merge(state, _coerce(json.loads(remote.stdout)))
            except Exception:
                pass
        # Keep our status.json across the reset — it describes the run that just happened.
        status_path = REPO / "status.json"
        status_body = status_path.read_text() if status_path.exists() else None
        _git("reset", "-q", "--hard", "origin/main")
        save(merged)
        if status_body is not None:
            status_path.write_text(status_body)
        _git("add", *files)
        if _git("diff", "--cached", "--quiet").returncode == 0:
            return
        _git(*GIT_ID, "commit", "-q", "-m", "Update newswire state [skip ci]")
        if _git("push", "-q", "origin", "HEAD:main").returncode == 0:
            return
        # Lost the race — loop and re-merge against the updated origin.
