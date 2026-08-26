"""Fetching and parsing candidate items.

Three discovery methods, chosen per source in sources.yaml:

  rss          A native feed. Preferred: real URLs, real timestamps, real teasers.
  gnews        Site-scoped Google News search, for outlets with no usable feed —
               Bloomberg, Reuters, Zawya, WAM, Khaleej Times, Gulf News, MEED, Arab News.
               Roughly half The Circuit's own sourcing backbone falls in here.
  gnews_entity Entity-scoped Google News search, not restricted to one site: PIF,
               Mubadala, ADNOC, DP World and the rest of the roster, caught wherever
               they surface. This has no counterpart in the JI original.

Three things this module does that a naive port would not:

1. **Parses tolerantly, and reports parse failure as its own state.** Fitch, Argaam,
   ADX, Treasury, OPEC and the World Bank all serve real feeds that a strict XML parser
   rejects outright. Under `xml.etree` those come back as zero items — indistinguishable
   from a dead outlet. feedparser recovers them, and when it cannot, the caller hears
   "parse failure", not "nothing published".

2. **Verifies the publisher on every Google News item.** Google honours `site:` loosely
   and will return other domains. Unchecked, a foreign story arrives wearing the wrong
   outlet's label.

3. **Strips the appended publisher name before anything reads the headline.** Google
   News titles end in " - Publisher". The original hard-coded `" - Times of Israel"`
   while the feed actually sent `" - The Times of Israel"`, so every headline carried
   the outlet name twice for months. Here the suffix comes from the item's own source
   element, never from a hand-typed string.
"""
from __future__ import annotations

import html
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import feedparser
import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
TIMEOUT = 25
PER_SOURCE_CAP = 80        # items considered per source per run
DEFAULT_WINDOW_MINUTES = 25
MAX_WORKERS = 10


class ParseFailure(RuntimeError):
    """The source answered, but what it served could not be read as a feed."""


@dataclass
class Item:
    source_key: str
    outlet: str
    title: str
    url: str
    published: datetime | None
    body: str = ""
    author: str = ""
    desk: str | None = None
    category: str = "gulf"
    method: str = ""
    paywall: bool = False
    threshold: int | None = None
    # Set by poll.py for undated items, from the persisted first-seen stamp.
    effective_date: datetime | None = None
    score: int = 0
    axes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------- text helpers

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def clean(text: str) -> str:
    """Strip tags and entities, normalise the smart-quote mojibake feeds emit."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\x92", "’").replace("\x93", "“").replace("\x94", "”")
    return re.sub(r"\s+", " ", text).strip()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _entry_date(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def canonical(url: str) -> str:
    """Dedup-stable form: no fragment, no query, no trailing slash."""
    u = (url or "").strip().split("#")[0].split("?")[0]
    return u.rstrip("/").lower()


# ------------------------------------------------------------------ http + parse

def _get(url: str, attempts: int = 3) -> requests.Response:
    """GET with a browser UA — Gulf sites and gov feeds 403 short ones.

    Retries only what a retry can fix: connection resets, timeouts, 429 and 5xx (Google
    News does these when hit rapidly). A 404 will not heal, so other 4xx raise at once.
    """
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
        else:
            if r.status_code != 429 and r.status_code < 500:
                r.raise_for_status()
                return r
            last = requests.HTTPError(f"HTTP {r.status_code} from {url}")
        if i < attempts - 1:
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


def parse_feed(body: bytes):
    """Parse a feed body, tolerating malformed markup.

    Raises ParseFailure only when nothing usable came back at all — a feed that is
    merely untidy (feedparser sets `bozo` but still yields entries) is fine, and saying
    otherwise would flag half the Gulf financial press as broken.
    """
    parsed = feedparser.parse(body)
    if not parsed.entries:
        detail = str(getattr(parsed, "bozo_exception", "") or "no entries")
        head = body[:400].decode("utf-8", "replace").lower()
        if "<html" in head:
            detail = f"served an HTML page, not a feed ({detail})"
        raise ParseFailure(detail)
    return parsed


# --------------------------------------------------------------------- gnews bits

def _gnews_url(query: str, days: int) -> str:
    q = quote(f"{query} when:{days}d", safe=":()+|\"")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def _gnews_publisher(entry) -> str:
    src = getattr(entry, "source", None)
    if isinstance(src, dict):
        return clean(src.get("title", ""))
    return clean(getattr(src, "title", "") if src else "")


def _strip_publisher(title: str, publisher: str) -> str:
    """Remove the ' - Publisher' Google News appends, using the publisher the item
    itself declared. Falls back to dropping any short trailing ' - X' segment.

    This runs before scoring, not just before display: a masthead containing a beat word
    ('Arabian Business', 'Gulf News') would otherwise score on the masthead alone.
    """
    t = title.strip()
    t = re.sub(r"^\[\d{4}-\d{2}-\d{2}\]\s*", "", t)
    if publisher and t.lower().endswith(f" - {publisher.lower()}"):
        return t[: -(len(publisher) + 3)].strip()
    return re.sub(r"\s+-\s+[^-]{3,40}$", "", t).strip()


# The Circuit's own domain, excluded from every entity query at the query itself. A
# newswire that hands the desk their own published stories is worse than useless — it
# looks like a source they missed. Sister JI properties are not excluded: they are not
# on this beat and will not surface.
SELF_DOMAINS = ("circuit.news",)

# Publishers never worth reading, regardless of query. Entity queries are not restricted
# to a site — that is the point of them — so a query for NEOM or Diriyah pulls in football
# fixtures and betting tipsters, both of which name Saudi clubs constantly, and a query
# for Qatar Airways pulls in embassy travel notices and content-farm spam.
# Publishers never worth reading. The first three groups are what an unrestricted entity
# query actually drags in, measured rather than imagined: a live query on the ports and
# airlines roster came back 24 of 26 items junk.
PUBLISHER_BLOCKLIST = (
    # Betting and sport — a query for NEOM or Diriyah hits Saudi football clubs.
    "22bet", "1xbet", "betway", "bet365", "sportskeeda", "onefootball", "goal.com",
    "footballcritic", "soccerway", "flashscore", "sofascore", "betting", "tipster",
    "predictions", "fantasy football", "dream11", "today's golfer", "heavy.com",
    "betpawa", "parimatch", "melbet", "stake.com", "oddspedia", "forebet",
    # User-generated and social platforms, which Google News indexes as publishers.
    "facebook", "instagram", "x.com", "twitter", "reddit", "youtube", "linkedin",
    "medium.com", "tiktok", "pinterest", "quora", "substack.com",
    # SEO farms that impersonate airline support desks. These host on legitimate-looking
    # domains — IEEE community pages, waiver forms, health portals — so the publisher name
    # is the only usable signal.
    "ieee", "smartwaiver", "lucent health", "eventbrite", "wixsite", "weebly",
    "blogspot", "wordpress.com", "groups.google", "issuu", "slideshare", "scribd",
    # Aggregators and notice boards seen in live output.
    "mshale", "news on air", "akashvani", "migflug", "travelobiz", "visaguide",
    # Job boards and sport streamers — a query for Talabat returns vacancies, one for
    # Roshn returns Saudi League fixtures.
    "naukrigulf", "bayt.com", "gulftalent", "indeed", "glassdoor", "fancode",
    "tipranks", "foundit", "monster gulf", "laimoon", "jobsora", "talent.com",
)

# Content-farm and support-desk titles. Every pattern here was taken from real output.
_SPAM_TOKEN = re.compile(r"\([A-Za-z0-9]{6,}\)")          # 'Qatar Airways (jbsnBKnqgh)'
_SPAM_TITLE = re.compile(
    r"(?:\+?\d[\d\-\(\)\s]{8,}\d)"                        # a phone number
    r"|\{\{|\}\}|\(\("                                     # template braces
    r"|complete guide|customer (?:service|support|care)|contact support"
    r"|book (?:flight|ticket)|booking number|extra bag|baggage allowance"
    r"|flight status|change flight|refund policy|check-?in online"
    r"|tickets? link|save on extra|days to go until",
    re.IGNORECASE,
)


def looks_like_spam(title: str) -> bool:
    """Is this a content farm rather than a story?

    Consumer-facing Gulf brands — the airlines above all — attract a large volume of
    fake support-desk pages that Google News indexes as news. They score well on the
    keyword axes (a real airline name, a real country, real business words), so scoring
    cannot catch them and this has to.
    """
    if _SPAM_TITLE.search(title):
        return True
    match = _SPAM_TOKEN.search(title)
    if not match:
        return False
    token = match.group(0)[1:-1]
    return any(c.islower() for c in token) and any(c.isupper() or c.isdigit() for c in token)


def _publisher_matches(publisher: str, accept: list[str]) -> bool:
    """Google honours site: loosely. Accept only the outlet we asked for."""
    p = publisher.lower()
    if any(b in p for b in PUBLISHER_BLOCKLIST):
        return False
    if not accept:
        return True
    return any(a.lower() in p or p in a.lower() for a in accept if a)


def is_english(title: str) -> bool:
    """Rough script check on a headline.

    Several Gulf sources are labelled by Google with their Arabic masthead — WAM, SPA and
    Argaam all come back as وكالة وام and friends — while the items themselves are the
    English editions, so the publisher name says nothing about the language. This guards
    the other case: a source flipping to its Arabic edition, the way Globes returns 83%
    Hebrew, would otherwise fill the digest with headlines the desk cannot scan.
    """
    letters = [c for c in title if c.isalpha()]
    if not letters:
        return True
    return sum(1 for c in letters if c.isascii()) / len(letters) >= 0.5


# ------------------------------------------------------------------- per method

def _mk(source: dict, title: str, url: str, published: datetime | None,
        body: str, author: str = "") -> Item:
    return Item(
        source_key=source["key"],
        outlet=source.get("outlet") or source["key"],
        title=title,
        url=url,
        published=published,
        body=body,
        author=author,
        desk=source.get("desk"),
        category=source.get("category", "gulf"),
        method=source["method"],
        paywall=bool(source.get("paywall")),
        threshold=source.get("threshold"),
    )


def _fetch_rss(source: dict, since: datetime) -> tuple[list[Item], int]:
    parsed = parse_feed(_get(source["url"]).content)
    items: list[Item] = []
    for e in parsed.entries[:PER_SOURCE_CAP]:
        published = _aware(_entry_date(e))
        if published and published < since:
            continue
        link = (getattr(e, "link", "") or "").strip()
        title = clean(getattr(e, "title", ""))
        if not link or not title:
            continue
        body = clean(getattr(e, "summary", "") or getattr(e, "description", ""))[:800]
        author = clean(getattr(e, "author", ""))
        items.append(_mk(source, title, link, published, body, author))
    return items, len(parsed.entries)


def build_query(source: dict) -> tuple[str, list[str]]:
    """The exact Google News query for a source, plus its accepted publishers.

    Shared with audit_sources.py deliberately. When the audit built its own version of
    the query it tested something the runtime never sends — and Google is sensitive
    enough to phrasing that the two disagreed completely: the bare listings query
    returned zero entries while the runtime's, with the self-exclusion appended,
    returned a hundred. An audit that tests a different string than production sends is
    worse than no audit, because it produces confident wrong answers in both directions.
    """
    if source["method"] == "gnews":
        query = f"site:{source['url']}"
        if source.get("query"):
            query += f" {source['query']}"
        return query, (source.get("publisher") or [source.get("outlet", "")])

    # gnews_entity — not site-restricted, that being the point of it. The Circuit's own
    # domain is excluded here rather than per source so a new query cannot forget to.
    exclusions = " ".join(f"-site:{d}" for d in SELF_DOMAINS)
    return f"{source['query']} {exclusions}", []


def _fetch_gnews(source: dict, since: datetime) -> tuple[list[Item], int]:
    days = max(1, math.ceil((now_utc() - since).total_seconds() / 86400))
    query, accept = build_query(source)

    parsed = parse_feed(_get(_gnews_url(query, days)).content)
    items: list[Item] = []
    for e in parsed.entries[:PER_SOURCE_CAP]:
        published = _aware(_entry_date(e))
        if published and published < since:
            continue
        link = (getattr(e, "link", "") or "").strip()
        raw_title = clean(getattr(e, "title", ""))
        if not link or not raw_title:
            continue
        publisher = _gnews_publisher(e)
        if not _publisher_matches(publisher, accept):
            continue  # Google leaked another domain into a site: query
        title = _strip_publisher(raw_title, publisher)
        if not title or not is_english(title) or looks_like_spam(title):
            continue
        if any(d in link.lower() for d in SELF_DOMAINS):
            continue  # belt and braces: the query already excludes these
        item = _mk(source, title, link, published, "")
        # For an entity query the reporting outlet is whoever filed it, not the query.
        if source["method"] == "gnews_entity" and publisher:
            item.outlet = publisher
        items.append(item)
    return items, len(parsed.entries)


_METHODS = {"rss": _fetch_rss, "gnews": _fetch_gnews, "gnews_entity": _fetch_gnews}


def window_start(source: dict, now: datetime, override_hours: float | None = None) -> datetime:
    """Freshness cutoff for one source.

    Per-source windows matter more than they sound. The gate assumes a source publishes
    to its feed the moment a story goes live; magazines and Google News-sourced feeds
    lag hours, so a single tight window silently rejects everything they ever publish.
    """
    if override_hours:
        return now - timedelta(hours=override_hours)
    minutes = source.get("window_minutes", DEFAULT_WINDOW_MINUTES)
    return now - timedelta(minutes=minutes)


def fetch_source(source: dict, since: datetime) -> tuple[list[Item], int]:
    """Fetch one source. Returns (items in window, entries seen).

    Raises ParseFailure or a requests error; poll.py catches per source so one bad feed
    cannot sink the run — the project's oldest guarantee.
    """
    fn = _METHODS.get(source["method"])
    if fn is None:
        raise ValueError(f"unknown method {source['method']!r} for {source.get('key')!r}")
    items, seen = fn(source, since)
    return [i for i in items if i.title and i.url], seen


def fetch_all(sources: list[dict], now: datetime, override_hours: float | None = None):
    """Fetch every source concurrently. Yields (source, items, entries, exception).

    Threaded because a sequential pass over sixty feeds took the original four minutes,
    against a fifteen-minute dispatch interval.
    """
    def one(source):
        try:
            items, seen = fetch_source(source, window_start(source, now, override_hours))
            return source, items, seen, None
        except Exception as e:  # noqa: BLE001 — reported per source, never fatal
            return source, [], 0, e

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        yield from pool.map(one, sources)
