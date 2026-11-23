"""The voices the setup offers by name, and the line each one says as its preview.

Each is a public voice in Fish Audio's library: anyone's Fish key can use it. Previews are made live on the user's
PC with their own key (``preview``): Fish's terms don't allow shipping audio made with their service, so no clips
come with the download. Change the list here; the first is the default.
"""

from __future__ import annotations

from dataclasses import dataclass

PREVIEW_LINE = (
    "[laughing]  Hey... [clear throat] I'm ready to help [soft] when you need me. [sighing] Just say the word!"
)
FISH_TTS_URL = "https://api.fish.audio/v1/tts"
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


def preview(api_key: str, voice_id: str, text: str = PREVIEW_LINE, timeout_s: float = 45.0) -> bytes:
    """The preview line in ``voice_id``, as MP3 bytes, made with the user's own Fish key. Raises PreviewError."""
    import httpx
    import ormsgpack

    from octavia.net import tls_context

    if not api_key.strip():
        raise PreviewError("a Fish Audio key is needed to hear the voices")
    request = {"text": text, "format": "mp3", "reference_id": voice_id, "normalize": True, "latency": "normal"}
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
