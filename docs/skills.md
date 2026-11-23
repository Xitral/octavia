# Skills

Its screen and web tasks can save what worked as a skill: a short, named list of steps (open the Design tab, type
the theme's name, click Apply) that a later task runs in one call, with what changes each time (the theme) filled in.
A tricky Canvas page or an app's flow is worked out once, and the next task there just uses it. Skills can use other
skills, so bigger ones are built from smaller ones.

Nothing becomes a skill without your yes, and only after its checks pass.

## Where skills come from

- **A task works something out.** When a screen or web task gets something done that would help again, it can
  propose it (`propose_skill`). The proposal is kept only if its rules pass (below) and its steps are steps that task
  really took and saw go through, in that order, with the very arguments it gave them (a skill it runs is matched step
  by step too): a proposal can't hold a step nobody saw work. When the task ends it
  says what it worked out and asks "Want me to save it as a skill?".
- **It keeps doing the same thing by hand.** After each task it counts the steps it took itself (no Claude usage).
  When the same run of at least 4 steps, two or more of them doing something, has turned up in 3 tasks in the same
  app or site, it offers to save it: "I've done the same 5 steps in PowerPoint by hand in several tasks now (...).
  Want me to save them as a skill?". What was typed and which files were opened are never kept for this, only that
  something was; each becomes an input of the skill.

A plain "yes" to either offer saves it (no second question). "Save that as a skill" does too. A "no", or saying
nothing, leaves it: a proposal nobody says yes to is dropped after 7 days, and a repeat you turned down isn't offered
again for 30 (nor is one you saved and later forgot).

Proposing and noticing need to know where a task was, which the playbooks tell: with **Learn each app and site** off,
saved skills still run, but nothing new is proposed or offered.

## Using it

Nothing to do: every screen and web task is told which skills it has (those for the app or site its instruction names
first) and runs one when it fits (`use_skill`). To look or tidy up:

- "What skills do you have?" / "Show me the theme skill." (`skills`: its steps, inputs, versions and how it's done)
- "Save the theme skill." / "Turn the theme skill back on." / "Bring back version 2 of it." (`save_skill`, asked)
- "Forget the theme skill." (`forget_skill`: asked for a saved skill, since it's gone for good with its versions; a
  proposal is just dropped)
- Settings window: **Skills** turns them off; **Offer skills for repeated steps** stops the offers. `skills` in
  `config/local.yaml` also has how many tasks (`min_tasks`, 3) and steps (`min_steps`, 4) a repeat needs.

## Status

- **proposed**: waiting for your yes; it never runs.
- **trial**: saved, not yet run cleanly in a real task (the task is told so).
- **ready**: it has run cleanly at least once.
- **off**: it failed 3 times in a row, so it turned it off and said so. `save_skill` turns it back on, on trial.

## When a skill fails

Its steps run one at a time and it stops at the first one that fails or doesn't show what that step expects. The task
is told which step and why, looks at the screen before it does anything else (the screen task's usual rule after a
step that didn't go through, which also keeps a skill from starting right after one), and finishes by hand or says
what's left. You hear it once per task: "My skill “Pick a theme” failed in “slides”, at step 2 of 3 (...): ...".
Three failures in a row turn it off, out loud. A step you said no to, or a pause or stop of the task, isn't the
skill's failure and isn't counted.

## Versions and removal

Skills are kept in its long-term memory beside the playbooks, apart from the facts about you (they never go in its
conversation prompt or show up in `recall`). Every change (your yes, a new title, turning it off, an older version
brought back) is a new version, and the last 10 are kept as history. How each version has been doing is counted in
`data/skills/stats.json`; the steps counted for repeats are in `data/skills/repeats.jsonl` (the last 60 tasks).
Forgetting a skill deletes it with all its versions.

## Safety

A skill is a shortcut, never a permission.

- **Every step goes through the task's own checks.** A skill's steps are run through the same executor as the task's
  own steps: the screen task's questions out loud for anything risky (sending, buying, deleting, typing into a
  terminal), the web task's send/buy/delete rules, your pause or stop, and the look-after-a-failure rule. A skill can
  do nothing the task couldn't do step by step.
- **Only that kind of task's tools.** A screen task's skill can use only a screen task's tools, a web task's only a
  web task's; never a restricted tool, never its self-improvement tools, and never the skill tools themselves (a skill
  runs another only as a skill step, at most 3 levels deep, never itself, at most 80 steps in all).
- **Nothing secret written into it.** Whatever it types or puts on the clipboard is always an input, never part of
  the skill (it may be a password), also where one skill fills in another's inputs; and nothing that looks like a
  secret, an email address or a long number is kept.
- **Checked again before each use.** The rules are checked when a skill is proposed, when you say yes, and before
  every run, against the tools tasks have that day.
- **Protected from its own builds.** The rules (`src/octavia/skills/rules.py`) and their tests can't be changed by a
  self-improvement build; the rest of the skills code is always shown to you before a build that changes it goes in,
  and a build can't declare its own `save_skill`, `use_skill`, `propose_skill` or `forget_skill`.
