# Local brain

A small model on this PC's GPU, beside the brain. Phase 1 (`docs/local-brain-phase1.md`) measured three models on its
eval set and picked Qwen3.5-4B: about five times faster to its first word than the brain, but wrong on about a
quarter of the cases, and it doesn't know when it's wrong. So it isn't trusted with any turn yet.

## Phase 2: shadow mode (`octavia.localbrain`)

After each of your turns, the local model is asked the same thing, and only what it would have done is written
down, next to what it did. It never answers, never runs a tool, and never holds up a turn.

- **When:** once its turn is over (`BrainFinished`, or the quick voice's `FastChatFinished`). Your own turns only:
  voice, typed and Discord, not scheduled jobs. One worker asks the model, one turn at a time. If turns come faster
  than it keeps up (four waiting), the oldest is dropped.
- **What it sees:** its system prompt without what changes daily (memories, recap, build status), built once at
  start, like the eval's, plus every tool's description and the last `history_turns` exchanges. llama-server keeps
  the prompt cached, so a turn reads only what's new. Only its first step is judged: a tool call is recorded with its
  arguments and never run.
- **The log:** `data/local_shadow.jsonl`, one line per turn: `text`, `route` (brain or fast_chat), `brain` (its tools,
  first word time, model, reply), `local` (its tools and arguments, its reply, `written` = markdown in a spoken
  reply, `first_s`, `total_s`, `prompt_ms`, `decode_tps`), `agree`, or `skipped` (game, not_loaded, error, busy,
  overlapped). `agree` compares its tools with it first step (`first_tools`: what it called before any result came
  back), since its first step is all it gets. A private argument (text it'd type) is logged by length only, as the
  tool log does. It holds what you said, like the routing log; lines older than 30 days go at start.
- **Overlaps:** when two of your turns run at once (a Discord text while you talk), tool calls can't be told apart,
  so both are skipped as `overlapped`.
- **Report:** `uv run octavia shadow [--days N]`: overall agreement, first word times side by side, and agreement by
  what it did (it first tool), with what the local model did instead.

### The GPU and games

- llama-server runs as a child process at below-normal priority, on 127.0.0.1 only (`local_brain.port`), in a
  Windows job that ends it when it exits or crashes. If something else already answers on that port, it doesn't
  start.
- It holds about 5 GB of the 7900 XT's 20 GB while loaded. Each shadowed turn runs the GPU nearly flat out for
  0.5 to 2 seconds, after its own turn.
- While a full-screen app it doesn't read (not a browser, Word...) is in front, turns are skipped. After
  `game_unload_s` (10 s; checked every 5 s, even mid-load) the model is unloaded and the game gets the memory back.
  It loads again once no game has been in front for `game_reload_s` (120 s): about 3 s to load and 14 s to read its
  prompt, in the background. A game in a window that doesn't cover the screen isn't noticed.
- If llama-server dies, it's started again at most every 10 minutes.

### Sharing the model

The addressee check (docs/addressee.md) asks the same server whether something overheard was meant for it. Each
has its own slot (`-np 2 -kvu --no-cache-idle-slots`, `localbrain/server.SLOTS`), so neither pushes the other's prompt
out of the cache. Without `--no-cache-idle-slots` the shadow re-read its whole 40k-token prompt after every check.

### Turning it off

`local_brain.enabled: false` in config/local.yaml. It's also off when `local_brain.server` or `local_brain.model`
isn't there (the defaults point at `E:\local-llm`, where phase 1 put them).

## Phase 3: answering (`local_brain.mode`, `octavia.localbrain.router`)

`mode: shadow` (the default) is phase 2 above. The other two put a router in front of the brain:

- **`route`:** the local model hears each of your own turns first, spoken, typed or texted over Discord, and, in one
  reply, either answers it (casual conversation, a quick fact it's sure of, arithmetic, or one simple action or check
  with `route_tools` and its read-only tools) or hands it to Claude with one line, `DELEGATE: <why>`
  (`localbrain/delegation.py`; the instruction is at the end of its system prompt, so the prompt stays cached; the
  why goes in the route log as `delegated_because`). Some turns go straight to Claude without asking it: a note on
  the turn that isn't only context (interrupted, a request on hold, something announced, a call...; what it already
  said, a tone guess, quiet mode, memories looked up, a call app on the mic and the Discord bridge's "texted from
  their phone" note are only context, and it's shown them; files or a voice message sent with a text aren't), longer
  than `route_max_words`, or risky or complex by policy (sending, writing, deleting, buying, the screen, files,
  remembering, tutoring, several steps, anything to look up, "play ..."). The quick voice (`fast_chat`) is off then, since it would take the small
  talk first.
- **Falling back to Claude:** its first step is asked whole, within `answer_timeout_s`, and checked before anything is
  said: handing over, an error, a timeout, a tool it may not use (not in `route_tools`, or not a safe one), arguments
  that don't validate, no tool call for a command (it'd only say it did it), or markdown in a spoken reply, and
  Claude answers instead. Once one of its tools has run, the turn stays its own.
- **`fallback_only`** and `route`: when Claude fails one of your turns before saying anything (out of usage, no
  connection...), the local model answers it, saying once that it's on its local brain. Besides `route_tools` and the
  read-only tools (find_on_screen, list_running_apps, get_current_time), it may then take one action per turn with
  click (by find_on_screen element number), type_text, press_keys, scroll, resume_screen_task or media_control: they
  run through the same executor as Claude's calls, so the risk checks, approvals and `submit_without_asking` apply
  exactly as for Claude. A second action is "the rest needs my cloud brain", and it never says it did something
  unless a tool that acts succeeded. For `claude_retry_s` (5 min) your turns go to it first, then Claude is tried again.
  If it can't answer either, you hear Claude's error as before. Claude's next turn is told what it said meanwhile.
- **The log:** `data/local_route.jsonl`, one line per turn: `brain` (local or claude), `why` (`route`, `down` or
  `claude`), `decision` (`answer`, `delegate`, `fallback`, or `policy` with the word in `policy`), `fallback` (what
  went wrong when Claude took over from it), `total_s`. "Which model was that?" names it too. In these modes the
  shadow comparison isn't written; in shadow mode it's asked with the same prompt, its line says whether it would
  have handed the turn over (`local.delegated`), and `octavia shadow` counts them.

### One mind (2026-10-25)

After the routing went live, the local model misrouted the simplest words: "yes" read as "hold off", "continue" played
the music instead of carrying on a DataCamp task, "Yeah." got its last answer again, it said it had started reading
PDFs it couldn't open, and once read its notes out loud. What changed:

- **One conversation.** The local model reads the same transcript Claude's turns come from (`RoutedBrain.conversation`
  = `Assistant.recent_lines`: every turn, whoever answered it, quick commands included), its last `history_turns`
  exchanges. Claude is still told what the local model said since its own last answer.
- **What hangs on the conversation is Claude's** (`delegation.follows_up`, policy `context: ...`): a bare reply ("yes",
  "sure", "continue", "do it", "what?"), an answer to a question of its own (its last line asked something), or words
  pointing back ("did you finish that", "the other one"). So is every turn while a screen task runs, is paused or
  waits on a question (`RoutedBrain.busy` = `Assistant.busy_with`, policy `work`), and anything only a tool could tell
  (health, school, to-dos, email, music, its builds: added to the policy words).
- **Sure, or Claude's.** Its first token's logprobs give its own odds of handing the turn over; when
  `1 - odds < route_min_confidence` (0.99) it's `fallback: unsure`, and every line logs `confidence`.
  It was 0.8 until 2026-10-26: replayed on 92 of the user's newest turns of the kind it hears, the 9B kept 40, only
  7 the way Claude handled them, and every one of those 7 was at 1.0, while 18 of the 33 wrong keeps were below 0.99.
- **A small focused job.** In `route` and `fallback_only` it gets a short prompt of its own
  (`delegation.focused_prompt`: who it is, in its personality file, when to answer and when to hand over, and
  examples of both) and only the tools it may call (`shadow.routed_tools`), not it ~42k-token prompt and every tool.
  `shadow` mode keeps its whole prompt, since it measures what Claude does.
- **Never pretending, never reading notes.** A reply that promises, claims or narrates an action (`CLAIMED`: "I'll
  start...", "I've picked it back up", "I'm working on...") is Claude's unless a tool that acts ran; one that repeats
  its own notes (`echoes_context`) is Claude's too.
- **Elsewhere:** a bare "continue", "resume" or "unpause" is never the media quick command (only "play", or the music
  named); a plain yes or no said while a screen task runs doesn't pause it.

## Fine-tuning (2026-10-26, `octavia.localbrain.tune`, `scripts/local_brain_train.py`)

Teaching the local model from its own turns, so it does what Claude would have done with them.

- **The data:** its Claude sessions (`data/claude-home/projects/*-data-brain/*.jsonl`) hold every turn of the user's it
  answered, exactly as rendered, with Claude's tool calls and reply. `python -m octavia.localbrain.tune build --root
  <her checkout>` turns them into the exact request the router sends the local model (`focused_prompt`, its tools,
  the last exchanges) and what it should have said: Claude's short spoken answer with no tool (**answer**), one call
  of a tool it may use and the reply to its result (**tool**), or `DELEGATE: <why>` for everything else. Turns the
  router never asks it about are labelled the same way and teach it which turns are Claude's. Its own warm-ups,
  Claude's failures, scheduled jobs and any turn with something that looks like a secret are left out. It writes to
  `E:\local-llm\finetune\data\route`, never into the repository.
- **Held out:** the newest 35% of the turns the router would ask it about (92 on 2026-10-26), and no training turn
  with the same words. `tune eval --url <llama-server> --label <name> [--lora 1.0]` replays them through
  `RoutedBrain._local_turn` itself (its checks, its confidence bar, tools answered with Claude's own results, nothing
  run), so a score is what the user would have heard: agreement (did what Claude did), wrong keeps (kept a turn that
  was Claude's), safe misses (handed over one it could have kept).
- **Training:** LoRA on Qwen3.5-4B with PyTorch for ROCm on Windows (E:\local-llm\finetune\.venv). Every example
  starts with the same ~3.9k tokens (prompt and tools), read once into a cache: ~10 s an example instead of ~2 min.
- **Never at its expense, never the PC's:** `--when-away 20` trains only after 20 minutes with no input, nothing
  full-screen and nothing said to it, and saves and stops at the first key, turn or STOP file. One run at a time
  (a lock). A hard 15 GiB cap: past it, it saves and stops (on 2026-10-26 a first run grew to 30 GiB, spilled into
  shared memory and the PC went down). While it trains, `data/local_brain_lent.json` tells it the GPU is lent:
  its model is unloaded as for a game and its turns go to Claude; the file counts only while the trainer lives and
  for five minutes after its last renewal.
