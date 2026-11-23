# Self-improvement

The assistant can fix and extend its own code. When it can't do something you
asked, gets something wrong, or crashes, it notes that. When you say "go ahead",
a separate coding agent builds the change, automatic checks test it, and after
you approve, the assistant installs it and restarts into the same conversation.
Each installed change is also a building block for the next one.

## Turning it on

1. Make the first commit once (builds start from a git commit):

   ```powershell
   uv run octavia evolve setup
   ```

2. In `config/local.yaml`:

   ```yaml
   self_improvement:
     enabled: true
   ```

3. Start the assistant as usual (`uv run octavia`). It now runs under a small
   supervisor process that restarts it into updates and undoes an update that
   crashes it.

## Using it

Talk to it:

- "You couldn't tell me the weather. Build yourself a weather tool."
- "Next time you get the time zone wrong, fix it." (it notes the problem)
- "What have you noted about yourself?" / "What's ready to install?"
- "How's the update going?" It tells you how long the build has been running
  (and how long recent ones took), what it's doing right now (the developer
  agent writing code, or the final checks), and how far through its plan it is.
- "Install it." It asks once, says it'll be right back, and restarts in a few
  seconds with the conversation intact.
- "Install them all." Every ready update goes in with one question and one
  restart. Several at once (or one built on older code) are first tested
  together with the newest code, in the background: it carries on talking and
  tells you if something doesn't pass. Anything that can't go in is left out,
  and it says why.
- "Build it and install it when it's done." You approve once; when the build
  passes, it installs itself and restarts once it's idle. If something in it
  deserves a look (a core file, edited tests, a new tool that would run without
  asking you), it tells you and waits for your "install it" instead.
- "Undo your last update."

When a build finishes, it tells you out loud (or only in the console, with
`announce: console`) and listens for your answer without the wake phrase. You
can cut an announcement short with the wake phrase or the hotkeys, like any
reply.

With `auto_build: all`, something it notes during a conversation starts
building right away, and it tells you it's on it (instead of offering to build
it), then tells you how the build ends, good or bad. With `auto_build: errors`,
fixes for errors in its own code are built quietly: you only hear about the ones
that succeed.

Builds run in the background, up to `parallel_builds` at once (2 by default; 1
means one at a time); a request beyond that waits its turn and starts when one
of the running builds finishes. Each build works in its own copy of the code,
and their developer agents work side by side (using Claude usage at the same
time), but their final test runs take turns, so they don't slow each other
down. Builds that finish side by side are installed like any others: merged
onto the newest code and tested together, leaving out one that clashes.
Installing (or undoing) while builds are running is fine: the update goes in
right away, and the restart waits until every running build is done, so nothing
is cut off. Installs and undos while a restart is already on its way join that
same restart. "Restart now" works even mid-build: it asks once, restarts right
away into whatever is installed, and the builds it cuts off start over by
themselves after the restart (they don't count against the daily limit again).
While an install is being tested it can't be dropped or undone halfway (it
says so); ask again once it's in.

Some things can't be built: the machinery that builds, tests, installs and
undoes updates, the permissions, the settings and the operating instructions are
protected. Ask for a change there and it says so instead of starting a build;
make those changes yourself (for example with Claude Code).

From the terminal:

```powershell
uv run octavia evolve list          # what's noted, building, ready, installed
uv run octavia evolve show 3        # the checks and the exact code change of #3
uv run octavia evolve build 3       # build #3 in this window, with progress
uv run octavia evolve build --request "add a tool that reads my battery level"
uv run octavia evolve install 3     # then restart the assistant
uv run octavia evolve install 3 5   # several, tested together first
uv run octavia evolve install --all # everything that's ready
uv run octavia evolve reject 3
uv run octavia evolve undo
```

Reading `evolve show` before you install is the best way to stay in control:
it lists every changed file, new tools and the risk level they declare, and
anything flagged for your attention.

## What keeps it safe

- **The builder works on a copy.** Each build gets its own git worktree
  (`data/evolve/worktrees/`). Your running assistant is untouched until you
  install.
- **Every step of the builder goes through a guard (code, not AI).** It can
  read and edit files only inside its copy, and it can run only the test suite,
  the linter and read-only git commands. It has no web access and no other
  programs.
- **Protected files can't change.** The self-improvement machinery, the
  permission policy and tool executor, the list of calls your own words can
  approve without a question and which words count (`src/octavia/tools/asked.py`),
  the configuration, the operating instructions (`personality/system.md`), the
  dependency lists (Python's, and the avatar app's npm packages and install
  script: `avatar/package.json`, its lock files, `.npmrc` and `node_modules`)
  and the git setup are off-limits, and so are OCTAVIA Futures' privacy controls
  (`src/octavia/futures/privacy.py`, the package's `__init__.py` and `tests/test_futures_privacy*.py`): a build
  can't loosen what it may learn, keep or bring up. So is any file Python would import in one's
  place: a package folder of the same name (`src/octavia/tools/asked/`), the
  same name with another ending (`asked.pyw`), or a copy in a `octavia/` folder
  at the top of the project. The packages those files are imported through
  (`src/octavia/__init__.py`, `tools/__init__.py`, `core/__init__.py`) are
  protected too, because a package can send its imports to another folder.
  A build that touches them is rejected, however it
  happened, so a build can never loosen its own rules. Add more with
  `extra_protected_paths` (the same look-alikes are covered).
- **One file per module.** A build that adds a second file for a module that
  already has one (`speech/__init__.py` beside `speech.py`, or a `octavia/`
  folder at the top beside `src/octavia/`) is rejected too, protected or not:
  Python would quietly use one and ignore the other, so what you reviewed might
  not be what runs. Turning a module into a package, removing the old file, is
  fine.
- **A build isn't lost to what isn't its fault.** The builder runs only the
  tests of what it changes (the gate runs them all) and formats only its own
  files; the gate formats them too, and applies ruff's safe fixes, before it
  checks. A few failing tests in files the build didn't touch are run again on
  their own and, if they still fail, on the code before the build: passing
  alone (unless they use code the build changed), or failing before it too,
  they don't count against it, and the build is flagged so it never installs
  itself. A ready build that no longer merges with newer code is redone once
  on top of it: its change is put back with a three-way merge, and the builder
  only resolves what clashes (flagged for you to look at).
- **Checks are re-run by code, not taken on trust.** After the builder says it's
  done, the gate diffs the change itself, runs ruff and the full test suite
  (including the tests that use the real speech models), and flags anything that
  deserves a look: core files, anything in the voice-input path (wake word,
  speech recognition, audio), the screen tools (`screen_capture.py`,
  `screen_pointing.py` and `_uia.py` in `src/octavia/tools/extensions/`), the
  mouse, keyboard and file tools (`computer_control.py`, `_input.py`,
  `files.py`: what counts as a risky click or file decides what runs without
  asking), anything in the desktop avatar app (`avatar/`), OCTAVIA Futures (`src/octavia/futures/` and its
  tools: what it records about you and when it speaks up on its own), edited or deleted tests,
  anything that changes how pytest runs, new tools that run without asking (or
  that are restricted and so would never run), large changes.
- **Speed and quality on real requests.** Passing tests don't say it still
  picks the right tool, or how long it takes. Once they pass, the gate replays
  54 requests you really made (`src/octavia/evolve/eval_cases.yaml`, from
  `data/routing_log.jsonl`) through the build's system prompt and tool list,
  with your model and effort, every tool faked (nothing runs; a web search is
  stopped the moment it starts it), beside the commit the build started from.
  A build fails when a request that commit handles fails twice in a row on the
  build (and still passes on that commit when asked again), when its median
  reply is clearly slower
  (paired case by case, at least 0.75 s and 20 %), or when every turn reads
  over 3,000 more tokens. The builder then gets one more round with the report.
  Nothing runs when the build didn't change what the model sees (prompt, tool
  specs, brain code, model settings); a recent run of the current code is
  reused (`data/eval/runs/`), so a run costs about 55 short Claude turns, two
  or three minutes, never more than nine. When Claude can't be reached, or the
  code it is compared with doesn't load, the build is flagged instead of failed. Every run is added to `data/eval/history.jsonl`, a dated record.
  To compare any checkout with the main one yourself:
  `uv run python -m octavia.evolve.evals ab <checkout>`; to replay the cases
  with the code on PYTHONPATH: `... evals run [--cases id,id]`. Both the
  cases and the harness are protected, so a build can't grade itself.
- **Only you decide what skips the question.** Your settings (`config/`,
  protected) say which tools go ahead on their own (`auto`). "What's on my
  screen?" is its own yes for a screenshot, but which tools that works for and
  which words count live in protected code, so no update can add a tool to that list or loosen
  the words by changing a file. Only the real screen tools get that yes: the
  very classes, loaded from their own files. A tool that takes one's name,
  copies it or claims to be it is asked as usual. The screen tools themselves
  decide what of your screen goes to Claude, so a change to them (or to a file
  standing in for one) is always flagged and never installs itself.
- **The avatar app is checked less, so it's always yours to look at.** The
  checks can't run the Electron app: they only syntax-check its JavaScript and
  run its unit tests (with `node`, when it's installed). A change to its code
  says so, and like every flagged change it never installs itself. Read it with
  `octavia evolve show` first.
- **You approve every install** (unless you choose `auto_install:
  safe_changes`, which still never auto-installs anything flagged). An update it
  installs on its own waits until it's idle, says so, and then restarts.
- **Your code only gets what passed.** An update built on older code, or
  several installed together, are merged and tested in a separate worktree
  first; only if the tests pass does your code move forward to exactly that
  result.
- **Everything is a commit.** Each install is one merge commit by
  "<name> (self-improvement)", so `git log` shows exactly what changed, and
  `octavia evolve undo` reverts it. If an update crashes the assistant within 30
  seconds of its first start, the supervisor reverts it automatically and tells
  you (updates installed together are undone together). That covers installs by voice, from `octavia chat` and from `octavia evolve
  install`, as long as the next start is a normal `octavia` run.
- **Usage is capped.** Builds use your Claude subscription (`builder_model`,
  default Opus). `max_builds_per_day` limits them (`parallel_builds` only sets
  how many run at once), and nothing is built unless you ask, unless you set
  `auto_build`.

What it can't protect against: the builder's tests run the code it wrote, on
your PC, with your permissions, before you review it. The guard keeps the
builder itself inside its copy, but the code it writes could do anything a
Python program can. That includes changing other code while it runs: an
ordinary new file could, once loaded, replace the protected list of calls your
words approve in memory, or the permission check itself. The protections above
look at which files a build changed and what tools it declares; they can't see
what its code does at run time, so only your review catches that. The code is
written by Claude from your own requests and its own logs, not by strangers, so
the realistic risk is a mistake, not malice. Still, only turn on
`auto_install` if you're comfortable with that.

## Limits

- **No new packages.** A feature that needs a new dependency (a Python package,
  or an npm package for the avatar app) is reported as "no change" with the
  reason. Add the package yourself (or ask Claude Code), then ask again.
- **New tools arrive with a restart.** The brain's tool list is fixed while
  it's connected, so installing restarts the assistant (a few seconds) and
  resumes the same conversation.
- **Uncommitted edits block only the updates that touch the same files.** If
  you (or Claude Code) changed a file an update also changes, that update waits
  until you commit; everything else installs and your edits stay as they are.
  Builds start from your last commit, so uncommitted edits aren't in them.
- **A health check only catches crashes.** An update that starts fine but
  behaves badly (for example, hears you less well) isn't undone automatically.
  That's why voice-input changes are always flagged for review. Say "undo your
  last update", or run `uv run octavia evolve undo`.
- **It gets better at the edges, not in its core.** The model behind it doesn't
  change. What grows is its code: tools, fixes, behaviour. That compounds, since
  each new tool makes the next request easier. But every change is limited by
  what the tests can check and by your review, so expect steady growth, not an
  explosion.

## Code you commit yourself

When you commit changes to it yourself (with Claude Code, say), it notices and
restarts into them on its own, so you don't have to ask. It waits until the
checkout has stayed put for `system.new_code_quiet_s` (three minutes), so a
series of commits goes in as one restart, and then until it's idle: no
conversation, nothing to say, and no build, install, screen task, background
task or call running. Commits that only touch docs and tests don't restart it.
It also waits while a merge or rebase is under way, while the checkout is on
another branch, or while its code has uncommitted edits, and it first imports
the new code in a separate process. If it doesn't load, or it changes its
dependencies (`pyproject.toml`, `uv.lock`, which need `uv sync` with it off),
it stays on what it has and says so once.
It says "I'm back, on your latest updates." when it's back. It needs the
supervisor (as its own updates do); `system.restart_for_new_code: false` turns
it off (`octavia/new_code.py`).

## Its night shift

At `self_improvement.nightly_at` (03:00 by default; `""` turns it off) it
goes through the turns since the last night (`data/routing_log.jsonl`) and
looks for patterns, not single bad turns: turns that ended in an error (two or
more of a kind), turns that kept you waiting over eight seconds for it first
word (three or more that started the same way, e.g. with `look_at_screen`), and
times you had to correct it or ask why it was sitting there. The worst few
(`nightly_builds`, 3) become improvements (source `nightly`) and are built like
any other, so each fix has to pass the gate, speed and quality check included.
A pattern whose last build failed or was dropped isn't built again by itself,
and `max_builds_per_day` still holds. Then it runs its real-request check on
the code it runs now (free when nothing the model sees changed).

Until `morning_report` (09:00) build results are shown on the console, not
said. The first time you talk to it after it, it sums the night up in a few
sentences: how many turns failed or were slow, what it started and where each
fix stands, and its check's numbers. "What did you do overnight?" works too.

Each night is a line in `data/evolve/nights.jsonl`, drawn as charts in
`data/evolve/progress.html` (its real first-word time, slow and failed turns,
the check's pass count, latency and tokens per turn): a dated record of whether
it is getting better. `octavia evolve night` shows what tonight's shift would
find (`--run` does it now); `octavia evolve progress` prints the record and
redraws the page.

### Mistakes it catches itself

The same night it also looks for single requests it got wrong
(`src/octavia/evolve/mistakes.py`), with no Claude or local-model calls: right
after one of its answers you said again what you wanted ("no, I meant...",
"I just told you to...", "no, I want you to"), asked the same thing again, said
it went wrong ("that's not what I asked", "why are you just sitting there"), or
brought the app its tool was for (Spotify, TickTick...) to the front yourself
after it fumbled it. For that last one only which app was in front when is
read (screen history's timeline and Futures' watch); browsers never count.

Each becomes a case of its real-request check, kept in `data/eval/caught.json`
(outside the code, so no build can change it). What it should have done comes
from how your correction was answered when you let that answer stand: tools it
used then and not the first time are expected, and driving the screen (click,
keys, scrolling) it dropped is forbidden. After a mere complaint its next move
is usually looking into it, so nothing is expected from it. When it did the
same both times you were correcting the facts ("I meant Steam"), which its check
can't grade, so nothing is kept.

New cases are candidates. One is graded once you keep it, or by itself once it
has a tool to expect and the same request (after the same words before it) went
wrong again and was settled the same way. Short requests ("try it now") are
never counted together, since they mean something new each time. At most eight
turns' worth are graded (a case that needs what you said before counts twice),
so the check keeps to its time. Graded cases stay for good, so a later change that
breaks one is caught. Each night, graded cases it still fails get a fix started
(two at most, ahead of the other findings); a case that fails both before and
after a build never fails that build, since the gate only counts requests it
handled before.

    octavia evolve mistakes                   # what it caught (--all: dropped too)
    octavia evolve mistakes show 3
    octavia evolve mistakes keep 3            # grade it for good
    octavia evolve mistakes keep 3 --expect play_on_spotify --forbid click
    octavia evolve mistakes drop 3            # not a mistake: not graded, not caught again
    octavia evolve mistakes scan [--days 30] [--dry-run]   # look through the log now

Or ask it "what mistakes have you caught?"; it can keep or drop one by number
when you say so, and set what it should have done (`caught_mistakes`). Apps
you keep out of screen history are never named in a case. The morning report says how many new ones
it caught.

## How it works

```text
note (brain tool / error watcher) ──► journal (data/evolve/journal.db)
build_improvement ──► worktree + branch octavia/<id>-<slug>
                      builder agent (Claude Code, guarded) edits + runs tests
                      gate: protected paths, flags, ruff, pytest, eval on real requests (one retry)
                      ──► ready ──► announced
install_improvement ──► merge --no-ff into main ──► health marker ──► restart (exit 75)
supervisor ──► restart; resume the conversation; announce
              crash before healthy? ──► git revert the merge, restart, announce
```

A build cut off by an intentional restart (an automatic install) is queued again
and rebuilt after the restart. One you asked for that was cut off by a crash or a
shutdown is marked failed, and the assistant tells you at its next start so you
can ask for it again. `octavia evolve list` and `show` also record an undo the
supervisor made while the assistant was down.

Code: `src/octavia/evolve/` (`policy.py` guard rules, `builder.py`, `gate.py`, `evals.py`,
`git.py`, `journal.py`, `watcher.py`, `manager.py`, `tools.py`,
`supervisor.py`). New tools the builder writes go in
`src/octavia/tools/extensions/`, which is loaded automatically. Worktrees have
no copy of the downloaded models, so the builder and the gate point
`OCTAVIA_MODELS_DIR` at the main checkout's `models/`. Paths starting with
`models/` follow that variable only for the checkout's own configuration, so a
test using a temporary project folder never touches the real models. The gate
runs pytest with the protected `pyproject.toml` as its only configuration, and
counts the tests as passed only when pytest reports passed tests with no failures.
