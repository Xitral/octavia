"""Skills: small, tested, reusable sequences of steps her screen and web tasks worked out, saved so a later task can
run them in one call (``use_skill``) and build bigger ones on top of them.

* ``skill``: a skill (named steps of the task tools, with inputs filled in when it's used) and where skills are kept:
  in her long-term memory beside the playbooks (``memory.base.APART``, category "playbook:skill:<name>"), so every
  change is a new version with the old ones kept as history, and forgetting one removes it with its history.
* ``rules``: what a skill may hold and do, checked by code before one is saved and again before each use (only the
  task's own tools, never a restricted one, nothing secret written into it, at most a few levels of skills in
  skills). Protected from her own builds, like the permission policy.
* ``runner``: ``SkillExecutor`` wraps a task's executor: a skill's steps go through it one by one, with every check
  the task's own steps get, and stop at the first that fails, which is said out loud. It also checks a skill a task
  proposes against the steps that task really took.
* ``repeats``: the manual sequences her tasks keep repeating, counted across tasks with no Claude usage, so she can
  offer to save one as a skill.
* ``wiring``: the hooks to screen and web tasks, through the playbooks' (``playbooks.wiring.TaskPlaybooks``).

Nothing is saved without the user's yes (``save_skill``), and only after its checks pass. User doc: docs/skills.md.
"""
