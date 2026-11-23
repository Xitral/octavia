"""Music: the MIDI songs Octavia writes and edits for the user, who makes music in BandLab (a browser DAW with no
public API), so everything ends up as a .mid file they drag into it (or into any other DAW).

The pieces, each built on an established open-source library where one fits:

- ``song``: the MIDI file itself, read and written with mido (MIT): tracks of notes in ticks, the tempo map, time
  and key signatures and section markers.
- ``theory``: keys, scales and chords. music21 (BSD) reads keys ("F# minor", "D dorian"), turns roman numerals
  ("iv7", "VImaj7", "V6") and chord symbols ("Am7", "G/B") into pitches, names chords and finds a file's key
  (Krumhansl-Schmuckler). It's slow to import, so only the functions that need it import it.
- ``presets``: the genres (progressions, tempo ranges, drum grooves, swing, default sounds), the General MIDI
  instruments and drum notes, moods and section roles: data, not code.
- ``voicing``, ``grooves``, ``parts``, ``feel``: our own code for what no library does well: voice-led chord
  voicings, drum grooves with fills and hi-hat rolls, bass lines, comping, pads, arpeggiators and melodies that
  follow the harmony, swing and humanizing.
- ``compose`` turns a description into a song; ``analysis`` and ``edit`` read and change existing files;
  ``library`` is where files go (Documents\\Octavia\\Music) and how they're named; ``preview`` plays one through
  Windows' own General MIDI synth.

All timing inside the composer is in quarter-note beats ("qn"); ``song`` converts to ticks.
"""
