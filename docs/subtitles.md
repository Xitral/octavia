# Live subtitles

What it says shows beside it as it says it, word by word, so anything hard to follow by ear (a formula,
code, an exact figure) can be read. The subtitle shows what it means rather than the words it speaks: math as math,
code and numbers as written.

## How it writes it

Its replies are written for the ear. Where reading helps, it writes both forms in one mark (personality/system.md):

    That's [[$x^2 + 2x + 1$|x squared plus two x plus one]].

Its voice says the part after the last `|`; the subtitle shows the part before it, with TeX in `$...$` and `` `code` ``
(the panels' own Markdown and TeX readers draw it). A mark with no `|` is said and shown alike. Text messages and its
chat window show the shown form too. A mark is never cut at a sentence break inside it; one left open for more than
600 characters is treated as a stray `[[`.

## What shows, and when

- Word by word, in two short rows at most (2026-11-22: a sentence at a time read as a wall of text). Each word shows
  as it says it, the one it's on a touch brighter and whiter; the row before stays above, dimmed, and an older row
  drifts out over the top. A formula or a bit of code lights up whole. A sentence starts a new row, and each row is
  laid out once with all its words, so nothing rewraps as they come in.
- Each word's real time comes from its audio (`src/octavia/tts/word_times.py`, 2026-11-22: syllable pacing alone didn't
  match its voice word for word). Neither of its voices gives word timestamps, so once all of a sentence's audio is
  made (`SpeechSegmentAudio`, usually before it plays) its own speech recognizer (Parakeet, already loaded to hear
  you; on the CPU, sharing its loaded model and threads: about 0.27 s for a 6 s sentence; the GPU is left alone) times
  its words. Those
  are matched to the words its voice was given: a laugh, breath or filler its voice adds (an inline cue) matches
  nothing and is skipped, and a word heard differently ("3x" for "three x") takes a time between its matched
  neighbours. It hears its voice before any effect (radio, robot). While the sentence plays, the speaker's own count
  of what it has played (which stands still when the audio runs late, less what the device still holds) says which
  word it's on, and each goes to the subtitles as it reaches it (`SpeechWordStarted`, `segment-word`). On the page,
  shown words that are said as written match those words; a formula takes the words between (`startWords`).
  Hearing you and timing its words take turns on the recognizer's lock, so neither slows the other's threads (hearing
  you waits at most for one sentence's timing); nothing is timed with subtitles off. The start of each sentence is
  where the audio stood at its mark, noted by the speaker itself (`mark_played_seconds`), so a busy event loop can't
  make its words late.
- A sentence still coming in is timed from its audio so far (`SpeechSegmentMaking` carries it as it grows): once
  0.8 s is in, and again each 0.6 s more, up to the last word the recognizer heard as said that starts 0.45 s before
  the end of what's in (never a guess past it); all of it timed replaces those. Measured on its own voice
  (`scripts/word_drift/early.py`, 16 Fish clips at the delivery speeds in its log, 1.7x and 2.1x real time): words
  with real times before it says them went from 39-50% to 88-91%; the early times are within 80 ms of the final ones
  (median; p90 160 ms). Before this (2026-11-22, its log), 17 of 36 sentences, nearly all the first or second of a
  reply, waited a median 2.4 s (up to 6.6 s) for their times, paced meanwhile: the "still mistimed sometimes".
  Each pass hears only the audio from half a second before the last word already timed, so it costs about the same
  however long the sentence; a pass cut off (it's interrupted) keeps the recognizer until it's done, so hearing you
  never runs beside it. If real times stop for 0.9 s while it talks, the page's pacing carries on from the last real
  word. Each reply logs how many sentences were timed as they started and how late the rest were ("Word times this
  reply").
- Until a sentence's first times come, or without
  them (lip sync or recognizer off), the words are paced (`avatar/renderer/subtitles/words.js`): each weighs its
  syllables, plus the pause after a comma or full stop. Its length is the voice's own count of its audio; until that
  comes, its recent pace (learnt sentence by sentence) times what its voice says of it. The page's clock runs while
  its voice level (`mouth`) says it's voicing and half a second into a silence, then waits. When the next sentence
  starts, or it's done, the rest of the sentence shows at once.
- Drift (`scripts/word_drift/drift.py`, 2026-11-22): against words whose starts are known (Windows' voice, word by
  word), pacing was off by 171 ms on average (p90 416 ms, 62% of words within 150 ms), the word times by 51 ms (p90
  108 ms, 97%). On its own voice (16 Fish clips, inline cues and not), pacing was 211 ms off its word times on
  average (p90 470 ms), even with each sentence's length known.
- After it finishes, the last row stays a few seconds (longer for a longer line) and fades. Talk over it, or press
  the stop hotkey, and it goes at once. A new reply starts clean.
- "Say that again" repeats its words and the same subtitles.
- Short phrases ("On it.") show briefly too.
- Nothing shows for a sentence whose math or words one of its open pop-up panels already shows.

## Where

Up and to the left of the orb, in a box like a PS2 menu panel (2026-11-22, the user asked for a better one): hazy
dark-blue glass with its scanlines and grain, a thin cool rim with a soft glow, a faint lit top edge and corner ticks,
dark enough to read on a white page or a busy screen. Its bottom rim is the underline, which its voice lights, and a
fine line runs from it to its edge (its top rim when the words sit below it), with a few sparks as each row comes in. The box is the size of the rows that stay
(a row leaving fades out over its top; the rows that stay glide up into it), and nothing is clipped unless one row is taller than the
room.

Each word comes in like a PS2 menu's text: a pale, cool ghost of it, a little larger, blurred, with a faint red and
cyan fringe and every other line dimmed, settles onto it and sharpens as it fades, over about half a second (quicker
when it's lively); the word itself is there from its first moment and the ghost peaks within a tenth of a second, so it never reads late. The ghost is its own small layer beside the text, never under the text's filters, and it moves on
its voice's own 30 fps beat, so it costs little: GPU 3D 2.4% while it talks against 2.3% without it
(`subtitles-clip.mjs --measure`, 2026-11-22). A CSS animation of it ran at 60 fps and cost about 10% more of a CPU
core. With no room on its left (or above), it flips to it right (or below), always inside
the work area of its monitor. It follows it when you Ctrl-drag it, on any monitor, and its saved spot with it.
When it flies off to point, perch on a window or do a lap, the words go with it, beside its body (not its window,
which then also spans its arm and the spot), with the line to it; while it points they sit on its side away from
the spot, so they never cover it (2026-10-30: they used to stay at its home spot). With the character instead of the
orb, it sits beside its head.

The window never takes focus, lets every click through, and is left out of screenshots and screen shares (like its
own window, `avatar.hide_from_capture`). With the pointer on the words they fade, as it does with it on it (its
own `SeeThrough` rule: the main process polls the cursor while a line is up and the page decides), so what's under
them shows; they come back a moment after it leaves. It's part of the avatar app, so it needs the avatar running.

## Its state in it

The subtitles share the orb's PS2 look (hazy, a little desaturated blue, scanlines, dither and grain, no gloss, no gold)
and move with it the way the orb does, from the same messages rather than canned loops:

- Its voice level (`mouth`), eased slowly into a steady glow, softly brightens the underline and the line to it and
  thickens a faint haze round the words, which drifts only while it speaks. Nothing pulses or flickers with its
  syllables: the user found that distracting (2026-10-27).
- Each sentence's feeling (the `segment`'s emotion, or an `emotion`/`mood` between sentences, as the orb takes them
  through `FEELS`): a light one rises in from below and blooms a little fuller, a heavy one settles in from above,
  dimmer; a lively one comes in quicker with more sparks. Its glow leans a little cyan when it's light and violet
  when it's heavy, easing over. A mood (thanked, it stays a little happy) carries into its next reply's lines.
- Cost: when it's quiet nothing redraws. Its voice is followed at 30 fps with opacity and transform only, and a
  changing feeling never touches the words (their filters are the costly part); the lean eases only through the
  lines and sparks round them. `node avatar/tools/subtitles-clip.mjs --measure` prints the app's GPU and CPU share
  while it talks (2026-10-27: GPU 3D 1.7% against 1.3% before). Word by word (2026-11-22): 2.5% against 1.8%, with each word lit at once; a short fade per word under the text filters cost 9.7%.

## Settings

`avatar.subtitles` (Settings, Character, "Subtitles"; default on). It applies from its next sentence.

## Code

- `src/octavia/tts/shown.py`: the marks; `Spoken` segments carry their shown form through the chunker.
- `src/octavia/avatar/subtitles.py`: the subtitle for a sentence, and the panel check.
- `src/octavia/tts/word_times.py`: its words' times from its audio, announced as the speaker reaches each.
- `avatar/lib/subtitles.js`, `avatar/lib/subtitle-layout.js`: the window and where it goes.
- `avatar/renderer/subtitles/`: the page (look, timing, scrolling).
- Preview: `node avatar/tools/subtitles-demo.mjs --out <dir>` captures it beside a real orb.
- `avatar/renderer/subtitles/feel.js`: its feeling and voice for the page (its slow glow, haze drift, lean), tested in node.
- `avatar/renderer/subtitles/words.js`: the words it lights up and when (units, syllable weights, its clock and pace), tested in node.
- Motion: `node avatar/tools/subtitles-clip.mjs --out <dir> [--app <older avatar dir>]` films its speaking a few
  sentences with feelings, and `python avatar/tools/subtitles-video.py out.mp4 before=<dir> after=<dir>` puts clips
  side by side on a half-white, half-dark ground.
