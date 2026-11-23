# OCTAVIA Futures

OCTAVIA Futures is OCTAVIA's evidence-based model of the user's world, of its own performance, and of what tends
to follow from what (internally: it *Personal Causal Twin*). It is not another memory. It keeps what happened,
measures how things turned out, and only then forms beliefs about them. Each belief comes with honest confidence,
and it scores its own predictions. A correlation is never passed off as a cause; only an experiment the user
agreed to can support a causal claim.

All of it runs locally in its own SQLite database and is plain deterministic code: no model call is needed to
record, learn, predict or decide whether to speak up. Claude is one cognitive resource among others (see
[Reasoning](#reasoning-providers)).

```text
 events (ordinary program logic, never a model)
   │
   ▼
 world model ─ episodes ─ "remind me when" triggers
   │
   ▼
 outcomes (a to-do done on time or not, a tool call that worked, a reply that needed correcting, a build passing)
   │
   ├─► predictions scored ──► accuracy, calibration
   └─► hypotheses re-mined from counts ──► status: speculative → observed → supported / weakened / rejected / stale
                               │
                               ├─► the next prediction uses what was learned
                               ├─► a heads-up, through the proactive gate (cooldowns, budgets, quiet hours)
                               └─► an experiment offered (runs only with the user's yes) ──► causal evidence
```

## Using it

Talk to it:

- "What have you learned about me recently?" / "What patterns have you noticed?" / "What are you unsure about?"
- "How accurate have your predictions been?" / "What keeps making me miss deadlines?" / "What has actually worked?"
- "What changed your mind recently?" / "What deadlines changed today?" / "What's blocked?" / "Who am I waiting on?"
- "I have four hours tonight. What should I work on?" / "Show me what happens if I do A instead of B."
- "Why are you recommending this?" / "What assumptions are you making?"
- "When Professor Smith replies, remind me to update the assignment." / "When I next open Blender, remind me about
  the character model." / "If the build succeeds, tell me."
- "I care about getting my GPA up." (a goal it plans around) / "The website is blocked on the API key."
- "Forget that hypothesis." / "Don't experiment with my reminders." / "Stop being proactive." / "Don't learn
  anything about my health." / "That wasn't helpful." / "Stop telling me that."

From the terminal (works while it runs):

```powershell
uv run octavia futures status              # the database, row counts, your controls
uv run octavia futures events --limit 50   # the newest events (--type task.completed)
uv run octavia futures world --kind task   # entities and their relations
uv run octavia futures hypotheses          # with their evidence
uv run octavia futures predictions         # open and settled, with the Brier score
uv run octavia futures recommendations     # what she said (or held back) on her own, and why
uv run octavia futures episodes            # what happened, grouped (also: outcomes, experiments, triggers,
                                          # performance; each takes --limit)
uv run octavia futures trace hypothesis:3  # the audit trail: what changed, what caused it, what reasoned it
uv run octavia futures report learned      # the same text a question gets
uv run octavia futures explain prediction 3
uv run octavia futures html                # everything, as one (ugly, complete) page: data/futures/debug.html
uv run octavia futures reset --yes         # delete everything learned (your controls stay)
```

## Kinds of knowledge

Every piece of knowledge is labelled with what it is (`schema.Knowledge`), and the reports and the brain keep the
labels:

| Kind | Example | Where |
|---|---|---|
| FACT | a task's due date, from TickTick | world model entities |
| OBSERVATION | "the build failed its checks" | events |
| CORRELATION | "to-dos due in the evening were finished on time 2 of 7 times, vs 9 of 11 otherwise" | hypotheses (claim `correlation`, status observed/supported/weakened) |
| HYPOTHESIS | a possible pattern with too little evidence, or the user's own idea | hypotheses (speculative, stale, user) |
| PREDICTION | "the essay will be done by 6 PM" (about 15%, from 1 of 6 similar) | predictions |
| CAUSAL_CLAIM | "a heads-up helps you finish on time" | hypotheses with claim `causal` (only from an experiment) |
| USER_PREFERENCE | "I care about my GPA" | objectives the user said, or confirmed |

Confidence is earned from counts (`stats.py`): a rate comes with its Wilson interval; a difference between two
groups gets a `Strength` (insufficient / weak / moderate / strong) that needs both enough samples and a clear enough
difference (two-proportion z); probabilities are the Beta(1,1) estimate from counts, rounded to the nearest 5 %
when spoken, always with the counts they come from ("about 15%, because 1 of 6 similar to-dos..."). With few
samples the answer is "not enough evidence yet", whatever the rate looks like.

## Architecture

Package `src/octavia/futures/`:

| File | What it does |
|---|---|
| `schema.py` | the vocabulary: registered event types (with their domain and sensitivity), `Knowledge`, `HypothesisStatus`, `Strength`, `Policy`, `Domain`, `PROTECTED_DOMAINS`, entity references `kind:key` |
| `store.py` | the database: one SQLite file, real migrations (`MIGRATIONS` + `PRAGMA user_version`), a re-entrant lock, `transaction()`, `run()` (to a worker thread), a small kv, and the audit trail (`record_change` → `changes` table + a JSON line on the `octavia.futures.trace` logger) |
| `events.py` | `FuturesEvent` (the normalized record) and `EventLog` (dedup by `dedup_key`, `transient` fields that are never stored) |
| `ingest.py` | the assistant's bus events → Futures events (`Normalizer`), deterministically: turns, tool calls, approvals, builds, to-dos, timers, mail, the app in front |
| `sources.py` | Futures' own watches: TickTick snapshots (diffing every look the reminders already take), Canvas (hourly, with a token), the app in front (names only) |
| `world.py` | entities and relations (Task → belongs_to → Project, Assignment → belongs_to → Course, Project → waiting_on → Person...), change history of due dates and statuses, queries (deadlines changed, open work, blocked, waiting on) |
| `episodes.py` | activity compressed into episodes (a conversation, a build, a long stretch in one app) with problems, what resolved them and what was learned; "this happened before" lookups |
| `outcomes.py` | how things turned out, with the conditions they happened in (features), counted once per key |
| `hypotheses.py` | the pattern finder and the hypothesis lifecycle |
| `predictions.py` | predictions from base rates, scored on resolution; accuracy and calibration |
| `experiments.py` | safe personal experiments: templates, the safety policy, balanced assignment, evaluation |
| `objectives.py` | the user's own goals ("you said") vs what it noticed and must ask about |
| `prospective.py` | "remind me when..." conditions and persistent triggers |
| `proactive.py` | the gate every unprompted remark goes through, and the recommendations it records |
| `offers.py` | offers of help before it's asked (Canvas work due soon, a meeting's link), how each answer is counted, and how take-up shapes the next offer |
| `explain.py` | traces (what it saw, which patterns, predictions, goals, assumptions, how sure) and their explanations |
| `selfeval.py` | its own measured performance per tool, model and build (the data foundation for model routing) |
| `simulate.py` | the conservative futures engine: alternatives with benefits, risks, dependencies, assumptions, uncertainty |
| `reasoning.py` | provider-agnostic reasoning (`Reasoner`, `ReasonerRouter`, `DeterministicReasoner`) |
| `privacy.py` | the user's controls: excluded sources, off-limits domains, no-experiment domains, proactive on/off, muted remarks, forgotten hypotheses; `redact` for any kept text |
| `engine.py` | `FuturesService`: owns all of the above, runs the pipeline, the tick, and what the tools do |
| `reports.py` | the text the tools and the CLI give (every line labelled with its kind of knowledge), forgetting, controls, goals, experiments |
| `wiring.py` | how the app starts it and connects it (`start_futures`, `connect`, `watch_ticktick`) |
| `commands.py` | `octavia futures ...` |

Tools: `src/octavia/tools/extensions/futures.py`. Prompt section: `brain/prompt.py` (`FUTURES`, `{{futures}}` in
`personality/system.md`).

### Threading and ordering

`FuturesService.on_event` is the event-bus subscriber and only puts the event on a queue (sync handlers run inline
wherever an event is emitted, including the audio path). One worker task takes the queue in order and runs each
step of the pipeline in a worker thread (`store.run`), so the event loop, and its voice with it, never waits on the
disk. Tools, the prompt summary and the CLI go through `FuturesService.call`, which puts the work on the same queue:
nothing ever runs beside the pipeline. What a step decides to say comes back as `Effects` (announcements, notes for
the brain), carried out on the loop.

It subscribes only to the event types `ingest.BUS_TYPES` lists, so a `BrainTextDelta` costs nothing.

### The pipeline, per event

`FuturesService._record`:

1. `EventLog.prepare` (type checked, id, time, domain, sensitivity), then the privacy step: a request or to-do
   whose words put it in a protected domain (`privacy.classify_domain`: health, finance, relationships,
   employment) is filed there, and which app was in front is dropped while the computer isn't recorded. An event
   from an excluded source or an off-limits domain is dropped here, before anything is kept.
2. `EventLog.append`: a duplicate (same `dedup_key`) stops here.
3. `WorldModel.apply` and `Episodes.observe`.
4. "Remind me when" triggers matched (not for baseline looks; a to-do ticked off by id alone is matched by its
   name from the world model), each firing through the gate as `INFORM`. A repeating one fires at most every 10
   minutes, so one opening seen twice (its tool, then the app in front) is one reminder.
5. The rule for its type (`engine._RULES`): predictions made or resolved, outcomes recorded, performance measured,
   hypotheses re-mined, heads-ups followed up.

In `octavia chat` (beside the voice assistant or alone) Futures records as usual, shows the user's own reminders at
once as a line on the terminal, leaves conversation episodes to the voice assistant, and leaves its own remarks
for it to say. With `futures.enabled: false` (or a database that can't be opened) its tools are taken out of the
brain's tool list and there's no prompt section.

The tick (`TICK_S` = 20 s, heavier checks at most every 60 s) closes idle episodes, expires predictions and
triggers, evaluates running experiments, looks for to-dos due soon that tend to slip, delivers pending remarks
once it's a good moment, and once a day marks stale hypotheses and deletes raw events past retention.

## Events

Event types are registered in `schema.EVENT_TYPES`. `EventLog.append` refuses an unknown type, so a typo can't start
a stream nobody reads. The data each type carries is in the table in `ingest.py`/`sources.py`. In short:

- The assistant itself: `assistant.started/stopped/reply`, `user.request/feedback/correction`, `routine.started/
  completed` (a scheduled job's turn, never taken as something the user said).
- The computer: `computer.application_opened/closed/context_changed/action_performed/action_failed`.
- To-dos: `task.created/updated/completed/deleted/seen/overdue/cleared`. School: `canvas.assignment_created/updated/
  submitted`, `canvas.grade_posted` (sensitive). Mail: `gmail.message_received` (sender only; the subject is
  transient, for matching triggers, and never stored), `gmail.message_sent` (registered; nothing produces it yet).
- Time and goals: `timer.created/completed/cancelled`, `goal.created/progress/completed`.
- Self-improvement: `build.started/patch_created/test_passed/test_failed/failure/installed/rolled_back`.
- Futures itself: `prediction.created/resolved`, `experiment.started/completed`, `trigger.fired`,
  `recommendation.delivered/feedback`, `hypothesis.changed`, `futures.failure`.

Where they come from, all ordinary code:

| Source | How |
|---|---|
| Turns, tool calls, approvals, dismissals, "thanks", announcements, builds | the assistant's own bus events (`ingest.Normalizer`); `BrainStarted/BrainFinished` carry `source`, `turn_id`, timing and usage |
| To-dos changed through its TickTick tools | `TaskChanged`, emitted by `tools/extensions/ticktick.py` (the only place a new task's id is known) |
| To-dos changed anywhere else | `sources.TaskSnapshots` diffs every look the reminders and the victory-lap watch take anyway (no extra requests); a to-do that left the list is checked with one `get_task_by_id` |
| Timers | `TimerChanged` from `tools/extensions/timers.py` (a chime-only ring too) |
| Mail | `EmailArrived` from the Gmail watcher (only while email alerts are on) |
| The app in front | `sources.ContextWatch`: polls every 2 s, reports an app once it stayed in front 5 s (`AppContextChanged`); program and display names only, never window titles |
| Canvas | `sources.CanvasWatch`: planner hourly, grades every 6 h, only with `CANVAS_API_TOKEN`; the first look is a baseline |
| The calendar | `sources.CalendarWatch`: the feeds every 15 min, only with `CALENDAR_ICS_URLS`; online meetings of the next two hours kept in memory for offers (a meeting in a protected domain is skipped); only an offer is kept, as a recommendation |

## Storage

`futures.database` (default `data/futures/futures.db`), WAL mode. Tables: `events`, `entities`, `entity_changes`,
`relations`, `episodes` (+ `episodes_fts`), `outcomes`, `hypotheses`, `hypothesis_evidence`, `predictions`,
`experiments`, `trials`, `performance`, `triggers`, `recommendations`, `objectives`, `controls`, `kv`, `changes`.

Migrations: `store.MIGRATIONS` is an ordered tuple of SQL scripts, and `PRAGMA user_version` says how many have run.
To change the schema, append a script (never edit one that shipped) and write a test that opens a database at the
previous version (`test_futures_store.py` does this for version 1 → 2, which added indexes for the open-episode,
recommendation and prediction lookups). A database from a newer version is refused, and a damaged one too: OCTAVIA then runs without
Futures and says so once (`wiring.start_futures`).

## World model

Entity kinds (`world.ENTITY_KINDS`): user, person, goal, project, task, commitment, course, assignment,
organization, conversation, routine, application, file, skill, model, experiment, hypothesis, prediction, timer,
improvement, email. Predicates: contributes_to, belongs_to, associated_with, concerns, evaluates, blocked_by,
waiting_on, part_of, created_by, used. `apply(event)` holds the rules per event type. Changes to a due date or a
status are kept in `entity_changes`, which is what "what deadlines changed today?" reads.

## Episodes

Activity is compressed, never kept frame by frame:

- a conversation (thread `session:<id>`): its requests, actions, failures, corrections, and a one-line outcome;
- a build (`build:<id>`): its checks failing, passing on another try, installing or being undone;
- a long stretch in one app (`focus:<app>`, 10 minutes or more).

A problem (a failed action, a correction) and what resolved it (the same action working on another try) become a
`learned` line and words in the episode's `signature`. When the same kind of failure happens again during a
conversation, the brain gets a one-line note: "This happened before: ..." (at most once a day per kind).

## Outcomes, predictions, self-evaluation

| Outcome kind | Success means | Recorded when |
|---|---|---|
| `task_on_time` | done by its due time (+10 min grace; all-day to-dos: by the end of the day) | it's ticked off (a repeating to-do moving to its next date counts as ticked off); or it was seen still open past its due time and still not ticked off once TickTick's next look after the grace could have said so; or it was pushed back after its due time had passed |
| `tool_call` | the call worked | every tool call |
| `brain_turn` | the reply went through without a correction | the next request (or 10 minutes of quiet) |
| `build` | the build passed its checks | the build ends |
| `recommendation` | the user took it up (thanks, followed it) | their reaction |

Features (conditions compared by the pattern finder): for to-dos `due_part_of_day`, `day_type`, `priority`,
`created_by`, `nudged`; for tool calls `tool`, `app`, `category`, `part_of_day`; for replies `kind`, `source`,
`part_of_day`.

Predictions: `task_on_time` for every dated to-do it sees (probability from to-dos due at the same part of the day,
falling back to all to-dos, saying so), and `build_passes` for every build. Each one is scored when it resolves
(`correct` = whether its side of 50 % came true); `reports accuracy` gives the count right, the Brier score and
calibration by probability band. A toss-up (exactly 50 %: nothing to go on) is neither right nor wrong: it counts
only in the Brier score. Predictions nobody could settle (a to-do it never saw finished) expire after a week rather
than being counted wrong. A to-do moved to another date gets a new prediction (the old one is cancelled, or
counted as a miss if its date had already passed); one moved out of the window the reminders look at is checked
with TickTick, so its new date is known.

Self-evaluation (`performance` table): every tool call, reply and build with success, failure class, latency,
retries, corrections, tokens and cost. `Performance.stats`, `repeated_failures` ("what has OCTAVIA repeatedly failed
at?") and `estimate(category, candidates)` are the data a future model router needs. No routing rule is hard-coded.

## Hypotheses

`Hypotheses.mine(metric)` compares, for each feature value with at least 4 outcomes, its success rate against the
rest (`stats.compare`). A clear enough difference (≥ 20 points, at least weak evidence) becomes a hypothesis, keyed
`metric|feature=value` (a two-valued feature gives one, not two mirror images). Lifecycle:

- `speculative`: weak evidence;
- `observed`: moderate evidence, a correlation;
- `supported`: strong evidence, still a correlation unless an experiment confirmed it;
- `testing`: an experiment is running on it, and only the experiment decides;
- `weakened`: it looked stronger before;
- `rejected`: plenty of data and no difference, or an experiment went against it;
- `stale`: no new evidence for 30 days.

Every change writes an evidence row and an audit entry (`changes`), which is how "which evidence changed a
hypothesis" and "what changed your mind recently" are answered. The statement always carries the counts, and
confounders are listed ("what kind of to-do it was, how big it was, a small sample"). A forgotten hypothesis is
suppressed by key, so the miner doesn't form it again from the same data.

## Experiments

`experiments.TEMPLATES` has one experiment, `task_nudge`: "Does a heads-up a few hours before a to-do is due help you
finish it on time?". It is offered once, when the observational `nudged` pattern reaches moderate evidence. That
pattern is confounded: it only gives heads-ups about to-dos that tend to slip, so only an experiment can tell cause
from coincidence. The variants are `nudge` and `no_nudge`, balanced and reproducible per to-do. A to-do is enrolled
only at a moment its heads-up could actually be said (the gate's dry run, `ProactiveGate.check`), so whatever keeps
a heads-up from being said (quiet hours, the budget, a conversation) keeps the to-do out of both groups alike. Its
constraints are kept in code: to-dos with a due time only; never school work (by a word list and by matching a
Canvas assignment's title), high-priority to-dos (or ones whose priority isn't known) or anything already overdue;
never a to-do filed under a protected domain; TickTick's own reminders stay as they are. It needs 6 of each, or runs
28 days at most. The result is judged against what the hypothesis claims (`engine._bears_out`): the observational
pattern usually says "to-dos I gave a heads-up about did worse" (confounded), so an experiment showing the heads-up
helps rejects that pattern rather than confirming it, and the experiment's own summary is what "what has worked"
reports. The same direction with at least moderate evidence makes the hypothesis a causal claim, for this user and
these to-dos. An experiment the user stops, or whose domain they put off-limits, is stopped, and its hypothesis goes
back to what its own evidence says.

The safety policy (`ExperimentPolicy`):

1. A domain the user put off-limits, or said "no experiments" for, is refused.
2. In a protected domain (relationships, health, finance, school, employment, people, communications) OCTAVIA never
   varies what it does. Only an experiment the user runs on themselves, knowingly, is possible there, and only with
   their approval.
3. A personal (human-facing) experiment always needs the user's explicit yes (`futures_experiment approve`, which the
   tool itself asks to confirm).
4. An internal experiment (one that only changes how it works) needs the yes too, unless autonomous internal
   experiments are allowed: `self_scoring.experiments: own` (the default) allows them for its experiments on itself.

## Self-scoring and experiments on itself

`selfscore.py`, `tuning.py`, `habits.py` and `habit_rules.py` (FORBIDDEN to builds). Settings under `self_scoring:`
(Settings, Self-improvement: "Judge its own replies", "Experiments on itself", "Keep what works").

**The verdict on every reply.** `ingest` notes how the user took its last reply when what they said or did came
within `CORRECTION_WINDOW_S` (90 s) of it, as a `user.reaction` (`selfscore.reaction`, no model): *thanked* ("thanks",
"perfect", a preset thanks), *corrected* (Futures' correction check, or "no, I meant" / "I just told you to"),
*repeated* (the same request in other words), *complained* ("that's not it", "why are you just sitting there"),
*too long* ("get to the point"), *asked again* (a "say that again" it answered with no model). The corrected,
repeated and complained signals are `evolve.mistakes.signal`, the ones its caught mistakes come from (the night's scan adds its own filters). A plain "no" or "no thanks" is an answer, never a verdict; "skip the song" or "brief me" is a new request, not "too long" (those phrases count only at the start); "thanks, but..." or "nice try" isn't a thanks. A reply
the user talked over is *cut off*. Anything against a reply outweighs a thanks. When the reply settles (the next
request, or `REPLY_SETTLED_S` of quiet) it becomes a `turn_verdict` outcome: success unless it went wrong, `actual` the
verdict ("fine" when nothing was said either way), features: question or request, voice or typed, part of day, the
first tool it used, and the experiment variants and habits it had. `brain_turn` is unchanged (a reply cut off is no
longer counted as "not corrected"). `futures_report topic self` ("how am I doing?") and `octavia futures report self`
say the last week against the week before, where replies went wrong most (by first tool, 3 or more), its experiments
on itself and its habits; the nightly reflection's digest includes the same text.

**Experiments on itself.** Three templates, run one at a time and each once, in this order, starting only once it
has `MIN_REPLIES` (30) judged replies in two weeks:

| Template | Kind | Units | Variants | Measured by |
|---|---|---|---|---|
| `brief_when_stressed` | internal | replies to something said in a stressed, frustrated, anxious or annoyed voice (tone at least `emotion.min_confidence`, heard in the last 10 s) | brief / usual | turn verdict; 15 each, 42 days at most |
| `answer_first` | internal | every reply to the user | answer_first / usual | turn verdict; 60 each, 28 days at most |
| `heads_up_timing` | personal: asked first | to-dos it'd give a heads-up about anyway (the heads-up experiment's constraints) | late (a third of `heads_up_h` before) / early (as now) | on time, every enrolled to-do (said or not); 6 each, 42 days |

`brief_when_stressed` is skipped when it can't hear tone (`emotion.enabled` or `emotion.note` off); the heads-up
timing waits until 4 heads-ups were said in a month and never runs beside `task_nudge`. A turn's variant is a coin
flip (`tuning.Tuner`, on the event loop, from a snapshot the worker hands it through `Hooks.tuning`), cached for 15 s
by its words so an early start and the turn agree; its note (fixed in `habit_rules.NOTES`, every one starting
"for this answer: ", which the local brain's router counts as context, so neither group moves between brains) joins
the turn's notes, and the (what, variant) pairs ride on `BrainStarted.tuning` to the reply, where the verdict is
counted for that group (`Experiments.enroll`, then `record`). A to-do in the heads-up timing is enrolled like the
heads-up experiment's: only at a moment its usual heads-up could be said, and only while the later time is still 30 minutes or more away (`ENROL_MARGIN_S`), so the groups really differ. A reply cut off with nothing said against it is in its record as *cut off* but isn't counted against its variant (a longer reply is talked over more just by lasting longer); with a correction or repeat it counts as that. An offer of one of its own left unanswered for 7 days is withdrawn so it can't hold up the rest. Switching `self_scoring.enabled` off or `experiments` to `off` stops a running one at the next look (about a minute), and its notes with it.

**Habits.** When one finishes with the change ahead by at least moderate evidence (`habit_rules.adoptable`), and
`keep_habits` is on, the change is kept: a row in `habits` with the experiment and its result in words, `adopted`
in the audit trail (cause `experiment:<id>`), and said through the gate ("I tested leading with the answer on myself.
... So I'm keeping it. If you'd rather I didn't, say "drop that habit"."). An unclear result is said as unclear and
changes nothing. A habit applies like its variant did, always, and only where its experiment's rules allowed (`heads_up_timing:
late` moves heads-ups to the later time, never for school work or high-priority to-dos). `my_habits` (list, drop, restore) and `octavia futures habits [drop|restore] N`: a dropped habit stops at
once (from the CLI, within a minute), stays dropped whatever a later result says, and its experiment never runs again on its own; restoring brings
it back. A Futures reset forgets habits too.

## "Remind me when"

`prospective.CONDITION_KINDS`: `app_opened`, `email_from`, `email_about`, `build_succeeded`, `build_failed`,
`task_completed`, `timer_done`, `assignment_posted`, `grade_posted`. A trigger is stored with its kind and value (its
condition is rebuilt from code on load, so it survives restarts and code updates). It is matched against every event
(transient fields included, so an email's subject can match without being stored), fires once (or every time), and
expires after 30 days by default. `email_from` also records that the user is waiting on that person, answered when
their mail arrives. A firing is `INFORM`: it is said even with proactive remarks switched off, because the user asked
for it, and it is never muted for being waved off ("that's all" after a reminder isn't a turn-down of reminders).
One the gate still can't say (its domain went off-limits, it waited too long) stays armed, and while the user has
muted reminders a matching event leaves it armed without writing anything. A repeating one fires at most every 10
minutes. A "when I tick off X" for a to-do ticked off by id alone gets the to-do's name from the world model, or
from TickTick (one request) when it has never seen it. `remind_me_when` is asked first when the turn took in text
from outside (a web page, an email, the screen, messages: `ingest.OUTSIDE_TOOLS`) or isn't the user's own (a
scheduled job), since it'd later say it as something the user asked for. It refuses a reminder that could never
fire (an off-limits domain, an unrecorded source) and says when the watch that would see it is off (email alerts or
Gmail not connected, Canvas, self-improvement).

## The proactive gate

Every remark it'd make on its own is a `Candidate` with a policy (IGNORE, INFORM, SUGGEST, ASK, ACT,
ACT_AND_INFORM, REQUIRE_CONFIRMATION), urgency and relevance. `ProactiveGate.consider` records a `Recommendation`
whatever it decides, and checks in this order:

1. IGNORE;
2. an off-limits domain;
3. proactive switched off (except INFORM);
4. a muted kind;
5. expired;
6. the same thing said within `cooldown_h` (12 h), or turned down within 3 days;
7. not important enough (urgency × relevance < 0.35; except INFORM);
8. too sensitive to bring up (except INFORM);
9. over budget (`max_per_hour` 2, `max_per_day` 6; except INFORM): dropped;
10. an offer of help past `offers_per_day` (3): dropped;
11. the same kind said within 20 minutes (except INFORM): it waits;
12. quiet hours (22:30-08:00; except INFORM): it waits;
13. the day's TickTick morning rundown still to come (from four hours before `ticktick.morning_summary` until it's
    said; except INFORM, offers of help, and a remark that would run out of time before it): it waits, and the
    rundown says it inside it (`morning_remarks`), quiet hours or not;
14. focus mode or Windows' Do Not Disturb (except INFORM): it waits;
15. a conversation going on: it waits, INFORM too.

An offer (a kind starting `offer_`) is made once for its deadline or meeting, never again however long ago, rather
than once per `cooldown_h`.

The budget and quiet hours are for remarks of its own: what the user asked to be told is said whenever nobody's
talking, and doesn't use up its budget.

Pending remarks are re-checked each tick and delivered one at a time, or dropped after a day. Until the assistant
has connected its voice (the first seconds after a start) everything counts as "busy", so nothing is marked said
that wasn't. It never acts on the
user's behalf from here: V1's gate only speaks (through `Assistant.announce`, with the cue `futures`). Anything with
side effects is a normal tool call the user asks for, through the executor and its confirmations.

What it says on its own today:

- a heads-up about a to-do due within `heads_up_h` (3 h), only when its records say to-dos like it tend to slip
  (probability under 50 % from at least 5 similar ones), or as the `nudge` variant of an experiment the user agreed
  to;
- a "remind me when" firing (INFORM);
- an experiment offer (ASK, once);
- an experiment's result (its own remark: with speaking up off it stays in `futures_report experiments`);
- an offer of help (`offers.py`, ASK): Canvas work due within `offer_assignment_h` (4 h) that Canvas still shows open
  ("Your history essay is due at 11:00 PM today. Want me to pull up the rubric?"), or as soon as a writing app
  (`work_apps`) comes to the front while one is due within a day; an online meeting starting within
  `offer_meeting_min` (10 min), from the calendar feeds ("Want me to open the meeting link?");
- whatever another part of OCTAVIA hands over: `FuturesService.add_source(RemarkSource(...))` is asked every minute
  for candidates (its reflections' thoughts), `FuturesService.suggest` takes one. Either way it goes through this gate,
  and the source hears back when it was said or turned down. There's no second way to speak up.

An offer only asks. A yes goes to the brain as an ordinary turn, with a note of what was offered, and the help runs
through the executor like any request: a submit, a send or a delete is still asked there. The offer is announced with
its read-only tool (`offers.OFFER_TOOLS`) as the `Offer`, only so the yes goes to the brain rather than the quick voice.
Its answer is kept on the recommendation: a short "yes" right after it (to the brain, the quick voice or a text)
is accepted, "not now" / "later" deferred (the moment was wrong, so it counts toward no mute), "no" / "no thanks"
rejected, the help used without a yes (`canvas_assignment`, `open_url`) followed, and nothing within ten minutes
ignored. How a kind of offer has been taken up over 30 days sets how relevant the next one is: after a miss the next
waits until its deadline is nearer, and a run of them with nothing taken up puts it under the threshold (an unanswered
one counts half: the user may have been away) until a week without one gives it another chance. `futures_report offers` (or `octavia futures report offers`) has the numbers per kind, why the rest were held back,
and the last few offers with their answer.

A heads-up that's still waiting when its to-do is ticked off, moved or deleted is dropped rather than said late.

How it went down is measured: "never mind" right after it counts as rejected, "thanks" as accepted, the to-do ticked
off as followed. Two rejections of a kind within a week mute that kind for a week, and `futures_feedback` "stop this
kind" mutes it for 30 days.

## Explainability

Every recommendation keeps a `Trace`: what it saw (event ids and what they were), the patterns it leaned on (with
their strength and kind of claim), the predictions (probability and basis), the user's goals (and whether the user
said them), the assumptions, how sure it is, and which provider reasoned it. The trace is a snapshot, so "why?"
answers what it thought then. `futures_explain` also explains a prediction, a hypothesis with its evidence rows, or
an experiment. The `changes` table and the `octavia.futures.trace` log say what changed each belief, what caused it
(an event id) and what reasoned it.

## Reasoning providers

`reasoning.Reasoner` is the interface: `name`, the task kinds it can do, and `async reason(task)`.
`ReasonerRouter` tries the capable reasoners in order, falls back on an exception or an error, and records each
attempt in the performance table (kind `reasoner`), so which provider works best for which task becomes data. V1
has one, `DeterministicReasoner`, for `simulate`: `futures_compare` builds a `Scenario` from the world model and its
history, and the conversation brain (Claude) talks the result through. That's the only model involvement, and it
costs no extra call.

To add a provider (a local model, Claude for episode summaries), subclass `Reasoner` and put it in the router's list
in `FuturesService.__init__`. The rest of Futures doesn't change.

## Privacy and control

The user owns this model:

- **Never recorded**: `futures.exclude_sources` (assistant, tools, ticktick, canvas, gmail, computer, evolve, timers,
  calendar).
- **Off-limits**: `futures.off_limits`, plus whatever the user says ("don't learn anything about my health"). Nothing
  from an off-limits domain is recorded, learned from, experimented on or brought up, and its watches stop (the app
  in front, Canvas, the TickTick snapshots). The config's list is a floor the user's words add to. Health, finance,
  relationships and employment are recognized by a word list (`privacy.classify_domain`) in requests and to-dos:
  "refill my prescription" is health. Ambiguous words only count in phrases ("pay rent", not "pay"; "my anxiety",
  not an essay about anxiety), coursework from Canvas stays school, and a to-do filed under a protected domain stays
  there when its own tool later touches it by id. What the list doesn't have can slip through, and the controls
  tool says so.
- **Deleted on request**: a hypothesis, a reminder, an episode, a prediction, a goal, every event from a source,
  everything about a domain, or everything (`futures_forget`, asked first except for cancelling a reminder, each one
  with a question of its own; `octavia futures reset`). Forgetting a domain takes its events, outcomes, episodes,
  entities, hypotheses (suppressed), the predictions and remarks about those entities, the reminders that watch it,
  its measurements and snapshots of it, and their audit entries. A reset clears the audit trail too, except the
  user's own settings. Raw events are also deleted after `retention_days` (180).
- **Speaking up**: `futures.proactive`, "stop being proactive", muted kinds. Loosening a control is asked first;
  tightening one never is.
- **Experiments**: `futures.experiments: off`, "don't experiment with X", "stop that experiment".
- **What's never kept**: window titles, what a tool returned, email subjects and bodies, what it typed, screenshots.
  The words of a request are kept only with `futures.keep_request_words`, and every secret in any kept text (a
  request, a to-do or assignment title, a reminder) is cut out by `privacy.redact`: the value after "password is"
  (or "pin:", "api key =", even without digits), card and ID numbers, keys and long tokens. Grades are kept as a band
  (high/mid/low), are sensitive, and are never said unprompted; so are to-dos filed under health or finance.
- **No hidden manipulation**: inferred things are never presented as facts, inferred goals are only proposed
  (`futures_goal confirm/decline`), and it never plans around a goal the user didn't say or confirm.

`futures/privacy.py` (the controls, the secret patterns, the domain word lists, and which privacy actions are asked
first), `futures/__init__.py` and `tests/test_futures_privacy*.py` (which include an end-to-end check that the
engine drops what's off-limits) are protected from self-improvement builds. The rest of `futures/` and
`tools/extensions/futures.py` is sensitive: always shown to the user, never installed on its own
(`evolve/policy.py`).

## Self-healing

A failure in any pipeline step is logged with `log.exception` (event type and stage only, never what the event was
about), which the self-improvement error watcher turns into an improvement item with the traceback. It is also
recorded as a `futures.failure` event. The pipeline carries on with the next event, and after 25 failures in ten
minutes it stops until the next start instead of filling the journal. Builds feed Futures in turn: every build is
predicted and scored, and its outcome, tests and cost are measured.

## Settings

`futures:` in `config/default.yaml` (see the comments there): `enabled`, `database`, `proactive`, `max_per_hour`,
`max_per_day`, `cooldown_h`, `quiet_hours`, `heads_up_h`, `context_watch`, `canvas_watch`, `offers`,
`offers_per_day`, `offer_assignment_h`, `offer_meeting_min`, `calendar_watch`, `work_apps`, `keep_request_words`,
`retention_days`, `exclude_sources`, `off_limits`, `experiments`.

## What's real in V1, and what isn't yet

Real:

- The event stream from every existing integration listed above.
- The world model, episodes, outcomes and self-evaluation.
- Hypotheses with their lifecycle, and predictions with scoring.
- The `task_nudge` experiment end to end.
- "Remind me when" for nine conditions.
- The proactive gate, explanations, the comparison of options, the privacy controls, the CLI and debug page, and
  failure reporting.

The full vertical slice runs on to-dos: event → world → prediction → heads-up → outcome → re-mined hypothesis →
better next prediction → experiment → causal claim. Tool calls and builds run through the same loop.

Deferred on purpose:

- **Sent mail.** `gmail.message_sent` and "did they reply to my email" need a read-only look at the Sent folder.
- **Package delivery.** There's no source for it yet; a trigger on a carrier's email is the workaround.
- **Duration history.** How long a task takes is needed for better comparisons; they say "assumed 1 h" for now.
- **Model-written episode summaries.** They would need a `Reasoner` for `summarize`.
- **More experiment templates**, such as reminder timing or routing strategies.
- **Inferred objectives.** The data model and the confirmation flow exist, but nothing proposes one yet.
- **Autonomous internal experiments.** Off, and none exist yet.
- **Model routing that uses `Performance.estimate`.**
- **Brain turns in background tasks.** They run on a private bus by design, so they're invisible to Futures.

## Extending it

- **A new event type**: register it in `schema._TYPES`. Produce it from ordinary code: a typed event on the bus that
  `ingest` maps, or a `FuturesService.submit` from a source. Add world/episode rules if it's about an entity.
- **A new outcome metric**: record `Outcomes.record(kind, ...)` with features, add it to `hypotheses.METRICS`, and
  call `_mine`/`_mine_every` for it.
- **A new prediction**: a rule in `engine._RULES` that calls `predictions.predict(...)` with a unique key, and one
  that resolves it.
- **A new experiment**: a `Template` in `experiments.TEMPLATES`, its constraints in code, and its variant consulted
  where the behaviour happens. It always goes through `ExperimentPolicy`.
- **A new reasoner**: see [Reasoning providers](#reasoning-providers).

Tests: `tests/test_futures_*.py` (`test_futures_engine.py` has the end-to-end loops, `test_futures_app.py` the app
wiring, tools, prompt and CLI). They use `FuturesStore(":memory:", clock=...)`: no network, no desktop, no sleeps.
