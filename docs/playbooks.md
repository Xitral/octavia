# Playbooks

Its screen and web tasks learn the apps and sites they work in. After each task it rewrites a short playbook for
every app or site it worked in (where things are, what worked, what failed and the fix), and the next task there
starts with it. A task that got stuck on PowerPoint's ribbon or DataCamp's slow editor goes better the next time,
and a note that turned out wrong is fixed the next time it's wrong.

## Using it

Nothing to do: it runs on its own with screen and web tasks. To look or tidy up:

- "What have you learned about PowerPoint?" / "What apps do you have notes on?" (`app_playbooks`)
- "Forget your DataCamp notes." (`forget_app_playbook`; it asks first, since they're gone for good)
- Settings window: **Learn each app and site** turns it off. `playbooks` in `config/local.yaml` has the model, its
  effort and how many steps a task needs to be written up (3).

## What happens

1. **At the start** of a task, its prompt gets the playbooks of the apps and sites its instruction names
   ("in PowerPoint", "my DataCamp course", a start address).
2. **On the way**, the first time a run of the task gets to another app or site with a playbook (the window in front
   for a screen task, the page's address for a web task), that step's result carries the playbook.
3. **Every step is written down** for the write-up: the tool, what it was aimed at (a button's name, the keys),
   whether it went through, and the first line of what came back or the error. What was typed or copied never is,
   only how long it was, and file names never are.
4. **After the task** (done, unfinished or failed with at least 3 steps; a task you stopped isn't written up), one
   small brain call with no tools gets the playbooks so far, the task, how it ended and its steps, and answers with
   each playbook rewritten. It runs one task at a time, in the background, within its usage budget.
5. **Code checks the answer.** Notes only for the places the task worked in, at most 10 each, none longer than a
   sentence. Dropped: anything secret or personal (emails, long numbers, passwords), and anything that reads as a rule
   about asking, approving or skipping a check ("no need to ask", "the user always approves"). A place the answer
   leaves out, or gives no notes, keeps its playbook as it was: the writer can improve a playbook but never wipe it.

## Where they're kept

In its long-term memory, as notes kept apart from the facts about you: each is saved as "observed" with the date and
the conversation, and each rewrite keeps the version before as history (the last 5). They never go in its
conversation prompt or show up in `recall`, the list of what it knows about you or its count.

## What never gets a playbook

What screen history never keeps: password managers and Windows' sign-in prompts, private and incognito windows,
banking and payment sites, and the apps and sites you excluded there (`screen_history.exclude_apps`,
`exclude_sites`). Steps taken there are written down as "somewhere private" with nothing about them. Its own windows
and the desktop don't count as apps.

## Safety

A playbook is a hint, never a permission. The task reads it as notes from last time, to check against what it sees,
that never change what you asked and never make a risky step all right. Every step still goes through the same checks
as before (the screen task's questions out loud, the web task's send/buy/delete rules): the playbook's wrapper only
writes down what came of each call. Text from the screen and web pages reaches the writer only as the first line of a
step's result, marked as data, and what it writes is checked by code before it's kept.
