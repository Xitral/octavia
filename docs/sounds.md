# Sounds

Short sounds tell you what it's doing, so you can follow it with headphones on and your back to the screen.
They're synthesized when it starts, no audio files, and play the instant something happens: over its voice rather
than after it.

There are three packs. **Ether** (the default) is soft and far away, as an old console's menu might have sounded:
the falcon's cues note for note, re-skinned as glass chimes with a slow shimmer, soft breaths of air and a faint pad
under the few that open or close something, then all put through one chain so they read as one family (pitched a
touch down, gently saturated, a long soft hall with an octave shimmer in it, the top dulled above 6.5 kHz). It sits
back further than falcon. **Falcon** is sleek and quick: glassy pings in D minor, rushes of air like a falcon's dive
and a short airy room around them. Both level every sound to the same loudness (ether counting its ringing), scaled by how much it should stand
out. **Classic** is it first set: chimes and music-box bells. Pick one in its settings window (Sound pack) or with
`audio.sound_pack`. All three are made from scratch: no recorded or borrowed sounds (see `docs/sound-credits.md`).

| You hear | It means |
|---|---|
| air rising into a chord | it's started and is ready (ether and falcon) |
| a rising two-note chime | it heard the wake phrase and is listening |
| a soft loop (ether and falcon: a sparse scan of glints over a low breath, ether slower and in a long hall; classic: a music box) | it's thinking about your request, before it starts on it (not for a quick answer, and never while it speaks or listens). Once it's working, the blips take over; the loop only comes back if it's gone quiet for 12 seconds, to say it's still on it |
| a tiny blip | a click, typed text, key press or scroll of its own |
| a camera shutter | it took a screenshot (with the avatar shown, right on its camera's flash) |
| a curious rise | it's about to ask you to approve something |
| a bright step up | it heard your **yes** |
| a soft step down | it heard your **no**, or got no answer (which counts as no) |
| a rising arpeggio | a task of several steps is done |
| a three-note sparkle | it's speaking up on its own (a build is ready, a reminder) |
| two soft falling notes | it stopped listening and is waiting for the wake phrase again |
| a step down / a step up | the mute hotkey turned its listening off / back on |
| a quick slide down | the stop hotkey stopped it |
| a low buzz | something went wrong |
| three bright rings | a timer is done |

Only your own requests make the working sounds: a scheduled job it runs on its own stays silent until it has
something to say.

## Settings

In `config/local.yaml`:

```yaml
audio:
  earcons: true          # false = none of these (a timer still rings)
  earcon_volume: 0.25    # 0 to 1
  sound_pack: ether      # or falcon, or classic
  working_sound: true    # false = no loop while she works; the other sounds stay
```

## Working quietly

Say "just work", "talk less", "stop narrating" or "work quietly" and, for the rest of that conversation, it doesn't
speak between the steps of a task: only what it says once it's done (or a question when it's stuck). The blips
tell you it's at it. "Talk me through it" or "you can narrate again" brings its commentary back.
To always work that way, set `conversation.narrate_work: false`.

## Other apps while it speaks

While it speaks, every other app that's playing (Spotify, a browser, a game, Discord), or that starts playing while
it speaks, fades down to a quarter of its own volume in the Windows Volume Mixer, and fades back to exactly where it
was when it's done, also when you cut it off. Apps are found on every output device, so with SteelSeries Sonar it
reaches the Media and Gaming channels, not just Chat where its voice plays. Its voice and sounds are never lowered,
nor are Sonar's own mixer, Voicemod and other audio routers (they carry its voice too), and an app you turned up or
down meanwhile stays where you put it. Ask it to change it ("keep the music louder when you talk", "duck to 10
percent", "don't lower Discord", "stop lowering other apps"): the `audio_ducking` tool saves it in `data/ducking.json`
(`enabled`, `level` from 0 to 1, `skip` app names, and `fade_down_s` / `fade_up_s`, 0.3 and 0.8 seconds).

## Real sounds in its answers

When hearing something helps explain it ("what does a peregrine falcon sound like?"), it can play a short clip in the
middle of its answer: it says a line, the falcon's call plays, then it goes on. The clip waits its turn in its speech,
so it never plays over its voice, and it uses one or two at most. It's found by description among your own WAV files
in `data/sounds/`, then Windows' sounds, then Wikimedia Commons, or from a WAV file path or link. Only WAV files can be
played; clips are cut to 12 seconds and leveled to its voice.

## For developers

- `src/octavia/core/sound_cues.py` (`SoundCues`) maps events to sounds; the wake chime, the shutter and the timer
  are played by the assistant itself, the mute hotkey's by the app.
- `src/octavia/audio/earcons.py` (`Earcons`) builds the chosen pack: the classic sounds are there,
  the falcon ones in `src/octavia/audio/falcon_sounds.py`, the ether ones in `src/octavia/audio/ether_sounds.py`. A new
  cue needs a sound in each (or a stand-in, as classic has for muting, or none, as it has for starting up); ether
  must have exactly falcon's cues. Falcon and ether level their sounds with `level`, and each one's `TIERS` says how
  much quieter than a standard cue one is. Every ether sound goes through `ps2` (the loop through `ps2_loop`), and
  none may run past `LONGEST_S` (1.95 s) tail and all.
- `AudioSink.overlay` mixes a sound over the speech queue without delaying it or its playback marks, and
  `AudioSink.set_loop` keeps one looping under everything with a fade in and out (`SoundDevicePlayer` mixes them
  in its callback, before the echo canceller's tap, so the canceller hears them too).
- `SpeechStream.play_clip` queues a sound between two sentences of a reply (no `SpeechSegmentStarted`: it isn't its
  voice); tools reach it through `ctx.extras["play_in_reply"]` (`Assistant.play_during_turn`), as
  `play_sound_in_reply` (`src/octavia/tools/extensions/reply_sounds.py`) does.
- Tests: `tests/test_sound_cues.py`, `tests/test_reply_sounds.py`.
