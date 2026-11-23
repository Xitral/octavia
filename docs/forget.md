# Forget the last stretch of time

Its tray menu has **Forget the last** (15 minutes, 30 minutes, hour, day), and its settings window has the same four
on the Privacy page. One confirm is the yes. Everything it kept from that time goes:

- what was said (turns, and the summary of a conversation that ran into it), notes it saved then (a note it
  corrected then is put back as it was), its journal, thoughts and feelings from then
- what OCTAVIA Futures saw and made of it, screen history, the computer events its automations watch
- its records of turns (claims, routing, what was heard), what it said on its own, failed requests not yet kept
- documents it read and promises it made then (both go into each fresh prompt), background tasks asked for then
  (a restart would run them again), panels from then, what its scheduled jobs said
- its logs, and its brains' transcripts with their tool results from then

Deleted rows are overwritten and its search indexes merged, so the words don't linger in the database files.

Before anything is deleted, every file that changes is copied whole to `octavia-backups` in your user folder, in a
folder named for the time and the choice. If that copy fails, nothing is deleted. The backups are yours to delete.

While it runs, it restarts into a new conversation and the forget runs first thing as it starts; it then says
"I've forgotten the last hour." With it off, the settings window does it right away. While
it's still starting up it does nothing and says to try again in a minute. If two are asked for before it restarts,
both windows go.

Not touched: settings, sign-ins, what it counts of your Claude limits, eval runs and night-shift scores, eval cases
you kept, background-task reports, overnight work, scheduled reminders, and the self-improvement journal. Older
records changed in place during the window (an entity's latest details, a thought marked as said) keep that change,
and transcripts of objective work in other projects' folders aren't reached.

From a terminal: `octavia forget 30 --dry-run` says store by store what would go; `octavia forget 30` does it (it asks
first; `--yes` skips that). Choices: 15, 30, 60, day.

How it works: `src/octavia/core/forget.py` (architecture section 55, decision D-029).
