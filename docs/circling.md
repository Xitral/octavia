# Circling something with the mouse

Move the pointer in a loop round something, no buttons held, and talk to it: it's what "this" means. A faint blue ring
shows it saw it and fades in about a second. There is nothing to learn, no sound, no words it has to hear, and nothing
happens if you don't go on to talk to it.

Settings: `circling.enabled`, `circling.linger_s` (20), `circling.ring`.

## How it was decided

The user asked for an adversarial look first, because this one lives or dies on feel. The questions, and what settled
each:

**Normal mouse movement must not set it off.** Judged on real movement, not on guesses: a recorder logged only the
user's pointer positions while this was built, and every version of the detector was replayed against the log. The first
version, which looked for a path that turned a full circle and was roundish, fired twice in 7 minutes on ordinary use: a
scribble, and going down a page and back up a narrow track. So a loop now also has to: wind round its own middle with
hardly any going backwards (not only turn a lot), be no longer than 1.75 times the ring round its box (a scribble is
far longer), have no hairpin anywhere (no more than about 130 degrees of turn in a tenth of its length; long ovals get
more at their tips), never stall for over a quarter of a second, take 0.18 to 3.2 s, reach all the way round, and be at
least 44 px across. Tall thin loops are held to 2.6:1, wide ones to 5:1: a line of text gets circled, going down and up
a page doesn't. Results are in `tests/test_circling.py`, including the two real false alarms' kinds.

**A false alarm has to cost little.** It's still possible, and idly circling the pointer while thinking looks exactly
like the real thing: no detector can tell them apart. So a circle does nothing by itself: no speech, no question, no
sound, only a ring that fades in about a second, once however many times you go round without resting. If you do talk to
it in the next 20 s it judges from your words whether you meant it, and the turn still pays a little whichever it was:
the crop goes with it and stays in the conversation, and it counts as having read the screen, so something you ask it to
remember in that turn is filed as observed rather than as your words, and an unasked step may get a question. That is
the price of its seeing it without a round trip; 20 s rather than longer keeps it rare.

**What counts as referring to it?** Nothing is matched. There's no list of "this", "that", "here": its brain gets the
picture and a note saying that when your words point at something on screen without naming it, they mean what's circled,
and to leave it alone otherwise. Tried against the real model: "what's this about?" answered about the circled error and
not the text around it, in 1.08 s to the first word, and "what's the capital of France?" with the same picture got Paris
and no mention of it.

**How long is it meaningful?** Until it's used, or 20 s. People usually gesture just before or as they speak, but "circle,
think, then ask" is real, and a too-long wait costs little since its brain still judges it against your words. Once a
turn has taken it, it's in the conversation, so "translate it" a minute later just works, and it isn't offered again. The
stop hotkey forgets it, and a turn that fails before it saw it gives it back, so saying it again still works.

**Circling while talking, or just after.** While it's capturing your words a loop counts a bit before it closes (300
rather than 340 degrees), so it isn't lost when you finish speaking first. If the loop lands only after your words went
to the brain, its next `look_at_screen` says where in that picture you circled.

**Lag.** The pointer is polled at 60 Hz (12 Hz once it has rested a second) from its own thread. A low-level mouse hook
would be simpler and would sit in the path of every mouse move you make, so anything holding Python up would make your
whole mouse lag: polling can't. The picture is taken at the moment the loop closes, in a few tens of milliseconds, so what
it sees is what was there when you circled it, even if the page has scrolled since. The picture comes with your words
rather than through a tool it has to call, which saves a whole model round trip (about 1.3 s, its usual API wait) before
it first word, and a small crop adds very little to each later request (about 0.4 s per MB in the conversation).

**The ring.** A small native layered window of its own rather than one of its Electron apps: it's up within milliseconds,
whether or not those apps run, and fading it only changes one number, so it costs nothing per frame however big it is.
It's click-through, can't take focus, stays out of screenshots and recordings (`visuals.hide_from_capture`) and takes its
panels' blues, with a dithered, scanlined haze. It shows once, when it saw the circle: a second one as a turn took it
would have said "it has it" even when the turn then failed.

## Rules that keep it safe

- Never while its own screen task has the mouse, while a button is held, or while the pointer is hidden or held in place
  (a game looking around).
- A screen that can't be read (locked, a secure desktop) gets no ring: it'd have nothing to show for it.
- Pixels are in memory only, and go when the circle lapses. They leave the PC only with a turn of yours said or typed at
  the PC (voice, its console, its chat window), not one from your phone, a Discord call, or one of its own.
- The picture goes only with the words you end up saying, so a turn with a live circle doesn't start early in your pause:
  a picture sent early can't be unsent if you go on to say otherwise. That costs the head start of an early turn.
- It never sends more than a look would: the picture goes only when its look at the screen needs no question
  (`permissions.tools: {look_at_screen: auto}`, as you have it). Otherwise it's told you circled something and looks the
  usual way, and that look names where. Saying not to look at your screen in the same breath ends the circle.
- A picture can show others' words, so a turn with one counts as having read the screen (`READS_OUTSIDE`): a risky step
  after it is asked, as after a look. Text in the picture can't ride your turn.
- The local model can't see pictures, so such a turn goes to Claude. If only the local model is there (Claude down, or set
  to local only), it's told it can't see what you circled and to say so rather than guess. The quick voice stands aside
  while a circle is live.

## Where

`src/octavia/gesture/circle.py` (the judgment, pure), `gesture/watch.py` (the thread, the picture, `CircleMemory`),
`platform/circle_ring.py` (the ring), `UserTurn.picture` (`brain/base.py`, sent by `brain/claude.py`),
`Assistant._circled`, `App._start_circling`. All protected from its own builds (`evolve/policy.py`).
