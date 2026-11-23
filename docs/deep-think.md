# Thinking hard questions through

Its conversation runs Opus at `brain.claude.effort: low` so it answers fast. Some questions deserve more: a
decision with real trade-offs, a plan with constraints, debugging something that resisted a first try, maths or
logic, or "think hard about this." For those it calls `think_hard`. It says one line that it's thinking it over,
keeps talking with you, and when the answer is ready it says it: two to four sentences that lead with the
conclusion. The full reasoning (the options it weighed, its assumptions, what would change its answer, and links)
is saved in `data/thoughts`, and "why?" or "walk me through it" reads it back (`get_thought`). "Never mind" while
it's thinking stops it (`cancel_thought`). A restart into new code waits for a think under way, like a background
task.

## How it works

- `tools/extensions/_deep_think.py`: `ThinkManager`, a background-task manager of its own (`_background_tasks.py`)
  with its own prompt, its own list (`data/thoughts/tasks.json`), a longer spoken answer (600 characters, cut at a
  sentence) and the limits below.
- `tools/extensions/deep_think.py`: the `think_hard`, `get_thought` and `cancel_thought` tools (category
  `deep_think`, removed when deep thinks don't run). All are SAFE: the thinking brain only
  has the background tasks' read-only tools (the time, saved notes, past conversations, the calendar, finding files)
  and the read-only web tools.
- `App._start_deep_think` builds it (voice mode only, like background tasks). The `# Thinking hard` prompt section
  (`brain/prompt.py` `DEEP_THINK`) is there only then.

## What it costs, and the limits

A think costs about as much usage as a short background task: a trial plan question took 24 s at `high`.

- One at a time.
- The ones it starts on its own are capped at `deep_think.max_per_day` (8; the day starts at 04:00), count against
  today's share of the usage budget for its own work (`usage_budget.daily_share`), and are charged to it as the job
  `deep_think`.
- The ones you ask for ("think hard about…", "take your time", "mull it over") don't count toward the cap and only
  wait for your own Claude limits (`usage_budget.max_five_hour`, `max_weekly`). Which is which is decided from your
  own words in the running turn (`asks_to_think`), never from what the model says.
- The budget is read fresh before each think (`usage.check_fresh`).
- When a think can't start, the tool says why and it answers directly instead.

## Settings

`deep_think` in `config/default.yaml`: `enabled`, `model` (opus), `effort` (high; low to max), `max_per_day`,
`time_limit_min` (10) and `web`. `model`, `effort`, `max_per_day` and `web` apply live. Three are in the settings
window under Brain.
