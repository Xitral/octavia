# Sound credits

Every sound cue OCTAVIA plays (the ether, falcon and classic packs, `src/octavia/audio/`) is synthesized with numpy
when it starts, from sine waves and seeded noise written in this repository. No recorded, sampled or downloaded audio
is used in any of them, and none copies or imitates a particular game's or console's sounds: the ether pack is only
in the spirit of an old console's menus (soft glass, a long hall, a dulled top).

| Pack | Source | Licence |
|---|---|---|
| ether | `src/octavia/audio/ether_sounds.py`, made from scratch (2026-11-22) | this repository's own code |
| falcon | `src/octavia/audio/falcon_sounds.py`, made from scratch (2026-10-11) | this repository's own code |
| classic | `src/octavia/audio/earcons.py`, made from scratch | this repository's own code |

If a pack ever layers in a recorded sound, add it here: its file, where it came from (a link to its page), its
author and its licence (CC0 only), and what was done to it.

Sounds it plays *inside* an answer ("what does a falcon sound like?") are not cues: they're found at the time, from
your own `data/sounds/`, Windows' sounds or Wikimedia Commons, and aren't kept in the repository (`docs/sounds.md`).
