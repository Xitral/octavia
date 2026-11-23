# Work before you ask

At night, while you're away, OCTAVIA prepares a few things it can tell you'll need, has each one checked, and
hands them to you in the morning to keep, drop or change. Code: `src/octavia/proactive/`, tools in
`tools/extensions/proactive.py`, settings under `proactive:` (Settings → Proactive help → "Work before you ask").

## What it does

1. **Its picture of your week** (`model.py`). Read fresh each night from what it already keeps: work due in Canvas,
   exams and weak topics in the study tracker, TickTick to-dos, today's and tomorrow's calendar, your projects (only
   the ones you list), the notes it keeps about you that are about priorities, school, work and how you like things,
   your usual hours from its own records, the hunches about you that Futures tested against what happened and
   that held (observed or supported; said as correlations, never causes: the curiosity loop's results), and how you
   answered its overnight work before. Every line says where it
   came from. Nothing is copied into a new store; `data/proactive/model.json` is only the last picture, for
   `my_picture_of_you`. Canvas, school and the calendar follow Futures' privacy controls, as the morning rundown does,
   and private calendar events are never named or prepared for.
2. **What's worth preparing** (`rules.py`, plain code): a review sheet and practice questions for an exam that's
   close (weak topics first, from the course's own material); a brief for an assignment (what's asked, the rubric as
   a checklist, the material, a plan to the due time, never the graded work itself); a review sheet for a Canvas
   quiz (never its answers); the groundwork or a message draft for a to-do due soon; a prep sheet for a meeting
   today or tomorrow; and, at the `projects` level only, one small improvement on a branch of a project you listed.
3. **Tonight's pick** (`planner.py`). What was prepared lately, turned down in the last 30 days, or is still waiting
   for your answer is skipped. One cheap model call (`rank_model`, default Sonnet at low effort) orders the rest
   against its picture of you; it can only order and drop, never add. The best `max_items` (2) run.
4. **Checked before it counts.** Each item runs as an objective with its plan already written, so the objectives
   reviewer, a session that didn't do the work, checks it against its acceptance criteria. An item is **ready** only
   when every step passed; one that ran but failed its check is said as that. Overnight claims are held to this too
   (`core/claims.py`): if it says it prepared something overnight and no checked item matches, it's told to
   correct itself on its next turn, whatever `claims.mode` is (short of `off`).
5. **The morning handoff** (`handoff.py`). Said inside the morning rundown ("Overnight I prepared a review sheet for
   ITCS 6150 AI Midterm, checked. Keep, drop or change any of it?"), or at your first turn after the night if there
   is no rundown. Nothing is said at night.
6. **Your answers** (`decide_overnight_work`): approve (keep it; for one it only planned, start it now), reject (not
   prepared again for a month), or redirect with how you want it (and `redo` to redo it your way now).
7. **What would you pick** (`learn.py`). When an item is planned it guesses whether you'll want it, from your past
   answers (overall, then the same kind, then the same course or project). The guess is kept on the item and scored
   against your answer; `overnight_work report=true` shows how often it's right. Its guesses only shape the plan
   (dropping what it's sure you'd turn down) once it has `steer_min_decisions` (10) answers and is right at least
   `steer_agreement` (70%) of the time.

## Limits, held in code

- Prepared work only. Research steps get the read-only task tools (no send, submit, buy, delete, call or text tool
  exists for them), every plan passes `rules.refused` before it runs, and each step's instruction carries the limits.
- Code only at the `projects` level, only in projects named in `proactive.projects`, only on an objective's branch.
  Merging stays yours (`merge_objective_work`). Its own code is never a target: that's the night shift's job.
- The usage budget: the night is skipped while its daily share is spent or your 5-hour window is 60% full or the
  week 80% (`usage_budget`), and every step of every item asks again before it runs.
- One objective is always left free for yours (`objectives.max_open`).
- Runs only inside `night` (01:00-08:00) once you've had no turn for `away_min` (30) minutes, once a night.

## Levels

| `proactive.level` | What happens |
|---|---|
| `off` | Nothing. |
| `suggest` | It plans at night and asks in the morning before doing anything. |
| `drafts` (default) | It prepares up to `max_items` read-only things overnight. |
| `projects` | Also one small change on a branch of a project in `proactive.projects`. |

## Records

`data/proactive/items.json` (each item, its check, its guess and your answer), `nights.jsonl` (what each night
found, skipped, refused and started), `model.json`, `state.json`.
