# Learning by watching (routines)

OCTAVIA notices your routines in what it already records and, at the moment one usually happens, offers to help:
"You usually open Spotify around now. Want me to open it?" It only ever asks. Nothing is opened or run unless you
say yes, and a "no" means it never offers that one again.

Code: `src/octavia/routines/` (observe, mine, store, service, rules, reports, wiring, commands) and the tools in
`src/octavia/tools/extensions/routines.py`. Settings: `routines:` in `config/default.yaml`.

## What it learns

| Kind | Example | How |
|---|---|---|
| `app_time` | "opens Spotify around 9:05 PM (5 of the last 7 weekdays with records)" | the first time each day an app came to the front; the hour-long window holding the most days |
| `app_after` | "opens Discord after Roblox (4 of 5 times Roblox came up first in a sitting)" | in a sitting at the PC (split by 45 minutes with nothing coming up), B within 10 minutes of A's first appearance, clearly more often than B shows up in a sitting at all (so a browser that's in every sitting follows nothing) |
| `ask_time` | "asks for the weather around 7:30 AM" | the first time each day it used one of a few read-only lookups (weather, calendar, Canvas due, TickTick to-dos) in a turn you started |

Days run from 4 AM to 4 AM, so the 1 AM end of an evening belongs to that evening. A routine can be for any day,
weekdays or weekends: a weekday one is said as one when it fits weekdays clearly better.

A routine is **learned** (may be offered) from at least `min_days` (4) separate days and at least `min_support`
(half) of the days it could have happened, out of at least 7 days of records. Below that, from 3 days and 30% of
them, it's only **watching**: listed, never offered. A routine the records stop showing goes at the next look.

## Where it comes from

Nothing new is captured. Everything is read through read-only connections:

- Futures' event log (`data/futures/futures.db`): the app in front (`computer.context_changed`, program and display
  names only, never titles) and its tool calls in your own turns.
- Screen history's timeline (`data/recall/screen_history.db`, the `activity` table): which app was in front when.
  Never its text, and browsers aren't in it any more (see docs/screen-history.md). Futures' watch still notes a
  browser coming to the front by name, so "opens Vivaldi around 9 AM" can be learned; nothing is ever read from it.

Never learned: Windows' own processes, installers, terminals, its own avatar, password managers and anything you keep
out of screen history (`recall.privacy.excluded_app` plus your list), apps in `routines.exclude_apps`, events in a
domain you put off-limits or marked sensitive, and events from a Futures source you keep out
(`futures.exclude_sources`). Putting `routines` itself off-limits ("don't learn my routines")
stops all of it. These rules are in `routines/rules.py`, which self-improvement builds can't change.

## When it offers

Only through Futures' proactive gate (a `RemarkSource`, prefix `routine_`), so everything that holds its other remarks
back holds these too: proactive remarks off, quiet hours, focus mode and Do Not Disturb, being in a conversation, the
hourly and daily budgets, cooldowns, and two turn-downs of a kind muting it for a week. On top of that:

- only a learned routine, at most `offers_per_day` (1) a day, each routine at most once a day;
- only while you're at the PC (keyboard or mouse in the last 2 minutes);
- a time routine from 10 minutes before its usual time to 30 after, and only if it hasn't happened yet today and the
  app isn't already open; an "after" routine within 3 minutes of the first app coming up;
- only what your privacy settings allow at that moment (an app you've since excluded, or a domain you've since put
  off-limits, is never offered, though it was learned before);
- an offer the gate holds back (you're talking with it, focus mode) is dropped after 2.5 minutes rather than said
  later, when it might no longer fit; it's proposed again at the next minute if it still does;
- the offer is announced with its tool (`open_application`, or the read-only lookup), so a plain "yes" goes to the
  brain, which does it like any request.

How it went is read from the records 15 minutes later: the app came up, or the lookup ran, counts as taken up. Three
offers in a row not taken up and it stops offering it ("stopped offering"). A "no" dismisses it for good, also when
the next look finds it again, and nothing else offers that app or lookup either ("Discord after Roblox" turned
down means no "Discord around 9 PM" either). "That's all" or "never mind" after a yes isn't a no. "Not now" is just for today.

## Seeing and clearing it

- `uv run octavia routines` lists what it learned, what it's watching and what it stopped offering, each with its
  evidence and number. `octavia routines look` looks through the records now.
- `octavia routines forget 3` never offers #3 again; `octavia routines offers-off` / `offers-on`;
  `octavia routines reset --yes` deletes everything it learned (the records it came from stay; the next daily look
  can find the same routines again, and the ones you said no to can be offered again, so use offers-off or
  `routines.enabled: false` to stop that). Numbers are never reused.
- By voice: "what routines have you noticed?", "why did you offer that?", "forget that routine", "stop suggesting my
  routines", "forget my routines". Turning offers back on and deleting everything are asked first.
- Off switch: `routines.enabled: false` (nothing learned, no tools) or `routines.offers: false` (learned and listed,
  never offered).

## Cost

It looks for routines once a day at `mine_at` (04:15), and at start when the last look is a day old: counting over
a few thousand rows on a background-priority thread, a few milliseconds. No Claude call, no local model, no GPU, so
the usage budget never comes into it. The check at each Futures look (every minute) is two indexed queries, and a
list of open windows only inside a routine's window.

## Not done

- No embeddings or a model reading the screen text for richer routines (a site you visit, a file you open): that
  would need browser text back in screen history, or a model, and the bar for an offer would have to rise with it.
- A yes is a single action. Chaining a routine ("open Spotify and start my playlist") would need a step that
  remembers what you did after opening it.
