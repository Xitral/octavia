# Objectives

Hand it an outcome instead of a command, and it works on it while you do something else: it plans it, splits it
into steps, runs them beside the conversation, has every step checked by something other than the agent that did it,
and tells you once when it's over.

## Using it

- "Make progress on my game while I'm away." (code, on a branch of that project)
- "Get the research for my history essay done: three solid sources on the Marshall Plan with quotes."
- "What have you been doing?" / "How's my game coming?" / "Anything need me?"
- "Tell me more about the game one." (every step, what its agent said, what the checks found)
- "Merge it." (exactly the verified commit goes into the branch your project has checked out, as one merge commit;
  it refuses on a detached HEAD or with uncommitted changes to tracked files)
- "Stop the essay one."
- "What projects can you work on?"

It says one line when an objective ends (or texts it on Discord when you're away), never one per step.

## What happens

1. **Plan.** A read-only Claude Code session looks at the project (or the web) and writes at most 8 steps, each with
   what to do and how a reviewer can tell it's done. Code checks the plan: known kinds, dependencies only on earlier
   steps, code only with a project that has commits.
2. **Self steps** (only for an objective about it, with no project): its self-improvement builds the change on a
   copy and its gate checks it (its tests, protected files, a replay of real requests). It says nothing about the
   build itself; the objective reports it, and installing waits for your word, as with every update of its own. The
   planner reads its own code for these, never its keys, settings or `data/`, and gets no web beside it.
3. **Research steps** run on the background-task machinery (web plus its read-only tools), two at once by default.
4. **Code steps** run one at a time, in order, in a git worktree of the project under
   `data/objectives/worktrees/`, on branch `octavia/objective-<n>`. Your checkout is never touched.
5. **Every step is verified.** Code: its runner commits the change, runs the project's check itself, then a reviewer
   session that didn't write it reads the diff against the acceptance criteria. Research: a reviewer grades the
   result and spot-checks it on the web. A step that fails gets one more try with what was wrong; then it's marked
   failed, its code is rolled back, and steps that need it are skipped.
6. **Report.** It keeps apart what you asked, what it planned, what agents said they did, what was verified, and
   what's unresolved. Only verified steps count as done.

## The checks it runs on your projects

Found from the project's files, never chosen by a model:

| Project has | Check |
|---|---|
| `Cargo.toml` | `cargo test` (one shared target folder per project, under `data/objectives/cache/`) |
| `package.json` with a real `test` script and installed `node_modules` | `npm test` (else `npm run build`) |
| a `.venv` and a `tests/` folder | that venv's `python -m pytest` |
| a `.sln` | `dotnet build` |

Set your own in `config/local.yaml`:

```yaml
objectives:
  checks:
    myrustgame: "cargo test --lib"
```

No check means code steps are only reviewed, not tested, and it says so. If the checks already fail before it
changes anything, they can't vouch for its code, and it says that too.

## What a code step can and can't do

It reads and edits files in its worktree, and runs only the project's check (with extra arguments, such as a test
name; for npm only after `--`), the project's other checks (its `test`/`lint`/`format`/`typecheck`/`build` package
scripts unless they or their pre/post hooks deploy, publish or start something; `node --test <file>`; `cargo
check`/`clippy`/`fmt --check`/`build`; ruff; `dotnet test`) and read-only git, and reads the output its own long
commands saved. It can't change manifests, lock files, build scripts, test-runner
settings, git's ignore, attribute and hook files, or CI (`Cargo.toml`, `package.json`, `build.rs`, `vitest.config.*`,
`.gitignore`, `.husky/`, `.github/`...), add packages, reach the web, or touch anything outside the worktree. A guard
checks every call, with the same path rules as its self-improvement builder, and the runner re-checks the committed
files afterwards.

Its commits on the branch skip the project's git hooks (a step could edit tracked ones, and a formatter hook would
change code after it was checked). A merge into your checkout runs your hooks as usual.

A borrowed `node_modules` (a link to your checkout's, only when git ignores it there) exists only while a code step
works and while the check runs. Every git command that touches files (add, commit, reset, clean) runs without it, so
nothing can be committed, reset or deleted through it into your folder.

As with self-improvement, the check runs code the step wrote, on your PC, with your permissions. The guard keeps the
session itself boxed in; it can't see what that code does when it runs, and it isn't a sandbox. Read the diff before
you merge.

## Limits

- At most 3 objectives at once (`objectives.max_open`), 8 steps each, 2 attempts per step, 3 restarts per step.
- Time limits: planning and reviews 15 minutes, a code step 45, a check 20.
- Usage: each step waits while your 5-hour or weekly Claude limit is as full as `usage_budget` allows
  (`objectives.respect_limits`). Its daily share for its own work doesn't apply: you asked for this.
  "What have you been doing?" says how much of your weekly limit moved while each one ran.
- It can't plan sending messages, publishing, deploying, buying or deleting. Merging is yours to say.

## Restarts

Everything is in `data/objectives/objectives.json`. A restart (an update, a crash, a reboot) cuts a running step
off; at the next start its unverified changes are reset away and it runs again, with what was wrong with its last
attempt. A step stays pending until its attempt really starts, so one waiting for its turn or for your usage limits
loses nothing to a restart and isn't counted as cut off. Restarts into new code wait while a step is being worked on
or checked.

## Settings

`objectives` in `config/default.yaml`: `enabled`, `project_roots` (empty: the folder its own checkout is in),
`checks`, `max_open`, `research_parallel`, `model`/`effort` (planner and code steps), `review_model`/`review_effort`,
`code_timeout_min`, `check_timeout_min`, `web`, `respect_limits`. Voice mode only.

## Code

`src/octavia/objectives/` (`runner.py` is the heart), tools in `src/octavia/tools/extensions/objectives.py`, wiring in
`App._start_objectives`. Tests: `tests/test_objectives.py`, `tests/test_objectives_app.py`.
