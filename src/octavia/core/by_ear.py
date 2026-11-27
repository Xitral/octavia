"""How alike two short English phrases sound, from their spelling: enough to tell that "good pop" may be a misheard
"shut up" and "good morning" isn't, without a pronouncing dictionary.

``sounds`` spells a phrase as sounds (one character each: ``_CONSONANTS`` and ``_VOWELS``) by rules that hold for the
everyday words speech recognition writes, plus the irregular few (``_WORDS``). The words run together as they're
said, and a t or d between two vowels is the flap of American speech ("shut up" is "shu-dup"). ``alike`` lines two of
them up (an edit distance whose swaps cost what the sounds differ by: a t for a d less than a t for an m) and gives
0..1. Used by ``core/cut_off.py``.
"""

from __future__ import annotations

import re
from functools import lru_cache

from octavia.core.text import normalize_text

# Consonants: (manner, place, voiced). R is the flap (the t in "butter", "shut up").
_CONSONANTS: dict[str, tuple[str, int, bool]] = {
    "p": ("stop", 0, False), "b": ("stop", 0, True), "t": ("stop", 2, False), "d": ("stop", 2, True),
    "k": ("stop", 4, False), "g": ("stop", 4, True), "R": ("flap", 2, True),
    "f": ("fric", 0, False), "v": ("fric", 0, True), "T": ("fric", 1, False), "D": ("fric", 1, True),
    "s": ("fric", 2, False), "z": ("fric", 2, True), "S": ("fric", 3, False), "Z": ("fric", 3, True),
    "h": ("fric", 5, False), "C": ("affr", 3, False), "J": ("affr", 3, True),
    "m": ("nasal", 0, True), "n": ("nasal", 2, True), "N": ("nasal", 4, True),
    "l": ("liquid", 2, True), "r": ("liquid", 3, True), "w": ("glide", 0, True), "y": ("glide", 3, True),
}  # fmt: skip
# Vowels: (height 0 low .. 3 high, backness 0 front .. 2 back, glides into another).
_VOWELS: dict[str, tuple[float, float, bool]] = {
    "i": (3, 0, False), "I": (2.5, 0, False), "e": (2, 0, True), "E": (1.5, 0, False), "a": (0.5, 0, False),
    "A": (0, 2, False), "O": (1, 2, False), "o": (2, 2, True), "U": (2.5, 2, False), "u": (3, 2, False),
    "V": (1, 1, False), "@": (1.5, 1, False), "Y": (0.5, 1, True), "W": (0.5, 1.5, True), "Q": (1, 2, True),
}  # fmt: skip
_MANNER = {
    frozenset({"stop", "flap"}): 0.3, frozenset({"flap", "liquid"}): 0.35, frozenset({"stop", "affr"}): 0.45,
    frozenset({"fric", "affr"}): 0.35, frozenset({"stop", "fric"}): 0.6, frozenset({"stop", "nasal"}): 0.6,
    frozenset({"liquid", "glide"}): 0.45, frozenset({"nasal", "liquid"}): 0.7, frozenset({"flap", "nasal"}): 0.6,
}  # fmt: skip
GAP = 0.7  # a sound heard that wasn't said, or the other way round
_SOFT_GAP = 0.35  # one easily swallowed: a schwa, an h

# Words spelling rules get wrong, as said.
_WORDS = {
    "the": "D@", "a": "@", "an": "@n", "to": "tu", "too": "tu", "two": "tu", "of": "@v", "you": "yu", "your": "yOr",
    "i": "Y", "is": "Iz", "it": "It", "its": "Its", "what": "wVt", "was": "wVz", "want": "wAnt", "one": "wVn",
    "once": "wVns", "do": "du", "does": "dVz", "done": "dVn", "come": "kVm", "some": "sVm", "are": "Ar", "said": "sEd",
    "have": "hav", "give": "gIv", "live": "lIv", "love": "lVv", "enough": "@nVf", "quiet": "kwY@t", "okay": "oke",
    "ok": "oke", "hey": "he", "they": "De", "there": "DEr", "their": "DEr", "where": "wEr", "were": "w@r",
    "here": "hir", "be": "bi", "he": "hi", "me": "mi", "we": "wi", "she": "Si", "no": "no", "go": "go", "so": "so",
    "oh": "o", "up": "Vp", "but": "bVt", "shut": "SVt", "hush": "hVS", "shush": "SVS", "off": "Of", "on": "An",
    "or": "Or", "good": "gUd", "could": "kUd", "would": "wUd", "should": "SUd", "put": "pUt", "push": "pUS",
    "full": "fUl", "pull": "pUl", "talking": "tOkIN", "talk": "tOk", "walk": "wOk", "all": "Ol", "call": "kOl",
    "great": "gret", "break": "brek", "eye": "Y", "buy": "bY", "bye": "bY", "hmm": "hm", "mhm": "mhm", "uh": "V",
    "um": "Vm", "huh": "hV", "yeah": "yE", "yes": "yEs", "what's": "wVts", "that's": "Dats", "it's": "Its",
    "don't": "dont", "i'm": "Ym", "now": "nW", "how": "hW", "wow": "wW", "down": "dWn", "show": "So", "know": "no",
    "low": "lo", "slow": "slo", "own": "on", "says": "sEz",
}  # fmt: skip
_FUNCTION_TH = frozenset({"this", "that", "these", "those", "them", "then", "than", "though", "thus", "there"})
_DIGRAPHS = (
    ("tch", "C"), ("dge", "J"), ("igh", "Y"), ("sh", "S"), ("ch", "C"), ("ph", "f"), ("ck", "k"), ("qu", "kw"),
    ("wh", "w"), ("ng", "N"), ("kn", "n"), ("wr", "r"), ("ee", "i"), ("ea", "i"), ("ai", "e"), ("ay", "e"),
    ("oa", "o"), ("oi", "Q"), ("oy", "Q"), ("au", "O"), ("aw", "O"), ("ou", "W"), ("ow", "W"), ("ie", "i"),
    ("ey", "i"), ("ue", "u"), ("ui", "u"), ("ook", "Uk"), ("ood", "Ud"), ("oo", "u"),
    ("or", "Or"), ("ar", "Ar"), ("er", "@r"), ("ir", "@r"), ("ur", "@r"), ("x", "ks"),
)  # fmt: skip
_SHORT = {"a": "a", "e": "E", "i": "I", "o": "A", "u": "V", "y": "I"}
_LONG = {"a": "e", "e": "i", "i": "Y", "o": "o", "u": "u", "y": "Y"}
_PLAIN = {"b": "b", "d": "d", "f": "f", "h": "h", "j": "J", "k": "k", "l": "l", "m": "m", "n": "n", "p": "p",
          "q": "k", "r": "r", "s": "s", "t": "t", "v": "v", "w": "w", "z": "z"}  # fmt: skip
_VOWEL_LETTERS = frozenset("aeiouy")
_WORD = re.compile(r"[a-z']+")


@lru_cache(maxsize=4096)
def word_sounds(word: str) -> str:
    """One word as sounds (``_CONSONANTS``, ``_VOWELS``): "pop" -> "pAp", "start" -> "stArt", "quite" -> "kwYt"."""
    if word in _WORDS:
        return _WORDS[word]
    word = word.replace("'", "")
    out: list[str] = []
    at = 0
    n = len(word)
    while at < n:
        ch = word[at]
        if word.startswith("th", at):
            out.append("D" if word in _FUNCTION_TH else "T")
            at += 2
            continue
        for spelled, sound in _DIGRAPHS:
            if word.startswith(spelled, at):
                out.extend(sound)
                at += len(spelled)
                break
        else:
            if ch in _VOWEL_LETTERS:
                if ch == "y" and at == 0:
                    out.append("y")
                elif ch == "e" and at == n - 1 and n > 2:
                    pass  # a silent final e: "quite", "hope"
                elif ch == "y" and at == n - 1:
                    out.append("Y" if sum(c in "aeiou" for c in word) == 0 else "i")  # "my", "sorry"
                elif ch == "o" and at == n - 1:
                    out.append("o")  # "go", "hello"
                elif at + 2 == n - 1 and word[-1] == "e" and word[at + 1] not in _VOWEL_LETTERS:
                    out.append(_LONG[ch])  # a vowel, a consonant, a silent e: "hope", "cute", "time"
                else:
                    out.append(_SHORT[ch])
            elif ch == "c":
                out.append("s" if at + 1 < n and word[at + 1] in "eiy" else "k")
            elif ch == "g":
                out.append("g")
            elif ch in _PLAIN:
                out.append(_PLAIN[ch])
            at += 1
    collapsed: list[str] = []
    for sound in out:
        if not collapsed or sound != collapsed[-1]:  # "tt", "ss": one sound
            collapsed.append(sound)
    return "".join(collapsed)


def sounds(text: str) -> str:
    """``text`` as it's said, its words run together: "shut up" -> "SVRVp" (the t flapped between the vowels)."""
    joined = "".join(word_sounds(w) for w in _WORD.findall(normalize_text(text)))
    out = list(joined)
    for i in range(1, len(out) - 1):
        if out[i] in "td" and out[i - 1] in _VOWELS and out[i + 1] in _VOWELS:
            out[i] = "R"
    return "".join(out)


def _place(a: int, b: int) -> float:
    if 5 in (a, b) and a != b:  # h: made in the throat, like nothing else
        return 0.7
    return min(0.8, 0.3 * abs(a - b))


def swap(a: str, b: str) -> float:
    """What hearing ``b`` for ``a`` costs: 0 the same sound, 1 nothing alike."""
    if a == b:
        return 0.0
    if a in _VOWELS and b in _VOWELS:
        (h1, b1, d1), (h2, b2, d2) = _VOWELS[a], _VOWELS[b]
        return min(0.7, 0.15 * abs(h1 - h2) + 0.15 * abs(b1 - b2) + 0.15 * (d1 != d2))
    if a in _CONSONANTS and b in _CONSONANTS:
        (m1, p1, v1), (m2, p2, v2) = _CONSONANTS[a], _CONSONANTS[b]
        manner = 0.0 if m1 == m2 else _MANNER.get(frozenset({m1, m2}), 0.9)
        return min(1.0, 0.55 * manner + 0.35 * _place(p1, p2) + 0.15 * (v1 != v2))
    other = b if a in _VOWELS else a
    if other in _CONSONANTS and _CONSONANTS[other][0] in ("liquid", "glide"):
        return 0.6
    return 1.0


def _gap(sound: str) -> float:
    return _SOFT_GAP if sound in "@h" else GAP


def distance(a: str, b: str) -> float:
    """Weighted edit distance between two sound strings (``swap``, ``GAP``)."""
    row = [0.0]
    for y in b:
        row.append(row[-1] + _gap(y))
    for x in a:
        prev, row[0] = row[0], row[0] + _gap(x)
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + _gap(x), row[j - 1] + _gap(y), prev + swap(x, y))
    return row[-1]


def alike(heard: str, said: str) -> float:
    """How alike two sound strings are, 0..1."""
    if not heard or not said:
        return 0.0
    return max(0.0, 1.0 - distance(heard, said) / (max(len(heard), len(said)) * GAP))


def is_vowel(sound: str) -> bool:
    return sound in _VOWELS


def beats(said: str) -> int:
    """How many syllables a sound string has: its vowel sounds."""
    return sum(s in _VOWELS for s in said)
