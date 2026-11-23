# Voice effects

An effect changes how its voice sounds, on Fish Audio and on its local voice alike. It's off unless you pick one,
and off is its voice exactly as before, sample for sample.

| Effect | Sounds like |
|---|---|
| Radio | an old receiver: a narrow band, a small speaker pushed a little |
| Console | a ship's computer: a swaying double of its voice in a small bright room |
| Robot | a faint metallic buzz and ring under its voice |
| Room | a soft small hall around it |
| Deeper | a touch lower and slower (4%), like a tape played slow |

Pick one by asking ("use your radio voice", "robot voice", "talk like a robot", "turn off the voice effect", "your
normal voice"; "back to normal" clears it with its brain and voice picks), from the tray (Voice effect), or in its
settings window (Voice, Voice effect). It's kept across restarts in `data/settings.json` as `tts.voice_effect`, and
heard from its next reply. Its preset phrases ("On it!") take it too.

How it works (`src/octavia/audio/voice_effects.py`): each effect is a chain of small numpy stages (a filter, a
saturation, a ring modulator, a doubler, a room, a tape) run on its voice's audio after it's made and before its
volume, chunk by chunk. A stage carries what it needs from one chunk to the next, so a reply streamed in pieces sounds
exactly as it would in one go, and nothing is held back: the first word plays when it would without an effect. One
chain spans a whole reply, so a room's echo runs from one sentence into the next when the next is already waiting;
otherwise its last ring plays out, faded, right after the sentence (before a pause while a tool runs, a sound, or the
end), never over the start of what it says after it. With an effect on, its volume's limiter
always runs, so no effect can clip; each preset is level-matched to its plain voice. Discord calls and echo
cancellation hear the effect too, as both take what the speaker plays.

Not here: its pitch changed with its timing kept. That takes a heavier algorithm, and cheap ones warble; Deeper moves
pitch and pace together instead.
