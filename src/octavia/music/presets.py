"""The data the composer works from: genres, the General MIDI instruments and drum notes, moods and section roles.

A genre (``Genre``) says how a style usually goes: its tempo range, swing, chord progressions for each kind of
section (in roman numerals, which music21 turns into chords in any key), drum groove, bass and comping rhythms, how
busy its melodies are and which General MIDI sounds play each part.

Rhythms are step strings, one character per sixteenth note of a 4/4 bar (16 per bar; 32 for a two-bar pattern):

- drums: ``X`` accent, ``x`` normal hit, ``v`` soft, ``o`` ghost note (only at complexity 3 and up), ``.`` rest;
- comping (chords): ``x`` a chord, ``-`` it keeps sounding, ``.`` silence;
- bass: ``R`` the chord's root, ``5`` its fifth, ``O`` the root an octave up, ``C`` another chord tone, ``A`` a
  note leading into the next chord's root, ``-`` the note keeps sounding, ``.`` silence; ``kick`` instead of a
  pattern means the bass plays with the kick drum (hip-hop and trap 808s).

Other time signatures get rhythms built for their beats (``grooves.meter_groove``) instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PARTS = ("drums", "bass", "chords", "melody", "arpeggio", "pad")

# --------------------------------------------------------------------------- General MIDI

GM_PROGRAMS = (
    "Acoustic Grand Piano", "Bright Acoustic Piano", "Electric Grand Piano", "Honky-tonk Piano",
    "Electric Piano 1", "Electric Piano 2", "Harpsichord", "Clavinet",
    "Celesta", "Glockenspiel", "Music Box", "Vibraphone", "Marimba", "Xylophone", "Tubular Bells", "Dulcimer",
    "Drawbar Organ", "Percussive Organ", "Rock Organ", "Church Organ", "Reed Organ", "Accordion", "Harmonica",
    "Tango Accordion",
    "Acoustic Guitar (nylon)", "Acoustic Guitar (steel)", "Electric Guitar (jazz)", "Electric Guitar (clean)",
    "Electric Guitar (muted)", "Overdriven Guitar", "Distortion Guitar", "Guitar Harmonics",
    "Acoustic Bass", "Electric Bass (finger)", "Electric Bass (pick)", "Fretless Bass", "Slap Bass 1",
    "Slap Bass 2", "Synth Bass 1", "Synth Bass 2",
    "Violin", "Viola", "Cello", "Contrabass", "Tremolo Strings", "Pizzicato Strings", "Orchestral Harp", "Timpani",
    "String Ensemble 1", "String Ensemble 2", "Synth Strings 1", "Synth Strings 2", "Choir Aahs", "Voice Oohs",
    "Synth Voice", "Orchestra Hit",
    "Trumpet", "Trombone", "Tuba", "Muted Trumpet", "French Horn", "Brass Section", "Synth Brass 1", "Synth Brass 2",
    "Soprano Sax", "Alto Sax", "Tenor Sax", "Baritone Sax", "Oboe", "English Horn", "Bassoon", "Clarinet",
    "Piccolo", "Flute", "Recorder", "Pan Flute", "Blown Bottle", "Shakuhachi", "Whistle", "Ocarina",
    "Lead 1 (square)", "Lead 2 (sawtooth)", "Lead 3 (calliope)", "Lead 4 (chiff)", "Lead 5 (charang)",
    "Lead 6 (voice)", "Lead 7 (fifths)", "Lead 8 (bass + lead)",
    "Pad 1 (new age)", "Pad 2 (warm)", "Pad 3 (polysynth)", "Pad 4 (choir)", "Pad 5 (bowed)", "Pad 6 (metallic)",
    "Pad 7 (halo)", "Pad 8 (sweep)",
    "FX 1 (rain)", "FX 2 (soundtrack)", "FX 3 (crystal)", "FX 4 (atmosphere)", "FX 5 (brightness)",
    "FX 6 (goblins)", "FX 7 (echoes)", "FX 8 (sci-fi)",
    "Sitar", "Banjo", "Shamisen", "Koto", "Kalimba", "Bagpipe", "Fiddle", "Shanai",
    "Tinkle Bell", "Agogo", "Steel Drums", "Woodblock", "Taiko Drum", "Melodic Tom", "Synth Drum", "Reverse Cymbal",
    "Guitar Fret Noise", "Breath Noise", "Seashore", "Bird Tweet", "Telephone Ring", "Helicopter", "Applause",
    "Gunshot",
)  # fmt: skip

# What people call sounds -> the General MIDI program closest to it.
INSTRUMENT_ALIASES = {
    "piano": 0, "grand piano": 0, "acoustic piano": 0, "keys": 4, "electric piano": 4, "e piano": 4, "epiano": 4,
    "rhodes": 4, "wurlitzer": 5, "wurli": 5, "dx7": 5, "clav": 7, "organ": 16, "hammond": 16, "b3": 16,
    "church organ": 19, "harpsichord": 6, "celeste": 8, "glock": 9, "bells": 10, "bell": 10, "music box": 10,
    "vibes": 11, "vibraphone": 11, "marimba": 12, "xylophone": 13, "chimes": 14,
    "guitar": 25, "acoustic guitar": 25, "nylon guitar": 24, "classical guitar": 24, "spanish guitar": 24,
    "electric guitar": 27, "clean guitar": 27, "jazz guitar": 26, "muted guitar": 28, "rock guitar": 29,
    "overdrive guitar": 29, "distorted guitar": 30, "distortion guitar": 30, "metal guitar": 30,
    "bass": 33, "bass guitar": 33, "electric bass": 33, "finger bass": 33, "pick bass": 34, "upright bass": 32,
    "double bass": 32, "acoustic bass": 32, "jazz bass": 32, "fretless": 35, "slap bass": 36, "synth bass": 38,
    "808": 38, "808 bass": 38, "sub bass": 38, "sub": 38, "reese": 39, "reese bass": 39, "moog bass": 39,
    "strings": 48, "string ensemble": 48, "orchestra": 48, "violin": 40, "viola": 41, "cello": 42,
    "pizzicato": 45, "harp": 46, "synth strings": 50, "choir": 52, "voices": 53, "vocal": 53, "ooh": 53,
    "trumpet": 56, "trombone": 57, "tuba": 58, "horn": 60, "french horn": 60, "horns": 61, "brass": 61,
    "synth brass": 62, "sax": 65, "saxophone": 65, "alto sax": 65, "tenor sax": 66, "oboe": 68, "clarinet": 71,
    "flute": 73, "pan flute": 75, "whistle": 78, "ocarina": 79,
    "lead": 81, "synth lead": 81, "synth": 81, "saw": 81, "saw lead": 81, "square": 80, "square lead": 80,
    "chiptune": 80, "8 bit": 80, "pluck": 46, "plucks": 46, "pad": 89, "synth pad": 90, "warm pad": 89,
    "polysynth": 90, "choir pad": 91, "ambient pad": 94, "sweep": 95, "kalimba": 108, "steel drums": 114,
    "sitar": 104, "banjo": 105, "koto": 107, "accordion": 21, "harmonica": 22,
}  # fmt: skip

# Drum kits are chosen with a program change on the drum channel (GS kits; plain GM players use the standard kit).
DRUM_KITS = {
    "standard": 0, "room": 8, "power": 16, "rock": 16, "electronic": 24, "808": 25, "tr-808": 25, "trap": 25,
    "jazz": 32, "brush": 40, "brushes": 40, "orchestra": 48,
}  # fmt: skip
DRUM_KIT_NAMES = {0: "standard kit", 8: "room kit", 16: "power kit", 24: "electronic kit", 25: "808 kit",
                  32: "jazz kit", 40: "brush kit", 48: "orchestra kit"}  # fmt: skip

# General MIDI drum notes (channel 10).
DRUM_NOTES = {
    "kick": 36, "snare": 38, "rim": 37, "clap": 39, "hat": 42, "pedal": 44, "ohat": 46, "crash": 49, "ride": 51,
    "tom_floor": 41, "tom_low": 45, "tom_mid": 47, "tom_high": 50, "tamb": 54, "cowbell": 56, "conga_hi": 63,
    "conga_low": 64, "timbale": 65, "shaker": 70, "clave": 75, "crash2": 57,
}  # fmt: skip

DRUM_VELOCITY = {"X": 118, "x": 98, "v": 72, "o": 48}


def program_name(program: int, drums: bool = False) -> str:
    if drums:
        return DRUM_KIT_NAMES.get(program, "drum kit")
    return GM_PROGRAMS[program] if 0 <= program < 128 else f"program {program}"


# --------------------------------------------------------------------------- genres


@dataclass(frozen=True)
class Genre:
    key: str  # what it's called in our own words, as used in file names: "lofi"
    name: str  # what it's called when we talk about it: "lo-fi"
    aliases: tuple[str, ...]
    tempo: tuple[int, int, int]  # (slowest, usual, fastest) BPM
    swing: int  # 50 = straight, 66 = triplet swing (MPC-style percent)
    swing_grid: float  # what's swung: 0.25 (sixteenths) or 0.5 (eighths), in beats
    mode: str  # the usual mode: "minor" or "major"
    # mode -> section role -> progressions (each a list of chords, one per chord slot; "V:2" lasts 2 beats).
    progressions: dict[str, dict[str, tuple[tuple[str, ...], ...]]]
    chord_beats: float  # how long a chord slot is, in beats
    color: int  # 0 keeps the chords as written; 1 adds sevenths, 2 sevenths and ninths (at higher complexity)
    groove: dict[str, str]  # drum -> step pattern
    chorus_groove: dict[str, str]  # merged into the groove in choruses and drops ("" removes a drum)
    hat_rolls: bool  # trap hi-hat rolls
    fill: str  # how fills go: toms, snare_roll, trap_roll, stop, breakbeat, dembow
    kit: int  # drum kit program
    bass: str  # step pattern, or "kick"
    bass_center: int  # MIDI note the bass line stays around
    comp: str  # chords step pattern
    strum: float  # seconds between a chord's notes (0: all at once)
    power_chords: bool  # chords are root-fifth-octave (rock guitar)
    arp_rate: float  # beats per arpeggio note
    melody_density: float  # notes per beat, about
    melody_grid: float  # the finest melody rhythm, in beats
    syncopation: float  # 0..1: how often melody notes land off the beat or anticipate it
    melody_center: int  # MIDI note the melody centers on
    parts: tuple[str, ...]  # what plays when no parts are asked for
    instruments: dict[str, int] = field(default_factory=dict)  # part -> GM program


def _p(*rows: str) -> tuple[tuple[str, ...], ...]:
    """Progressions written as strings: _p("i VI III VII", "i iv v i")."""
    return tuple(tuple(row.split()) for row in rows)


_DEFAULT_PARTS = ("drums", "bass", "chords", "melody")

GENRES: dict[str, Genre] = {}


def _add(genre: Genre) -> None:
    GENRES[genre.key] = genre


_add(
    Genre(
        key="boombap",
        name="boom-bap",
        aliases=("boom bap", "boom-bap", "boombap", "hip hop", "hip-hop", "hiphop", "rap", "old school", "90s hip hop"),
        tempo=(82, 90, 98),
        swing=58,
        swing_grid=0.25,
        mode="minor",
        progressions={
            "minor": {
                "verse": _p("i7 iv7 i7 v7", "i7 VImaj7 iv7 v7", "i7 i7 iv7 iv7"),
                "chorus": _p("VImaj7 VII7 i7 i7", "iv7 v7 i7 VImaj7"),
                "bridge": _p("VImaj7 v7 iv7 v7"),
            },
            "major": {
                "verse": _p("Imaj7 vi7 ii7 V7", "IVmaj7 iii7 vi7 V7"),
                "chorus": _p("IVmaj7 V7 iii7 vi7", "ii7 V7 Imaj7 Imaj7"),
                "bridge": _p("vi7 ii7 IVmaj7 V7"),
            },
        },
        chord_beats=4,
        color=1,
        groove={
            "kick": "x......x..x.....x......x..x..x..",
            "snare": "....X.......X.......X.......X...",
            "hat": "x.v.x.v.x.v.x.v.x.v.x.v.x.v.x.vx",
            "rim": "..........o..o............o....o",
        },
        chorus_groove={"ohat": "..............v...............v.", "tamb": "....v.......v..."},
        hat_rolls=False,
        fill="stop",
        kit=8,
        bass="kick",
        bass_center=38,
        comp="x-------x---.-..",
        strum=0.012,
        power_chords=False,
        arp_rate=0.5,
        melody_density=0.75,
        melody_grid=0.25,
        syncopation=0.35,
        melody_center=74,
        parts=_DEFAULT_PARTS,
        instruments={"chords": 0, "bass": 33, "melody": 73, "arpeggio": 11, "pad": 48},
    )
)

_add(
    Genre(
        key="trap",
        name="trap",
        aliases=("trap", "drill", "atlanta", "808", "rage"),
        tempo=(130, 140, 160),
        swing=50,
        swing_grid=0.25,
        mode="minor",
        progressions={
            "minor": {
                "verse": _p("i VI iv V", "i i VI VII", "i iv VI V"),
                "chorus": _p("VI VII i i", "i VI III VII"),
                "bridge": _p("iv iv VI V"),
            },
            "major": {
                "verse": _p("I V vi IV", "vi IV I V"),
                "chorus": _p("IV V vi vi", "I V vi IV"),
                "bridge": _p("ii IV V V"),
            },
        },
        chord_beats=4,
        color=0,
        groove={
            "kick": "x......x..x.....x.....x...x..x..",
            "clap": "........X...............X.......",
            "snare": "........x...............x......o",
            "hat": "xvxvxvxvxvxvxvxv",
        },
        chorus_groove={"ohat": "......v.........", "kick": "x......x..x...x.x.....x...x..x.."},
        hat_rolls=True,
        fill="trap_roll",
        kit=25,
        bass="kick",
        bass_center=34,
        comp="x---------------",
        strum=0.0,
        power_chords=False,
        arp_rate=1 / 3,
        melody_density=1.25,
        melody_grid=0.25,
        syncopation=0.4,
        melody_center=79,
        parts=_DEFAULT_PARTS,
        instruments={"chords": 0, "bass": 38, "melody": 10, "arpeggio": 10, "pad": 89},
    )
)

_add(
    Genre(
        key="lofi",
        name="lo-fi",
        aliases=("lofi", "lo-fi", "lo fi", "lofi hip hop", "chillhop", "study beats", "jazzy hip hop", "chill"),
        tempo=(68, 80, 92),
        swing=60,
        swing_grid=0.25,
        mode="minor",
        progressions={
            "minor": {
                "verse": _p("i7 iv7 VII7 IIImaj7", "VImaj7 v7 i7 i7", "i7 VImaj7 iiø7 V7"),
                "chorus": _p("IIImaj7 VImaj7 iiø7 V7", "iv7 VII7 IIImaj7 VImaj7"),
                "bridge": _p("VImaj7 VImaj7 v7 v7"),
            },
            "major": {
                "verse": _p("IVmaj7 iii7 ii7 Imaj7", "Imaj7 vi7 ii7 V7", "ii7 V7 Imaj7 vi7"),
                "chorus": _p("IVmaj7 V7 iii7 vi7", "ii7 V7 Imaj7 Imaj7"),
                "bridge": _p("vi7 vi7 ii7 V7"),
            },
        },
        chord_beats=4,
        color=2,
        groove={
            "kick": "x.........x.....x......x..x.....",
            "snare": "....x.......x.......x.......x...",
            "hat": "x.v.x.v.x.v.x.v.",
            "rim": ".......o.......o",
        },
        chorus_groove={"shaker": "..v...v...v...v."},
        hat_rolls=False,
        fill="stop",
        kit=40,
        bass="R-------R---5-A-",
        bass_center=38,
        comp="x---------------",
        strum=0.022,
        power_chords=False,
        arp_rate=0.5,
        melody_density=0.7,
        melody_grid=0.25,
        syncopation=0.3,
        melody_center=72,
        parts=_DEFAULT_PARTS,
        instruments={"chords": 4, "bass": 33, "melody": 11, "arpeggio": 46, "pad": 89},
    )
)

_add(
    Genre(
        key="house",
        name="house",
        aliases=("house", "deep house", "tech house", "edm", "dance", "club", "techno", "disco house", "garage"),
        tempo=(118, 124, 130),
        swing=54,
        swing_grid=0.25,
        mode="minor",
        progressions={
            "minor": {
                "verse": _p("i7 VImaj7 VII i7", "i VII VI VII", "i7 iv7 VImaj7 v7"),
                "chorus": _p("VI VII i i", "iv7 v7 VImaj7 VII"),
                "bridge": _p("VI VI VII VII"),
            },
            "major": {
                "verse": _p("IVmaj7 V7 iii7 vi7", "vi IV I V", "I V vi IV"),
                "chorus": _p("IV V vi vi", "ii7 V7 Imaj7 vi7"),
                "bridge": _p("IV IV V V"),
            },
        },
        chord_beats=4,
        color=1,
        groove={
            "kick": "X...x...X...x...",
            "clap": "....x.......x...",
            "ohat": "..x...x...x...x.",
            "hat": "v.v.v.v.v.v.v.v.",
        },
        chorus_groove={"shaker": "vvvvvvvvvvvvvvvv", "ride": "..v...v...v...v."},
        hat_rolls=False,
        fill="snare_roll",
        kit=24,
        bass="..R-..R-..R-..O-",
        bass_center=40,
        comp="..x-..x-..x-..x-",
        strum=0.0,
        power_chords=False,
        arp_rate=0.25,
        melody_density=1.0,
        melody_grid=0.25,
        syncopation=0.45,
        melody_center=74,
        parts=("drums", "bass", "chords", "arpeggio"),
        instruments={"chords": 0, "bass": 38, "melody": 81, "arpeggio": 80, "pad": 89},
    )
)

_add(
    Genre(
        key="pop",
        name="pop",
        aliases=("pop", "pop music", "top 40", "dance pop", "synth pop", "synthpop", "indie pop", "kpop", "k-pop"),
        tempo=(92, 112, 128),
        swing=50,
        swing_grid=0.25,
        mode="major",
        progressions={
            "major": {
                "verse": _p("I V vi IV", "vi IV I V", "I vi IV V"),
                "prechorus": _p("ii IV V V", "IV IV V V"),
                "chorus": _p("IV I V vi", "I V vi IV"),
                "bridge": _p("vi IV I V", "ii V iii vi"),
            },
            "minor": {
                "verse": _p("i VI III VII", "i VII VI VII"),
                "prechorus": _p("iv iv V V"),
                "chorus": _p("VI III VII i", "VI VII i i"),
                "bridge": _p("iv VI VII VII"),
            },
        },
        chord_beats=4,
        color=0,
        groove={
            "kick": "x.......x.x.....x.......x.x..x..",
            "snare": "....X.......X...",
            "hat": "x.v.x.v.x.v.x.v.",
        },
        chorus_groove={"clap": "....x.......x...", "ohat": "..............v."},
        hat_rolls=False,
        fill="toms",
        kit=0,
        bass="R-----R-R-----A-",
        bass_center=40,
        comp="x-----x-----x---",
        strum=0.0,
        power_chords=False,
        arp_rate=0.25,
        melody_density=1.15,
        melody_grid=0.25,
        syncopation=0.45,
        melody_center=72,
        parts=_DEFAULT_PARTS,
        instruments={"chords": 0, "bass": 33, "melody": 80, "arpeggio": 46, "pad": 48},
    )
)

_add(
    Genre(
        key="rock",
        name="rock",
        aliases=(
            "rock",
            "rock and roll",
            "punk",
            "pop punk",
            "alt rock",
            "alternative",
            "grunge",
            "indie rock",
            "metal",
        ),
        tempo=(100, 124, 160),
        swing=50,
        swing_grid=0.5,
        mode="major",
        progressions={
            "major": {
                "verse": _p("I bVII IV I", "I IV V IV", "I V IV IV"),
                "chorus": _p("IV I V V", "I V vi IV"),
                "bridge": _p("vi IV V V"),
            },
            "minor": {
                "verse": _p("i VII VI VII", "i III VII VI"),
                "chorus": _p("VI VII i i", "III VII iv VI"),
                "bridge": _p("iv iv VI VII"),
            },
        },
        chord_beats=4,
        color=0,
        groove={
            "kick": "x.......x.x.....x.x.....x.x.....",
            "snare": "....X.......X...",
            "hat": "x.x.x.x.x.x.x.x.",
        },
        chorus_groove={"hat": "", "ride": "x.x.x.x.x.x.x.x."},
        hat_rolls=False,
        fill="toms",
        kit=16,
        bass="R-R-R-R-R-R-R-R-",
        bass_center=40,
        comp="x-x-x-x-x-x-x-x-",
        strum=0.0,
        power_chords=True,
        arp_rate=0.5,
        melody_density=1.0,
        melody_grid=0.25,
        syncopation=0.25,
        melody_center=67,
        parts=_DEFAULT_PARTS,
        instruments={"chords": 29, "bass": 34, "melody": 30, "arpeggio": 27, "pad": 18},
    )
)

_add(
    Genre(
        key="dnb",
        name="drum and bass",
        aliases=("drum and bass", "drum & bass", "drum n bass", "dnb", "d&b", "jungle", "liquid", "neurofunk"),
        tempo=(165, 174, 178),
        swing=50,
        swing_grid=0.25,
        mode="minor",
        progressions={
            "minor": {
                "verse": _p("i VI III VII", "i7 VImaj7 iv7 v7"),
                "chorus": _p("VI VII i i", "iv VI VII i"),
                "bridge": _p("VI VI VII VII"),
            },
            "major": {
                "verse": _p("I V vi IV", "IVmaj7 V vi vi"),
                "chorus": _p("IV V vi vi", "vi IV I V"),
                "bridge": _p("IV IV V V"),
            },
        },
        chord_beats=8,
        color=1,
        groove={
            "kick": "x.........x.....x.........x.x...",
            "snare": "....X.......X.......X.......X...",
            "hat": "x.v.x.v.x.v.x.v.",
            "rim": ".......o.o....o.",
        },
        chorus_groove={"ride": "v.v.v.v.v.v.v.v.", "shaker": "vvvvvvvvvvvvvvvv"},
        hat_rolls=False,
        fill="breakbeat",
        kit=24,
        bass="R---------R---A-",
        bass_center=36,
        comp="x---------------",
        strum=0.0,
        power_chords=False,
        arp_rate=0.25,
        melody_density=0.75,
        melody_grid=0.25,
        syncopation=0.35,
        melody_center=74,
        parts=("drums", "bass", "pad", "melody"),
        instruments={"chords": 90, "bass": 39, "melody": 81, "arpeggio": 80, "pad": 89},
    )
)

_add(
    Genre(
        key="reggaeton",
        name="reggaeton",
        aliases=("reggaeton", "dembow", "latin", "latin trap", "perreo", "moombahton", "urbano"),
        tempo=(88, 94, 102),
        swing=50,
        swing_grid=0.25,
        mode="minor",
        progressions={
            "minor": {
                "verse": _p("i VI III VII", "i iv VII III", "i VII VI V"),
                "chorus": _p("VI VII i i", "i VI III VII"),
                "bridge": _p("iv VI VII VII"),
            },
            "major": {
                "verse": _p("I V vi IV", "vi IV I V"),
                "chorus": _p("IV V vi I", "I V vi IV"),
                "bridge": _p("ii IV V V"),
            },
        },
        chord_beats=4,
        color=0,
        groove={
            "kick": "X...x...X...x...",
            "snare": "...x..x....x..x.",
            "hat": "v.v.v.v.v.v.v.v.",
        },
        chorus_groove={"timbale": "..........v...v.", "clap": "...v..v....v..v."},
        hat_rolls=False,
        fill="dembow",
        kit=24,
        bass="R-----R-R-----5-",
        bass_center=36,
        comp="x-.x-.x.x-.x-.x.",
        strum=0.0,
        power_chords=False,
        arp_rate=0.25,
        melody_density=1.3,
        melody_grid=0.25,
        syncopation=0.6,
        melody_center=72,
        parts=_DEFAULT_PARTS,
        instruments={"chords": 24, "bass": 38, "melody": 80, "arpeggio": 46, "pad": 89},
    )
)

_add(
    Genre(
        key="funk",
        name="funk",
        aliases=("funk", "funky", "disco", "boogie", "p-funk", "nu disco"),
        tempo=(95, 106, 118),
        swing=54,
        swing_grid=0.25,
        mode="minor",
        progressions={
            "minor": {
                "verse": _p("i7 i7 IV7 IV7", "i7 IV7 i7 IV7"),
                "chorus": _p("VImaj7 VII7 i7 i7", "iv7 v7 i7 IV7"),
                "bridge": _p("iv7 iv7 VII7 VII7"),
            },
            "major": {
                "verse": _p("I7 IV7 I7 IV7", "I7 I7 IV7 bVII7"),
                "chorus": _p("IV7 IV7 I7 I7", "ii7 V7 I7 I7"),
                "bridge": _p("IV7 IV7 V7 V7"),
            },
        },
        chord_beats=4,
        color=1,
        groove={
            "kick": "x.x.......x..x..",
            "snare": "....X..o.o..X..o",
            "hat": "XvxvXvxvXvxvXvxv",
        },
        chorus_groove={"ohat": "..............x.", "cowbell": "x...x...x...x..."},
        hat_rolls=False,
        fill="toms",
        kit=0,
        bass="R..O.R..R.5.O.A.",
        bass_center=38,
        comp="x.x..x.x.x..x.x.",
        strum=0.0,
        power_chords=False,
        arp_rate=0.25,
        melody_density=1.1,
        melody_grid=0.25,
        syncopation=0.55,
        melody_center=70,
        parts=_DEFAULT_PARTS,
        instruments={"chords": 7, "bass": 36, "melody": 65, "arpeggio": 27, "pad": 16},
    )
)

_add(
    Genre(
        key="rnb",
        name="R&B",
        aliases=("r&b", "rnb", "r and b", "rhythm and blues", "neo soul", "soul", "slow jam"),
        tempo=(66, 84, 100),
        swing=56,
        swing_grid=0.25,
        mode="minor",
        progressions={
            "minor": {
                "verse": _p("VImaj7 v7 i7 iv7", "i7 iv7 v7 VImaj7"),
                "chorus": _p("IIImaj7 iv7 v7 i7", "VImaj7 VII7 IIImaj7 VImaj7"),
                "bridge": _p("iv7 iv7 v7 v7"),
            },
            "major": {
                "verse": _p("IVmaj7 iii7 vi7 ii7", "Imaj7 vi7 ii7 V7"),
                "chorus": _p("IVmaj7 V7 iii7 vi7", "ii7 iii7 IVmaj7 V7"),
                "bridge": _p("vi7 vi7 IVmaj7 V7"),
            },
        },
        chord_beats=4,
        color=2,
        groove={
            "kick": "x......x.x......x......x.x...x..",
            "snare": "....X.......X...",
            "hat": "xvxvxvxvxvxvxvxv",
            "rim": "...........o...o",
        },
        chorus_groove={"clap": "....v.......v...", "shaker": "..v...v...v...v."},
        hat_rolls=True,
        fill="stop",
        kit=0,
        bass="R-------..R-5-A-",
        bass_center=36,
        comp="x-------x-----x-",
        strum=0.018,
        power_chords=False,
        arp_rate=0.5,
        melody_density=0.95,
        melody_grid=0.25,
        syncopation=0.5,
        melody_center=70,
        parts=_DEFAULT_PARTS,
        instruments={"chords": 4, "bass": 35, "melody": 85, "arpeggio": 11, "pad": 89},
    )
)

# --------------------------------------------------------------------------- sections


@dataclass(frozen=True)
class Role:
    """What a kind of section does: how hard it hits (0..1), how the drums play, which parts sit out."""

    name: str
    energy: float
    drums: str  # "full", "lite" (hats and a downbeat kick) or "build" (full, with a long fill)
    rests: tuple[str, ...]  # parts that sit out when other parts play
    progression: str  # whose progressions it uses: verse, chorus, prechorus, bridge
    melody_shift: int  # semitones higher (or lower) than the genre's melody center


ROLES = {
    "intro": Role("intro", 0.45, "lite", ("melody", "bass"), "verse", 0),
    "verse": Role("verse", 0.7, "full", ("pad",), "verse", -2),
    "prechorus": Role("prechorus", 0.8, "build", ("pad",), "prechorus", 0),
    "chorus": Role("chorus", 1.0, "full", (), "chorus", 3),
    "bridge": Role("bridge", 0.55, "lite", (), "bridge", 0),
    "breakdown": Role("breakdown", 0.4, "lite", ("bass", "drums"), "bridge", 0),
    "drop": Role("drop", 1.0, "full", (), "chorus", 3),
    "outro": Role("outro", 0.45, "lite", ("melody",), "verse", -2),
    "loop": Role("loop", 0.85, "full", (), "verse", 0),
}

ROLE_WORDS = {
    "intro": "intro", "introduction": "intro", "opening": "intro",
    "verse": "verse", "rap": "verse", "a": "verse",
    "prechorus": "prechorus", "pre-chorus": "prechorus", "pre chorus": "prechorus", "build": "prechorus",
    "buildup": "prechorus", "build-up": "prechorus", "build up": "prechorus", "lift": "prechorus", "riser": "prechorus",
    "chorus": "chorus", "hook": "chorus", "refrain": "chorus", "b": "chorus",
    "drop": "drop", "climax": "drop",
    "bridge": "bridge", "middle 8": "bridge", "middle eight": "bridge", "c": "bridge", "solo": "bridge",
    "interlude": "bridge", "instrumental": "bridge",
    "breakdown": "breakdown", "break": "breakdown",
    "outro": "outro", "ending": "outro", "end": "outro", "coda": "outro",
    "loop": "loop", "beat": "loop", "main": "loop",
}  # fmt: skip


def role_of(section_name: str) -> Role:
    """The role a section's name says ("Chorus 2" is a chorus, "hook" too); a verse when it doesn't say."""
    words = section_name.casefold().strip()
    for suffix in "0123456789 ":
        words = words.rstrip(suffix)
    if words in ROLE_WORDS:
        return ROLES[ROLE_WORDS[words]]
    for word, role in ROLE_WORDS.items():
        if len(word) > 2 and word in words:
            return ROLES[role]
    return ROLES["verse"]


# --------------------------------------------------------------------------- moods


@dataclass(frozen=True)
class Mood:
    mode: str | None  # the mode it leans to when no key is given
    tempo: float  # -1 (the genre's slowest) .. +1 (its fastest)
    energy: float  # multiplies how hard the parts play
    melody_shift: int


MOODS = {
    "sad": Mood("minor", -0.5, 0.85, -2), "melancholy": Mood("minor", -0.5, 0.85, -2),
    "emotional": Mood("minor", -0.3, 0.9, 0), "heartbreak": Mood("minor", -0.4, 0.85, -2),
    "lonely": Mood("minor", -0.5, 0.8, -2), "nostalgic": Mood(None, -0.3, 0.85, 0),
    "dark": Mood("minor", 0.0, 1.0, -3), "evil": Mood("minor", 0.1, 1.05, -3), "menacing": Mood("minor", 0.1, 1.05, -3),
    "aggressive": Mood("minor", 0.6, 1.15, 0), "angry": Mood("minor", 0.6, 1.15, 0), "hard": Mood("minor", 0.4, 1.1, 0),
    "chill": Mood(None, -0.5, 0.8, 0), "relaxed": Mood(None, -0.6, 0.78, 0), "calm": Mood(None, -0.7, 0.75, 0),
    "mellow": Mood(None, -0.5, 0.8, 0), "peaceful": Mood("major", -0.7, 0.75, 0), "dreamy": Mood(None, -0.4, 0.8, 3),
    "sleepy": Mood(None, -0.9, 0.7, 0), "study": Mood(None, -0.5, 0.78, 0), "smooth": Mood(None, -0.2, 0.88, 0),
    "romantic": Mood("major", -0.3, 0.85, 0), "sexy": Mood("minor", -0.3, 0.88, 0), "jazzy": Mood(None, -0.2, 0.88, 0),
    "happy": Mood("major", 0.4, 1.05, 3), "upbeat": Mood("major", 0.5, 1.05, 3), "uplifting": Mood("major", 0.3, 1.05, 3),
    "bright": Mood("major", 0.3, 1.0, 3), "joyful": Mood("major", 0.5, 1.05, 3), "fun": Mood("major", 0.5, 1.05, 2),
    "summer": Mood("major", 0.3, 1.0, 2), "hopeful": Mood("major", 0.0, 0.95, 2),
    "energetic": Mood(None, 0.7, 1.15, 2), "hype": Mood(None, 0.8, 1.15, 2), "intense": Mood("minor", 0.6, 1.15, 0),
    "epic": Mood("minor", 0.2, 1.1, 2), "cinematic": Mood("minor", 0.0, 1.0, 0), "triumphant": Mood("major", 0.3, 1.1, 3),
    "mysterious": Mood("minor", -0.2, 0.85, 0), "spooky": Mood("minor", -0.2, 0.85, 0), "groovy": Mood(None, 0.2, 1.0, 0),
}  # fmt: skip

NEUTRAL_MOOD = Mood(None, 0.0, 1.0, 0)


def mood_of(text: str | None) -> tuple[Mood, list[str]]:
    """The mood a description holds ("dark and chill"), averaged over the mood words it uses, with those words."""
    if not text:
        return NEUTRAL_MOOD, []
    words = [w.strip(",.;!-") for w in text.casefold().replace("/", " ").split()]
    found = [w for w in words if w in MOODS]
    if not found:
        return NEUTRAL_MOOD, []
    moods = [MOODS[w] for w in found]
    modes = [m.mode for m in moods if m.mode]
    return (
        Mood(
            mode=modes[0] if modes else None,
            tempo=sum(m.tempo for m in moods) / len(moods),
            energy=sum(m.energy for m in moods) / len(moods),
            melody_shift=round(sum(m.melody_shift for m in moods) / len(moods)),
        ),
        found,
    )


# Keys to pick from when none is asked for (common, comfortable keys for each mode).
COMMON_TONICS = {
    "minor": ("A", "C", "D", "E", "F", "F#", "G", "B", "C#"),
    "major": ("C", "D", "E", "F", "G", "A", "B-", "E-"),
}
