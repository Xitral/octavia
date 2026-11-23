"""Objectives: an outcome the user hands her ("make progress on my game while I'm away") that she plans, splits into
steps, runs beside the conversation, has checked by something other than the agent that did the work, and reports on.

* ``models``: the objective and its steps, and what each status means.
* ``store``: the list, kept in ``data/objectives/objectives.json`` so it survives a restart.
* ``projects``: the user's git projects she may code in, and how each one is checked.
* ``session``: one guarded Claude Code session (the planner, a code step, a reviewer).
* ``guard``: what each of those sessions may touch.
* ``prompts``: what each session is told, and the shape of its answer.
* ``runner``: plans, runs and verifies the steps, within limits, and resumes after a restart.
* ``report``: "what have you been doing?", across objectives, background tasks and her own builds.

The tools are in ``tools/extensions/objectives.py``; the app builds the runner (``App._start_objectives``). See
``docs/objectives.md``.
"""
