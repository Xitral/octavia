"""Work she does before she's asked (docs/proactive.md).

At night, once the user has been away a while, she reads what she already knows of their week (``model``: Canvas,
the study tracker, TickTick, the calendar, their projects, their notes and their own past decisions, never copied
into a store of its own), finds things worth preparing (``rules``, plain code), lets one cheap model call rank them
(``planner``), and runs the best few as objectives with their plan already written, so each one is checked by a
reviewer that didn't do it. In the morning (``handoff``) she says what's ready and checked, inside the morning
rundown, and the user approves, rejects or redirects each item. ``learn`` guesses each answer before it's given and
keeps score, and its guesses only shape the plan once they've been right often enough.

What it may never do is held in code (``rules.refused``, read-only tools, branches only): prepared work, nothing sent,
submitted, bought, deleted, called or texted.
"""
