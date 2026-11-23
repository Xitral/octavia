# Its Claude usage budget

Its own background work runs on your Claude subscription: the night shift (03:00), the builds it starts by itself,
its real-request check, and its nightly reflection (03:30). On 2026-10-08 these jobs, together with a few Claude Code
sessions, used up the subscription's limit overnight. With extra usage off, work stopped partway through. The budget
keeps that background work small enough that you always have room to talk to it.

## What it measures

Every Claude Code turn reports the account's limits (a `rate_limit_event`). That's the same pair of numbers `/usage`
shows in Claude Code:

- the **5-hour window**: how full it is (0 to 100%) and when it resets
- the **weekly limit**: how full it is and when it resets (a per-model weekly window counts if it's fuller)
- a status: `allowed`, `allowed_warning` (close to a limit) or `rejected` (a limit is hit)

They are your limits, shared with your own Claude Code sessions. Nothing is estimated from tokens or prices. Every
report from its brain, the quick voice, the builder and the eval runs' subprocess is appended to
`data/usage/observations.jsonl`.

## What it does with it

Before each background job it checks the latest report, and the job is skipped when:

- a limit is hit or near, until that window resets
- your 5-hour window is at least `max_five_hour` full (60%), or your weekly limit at least `max_weekly` (80%)
- today's background jobs already used `daily_share` of your weekly limit (5%)

A job's use is how much the weekly window grew while it ran. Jobs that run side by side are counted once. Anything
else that ran at the same time (your own Claude Code, a conversation with it) is counted too, so the estimate is on
the high side and never on the low side.

What's checked and what isn't:

| Job | Checked | When it's skipped |
| --- | --- | --- |
| Night-shift fixes, and fixes for errors or notes it builds by itself (`auto_build`) | when queued, and again as each one starts | it goes back to "noted", and you can still ask for it. If it'd said it was building it, it tells you it didn't |
| Its real-request check at night | before it runs | the night's record says so |
| Its nightly reflection | before it runs | recorded as skipped. One you ask for ("think it over") is never held back |
| Anything you ask it yourself: a conversation, a build, a background task, a scheduled job | never | never |

Each skip is logged once a day per job in `data/usage/skips.jsonl`, and the morning report includes it: "To leave your
Claude usage for you, I skipped my nightly reflection (your 5-hour Claude window is 72% used, ...)".

## Seeing it

- `uv run octavia usage` shows your limits now, its jobs and what each used, and what it skipped over the last
  7 days (`--days N` to change that)
- Ask it "how's my Claude usage?" (the `claude_usage` tool)

## Settings

```yaml
usage_budget:
  enabled: true
  daily_share: 0.05    # her background share per day, of your WEEKLY limit; 0 = no background Claude work at all
  max_five_hour: 0.6   # wait while your 5-hour window is at least this full
  max_weekly: 0.8      # ...or your weekly limit is
```

`src/octavia/core/usage.py` and its tests are protected (`evolve/policy.py`), so a build can't raise its own allowance.

## Limits of this design

- A build that's already running isn't stopped partway through. The checks happen between jobs. A single build
  (with its eval gate) can use a few percent of the week.
- With no recent report, the first Claude call of a job isn't counted (there's no starting point before it).
- Reports are rounded to whole percents and come only when a number changes, so the last bit of a job's use often
  shows up in the next reading and isn't charged to it. A small job can show as 0%.
- A reflection skipped for the budget isn't retried later that night.
- Builds it queued by itself are checked on the last reading when queued (no fresh one: that can happen inside a
  conversation turn), then on a fresh reading as each one starts.
