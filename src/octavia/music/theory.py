"""Keys, scales and chords, with music21 doing the music theory.

``parse_key`` reads a key the way people say it ("A minor", "F#m", "B flat major", "D dorian"); music21 gives its
scale and key signature. ``realize`` turns a chord written either as a roman numeral in the key ("iv7", "VImaj7",
"V6", "bVII") or as a chord symbol ("Am7", "Fmaj7", "G/B") into a ``Chord``: its root, tones in stacking order
(root, third, fifth, seventh, ninth) and bass note, as pitch classes, and its name. ``enrich`` adds sevenths and
ninths that belong in the key. ``detect_chord`` and ``estimate_key`` go the other way, for files the user already
has: the chord a bar's notes make, and the key a song's notes are in (music21's Krumhansl-Schmuckler key finder).

music21 takes a few seconds to import, so it's imported inside the functions that use it (the first call pays).
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass

NOTE_NAMES_SHARP = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
NOTE_NAMES_FLAT = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")

MINOR_FAMILY = ("minor", "dorian", "phrygian", "locrian", "aeolian")
MODES = {
    "": "major", "maj": "major", "major": "major", "ionian": "major", "m": "minor", "min": "minor", "minor": "minor",
    "aeolian": "minor", "natural minor": "minor", "harmonic minor": "minor", "melodic minor": "minor",
    "dorian": "dorian", "phrygian": "phrygian", "lydian": "lydian", "mixolydian": "mixolydian", "locrian": "locrian",
}  # fmt: skip
_KEY = re.compile(r"^\s*([a-g])\s*(#|sharp|-|flat|b)?\s*(.*?)\s*(key)?\s*$", re.IGNORECASE)

# Key signatures (sharps, negative for flats) -> mido's key names.
_MAJOR_SIGNATURES = {0: "C", 1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "F#", 7: "C#",
                     -1: "F", -2: "Bb", -3: "Eb", -4: "Ab", -5: "Db", -6: "Gb", -7: "Cb"}  # fmt: skip
_MINOR_SIGNATURES = {0: "Am", 1: "Em", 2: "Bm", 3: "F#m", 4: "C#m", 5: "G#m", 6: "D#m", 7: "A#m",
                     -1: "Dm", -2: "Gm", -3: "Cm", -4: "Fm", -5: "Bbm", -6: "Ebm", -7: "Abm"}  # fmt: skip


class TheoryError(ValueError):
    """A key or chord that can't be read; the message says how to say it."""


def _m21():
    import music21  # noqa: F401  (slow: imported on first use)

    return music21


def pc_name(pc: int, flats: bool) -> str:
    return (NOTE_NAMES_FLAT if flats else NOTE_NAMES_SHARP)[pc % 12]


def _plain(name: str) -> str:
    """A music21 note name the way people write it: "B-" is "Bb"."""
    return name.replace("-", "b")


# --------------------------------------------------------------------------- keys


@dataclass(frozen=True)
class Key:
    tonic: str  # spelled as music21 spells it: "F#", "B-"
    mode: str  # major, minor, dorian, phrygian, lydian, mixolydian, locrian
    tonic_pc: int
    scale: tuple[int, ...]  # the seven scale degrees' pitch classes, from the tonic
    sharps: int  # the key signature: sharps, or flats as a negative number

    @property
    def name(self) -> str:
        """ "A minor", "Bb major", "D dorian"."""
        return f"{_plain(self.tonic)} {self.mode}"

    @property
    def label(self) -> str:
        """For file names: "A-minor"."""
        return f"{_plain(self.tonic)}-{self.mode}"

    @property
    def family(self) -> str:
        """Which progressions fit it: minor-sounding modes use the minor ones."""
        return "minor" if self.mode in MINOR_FAMILY else "major"

    @property
    def flats(self) -> bool:
        return self.sharps < 0 or "-" in self.tonic

    @property
    def mido_name(self) -> str:
        """The key signature as mido names it ("Am", "Bb"); a mode gets the signature it shares ("D dorian": "C")."""
        sharps = max(-7, min(7, self.sharps))
        if self.mode == "major":
            return _MAJOR_SIGNATURES[sharps]
        if self.mode == "minor":
            return _MINOR_SIGNATURES[sharps]
        return _MAJOR_SIGNATURES[sharps]

    def m21(self):
        return _music21_key(self.tonic, self.mode)

    def transposed(self, semitones: int) -> Key:
        """The same mode a number of semitones away, spelled with the fewest sharps or flats."""
        pc = (self.tonic_pc + semitones) % 12
        options = {NOTE_NAMES_SHARP[pc], NOTE_NAMES_FLAT[pc].replace("b", "-")}
        keys = [make_key(name, self.mode) for name in options]
        return min(keys, key=lambda k: (abs(k.sharps), k.sharps < 0))


@functools.cache
def _music21_key(tonic: str, mode: str):
    return _m21().key.Key(tonic, mode)


@functools.cache
def make_key(tonic: str, mode: str) -> Key:
    m21_key = _music21_key(tonic, mode)
    scale = tuple(p.pitchClass for p in m21_key.pitches[:7])
    return Key(m21_key.tonic.name, m21_key.mode, m21_key.tonic.pitchClass, scale, m21_key.sharps)


def parse_key(text: str) -> Key:
    """ "A minor", "Am", "f# min", "B flat major", "Eb", "D dorian" -> Key. Raises TheoryError."""
    match = _KEY.match(text.replace("♯", "#").replace("♭", "b"))
    mode_words = " ".join((match.group(3) if match else "").casefold().split())
    if not match or mode_words not in MODES:
        raise TheoryError(
            f"I couldn't read the key '{text}'. Say it like 'A minor', 'F# major', 'Bb minor' or 'D dorian'."
        )
    letter, accidental = match.group(1).upper(), (match.group(2) or "").casefold()
    tonic = letter + ("#" if accidental in ("#", "sharp") else "-" if accidental in ("b", "-", "flat") else "")
    try:
        return make_key(tonic, MODES[mode_words])
    except Exception as exc:  # music21 rejects what it can't spell
        raise TheoryError(f"I couldn't use the key '{text}' ({exc}).") from exc


def key_signature_key(mido_name: str) -> Key | None:
    """The key a file's key signature names ("Am", "Bb"), or None."""
    name = mido_name.strip()
    minor = name.endswith("m")
    tonic = name[:-1] if minor else name
    try:
        return make_key(tonic.replace("b", "-") if len(tonic) > 1 else tonic, "minor" if minor else "major")
    except Exception:
        return None


# --------------------------------------------------------------------------- chords

QUALITIES = {  # name suffix -> intervals above the root
    "": (0, 4, 7), "m": (0, 3, 7), "dim": (0, 3, 6), "aug": (0, 4, 8), "sus4": (0, 5, 7), "sus2": (0, 2, 7),
    "7": (0, 4, 7, 10), "maj7": (0, 4, 7, 11), "m7": (0, 3, 7, 10), "m7b5": (0, 3, 6, 10), "dim7": (0, 3, 6, 9),
    "5": (0, 7),
}  # fmt: skip


@dataclass(frozen=True)
class Chord:
    root: int  # pitch class
    tones: tuple[int, ...]  # pitch classes in stacking order: root, third, fifth, seventh, ninth...
    bass: int  # the pitch class in the bass (the root unless it's an inversion or slash chord)
    name: str  # "Am7", "G/B"
    figure: str = ""  # what it was written as: "i7", "Am7"

    @property
    def intervals(self) -> tuple[int, ...]:
        return tuple((t - self.root) % 12 for t in self.tones)

    @property
    def is_minor(self) -> bool:
        return 3 in self.intervals and 4 not in self.intervals


@dataclass(frozen=True)
class ChordSpan:
    """A chord sounding from ``start`` to ``end`` (beats)."""

    start: float
    end: float
    chord: Chord


_ROMAN = re.compile(r"^[b#♭♯]?(VII|VI|IV|V|III|II|I|vii|vi|iv|v|iii|ii|i)(?![a-z])", re.ASCII)
_SYMBOL = re.compile(r"^[A-G]")


def is_roman(figure: str) -> bool:
    """Written as a roman numeral ("iv7", "bVII"), so it means a chord only in a key."""
    return bool(_ROMAN.match(figure.strip().replace("♭", "b").replace("♯", "#")))


def _stacked(m21_chord) -> list:
    """A music21 chord's pitches in stacking order (root, 3rd, 5th, 7th, 9th, 11th, 13th), missing ones skipped."""
    root = m21_chord.root()
    order = [root]
    for step in (3, 5, 7, 9, 11, 13):
        try:
            found = m21_chord.getChordStep(step)
        except Exception:
            found = None
        if found is not None and all(found.pitchClass != p.pitchClass for p in order):
            order.append(found)
    for p in m21_chord.pitches:  # anything left (an added note)
        if all(p.pitchClass != q.pitchClass for q in order):
            order.append(p)
    return order


_NAMES = {frozenset(shape): suffix for suffix, shape in QUALITIES.items()} | {
    frozenset(shape): suffix
    for suffix, shape in {
        "add9": (0, 2, 4, 7),
        "madd9": (0, 2, 3, 7),
        "9": (0, 2, 4, 7, 10),
        "maj9": (0, 2, 4, 7, 11),
        "m9": (0, 2, 3, 7, 10),
        "6": (0, 4, 7, 9),
        "m6": (0, 3, 7, 9),
        "7sus4": (0, 5, 7, 10),
        "m(maj7)": (0, 3, 7, 11),
        "7b5": (0, 4, 6, 10),
        "aug7": (0, 4, 8, 10),
        "m11": (0, 2, 3, 5, 7, 10),
        "13": (0, 2, 4, 7, 9, 10),
    }.items()  # fmt: skip
}


def chord_name(root_name: str, intervals: tuple[int, ...], bass_name: str | None = None) -> str:
    """A chord symbol from its root and intervals: "Am7", "Fmaj7", "Bm7b5", "G/B"."""
    pcs = frozenset(i % 12 for i in intervals)
    suffix = _NAMES.get(pcs)
    if suffix is None:  # something unusual: name what it's built on
        minor = 3 in pcs and 4 not in pcs
        suffix = ("m" if minor else "") + ("7" if 10 in pcs else "maj7" if 11 in pcs else "")
    name = f"{_plain(root_name)}{suffix}"
    if bass_name and _plain(bass_name) != _plain(root_name):
        name += f"/{_plain(bass_name)}"
    return name


def realize(figure: str, key: Key) -> Chord:
    """A chord written as a roman numeral in ``key`` ("iv7", "bVII", "V6") or as a symbol ("Am7", "Bb/D").
    Raises TheoryError."""
    m21 = _m21()
    text = figure.strip().replace("♭", "b").replace("♯", "#")
    if text[:1] in "abcdefg" and not _ROMAN.match(text):
        text = text[0].upper() + text[1:]  # "am" said for "Am"
    try:
        if _ROMAN.match(text):
            m21_chord = m21.roman.RomanNumeral(text, key.m21())
        elif _SYMBOL.match(text):
            m21_chord = m21.harmony.ChordSymbol(_symbol_for_music21(text))
        else:
            raise ValueError("not a chord")
        pitches = _stacked(m21_chord)
        if len(pitches) < 2:
            raise ValueError("no notes")
    except TheoryError:
        raise
    except Exception as exc:
        raise TheoryError(
            f"I couldn't read the chord '{figure}'. Write chords as roman numerals ('i iv VI V', 'ii7 V7 Imaj7') "
            "or chord symbols ('Am F C G', 'Dm7 G7 Cmaj7')."
        ) from exc
    root = pitches[0]
    bass = m21_chord.bass()
    intervals = tuple((p.pitchClass - root.pitchClass) % 12 for p in pitches)
    return Chord(
        root=root.pitchClass,
        tones=tuple(p.pitchClass for p in pitches),
        bass=bass.pitchClass,
        name=chord_name(root.name, intervals, bass.name),
        figure=figure,
    )


def _symbol_for_music21(text: str) -> str:
    """Chord symbols use "-" for flat in music21: "Bbmaj7/Db" -> "B-maj7/D-"."""
    out = re.sub(r"^([A-G])b", r"\1-", text)
    return re.sub(r"/([A-G])b", r"/\1-", out).replace("min", "m")


def enrich(chord: Chord, key: Key, level: int) -> Chord:
    """``chord`` with a seventh (level 1) or a seventh and a ninth (level 2) added, if it hasn't one: the key's own
    note when the chord belongs to the key, else the one its quality takes (a major chord from outside the key
    becomes a dominant seventh, which is how borrowed chords usually sound)."""
    if level <= 0 or len(chord.tones) < 3 or chord.intervals[:3] == (0, 4, 8):
        return chord
    tones = list(chord.tones)
    intervals = chord.intervals
    diatonic = all(t in key.scale for t in tones)
    if len(tones) == 3:
        if diatonic and chord.root in key.scale:
            tones.append(key.scale[(key.scale.index(chord.root) + 6) % 7])
        else:  # minor -> m7, diminished -> m7b5, major -> dominant 7
            tones.append((chord.root + 10) % 12)
    if level >= 2 and len(tones) == 4 and 6 not in intervals:
        if diatonic and chord.root in key.scale:
            ninth = key.scale[(key.scale.index(chord.root) + 1) % 7]
        else:
            ninth = (chord.root + 2) % 12
        if (ninth - chord.root) % 12 == 2:  # a flat ninth clashes; leave it off
            tones.append(ninth)
    if len(tones) == len(chord.tones):
        return chord
    new_intervals = tuple((t - chord.root) % 12 for t in tones)
    flats = key.flats
    bass = pc_name(chord.bass, flats) if chord.bass != chord.root else None
    return Chord(
        chord.root, tuple(tones), chord.bass, chord_name(pc_name(chord.root, flats), new_intervals, bass), chord.figure
    )


def power_chord(chord: Chord) -> Chord:
    """Root and fifth only (and the root on top), as rock guitars play them."""
    fifth = (chord.root + 7) % 12 if 7 in chord.intervals or len(chord.tones) < 3 else chord.tones[2]
    return Chord(chord.root, (chord.root, fifth), chord.root, chord.name.split("/")[0] + "5", chord.figure)


def tokens(progression: str) -> list[str]:
    """A progression as written by a person: "Am F C G", "i - VI - III - VII", "ii7|V7|Imaj7"."""
    cleaned = re.sub(r"[,|>→]+|\s-\s|–|—", " ", progression)
    return [t for t in cleaned.split() if t and t != "-"]


def split_beats(token: str) -> tuple[str, float | None]:
    """ "V:2" -> ("V", 2.0): a chord with how many beats it lasts, when the token says."""
    if ":" in token:
        figure, _, beats = token.partition(":")
        try:
            value = float(beats)
        except ValueError:
            raise TheoryError(f"'{token}': after the colon goes how many beats the chord lasts, like 'V:2'.") from None
        if not 0.25 <= value <= 32:
            raise TheoryError(f"'{token}': a chord lasts between a quarter beat and 32 beats.")
        return figure, value
    return token, None


# --------------------------------------------------------------------------- reading what's there


def roman_for(root: int, intervals: tuple[int, ...], key: Key) -> str:
    """The roman numeral of a chord (root pitch class and intervals) in ``key``: "iv7", "bVII", "V7"."""
    numerals = ("I", "II", "III", "IV", "V", "VI", "VII")
    prefix = ""
    if root in key.scale:
        degree = key.scale.index(root)
    elif (root + 1) % 12 in key.scale:
        degree, prefix = key.scale.index((root + 1) % 12), "b"
    elif (root - 1) % 12 in key.scale:
        degree, prefix = key.scale.index((root - 1) % 12), "#"
    else:
        return "?"
    pcs = set(intervals)
    minor = 3 in pcs and 4 not in pcs
    numeral = numerals[degree].lower() if minor else numerals[degree]
    if pcs >= {3, 6} and 7 not in pcs:
        return prefix + numeral.lower() + ("ø7" if 10 in pcs else "o7" if 9 in pcs else "o")
    if pcs >= {4, 8} and 7 not in pcs:
        return prefix + numeral + "+"
    suffix = "maj7" if 11 in pcs and not minor else "7" if 10 in pcs or 11 in pcs else ""
    return prefix + numeral + suffix


@dataclass(frozen=True)
class Detected:
    root: int
    quality: str  # a QUALITIES key
    score: float

    @property
    def intervals(self) -> tuple[int, ...]:
        return QUALITIES[self.quality]

    def chord(self, key: Key | None) -> Chord:
        flats = key.flats if key else False
        tones = tuple((self.root + i) % 12 for i in self.intervals)
        return Chord(self.root, tones, self.root, f"{pc_name(self.root, flats)}{self.quality}")


def detect_chord(weights: list[float], bass: int | None, key: Key | None) -> Detected | None:
    """The chord that best explains a stretch of music: ``weights`` is how much each pitch class sounds (by
    duration and loudness), ``bass`` the lowest one heard most. None when there's next to nothing."""
    total = sum(weights)
    if total <= 1e-9:
        return None
    w = [x / total for x in weights]
    peak = max(w)
    scale = set(key.scale) if key else set()
    best: Detected | None = None
    for root in range(12):
        if w[root] < 0.02:
            continue
        for quality, shape in QUALITIES.items():
            tones = [(root + i) % 12 for i in shape]
            inside = sum(w[t] for t in tones)
            missing = sum(1 for t in tones if w[t] < 0.15 * peak)
            score = inside - 0.55 * (1 - inside) - 0.16 * missing
            score -= 0.035 * max(0, len(tones) - 3) + (0.06 if quality in ("5", "sus2", "sus4", "aug", "dim7") else 0)
            if bass is not None and bass == root:
                score += 0.12
            if scale and all(t in scale for t in tones):
                score += 0.04
            if best is None or score > best.score:
                best = Detected(root, quality, score)
    return best


def estimate_key(weights: list[float], hint: Key | None = None, home: set[int] | None = None) -> tuple[Key, float]:
    """The key a song is in from how much each pitch class sounds, with music21's Krumhansl-Schmuckler key finder,
    and how sure it is (its correlation, -1..1).

    Relative keys (A minor and C major) share their notes, so the profile alone often can't tell them apart: a key
    signature in the file (``hint``) decides when it names one of them or fits nearly as well as the best, and
    otherwise the notes the music starts and ends on in the bass (``home``) do."""
    m21 = _m21()
    total = sum(weights)
    if total <= 1e-9:
        return hint or make_key("C", "major"), 0.0
    stream = m21.stream.Stream()
    for pc, weight in enumerate(weights):
        if weight > 0:
            note = m21.note.Note(pc + 60)
            note.quarterLength = max(0.25, round(weight / total * 64 * 4) / 4)
            stream.append(note)
    found = m21.analysis.discrete.KrumhanslSchmuckler().getSolution(stream)  # analyze("key") would be Aarden-Essen
    candidates = [found, *getattr(found, "alternateInterpretations", [])]
    best = make_key(found.tonic.name, found.mode).transposed(0)  # "A# minor" is said "Bb minor"
    score = float(found.correlationCoefficient)
    if hint is not None:
        if set(hint.scale) == set(best.scale):
            if not (home and hint.tonic_pc not in home and best.tonic_pc in home):  # unless the music says otherwise
                return hint, score
            return best, score
        for candidate in candidates[:4]:
            if (
                candidate.tonic.pitchClass == hint.tonic_pc
                and candidate.mode == hint.family
                and candidate.correlationCoefficient >= score - 0.12
            ):
                return hint, float(candidate.correlationCoefficient)
    if home and best.tonic_pc not in home:
        for candidate in candidates[1:6]:
            other = make_key(candidate.tonic.name, candidate.mode).transposed(0)
            if (
                set(other.scale) == set(best.scale)
                and other.tonic_pc in home
                and candidate.correlationCoefficient >= score - 0.1
            ):
                return other, float(candidate.correlationCoefficient)
    return best, score
