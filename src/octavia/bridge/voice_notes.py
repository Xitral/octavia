"""Voice messages from the user's phone, written down with her own speech recognition.

Discord's voice messages are Ogg files of Opus audio. ``ogg_packets`` takes the Opus packets out of the Ogg pages
(no ffmpeg needed), the libopus decoder her calls already use (``discord_voice._decoder``) turns them into 16 kHz
mono, and the local speech recognition writes that down in pieces of at most ``_PIECE_S``, each cut at its quietest
moment so a word isn't split in two. The bridge then takes the words as the user's message, as if they'd typed them.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from octavia.stt.base import SpeechToText

log = logging.getLogger(__name__)

RATE = 16000
MAX_SECONDS = 600.0  # a longer note: only its first ten minutes are written down
_PIECE_S = 30.0  # what the speech model takes at once without its memory growing (``stt/parakeet.py``)
_LOOK_BACK_S = 5.0  # where in a piece's last seconds it may be cut
_FRAME = 320  # 20 ms, the steps a cut is looked for in
_OPUS_RATE = 48000  # what an Opus header's pre-skip is counted in
HEAD_BYTES = 27 + 255 + 8  # an Ogg page header with the most segments, then "OpusHead"


def is_ogg_opus(head: bytes) -> bool:
    """The start of an Ogg file whose first stream is Opus (a Discord voice message): its first page's body begins
    with the Opus header. ``head`` is the file's first ``HEAD_BYTES``."""
    if len(head) < 27 or not head.startswith(b"OggS"):
        return False
    body = 27 + head[26]
    return head[body : body + 8] == b"OpusHead"


def ogg_packets(data: bytes) -> Iterator[bytes]:
    """The packets of the first stream in an Ogg file, in order, joined across pages. A cut-off file gives what it
    has; anything that isn't Ogg raises ``ValueError``."""
    pos = 0
    serial: int | None = None
    partial: list[bytes] = []
    while pos + 27 <= len(data):
        if data[pos : pos + 4] != b"OggS":
            raise ValueError("not an Ogg file")
        page_serial = struct.unpack_from("<I", data, pos + 14)[0]
        if serial is None:
            serial = page_serial
        count = data[pos + 26]
        body = pos + 27 + count
        if body > len(data):
            return
        for size in data[pos + 27 : body]:
            if page_serial == serial:
                partial.append(data[body : body + size])
                if size < 255:  # a packet's last segment
                    yield b"".join(partial)
                    partial = []
            body += size
        pos = body


def decode_ogg_opus(data: bytes, decoder: Any) -> np.ndarray:
    """An Ogg Opus file as 16 kHz mono float32 (at most ``MAX_SECONDS`` of it). ``decoder`` decodes one Opus packet
    to 16 kHz mono 16-bit PCM (``decode(packet, fec=False)``). Raises ``ValueError`` when it isn't Ogg Opus."""
    packets = ogg_packets(data)
    head = next(packets, b"")
    if not head.startswith(b"OpusHead") or len(head) < 19:
        raise ValueError("not Opus audio")
    skip = struct.unpack_from("<H", head, 10)[0] * RATE // _OPUS_RATE  # the encoder's warm-up, not audio
    next(packets, None)  # OpusTags
    limit = int(MAX_SECONDS * RATE)
    chunks: list[bytes] = []
    total = 0
    for packet in packets:
        if not packet:
            continue
        pcm = decoder.decode(packet, fec=False)
        chunks.append(pcm)
        total += len(pcm) // 2
        if total >= skip + limit:
            log.info("A voice message runs over %.0f minutes: only its start is written down", MAX_SECONDS / 60)
            break
    pcm16 = np.frombuffer(b"".join(chunks), dtype="<i2")
    return pcm16[skip : skip + limit].astype(np.float32) / np.float32(32768.0)


def pieces(audio: np.ndarray, piece_s: float = _PIECE_S, look_back_s: float = _LOOK_BACK_S) -> list[np.ndarray]:
    """``audio`` in pieces of at most ``piece_s``, each cut at the quietest 20 ms of its last ``look_back_s`` (as late
    as it can be, so a note with no pause in it is still cut into full pieces)."""
    size = int(piece_s * RATE)
    look = max(_FRAME, min(int(look_back_s * RATE), size) // _FRAME * _FRAME)
    out: list[np.ndarray] = []
    start = 0
    while audio.size - start > size:
        first = start + size - look
        frames = audio[first : first + look].reshape(-1, _FRAME)
        levels = np.mean(frames * frames, axis=1)
        quietest = len(levels) - 1 - int(np.argmin(levels[::-1]))  # the latest of equally quiet moments
        cut = first + quietest * _FRAME + _FRAME // 2
        out.append(audio[start:cut])
        start = cut
    out.append(audio[start:])
    return [piece for piece in out if piece.size]


def opus_decoder() -> Any:
    """The 16 kHz mono libopus decoder her calls use (discord.py ships libopus for Windows)."""
    from octavia.bridge.discord_voice import _decoder

    return _decoder()


async def transcribe(path: Path, stt: SpeechToText, decoder: Callable[[], Any] = opus_decoder) -> str:
    """What the user says in the voice message at ``path`` ("" when no words were made out). Raises ``ValueError``
    when it isn't Ogg Opus, and whatever the decoder or the speech model raise."""

    def read() -> np.ndarray:
        data = path.read_bytes()
        if not is_ogg_opus(data[:HEAD_BYTES]):
            raise ValueError("not an Ogg Opus file")
        return decode_ogg_opus(data, decoder())

    audio = await asyncio.to_thread(read)
    words: list[str] = []
    for piece in pieces(audio, _PIECE_S):
        result = await stt.transcribe(piece)
        if text := result.text.strip():
            words.append(text)
    return " ".join(words)
