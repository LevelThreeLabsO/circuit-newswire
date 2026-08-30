# circuit-newswire

Watches the publications The Circuit actually reports from, keeps the small fraction that
is Gulf business news, and posts one bundled digest to Slack every 15 minutes. Runs 24/7
on GitHub Actions — it does not depend on any laptop being awake.

A port of the JI Newswire (`JI_HourlyDigest.gs`, 1,879 lines of Apps Script) to The
Circuit's beat, built to the `circuit-newswire-spec` build document. Two things changed
from the original: the source list and the scoring vocabulary. Both are data files.
Everything else is the same machinery, minus the six failure modes catalogued below.

## Why it is not on Apps Script

The original works, but every constraint that made it hard to debug was Apps Script's:
execution logs visible only in the editor UI, stored settings invisible from outside,
deploy credentials expiring every ~40 minutes, and a hard runtime ceiling the system
silently died against. Here logs are `gh run view`, deploys are a git push, secrets are
first-class, and the schedule is a cron line.

## How it decides what matters

Additive keyword scoring, no AI. Four axes, and the weighting is the whole design:

| Axis | Points |
|---|---|
| Named commercial entity (PIF, ADNOC, DP World, Emaar…) | 3 |
| Named principal (MBS, MBZ, Tahnoun, Al-Rumayyan…) | 3 |
| Conflict economics (Hormuz, rerouting, war-risk insurance) | 3 |
| Geography (the GCC, plus Egypt/Jordan/Iraq/Türkiye…) | 2 |
| Sector — business, ventures, technology, science | 2 |
| Money scale (`$`, billion, million, percentages) | 1 |

**Geography is worth less than the threshold on purpose.** Reading 269 of The Circuit's
own story headlines makes the reason plain: of their eleven Egypt stories, ten carry a
business or technology term — a tire factory, an IMF payout, a minerals survey, an
airport terminal, Cairo stocks. Egypt never appears as Egypt. It appears as a venue for
capital. So at the default threshold of 4, "Saudi Arabia arrests cleric" scores 2 and
dies, while "Egypt seeks bids to build a $500 million tire factory" scores 5.

Per-source thresholds do the rest: 3 for AGBI and Zawya, which publish nothing off-beat;
6 for Bloomberg, Reuters, the two Gulf consumer dailies and Argaam's market-disclosure
firehose.

Measured against fixtures on every change (`poll.py --selftest`): **94.8% recall** on
Circuit's real story headlines, **8.3%** pass rate on a curated negative set of Gulf
tabloid and general-wire headlines.

### The limitation, stated up front

This is literal word matching. It cannot tell how central a subject is, cannot tell
reporting from commentary about reporting, and cannot catch a story that avoids its
vocabulary. Every gap gets closed by adding a word after a miss. `judge:` in
`scoring.yaml` is a dormant seam — setting it true fails loudly rather than pretending —
where a Gemini relevance pass would drop in, following
`ji-govt-watcher/src/classifier.py`. The right time to reconsider is when the word list
starts feeling like a treadmill, not after 165 terms accumulate.

## Sources

The list is not a guess about who covers the Gulf. Every outbound link in 300 Circuit
posts was counted, and in their own original stories they rely on Bloomberg (68 links),
Reuters (28), The National (24), AGBI (19), FT (11), Zawya (11), Arab News (9), WSJ (7),
NYT (5) and WAM (4). Eleven plausible-looking trade outlets — OilPrice, Splash247,
Offshore Energy, PV Magazine, Al-Monitor, MEMO, Al Jazeera, Wamda, Globes, Calcalist,
Times of Israel — were cited **zero times between them** and are deliberately absent.

Three discovery methods, set per source in `sources.yaml`:

- `rss` — a native feed. AGBI, The National (Business), Arabian Business, Forbes Middle
  East, FT Middle East, Intelligence Online.
- `gnews` — site-scoped Google News, for outlets with no usable feed: Bloomberg, Reuters,
  Zawya, Arab News, WAM, SPA, Argaam, Khaleej Times, Gulf News, MEED, Semafor, WSJ, NYT,
  The Peninsula. Links are Google redirects — they open the real article and are stable
  dedup keys, but they are not the pretty URL.
- `gnews_entity` — entity-scoped, not restricted to any site: the sovereign funds, the
  energy champions, ports and logistics, giga-projects, Gulf tech, and Hormuz. This is
  how a PIF stake surfaces when nobody Gulf-side has written it up yet.

**Audit the sources monthly.** A dead feed answers HTTP 200 and serves a web page, so
nothing else will ever notice:

```bash
python3 poll.py --audit
```

The first audit of this list caught four problems that would all have been silent: Arab
News' native feed serving items 16 days stale, Argaam serving HTML at every documented
feed path, and WAM and SPA being rejected 100% by the publisher check because Google
labels them with their Arabic mastheads while returning the English editions.

## Operating it

```bash
python3 poll.py                            # a real run (needs SLACK_WEBHOOK_URL)
python3 poll.py --dry-run                  # print the digest, post nothing, touch no state
python3 poll.py --dry-run -v --window-hours 3   # …and show what was dropped, and why
python3 poll.py --dry-run --source agbi    # one source, end to end
python3 poll.py --audit                    # feed health for every source
python3 poll.py --selftest                 # scoring recall + noise against fixtures
python3 poll.py --score "some headline"     # score one headline, see which axes hit
python3 poll.py --test-webhook             # assert Slack answers `ok`
python3 poll.py --status                   # what happened on the last run
```

`--dry-run` never posts and never writes state. Local runs need the venv:
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

### Tuning

`sources.yaml` and `scoring.yaml` are re-read every run, so an edit takes effect on the
next tick. After any scoring change run `--selftest`: it reports recall *and* noise, so a
word you add to close one miss shows its cost as well as its benefit. Regenerate the
fixtures from live data with `python3 tests/regen_fixtures.py`.

## Observability

There is no server, and none is needed. `status.json` is committed on every run, so on a
public repo the raw URL **is** the status endpoint:

```
https://raw.githubusercontent.com/LevelThreeLabsO/circuit-newswire/main/status.json
```

It carries the last run's start and finish, duration against the 15-minute dispatch
interval, per-source outcomes, counts at each gate, whether Slack accepted the post, the
last error with traceback, and per-source streaks of empty or unparseable responses.

This file exists because of how the original failed. It had no way to report its own
state, so when it broke every diagnosis was an inference from what was missing in Slack.
Three consecutive diagnoses were wrong; the real cause was found minutes after a status
endpoint finally existed.

The watcher also reports on itself in-channel: a **silence alarm** if nothing has posted
in six hours, and a **feed-health line** naming any source quiet or unparseable for 48
hours — the check that would have caught JPost's eighteen dead days in the original.

## The six failure modes this is built against

All six ran silently in production in the original. None announced itself.

1. **Fire-and-forget delivery.** Posting without checking the response. When the webhook
   was revoked, runs reported success into a dead endpoint for eight days. Here a post is
   not delivered unless Slack's body is literally `ok`.
2. **Marking items sent before they are sent.** State was saved before the post, so a
   delivery outage consumed eight days of coverage rather than queueing it. Here keys are
   claimed before sending *and rolled back* if Slack refuses.
3. **A store that outgrows its container.** Keyed on full URLs against a 9KB ceiling, the
   original's memory filled after 26 items and then silently failed to save, so every run
   started amnesiac. Here keys are 16-character hashes.
4. **Per-item service calls.** Two cache lookups per item became ~14,000 calls per run and
   dragged runs to 25 minutes, until nine more sources pushed them past the runtime
   ceiling. Here state is read once per run and duration is logged every run.
5. **Concurrency guards.** Two outages caused by the guard rather than by concurrency: a
   real lock wedged when a run was killed holding it, and its self-expiring replacement
   turned away two scheduled runs. There is no lock here — the workflow serializes runs,
   and claim-before-send already prevents double posting.
6. **Orphaned schedules.** A colleague's trigger kept firing after they left, duplicating
   every post, and could not be deleted from another account. This lives in the
   LevelThreeLabsO org, not a personal account, and the schedule is a line in a file.

## One poller at a time

Cadence comes from GitHub Actions alone. A local launchd agent ran alongside it for two
days as a stopgap during a GitHub outage, and it caused a duplicate post: the cloud
claimed and sent a Gulf News story at 14:55:13, the Mac claimed and sent the same story
at 14:56:45, because the local run read the shared state a moment before the cloud's
claim reached git.

Claim-before-send protects overlapping runs of one poller, which exchange state within a
single git remote and a single concurrency group. It cannot protect two independent
schedulers whose only channel is a push that lands seconds later. So: run one. The agent
is unloaded, and the plist is kept only for emergencies:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jewishinsider.circuit-newswire.plist
launchctl bootout   gui/$(id -u)/com.jewishinsider.circuit-newswire
```

## Setup

1. Slack incoming webhook → repo secret `SLACK_WEBHOOK_URL`. Never in code: the original
   hard-coded it in five files and one rotation took the whole system down. Paste it with
   no trailing whitespace — a stray newline fails identically to a revoked token.
2. A cron-job.org job hitting this repo's `workflow_dispatch` API every 15 minutes, the
   same pattern as the article watchers. GitHub's own `*/15` schedule is in the workflow
   as a laggy fallback.
3. First run baselines silently — it records what is currently in the feeds and posts a
   single "watcher is live" message rather than dumping the backlog.

## Layout

```
poll.py              one run: fetch → score → dedup → digest → post → status
sources.yaml         who to read, per-source thresholds and freshness windows
scoring.yaml         the vocabulary, the noise vetoes, the byline bypass
audit_sources.py     is every feed alive, fresh and parseable?
src/fetch.py         three discovery methods, tolerant parsing, publisher verification
src/score.py         the four-axis scorer and the dormant judge seam
src/dedup.py         URL hash, headline hash, cross-outlet title overlap
src/digest.py        the bundled message format and the preview rules
src/state.py         git-committed dedup state, merged across overlapping runs
src/status.py        status.json
src/slack_client.py  delivery that fails loudly
tests/selftest.py    recall and noise gates
```
