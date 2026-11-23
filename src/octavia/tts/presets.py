"""The voices the setup offers by name, and the line each one says as its preview.

Each is a public voice in Fish Audio's library: anyone's Fish key can use it. Previews are made live on the user's
PC with their own key (``preview``): Fish's terms don't allow shipping audio made with their service, so no clips
come with the download. Change the list here; the first is the default.
"""

from __future__ import annotations

from dataclasses import dataclass

# The user's line without its cue tags: on these voices the laugh, throat-clear and sigh came out as odd noises
# (2026-11-23), so the words carry it alone.
PREVIEW_LINE = "Hey. I'm ready to help whenever you need me. Just say the word."
FISH_TTS_URL = "https://api.fish.audio/v1/tts"


def greeting(name: str, user_name: str = "", wake_word: str = "") -> str:
    """What it says the first time it's awake (never the preview line): its own name, theirs if given, and how to
    call it."""
    hello = f"Hi, {user_name.strip()}." if user_name.strip() else "Hi."
    call = (wake_word or name).strip() or "Octavia"
    return f"{hello} I'm {name.strip() or 'Octavia'}. Whenever you need something, just say {call}."


PREVIEW_MODEL = "s2.1-pro-free"


@dataclass(frozen=True)
class Preset:
    name: str
    voice_id: str
    about: str


PRESETS: tuple[Preset, ...] = (
    Preset("Octavia", "f88993cf418c4fb798f1551bc51f0ef8", "OCTAVIA's own voice: warm, expressive"),
    Preset("Rosalyn", "8e79322f8c064a9c965408f140c26fee", "soft and steady"),
    Preset("James", "e4ffba2adbd7417b8fd2621f61b5d8ad", "easygoing"),
    Preset("Ryker", "c109ef2dbb044a73acfcb020d5a1c94f", "low and calm"),
    Preset("Eliza", "7ec05ae7427d49a4b2a7a79d5b4781ab", "dry and deadpan"),
    Preset("Brian", "2bd17d122405429cb89c4be14edca7b8", "old-broadcast, a little eerie"),
)
DEFAULT = PRESETS[0]


def by_id(voice_id: str) -> Preset | None:
    return next((p for p in PRESETS if p.voice_id == voice_id), None)


class PreviewError(RuntimeError):
    pass


PREVIEW_RATE = 44100


def preview(api_key: str, voice_id: str, text: str = PREVIEW_LINE, timeout_s: float = 45.0, fmt: str = "mp3") -> bytes:
    """The preview line in ``voice_id`` (MP3 bytes, or 16-bit mono PCM at PREVIEW_RATE with ``fmt="pcm"``), made with
    the user's own Fish key. Raises PreviewError."""
    import httpx
    import ormsgpack

    from octavia.net import tls_context

    if not api_key.strip():
        raise PreviewError("a Fish Audio key is needed to hear the voices")
    request = {"text": text, "format": fmt, "reference_id": voice_id, "normalize": True, "latency": "normal"}
    if fmt == "pcm":
        request["sample_rate"] = PREVIEW_RATE
    try:
        r = httpx.post(
            FISH_TTS_URL,
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "model": PREVIEW_MODEL,
                "Content-Type": "application/msgpack",
            },
            content=ormsgpack.packb(request),
            timeout=timeout_s,
            verify=tls_context(),
        )
    except httpx.HTTPError as exc:
        raise PreviewError(f"couldn't reach Fish Audio ({type(exc).__name__})") from exc
    if r.status_code in (401, 403):
        raise PreviewError("Fish Audio didn't accept that key")
    if r.status_code == 402:
        raise PreviewError("that Fish Audio account is out of credit")
    if r.status_code != 200 or not r.content:
        raise PreviewError(f"Fish Audio answered HTTP {r.status_code}")
    return r.content


def with_effect(pcm: bytes, effect: str, rate: int = PREVIEW_RATE) -> bytes:
    """Raw 16-bit preview audio through the same voice effect the live voice uses (audio/voice_effects.py), as a
    WAV file, so a preview sounds like it will."""
    import io
    import wave

    import numpy as np

    from octavia.audio import voice_effects

    audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    before = float(np.sqrt(np.mean(audio**2))) if audio.size else 0.0
    audio = voice_effects.apply(audio, voice_effects.picked(effect), rate)
    after = float(np.sqrt(np.mean(audio**2))) if audio.size else 0.0
    if before > 0 and after > 0:  # as loud as without it, as the live voice is levelled after its effect
        audio = audio * (before / after)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak > 0.99:
        audio = audio * (0.99 / peak)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((audio * 32767.0).astype("<i2").tobytes())
    return out.getvalue()
