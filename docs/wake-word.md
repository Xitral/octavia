# Wake word

The wake phrase comes from `assistant.wake_phrase` in config. Nothing in the
code assumes "computer".

## Engines

| `wake.engine` | Phrase | Setup | Notes |
|---|---|---|---|
| `sherpa_kws` (default) | **any phrase**, set in config | none (`octavia setup` downloads an 18 MB model) | Open-vocabulary keyword spotting (sherpa-onnx, zipformer trained on GigaSpeech). About 2% of one CPU core. |
| `openwakeword` | the phrase your model was **trained** for | train a model (below) | The most robust in noise. Optional: `uv sync --extra openwakeword`. |
| `keyboard` | press Enter | none | Development only. |

What was measured on this PC during development with `sherpa_kws`:

- "hey computer" and "hey rainbow" were detected in 100% of clean synthesized
  clips (David/Zira voices at three speaking rates).
- Zero false wakes on 5.8 hours of real read speech (LibriSpeech), and none on
  sentences like "my computer is running slowly".
- A command said in the same breath ("hey computer what time is it") is
  captured in full. The detector reports how far past the phrase it fired, and
  that audio is replayed into the capture.
- Recall drops in heavy noise (about 13 of 16 at 10 dB SNR). A trained
  openWakeWord model held up better in noise (18 of 18 at 0 dB for "hey
  octavia").

## Talking to it in one breath (the second listen)

You don't have to wait for the chirp. Say its name and your request together,
"OCTAVIA, what's the weather?", and it answers it. If the wake detector hears
the name, the request is captured right after it (as above). If the detector
misses it, a second listen catches it: while it waits for the wake word, each
stretch of speech (up to `wake.second_chance_max_s`, 8 s) that the detector
didn't fire on is transcribed by the same local speech recognition used for
requests (Parakeet, about 0.1 s for a short phrase), and when it **starts**
with its name, it wakes:

- with words after the name, those words are your request, run at once, with
  no chime (nothing to wait for);
- with the name alone ("Hey OCTAVIA."), it chimes and listens, as after the
  wake word.

The name has to come first, after at most a "hey", "hi", "okay" or "so": "I
told OCTAVIA yesterday..." is ordinary talk and is ignored. Near misses of the
name ("Octavio") and your `extra_spellings` count, and the speech vocabulary
(`personality/vocabulary.yaml`) fixes a name that keeps being misheard before
it's checked. It never runs while it speaks or for a second after (its own
voice saying its name doesn't wake it), during a conversation, a question, or
the wake cooldown, or while a screen task works beside the conversation (the
stop listener has the microphone's voice detector then). It transcribes at most
one stretch at a time and one per 1.5 s, and skips one when speech recognition
is busy. The audio stays in memory and is dropped after; what's heard is never
logged unless it woke it. The log counts, at most once a minute, how often the
wake detector woke it and how often only the second listen did (`Wake word so
far: ...`), so you can see how often the detector misses. `wake.second_chance:
false` turns it off.

## Renaming the assistant

```yaml
# config/local.yaml
assistant:
  name: "Dash"
  wake_phrase: "hey dash"
```

Then vet the new phrase:

```powershell
uv run octavia wake-check                       # the configured phrase
uv run octavia wake-check --phrase "hey nova"   # try one without editing config
```

`wake-check` synthesizes the phrase with Windows' English voices at several
speaking rates, runs look-alike sentences as negatives, and reports hits and
false wakes. Tips:

- **Use two or more words.** A single word fires on every sentence that contains it.
- **Two-syllable names are more reliable than one-syllable ones.** In testing,
  "hey dash" got 16 of 18 and "hey rainbow" 18 of 18.
- If an unusual name gets missed, add phonetic spellings:

  ```yaml
  wake:
    sherpa_kws:
      extra_spellings: ["hey castrel"]   # for "hey kestrel"
  ```

- Tune with `threshold` (higher means fewer false wakes) and `boost` (higher
  means easier to trigger), and confirm with your real voice using
  `uv run octavia listen-test`.

## Training a dedicated model (optional, for the most robust detection)

Once the name is final, you can train an openWakeWord-compatible model on
synthetic speech. You need an NVIDIA GPU, which means a cloud one: the local
AMD card isn't supported by these pipelines.

1. **livekit-wakeword** (Apache-2.0): one YAML file (`target_phrases:
   ["hey computer"]`, `model_type: conv_attention`), then `livekit-wakeword run
   config.yaml`. Needs torch and espeak-ng. Use Linux, WSL2 or a cloud GPU
   (Windows isn't documented). It's newer, and its accuracy numbers are its
   own benchmark. Also note: this project runs trained classifiers through
   openWakeWord's streaming runtime, and loading livekit-wakeword's output there
   hasn't been tested.
2. **openWakeWord Colab**: the official notebook has bit-rotted; use the
   community-fixed 2026 version (github.com/alfiedennen/openwakeword-colab-2026).
   About 75-90 minutes on a Colab L4, 2.5 hours on a free T4.

Either way you get an `.onnx` file. Put it in `models/wake/` and switch the
engine in `config/local.yaml`:

```yaml
wake:
  engine: openwakeword
  openwakeword:
    model_path: "models/wake/hey_computer.onnx"
    threshold: 0.5
```

Then install the extra and fetch openWakeWord's feature models (setup only
fetches them once the engine is set to `openwakeword`):

```powershell
uv sync --extra openwakeword
uv run octavia setup
```

You can keep `sherpa_kws` as a fallback: switching back is one config line,
which also covers you while a model for a new name is training.

## Not used, and why

- **Picovoice Porcupine**: the free tier was discontinued on 2026-06-30, it
  needs an online AccessKey check, and it has no built-in "hey computer".
- **Vosk grammar mode**: it produced "hey computer" about 11 times in 6
  minutes of look-alike sentences.
- **Sending mic audio to a cloud service** to spot the wake word: never. All
  listening is local.
