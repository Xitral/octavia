# Routing: which turns skip the brain

Every turn you speak or type passes these checks in order, and the first one that fits answers it:

1. **Stop or dismissal:** "stop", "never mind". It stops, with no model call.
2. **Preset reply:** "thanks". It answers from `personality/quick_replies.yaml`, instantly.
   **Say that again:** "can you repeat that?", "I didn't catch that", "what did you just say?". It says its last
   reply again, word for word, with no model call (`core/say_again.py`); a screen task carries on meanwhile. Only the
   whole utterance counts, so "pause the music and repeat what you said" goes to the brain.
3. **Quick command (the rule fast path):** the tool runs directly and it says a short line, with no model call:
   - timers: "set a timer for 5 minutes", "10 minute timer for the laundry", "cancel the timer";
   - volume: "volume up", "turn it down", "set the volume to 40 percent", "mute", "unmute";
   - playback: "pause", "resume", "next song", "skip", "go back a song";
   - the time: "what time is it";
   - who answered: "which model was that?" It says the model that wrote its last answer (from what Claude
     reports, e.g. "Claude Opus 4.1"), or that a rule or preset answered with no model. Asked any other way, the
     brain knows too: every turn tells it where the previous answer came from.
4. **Small talk (its quick voice):** "how's it going", "that's hilarious", "I'm so tired today", "lol yeah". A
   small Claude session with no tools (Haiku, on your subscription) answers in its own personality, its first word in
   about half a second instead of the brain's 1.5 to 2.5. It's tried only for short things you say or type (16 words
   at most) that aren't a request ("open Spotify") or a real question ("what's the weather?"), and not while a screen
   task runs, while it works quietly, or when you're answering its question or offer ("want me to put it on?").
   - It sees the last few lines of the conversation, so it stays on topic, and it never claims to have done or looked
     anything up. With each message it also gets a few notes (`SystemPromptBuilder.small_talk_context`): its
     about-me notes, its mood today, and up to ten plain facts about you from its memory (where you live, family,
     favorites), never money, credentials, health, grades, heartache or how-to notes. It waits at most 0.15 s for
     them. When what you said needs a tool, real information or thought, it says so with a hidden token (never
     spoken) and the brain answers as before. So does a quick voice that's slow (nothing within 2.5 s), fails or isn't
     connected: you only lose the time it took, usually about half a second.
   - You can talk over it like any reply. "Which model was that?" names it (e.g. "Claude Haiku 4.5"), and the brain's
     next turn is told what it said, so the conversation carries on.
5. **The brain** (Claude) for everything else.

A quick command counts only when it's the whole thing you said, give or take "hey OCTAVIA", "can you" and "please".
For example, "play something to study to and remind me in 20 minutes" goes to the brain. If a quick command's
tool fails, the brain gets it with the error, so it never just goes quiet. The next turn tells the brain what was
handled, so "make it ten minutes instead" makes sense. Each call goes through the same permission checks and audit
log as one the brain makes.

## Settings (`config/local.yaml`)

```yaml
routing:
  fast_path: "on"       # on | shadow (the brain answers, the log notes which rule would have) | off
  log: true             # one line per turn in log_file
  log_file: "data/routing_log.jsonl"
fast_chat:
  enabled: true         # false = the brain answers small talk too
  model: "haiku"
  max_words: 16         # longer = straight to the brain
  timeout_s: 2.5        # no reply started by then = the brain answers
  context_turns: 6      # recent lines of the conversation it sees
```

## The log and its report

`uv run octavia routing` (add `--days 30` for a month, or `--days 0` for everything) shows:
- how your turns were routed;
- how many needed no model call;
- how much small talk the quick voice answered, its median first word, and how often it passed to the brain (and
  why: handoff, timeout, error...); a passed turn's line has `fast_chat` and `fast_chat_s`;
- how many brain turns a rule could have answered (their only tools were quick-command ones), with those
  sentences, as candidates for new rules;
- the time from the end of your speech to the first sound and to the reply, p50 and p95, per route.

The log holds what you said; it stays on this PC (`data/` isn't committed).

## For developers

- `src/octavia/core/fast_path.py`: the rules (`RULES`), the tools they may call (`FAST_TOOLS`) and duration
  parsing.
- `src/octavia/core/routing_log.py`: `RoutingLog` (built from events) and `report`.
- `Assistant._fast_command` in `src/octavia/core/quick_turns.py`.
- `src/octavia/brain/fast_chat.py`: the quick voice (`FastChat`), which turns try it (`small_talk`), its prompt
  (`SMALL_TALK_PROMPT`, around its personality from `SystemPromptBuilder.persona_values`) and the handoff token.
  `Assistant._small_talk` routes to it; `FastChatStarted` / `FastChatFinished` events log it (route `fast_chat`).
- Tests: `tests/test_fast_path.py`, `tests/test_fast_chat.py`.
