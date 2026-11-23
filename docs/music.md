# Music: MIDI songs for BandLab

OCTAVIA writes and edits MIDI files for you to drag into BandLab (or any other DAW). BandLab has no public API, so
it can't put tracks in a project itself; it saves a `.mid` and tells you where it is. Each part arrives in
BandLab as its own track, where you pick the sounds you like.

Everything goes to **`Documents\Octavia\Music`** (wherever Windows keeps your Documents folder, OneDrive
included). Files are named for what's in them, like `2026-10-06 lofi-chill A-minor 80bpm.mid`, and nothing is
ever overwritten: a name that's taken gets ` (2)`, and an edit is always saved as a new file next to the
original. The tools only read MIDI files and create new ones in that folder, so they never ask first.

## The tools

| Tool | What it does |
|---|---|
| `compose_midi` | Writes a new multi-track song from a description. |
| `edit_midi` | Changes a file: transpose, tempo, extend, add a part, change an instrument, quantize, humanize, split. |
| `describe_midi` | Says what's in a file: tracks, instruments, tempo, meter, length, key, sections and chords per bar. |
| `preview_midi` | Plays a file through the basic General MIDI synth built into Windows, for a rough listen. |

Things to say:

- "Make me a dark trap beat in C minor at 140." / "A chill lo-fi loop, 8 bars." / "A pop song with an intro,
  two verses and choruses and an outro."
- "Use Am F C G." / "Put the chords on a Rhodes and the melody on a flute." / "Make it busier." / "Same thing
  with seed 4821."
- "Add drums to that." / "Transpose it up two." / "Make it 90 BPM." / "Make it 8 bars longer." / "Split it into
  tracks." / "Quantize it to sixteenths." / "What key is my song in?"
- "Let me hear it." / "Stop."

### compose_midi

| Setting | Default | Notes |
|---|---|---|
| `style` | pop | boom-bap (hip-hop), trap, lo-fi, house, pop, rock, drum and bass, reggaeton, funk, R&B; usual names work too (chillhop, dnb, dembow, drill, neo soul...) |
| `key` | suits the mood | "A minor", "F# major", "Bb minor", "D dorian". With chord symbols and no key, the key comes from the chords. |
| `tempo_bpm` | the genre's usual | 40-220; a mood nudges the default within the genre's range |
| `time_signature` | 4/4 | 3/4, 6/8, 7/8, 5/4... (genre grooves are for 4/4; other meters get grooves built from their beats) |
| `bars` | 16 | 1-256 |
| `sections` | by length | e.g. intro 4, verse 8, pre-chorus 4, chorus 8, bridge 8, outro 4 (also hook, drop, breakdown). Default: a loop up to 8 bars, verse and chorus up to 16, then intro, verses, choruses (a bridge in long songs) and an outro. |
| `mood` | | dark, chill, sad, happy, energetic, dreamy, aggressive, romantic, epic... (sets the mode, tempo and energy) |
| `parts` | the genre's | drums, bass, chords, melody, arpeggio, pad |
| `instruments` | the genre's | per part, a General MIDI name or number: `{"chords": "rhodes", "bass": "808", "drums": "808 kit"}` |
| `progression` | the genre's | roman numerals (`i VI III VII`, `ii7 V7 Imaj7`) or chord symbols (`Am F C G`); `V:2` makes a chord last 2 beats |
| `complexity` | 3 | 1 simple ... 5 busy: fills, ghost notes, sevenths and ninths, busier melodies and basslines |
| `humanize` | 0.5 | 0 puts every note exactly on the grid; 1 is loose, like a live player |
| `swing` | the genre's | 50 straight ... 66 triplet swing (lo-fi and boom-bap swing by default) |
| `seed` | random | the result says the seed; the same seed and settings give the same file |

What it writes:

- **Drums** on channel 10 with General MIDI drum notes, in the genre's groove: boom-bap with a lazy snare,
  trap with 808s and hi-hat rolls, four-on-the-floor house, two-step drum and bass, the reggaeton dembow, funk
  with ghost notes, and so on. Choruses add drums (open hats, claps, a ride), intros and bridges play lighter,
  every section ends in a fill in the genre's style (tom runs, snare rolls, trap rolls, a hip-hop drop-out) and a
  crash marks the next one.
- **Chords** voice-led: each chord's notes are chosen to move as little as possible from the last, kept in
  register, with no muddy low thirds; rock plays power chords, lo-fi and R&B roll their chords like a hand on
  keys.
- **Bass** on the roots in the genre's rhythm, with fifths, octaves and notes leading into the next chord; the
  hip-hop and trap bass plays with the kick.
- **Melody** in phrases built from a motif that comes back (as hooks do), chord tones on strong beats, passing
  and neighbor tones between, leaps answered by steps, an arch over each phrase, and a resolution at the end. A
  chorus over the same chords sings the same melody each time.
- **Arpeggio** and **pad** follow the chords too.
- The file has the tempo, time and key signatures and a marker at each section, which most DAWs show.

### edit_midi

Point it at a file by name (or part of one: "the lofi one") in the music folder, or a full path anywhere; with no
file it takes the newest one there. Several changes go in one call and happen in this order: `transpose`
(semitones, drums stay put), `tempo_bpm`, `extend_bars` (repeats the last phrase, before the outro if the file has
one), `add_part` (with optional `style`, `instrument`, `complexity`, `seed`), `change_instrument` (a track by its
number from `describe_midi`, its name, or "bass"/"drums"), `quantize` (1/4 ... 1/32, triplets 1/8t, 1/16t, with a
`quantize_strength`), `humanize`, and `split_tracks` (one file per track as well).

An added part fits the file: it's in the key `describe_midi` finds, over the chords it finds bar by bar, in the
file's meter and tempo, and follows its section markers when it has them. Without a `style`, the groove is chosen
from the tempo (under 76 lo-fi, then boom-bap, pop, house, trap, and drum and bass from 162).

### describe_midi

Lists each track (number, name, instrument, channel, range, note count), the tempo and meter (and changes), the
length in bars and time, the key (with how sure it is, and what the key signature says if it disagrees), the
section markers and the chords for each bar with roman numerals. Format 0 files are split into one track per
channel, as DAWs show them.

### preview_midi

Plays through the Microsoft GS Wavetable Synth that comes with Windows, via the MCI sequencer: no window and
nothing to install. It sounds like a basic General MIDI synth, nothing like BandLab's instruments, so it's only
for hearing the notes. `stop: true` stops it. On a computer without that synth it says so and suggests opening the
file in BandLab instead.

## How it's built

The MIDI work stands on established open-source libraries:

- [mido](https://github.com/mido/mido) (MIT) reads and writes the files.
- [music21](https://github.com/cuthbertLab/music21) (BSD-3) does the theory: reading keys, turning roman numerals
  and chord symbols into chords in a key, and finding a file's key (Krumhansl-Schmuckler). It's imported on first
  use, which takes a few seconds once.
- [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) (MIT, already used elsewhere) matches genre and instrument
  names.

On top of them, `src/octavia/music/`:

| Module | What it does |
|---|---|
| `presets.py` | Data: the genres (tempo ranges, swing, progressions per section, drum grooves, bass and comping rhythms, sounds), General MIDI instruments and drums, moods, section roles |
| `theory.py` | Keys, chords, extensions, roman numerals, chord and key detection (music21) |
| `meter.py` | Time signatures: bar lengths, beats, strong beats |
| `voicing.py` | Voice-led chord voicings |
| `grooves.py` | Drum parts: grooves, fills, crashes, hi-hat rolls |
| `parts.py` | Bass, chords, pad, arpeggio and melody |
| `feel.py` | Swing, strumming, humanizing |
| `compose.py` | A description -> a plan -> a song |
| `song.py` | The MIDI file (mido) |
| `analysis.py` | Key and chords of an existing file, and its description |
| `edit.py` | The edits |
| `library.py` | The music folder, file names, finding files |
| `preview.py` | Playing through Windows' synth |

The tools are in `src/octavia/tools/extensions/music.py`; tests in `tests/test_midi_tools.py` (they write to a
temporary folder and never play sound).
