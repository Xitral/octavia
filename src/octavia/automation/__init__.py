"""When-this-do-that automation: one event bus every watcher publishes to, and the user's rules that act on it.

* ``events``: ``BusEvent``, the one schema (type, source, time, payload, provenance, sensitivity), and the event types
  known so far.
* ``publish``: the assistant's typed bus events (mail seen by the Gmail watcher, the app in front, TickTick and timer
  changes, builds, the lead tracker) turned into ``BusEvent``s, so existing watchers publish without changing.
* ``store``: one SQLite file with the rolling event log (kept ``RETENTION_DAYS``), the rules and every run.
* ``rules``: a rule (event pattern, conditions, time window, freshness, actions), matching, and how risky each
  action is (``classify``).
* ``engine``: ``RuleEngine`` matches events to rules and runs their actions through the tool executor, with the
  safeguards (chain depth, rate limits, loop detection, kill switch); also the dry run.

Tools: ``tools/extensions/automation_rules.py``.
"""
