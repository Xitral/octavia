"""Octavia Futures: her evidence-based model of the user's world, of her own performance, and of what tends to follow
from what (docs/futures.md).

It is not another memory. Everything starts as a normalized event (``events``) made by ordinary program logic, not by
the model: a task ticked off, an app brought to the front, a build that failed its tests. From the stream she keeps
a world model of entities and relations (``world``), compresses activity into episodes (``episodes``), measures
outcomes (``outcomes``), and only then forms beliefs about them: hypotheses whose confidence is earned from counts
(``hypotheses``), predictions she scores herself on (``predictions``) and, with the user's yes, small experiments
(``experiments``). "Remind me when..." lives in ``prospective``; whether she speaks up at all is decided, with
cooldowns and budgets, in ``proactive``; every recommendation keeps its reasons (``explain``). What she may learn
from, and what the user has put off-limits, is in ``privacy``.

A correlation is never a causal claim here: only an experiment where she varied one thing on purpose can make one.
Claude is one cognitive resource among others (``reasoning``); nothing in this package needs it.

This file and ``privacy.py`` are protected from self-improvement builds, and the rest is always shown to the user
before it installs (evolve/policy.py): a package's ``__init__`` can redirect its imports, and the privacy rules are
the user's.
"""
