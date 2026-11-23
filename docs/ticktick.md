# TickTick

OCTAVIA can see and manage your TickTick: tasks, lists, habits, focus sessions and countdowns. Ask its things like
"what's on my plate today?", "add 'buy milk' to Groceries", "move the dentist task to Friday", "did I do my
reading habit today?" or "how much did I focus this week?". It also tells you out loud when a task comes due, and
what's due each morning.

## Setting it up

1. Make an API token in TickTick: on the web (ticktick.com), click your avatar, then **Settings**, then
   **Account & Security**, then **API Token**, and create one.
2. Add it to the `.env` file in the project folder (never to a YAML file):

   ```
   TICKTICK_API_TOKEN=paste-the-token-here
   ```

3. Restart the assistant.

The first start with a token asks TickTick which tools it offers and saves the list in `data/ticktick/tools.json`;
later starts use that list at once and refresh it in the background (a change shows up after the next restart).

## What it asks before doing

Every TickTick action goes through the same permission rules as its other tools:

| what | examples | default |
|---|---|---|
| reading | your tasks, lists, habits, focus stats, searching | just does it (with the magnifying glass up) |
| adding | a task, a list, a checklist item | just does it |
| changing | completing, rescheduling, editing, moving, checking in a habit | `auto`: just does it when you asked; asks when it'd do it on its own |
| deleting | a task, a list, a habit | asks first |

Change the defaults in `config/local.yaml`:

```yaml
ticktick:
  changes: auto      # allow | auto | confirm | deny
  deletes: confirm   # allow | auto | confirm | deny
```

A rule for one tool in `permissions.tools` wins over these, e.g. `permissions: {tools: {ticktick_complete_task:
confirm}}`. Her tools are named after TickTick's (`ticktick_<name>`); the log's "Tools:" line lists them.

TickTick's server has over fifty tools, and every tool's description goes to Claude with each request. So the
everyday ones (tasks, lists, habits, focus, countdowns) are tools of their own, and the rest (kanban columns,
comments, tags, list groups, bulk edits) are reached through one, `ticktick_more`, which names them and looks up a
tool's details only when it needs one. That keeps TickTick to about 2,400 tokens per request instead of about
14,600. Through `ticktick_more`, reading never asks, changes follow `changes`, and deleting always asks.

TickTick has answered an update with the task as it was (a start date asked for, the answer showing the old one,
nothing saved). So after an update that sets a task's title, priority, dates or all-day flag, it reads the task back
and tells Claude what's really saved. Dates it didn't keep are sent once more together with what a span from a start
date to a due date needs (both dates in TickTick's own format, the all-day flag, the task's list and time zone); if
they still aren't saved, the update counts as failed, so it doesn't say it's done.

## Reminders

TickTick can't tell anyone when a task comes due, so it looks every few minutes (a request to TickTick, no Claude
usage) and says it when the time comes: "Heads up: “Call the dentist” is due now." Once a day, around the time you
set, it says what's due today (with the time, for a timed task) and what's overdue, by name. A reminder is said
once, even across restarts.

The rundown is its daily briefing: the first thing it brings up each day, in one short message. In it:

- what's due today and what's overdue, by name (Canvas work due today that isn't handed in too, with a Canvas
  token, the Canvas tools on, and Canvas and school not kept out of Futures; a Canvas task already copied into
  TickTick is named once);
- what's left on your calendar today (with a calendar connected and `get_calendar_events` on; what already ended
  isn't said, an event about health, money, relationships or work is "something personal", and "your calendar's
  clear" only when it could read it and Canvas);
- one line on what changed since you last talked: a deadline that moved, and how many of its hunches about you
  changed (ask "what changed your mind?" for which). Only what Futures may bring up unprompted: nothing from a
  source or area you kept out, nothing sensitive, nothing while Futures' proactive remarks are off.
- remarks of its own from OCTAVIA Futures (a nudge about tonight's lab from its nightly reflection, say), which wait
  for it from four hours before its time until it's said (at most three hours past its time, or an hour after it
  starts on a day it missed it, so they don't go stale): no nudge at 8, then a rundown at 9 that says nothing's
  due. What can't wait or can't go inside it still comes on its own: an offer of help ("Want me to open it?"), or a
  heads-up that would be stale by then. Nothing waits while TickTick isn't answering.

It says it at the first look after its time once it's idle (nobody talking with it). A morning it missed (the PC
was off, or it was busy) is caught up at it first idle moment later that day, until 10 PM ("Afternoon! Catching
you up on today."). It's said once a day, even across restarts. A source that fails or takes over a minute (Canvas,
the calendar, Futures) is left out rather than holding it up.

```yaml
ticktick:
  reminders: true          # say timed tasks when they come due
  remind_before_min: 0     # ...or this many minutes before
  check_every_min: 5       # how often she looks
  morning_summary: "09:00" # the daily briefing ("" = off); caught up later that day if missed
```

Tasks with only a date (all-day tasks) are in the morning rundown, not said at a time.

## Privacy

- The token lives in `.env` and is never written to a log or shown to Claude.
- What it reads from TickTick (task titles, notes) goes to Claude when it answers you, like anything else a tool
  returns, and is kept in the brain's conversation history (see the privacy notes in `docs/avatar.md`).
- The saved tool list (`data/ticktick/tools.json`) holds TickTick's tool descriptions only, none of your data;
  `data/ticktick/reminded.json` holds the IDs and times of reminders already said.

## When something's wrong

- "TickTick isn't connected yet": the token is missing from `.env`, or the assistant hasn't restarted since.
- "TickTick refused the token": make a new one in TickTick and replace it in `.env`.
- "couldn't be reached when the assistant started": TickTick was down or the PC was offline at the first start;
  restart once it's back.
