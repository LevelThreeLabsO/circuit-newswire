#!/usr/bin/env python3
"""Post the three-times-daily briefing: five stories an editor should not miss.

    python3 brief.py --dry-run        print it, post nothing, touch no state
    python3 brief.py --dry-run --window-hours 24
    python3 brief.py --explain        show the full ranking and why each scored
    python3 brief.py                  post now, whatever the clock says
    python3 brief.py --if-due         post only if a 6am/12pm/6pm ET edition is unsent

Reads posted_log.json — what the newswire actually sent — not the Slack channel, so it
needs no read access to anyone's workspace and cannot disagree with what was posted.

Posts to its OWN webhook, a separate channel from the newswire. That separation is
deliberate: the newswire's channel gets stories and nothing else.

Editions are 6am, midday and 6pm Eastern. With --if-due the check rides poll.yml's
15-minute cadence: it asks "is an edition due and unsent?" and exits silently otherwise,
so no separate trigger is needed. The window is measured from the previous briefing
rather than a fixed number of hours, so a missed run is covered by the next one instead
of leaving a gap.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from src import brief, postlog, state
from src.slack_client import DeliveryError, SlackClient

load_dotenv()

# Used only when there is no record of a previous briefing (the first run, or a fresh
# clone). Slightly more than the longest gap between editions — 6pm to 6am — so the first
# briefing has a full period to work with rather than a sliver.
DEFAULT_WINDOW_HOURS = 13


def window_hours(entries: list[dict], override: float | None) -> float:
    if override:
        return override
    last = postlog.last_brief_at(entries)
    if not last:
        return DEFAULT_WINDOW_HOURS
    try:
        then = datetime.fromisoformat(last)
    except ValueError:
        return DEFAULT_WINDOW_HOURS
    if not then.tzinfo:
        then = then.replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - then).total_seconds() / 3600
    # Floor at one hour so a double-fire cannot produce an empty briefing, and cap at a
    # day so a long outage does not dredge up stale stories.
    return max(1.0, min(hours, 24.0))


def main() -> int:
    p = argparse.ArgumentParser(description="Five stories an editor should not miss")
    p.add_argument("--dry-run", action="store_true", help="print it; post nothing")
    p.add_argument("--window-hours", type=float, default=None,
                   help="override the period (default: since the last briefing)")
    p.add_argument("--explain", action="store_true", help="show the full ranking")
    p.add_argument("--if-due", action="store_true",
                   help="post only if an edition (6am/12pm/6pm ET) is due and unsent")
    args = p.parse_args()

    entries = postlog.load()

    # --if-due lets the briefing ride the newswire's 15-minute cadence instead of needing
    # its own triggers. That matters because the Apps Script briefing triggers were never
    # installed — setup ran a minute before the code containing them was pushed — so no
    # scheduled edition ever fired, while the newswire's own trigger ran 36 times out of
    # 40. Riding a mechanism proven to work beats adding a second one to maintain.
    edition = None
    if args.if_due:
        current = brief.current_edition()
        if current is None:
            print("Before the first edition of the day; nothing due.")
            return 0
        edition, label = current
        if postlog.edition_posted(entries, edition):
            print(f"{edition} already posted; nothing due.")
            return 0
        print(f"{edition} is due.")

    hours = window_hours(entries, args.window_hours)
    period = postlog.since(entries, hours)
    stories = [e for e in period if e.get("kind") != "brief"]

    print(f"[{'DRY-RUN' if args.dry_run else 'LIVE'}] {len(stories)} stories in the "
          f"last {hours:.1f}h")

    if not stories:
        # Silence, not a message saying there is nothing. An empty briefing is noise, and
        # the newswire's own channel already shows the desk that the wire is quiet.
        print("Nothing to brief.")
        return 0

    ranked = brief.rank(stories)

    if args.explain:
        print(f"\n{'weight':>7}  {'corr':>4}  outlet / headline")
        for e in ranked:
            print(f"{e['weight']:7.2f}  {e.get('corroboration', 0):4}  "
                  f"{(e.get('outlet') or '')[:18]:20} {(e.get('title') or '')[:64]}")
        print()

    shortlist = ranked[:brief.SHORTLIST]
    chosen, how = brief.choose(shortlist)
    if not chosen:
        print("Nothing survived selection.")
        return 0

    label = brief.current_edition()[1] if brief.current_edition() else None
    text = brief.format_brief(chosen, how, hours, len(stories), label=label)
    print(f"\nselection: {how}\n")
    print(text)

    if args.dry_run:
        print("\n[DRY-RUN] not posted; log untouched.")
        return 0

    webhook = os.environ.get("SLACK_BRIEF_WEBHOOK_URL")
    if not webhook:
        sys.exit("SLACK_BRIEF_WEBHOOK_URL is not set. Use --dry-run to inspect.")

    slack = SlackClient(webhook_url=webhook)
    try:
        slack.post(text)
    except DeliveryError as e:
        # Do NOT mark the briefing as done: the next run should cover this period rather
        # than skip it. Same rule as the newswire's claim-before-send rollback.
        print(f"DELIVERY FAILED: {e}", file=sys.stderr)
        return 1

    # Mark it done only after Slack accepted, then commit the log so the next run — in a
    # fresh checkout — knows when this edition went out and covers the right period.
    postlog.mark_brief(entries, len(chosen), edition=edition)
    postlog.save(entries)
    state.record(state.load(), files=("watcher_state.json", "status.json", "posted_log.json"))
    print(f"\nPosted {len(chosen)} item(s) by {how}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
