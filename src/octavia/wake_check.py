"""`octavia wake-check`: test the configured wake phrase before relying on it.

Synthesizes the phrase with every installed Windows voice at a few speaking
rates (positives), plus sentences that reuse its words without saying it
(negatives), and runs them through the configured wake engine. A cheap way
to vet a new assistant name. Real-voice testing (`octavia listen-test`) is
still the final word.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

from octavia.config import AppConfig

_SCRIPT = r"""
param([string]$Dir, [string]$ListFile)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$voices = @($s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'en*' } | ForEach-Object { $_.VoiceInfo.Name })
$i = 0
foreach ($line in Get-Content -Encoding UTF8 $ListFile) {
  $parts = $line.Split("`t"); $kind = $parts[0]; $text = $parts[1]
  foreach ($v in $voices) {
    foreach ($rate in @(-2, 0, 3)) {
      if ($kind -eq "neg" -and $rate -ne 0) { continue }
      $s.SelectVoice($v); $s.Rate = $rate
      $out = Join-Path $Dir ("{0}_{1}.wav" -f $kind, $i); $i++
      $s.SetOutputToWaveFile($out, $fmt); $s.Speak($text); $s.SetOutputToNull()
      Write-Output ("{0}`t{1}`t{2}`t{3}" -f $out, $kind, $v, $rate)
    }
  }
}
"""


def _sentences(phrase: str) -> tuple[list[str], list[str]]:
    words = phrase.split()
    name = words[-1]
    positives = [f"{phrase}.", f"{phrase}, what time is it?", f"{phrase} open the calendar"]
    negatives = [
        f"The {name} over there is broken again, so I'll fix it later.",
        f"I said {name}, not {' '.join(words[:-1]) or 'hey'} you.",
        f"{' '.join(words[:-1]) or 'Hey'}, can you pass me the salt?",
        "Hey, come here and look at this, it's pretty cool.",
        "My computer has been running slowly all day today.",
        "We should compute the total before the meeting starts.",
    ]
    return positives, negatives


def _read(path: str) -> np.ndarray:
    with wave.open(path) as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    pad = np.zeros(12000, np.float32)
    return np.concatenate([pad, audio, pad])


def run_wake_check(config: AppConfig) -> int:
    if sys.platform != "win32":
        print("wake-check uses Windows speech synthesis; it only runs on Windows.")
        return 1
    from octavia.wake import create_wake

    phrase = config.assistant.wake_phrase
    detector = create_wake(config)
    detector.load()
    positives, negatives = _sentences(phrase.lower())
    with tempfile.TemporaryDirectory(prefix="wake-check-") as tmp:
        folder = Path(tmp)
        (folder / "say.ps1").write_text(_SCRIPT, encoding="utf-8")
        (folder / "list.txt").write_text(
            "\n".join([*(f"pos\t{t}" for t in positives), *(f"neg\t{t}" for t in negatives)]), encoding="utf-8"
        )
        print(f'Synthesizing test speech for "{phrase}"...')
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(folder / "say.ps1"),
             "-Dir", str(folder), "-ListFile", str(folder / "list.txt")],
            capture_output=True, text=True, timeout=600,
        )  # fmt: skip
        rows = [line.split("\t") for line in out.stdout.splitlines() if line.count("\t") == 3]
        if not rows:
            print("Windows speech synthesis produced nothing:", out.stderr.strip()[:300])
            return 1
        hits = misses = false_wakes = 0
        neg_seconds = 0.0
        missed: list[str] = []
        for path, kind, voice, rate in rows:
            audio = _read(path)
            detector.reset()
            fired = any(detector.process(audio[i : i + 512]) for i in range(0, len(audio) - 511, 512))
            if kind == "pos":
                hits += fired
                if not fired:
                    misses += 1
                    missed.append(f"{voice.split()[1] if ' ' in voice else voice} rate {rate}")
            else:
                neg_seconds += len(audio) / 16000
                false_wakes += fired
    total = hits + misses
    print(f"\nDetected {hits}/{total} times the phrase was said.")
    if missed:
        print("  missed: " + ", ".join(missed))
    print(f"False wakes: {false_wakes} in {neg_seconds:.0f} s of similar-sounding sentences.")
    if misses:
        print(
            "\nTips: add other spellings of the name under wake.sherpa_kws.extra_spellings "
            "(spell it how it sounds), or raise wake.sherpa_kws.boost slightly."
        )
    if false_wakes:
        print("\nTips: raise wake.sherpa_kws.threshold, or pick a longer, more distinctive phrase.")
    return 0 if hits == total and not false_wakes else 1
