"""Playbooks: what she learned about an app or a site from her own screen and web tasks, so the next task there starts
knowing where things are, what worked and what failed (and the fix).

* ``book``: a playbook (a few short notes for one app or site), what names it, and where it's kept: in her long-term
  memory, as a note kept apart from the facts about the user (``memory.base.APART``), so it has the same provenance
  ("observed", the date) and its rewrites keep the old version as history.
* ``trace``: the record of one task's steps, taken by a wrapper round the task's executor, which also hands the task
  the playbook of an app or a site the first time it gets there.
* ``writer``: after a task, one tool-less brain call rewrites the playbooks of the apps and sites it worked in from
  that record; code decides what's kept (``writer.check_answer``).

The app wires it to screen and web tasks (``App._start_playbooks``); ``app_playbooks`` lets the user see and forget
them. User doc: docs/playbooks.md.
"""
