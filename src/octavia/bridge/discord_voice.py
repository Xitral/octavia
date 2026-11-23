"""Talking with her over a Discord voice call: the user joins a voice channel on their server, she joins too, and
the call is her ears and her voice until they leave (docs/discord-bridge.md, "Calling her").

* **Following the owner.** Only ``discord_bridge.owner_id`` is followed: when they join a voice channel she can reach
  (in a server her bot is in, with Connect and Speak there), she joins it; when they move she moves, and when they
  leave she leaves. Anyone else coming and going changes nothing.
* **Only the owner is heard.** Discord sends every speaker's audio to everyone in the channel. Each packet says whose
  stream it is (an SSRC, which the voice gateway's SPEAKING message ties to a user), and only the owner's is
  decrypted and decoded; the rest is dropped unread.
* **Receiving.** discord.py sends voice but doesn't receive it, so the packets are read here, off its voice socket:
  the transport layer (``aead_xchacha20_poly1305_rtpsize``, the one mode discord.py offers) with the connection's
  secret key, then Discord's end-to-end encryption (DAVE) with the connection's own ``davey`` session, then Opus,
  decoded straight to 16 kHz mono for her ears (``audio/call_audio.CallInput``).
* **Sending.** Everything her speaker plays (her voice, her sounds) is also handed to the call through the player's
  render tap (``CallOutput``), and the PC itself goes quiet for the call (``mute_pc_on_call``); discord.py encodes
  and encrypts it.
* **Her side.** ``Assistant.start_call`` / ``end_call``: during a call her frames come from it
  (``CallAwareSource``), the user doesn't need the wake word (``call_wake_word``), and the brain is told they're
  on a call from their phone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from octavia.audio.call_audio import CallAwareSource, CallInput, CallOutput
from octavia.core.events import EventBus, Notice

if TYPE_CHECKING:
    from octavia.config import AppConfig

log = logging.getLogger(__name__)
audit = logging.getLogger("octavia.audit")

_RTP_VERSION = 2
_OPUS_PAYLOAD_TYPE = 0x78
_CONNECT_TIMEOUT_S = 30.0
_JOINED_LINE = "I'm here."  # said into the call once she's in it


# ---------------------------------------------------------------------------------------------------- packets
def parse_rtp(packet: bytes) -> tuple[int, int, int, int] | None:
    """An RTP voice packet's (ssrc, sequence, header length, extension words), or None for anything else (RTCP,
    keep-alives, too short)."""
    if len(packet) < 12 or packet[0] >> 6 != _RTP_VERSION:
        return None
    if 200 <= packet[1] <= 204 or packet[1] & 0x7F != _OPUS_PAYLOAD_TYPE:
        return None  # RTCP sender/receiver reports, or not Opus
    csrc_count = packet[0] & 0x0F
    header = 12 + 4 * csrc_count
    extension_words = 0
    if packet[0] & 0x10:  # a header extension: its 4-byte preamble is in the clear, its body is encrypted
        if len(packet) < header + 4:
            return None
        extension_words = struct.unpack_from(">H", packet, header + 2)[0]
        header += 4
    if len(packet) < header + 16 + 4:  # at least the AEAD tag and the nonce
        return None
    sequence = struct.unpack_from(">H", packet, 2)[0]
    ssrc = struct.unpack_from(">I", packet, 8)[0]
    return ssrc, sequence, header, extension_words


def decrypt_rtpsize(packet: bytes, header: int, extension_words: int, secret_key: bytes) -> bytes:
    """The payload of an ``aead_xchacha20_poly1305_rtpsize`` packet: the header (with the extension preamble) is the
    associated data, a 4-byte counter at the end is the nonce, and the encrypted header-extension body is cut off
    the front of what comes out. Raises when the packet doesn't decrypt."""
    from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_decrypt

    aad = packet[:header]
    nonce = packet[-4:] + bytes(20)
    plain = crypto_aead_xchacha20poly1305_ietf_decrypt(packet[header:-4], aad, nonce, secret_key)
    if extension_words:
        plain = plain[4 * extension_words :]
    if packet[0] & 0x20 and plain:  # RTP padding: its last byte says how much
        plain = plain[: -plain[-1]] if plain[-1] <= len(plain) else b""
    return plain


def encrypt_rtpsize(header: bytes, payload: bytes, secret_key: bytes, counter: int) -> bytes:
    """The other way round, as discord.py sends (for the tests)."""
    from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt

    nonce = struct.pack(">I", counter)
    return header + crypto_aead_xchacha20poly1305_ietf_encrypt(payload, header, nonce + bytes(20), secret_key) + nonce


class Receiver:
    """Reads the voice socket (on discord.py's socket thread): the owner's packets become 16 kHz PCM in ``call``.

    ``state`` is the voice connection (``secret_key``, ``mode``, ``dave_session``); ``ssrcs`` maps SSRC to user id
    (kept by the voice client from the gateway's SPEAKING messages); ``decoder`` turns Opus into 16 kHz mono PCM.
    """

    def __init__(self, owner_id: int, call: CallInput, state: Any, ssrcs: dict[int, int], decoder: Any) -> None:
        self.owner_id = owner_id
        self.call = call
        self.state = state
        self.ssrcs = ssrcs
        self.decoder = decoder
        self.packets = 0  # the owner's, decoded
        self.failures = 0
        self.unknown = 0  # packets from a stream nobody was tied to yet (no SPEAKING message for it)
        self._last: tuple[int, int] | None = None  # (ssrc, sequence) of the last packet played

    def __call__(self, packet: bytes) -> None:
        try:
            self._receive(packet)
        except Exception:
            self.failures += 1
            if self.failures == 1 or self.failures % 500 == 0:
                log.warning("Couldn't read a call packet (%d so far)", self.failures, exc_info=True)

    def _receive(self, packet: bytes) -> None:
        parsed = parse_rtp(packet)
        if parsed is None:
            return
        ssrc, sequence, header, extension_words = parsed
        user = self.ssrcs.get(ssrc)
        if user != self.owner_id:
            if user is None:
                self.unknown += 1
            return  # someone else's voice (or not yet known whose): never decrypted
        if self.state.mode != "aead_xchacha20_poly1305_rtpsize":
            raise RuntimeError(f"unsupported voice encryption mode {self.state.mode!r}")
        payload = decrypt_rtpsize(packet, header, extension_words, bytes(self.state.secret_key))
        opus = self._end_to_end(payload)
        if opus is None:
            return
        last = self._last
        if last is not None and last[0] == ssrc and (sequence - last[1]) % 65536 > 32768:
            return  # older than one already played: arrived out of order, too late
        self._last = (ssrc, sequence)
        self.call.feed_pcm16(self.decoder.decode(opus, fec=False))
        self.packets += 1

    def _end_to_end(self, payload: bytes) -> bytes | None:
        """The Opus frame inside the DAVE layer (the payload as it is while the call isn't end-to-end encrypted)."""
        session = getattr(self.state, "dave_session", None)
        if session is None or not getattr(self.state, "dave_protocol_version", 0):
            return payload
        try:
            import davey

            return session.decrypt(self.owner_id, davey.MediaType.audio, payload)
        except Exception:
            with contextlib.suppress(Exception):
                if session.can_passthrough(self.owner_id):
                    return payload  # a transition to or from an unencrypted call: frames come in the clear
            return None  # not decryptable yet (the group is still being set up): a moment of silence


# ------------------------------------------------------------------------------------------- discord.py glue
def _decoder() -> Any:
    """An Opus decoder that gives 16 kHz mono, what her ears take (libopus resamples as it decodes)."""
    from discord import opus

    class Decoder16k(opus.Decoder):
        SAMPLING_RATE = 16000
        CHANNELS = 1
        SAMPLES_PER_FRAME = 320
        SAMPLE_SIZE = 2
        FRAME_SIZE = 640

    return Decoder16k()


def voice_client_class() -> type:
    """discord.py's ``VoiceClient``, plus the SSRC of each user who speaks (from the voice gateway's SPEAKING and
    CLIENT_DISCONNECT messages), which receiving needs to tell the owner's audio from anyone else's."""
    import discord
    from discord.voice_state import VoiceConnectionState

    class CallVoiceClient(discord.VoiceClient):
        def __init__(self, client: Any, channel: Any) -> None:
            self.ssrcs: dict[int, int] = {}
            super().__init__(client, channel)

        def create_connection_state(self) -> Any:
            return VoiceConnectionState(self, hook=self._voice_hook)

        async def _voice_hook(self, ws: Any, msg: dict[str, Any]) -> None:
            op, data = msg.get("op"), msg.get("d") or {}
            if op == 5 and "ssrc" in data and "user_id" in data:  # SPEAKING
                self.ssrcs[int(data["ssrc"])] = int(data["user_id"])
            elif op == 13 and "user_id" in data:  # CLIENT_DISCONNECT
                gone = int(data["user_id"])
                for ssrc in [s for s, user in self.ssrcs.items() if user == gone]:
                    del self.ssrcs[ssrc]

    return CallVoiceClient


def pcm_source(output: CallOutput) -> Any:
    """What discord.py plays into the call: 20 ms of 48 kHz stereo PCM per read, forever (silence between replies)."""
    import discord

    class CallPcm(discord.AudioSource):
        def read(self) -> bytes:
            return output.read()

        def is_opus(self) -> bool:
            return False

    return CallPcm()


# -------------------------------------------------------------------------------------------------- the call
class VoiceCalls:
    """Follows the owner in and out of voice channels (see the module docstring). ``client`` is the bridge's
    logged-in discord.py client; ``source`` her call-aware microphone; ``player`` her speaker; ``assistant`` takes
    ``start_call``/``end_call`` and ``announce``. ``connect`` joins a channel (for the tests)."""

    def __init__(
        self,
        config: AppConfig,
        events: EventBus,
        assistant: Any,
        source: CallAwareSource,
        player: Any,
        owner_id: Callable[[], int],
        *,
        connect: Callable[[Any], Any] | None = None,
        receiver_parts: Callable[[], Any] = _decoder,
        playable: Callable[[CallOutput], Any] = pcm_source,
    ) -> None:
        self.config = config
        self.settings = config.discord_bridge
        self.events = events
        self.assistant = assistant
        self.source = source
        self.player = player
        self.owner_id = owner_id
        self._connect = connect or self._connect_channel
        self._decoder = receiver_parts
        self._playable = playable
        self.input = CallInput()
        self.output = CallOutput(int(getattr(player, "sample_rate", 48000)))
        self.voice: Any = None  # the voice client while she's in a channel
        self.channel: Any = None
        self.receiver: Receiver | None = None
        self._lock = asyncio.Lock()
        self._told_cant: set[int] = set()  # channels she said once she can't join

    @property
    def on_call(self) -> bool:
        return self.voice is not None

    def notice(self, message: str, level: int = logging.INFO) -> None:
        self.events.emit(Notice(source="DISCORD", message=message, level=level))

    def tap(self, block: Any) -> None:
        """The speaker's render tap (audio thread): what she plays, for the call."""
        self.output.feed(block)

    # ------------------------------------------------------------------ following the owner
    async def owner_moved(self, after_channel: Any) -> None:
        """The owner's voice state changed: be where they are (or nowhere)."""
        async with self._lock:
            if after_channel is None:
                await self._leave("you left the call")
                return
            if self.channel is not None and getattr(self.channel, "id", None) == getattr(after_channel, "id", None):
                return
            await self._leave(None)
            await self._join(after_channel)

    async def find_owner(self, client: Any) -> None:
        """At login: join the owner if they're already in a voice channel."""
        owner = self.owner_id()
        for guild in getattr(client, "guilds", ()) or ():
            for channel in getattr(guild, "voice_channels", ()) or ():
                if owner in (getattr(channel, "voice_states", None) or {}):
                    await self.owner_moved(channel)
                    return

    def _can_join(self, channel: Any) -> str | None:
        """Why she can't join ``channel``, in words for the user, or None."""
        guild = getattr(channel, "guild", None)
        me = getattr(guild, "me", None)
        if me is None or not hasattr(channel, "permissions_for"):
            return None
        allowed = channel.permissions_for(me)
        missing = [name for name in ("connect", "speak") if not getattr(allowed, name, True)]
        if missing:
            return (
                f"my bot doesn't have {' or '.join(m.capitalize() for m in missing)} in {channel.name}: give its role "
                "Connect and Speak in that server (Server Settings, Roles) and join again"
            )
        return None

    async def _join(self, channel: Any) -> None:
        if (problem := self._can_join(channel)) is not None:
            if channel.id not in self._told_cant:
                self._told_cant.add(channel.id)
                self.notice(f"Can't join your call: {problem}.", logging.WARNING)
            return
        try:
            voice = await self._connect(channel)
        except Exception as exc:
            self.notice(
                f"Couldn't join your call in {getattr(channel, 'name', 'that channel')} ({exc}).", logging.WARNING
            )
            log.warning("Joining the voice channel failed", exc_info=True)
            return
        self.voice, self.channel = voice, channel
        self._told_cant.discard(channel.id)
        self.output.clear()
        self.output.active = True
        if self.settings.mute_pc_on_call and hasattr(self.player, "muted"):
            self.player.muted = True
        try:
            self.receiver = Receiver(
                self.owner_id(), self.input, voice._connection, getattr(voice, "ssrcs", {}), self._decoder()
            )
            voice._connection.add_socket_listener(self.receiver)
            voice.play(self._playable(self.output), after=self._stopped_playing)
        except Exception:
            log.exception("Setting up the call's audio failed")
            await self._leave(None)
            self.notice("I joined your call but couldn't set up its audio; I left again.", logging.WARNING)
            return
        self.source.start_call(self.input)
        self.assistant.start_call()
        audit.info("discord call joined channel=%s", getattr(channel, "id", "?"))
        self.notice(f"On a call with you in {getattr(channel, 'name', 'voice')}.")
        self.assistant.announce(_JOINED_LINE, listen=True, cue="call", relay=False, kind="reply")

    def _stopped_playing(self, error: BaseException | None) -> None:
        if error is not None:
            log.warning("Sending audio to the call stopped: %s", error)

    async def _leave(self, why: str | None) -> None:
        voice, self.voice, self.channel = self.voice, None, None
        if voice is None:
            return
        self.source.end_call()
        self.assistant.end_call()
        self.output.active = False
        self.output.clear()
        if hasattr(self.player, "muted"):
            self.player.muted = False
        receiver, self.receiver = self.receiver, None
        with contextlib.suppress(Exception):
            if receiver is not None:
                voice._connection.remove_socket_listener(receiver)
                log.info(
                    "Call ended: %d of your packets heard, %d unreadable, %d from a stream not tied to anyone",
                    receiver.packets, receiver.failures, receiver.unknown,
                )  # fmt: skip
        with contextlib.suppress(Exception):
            voice.stop()
        with contextlib.suppress(Exception):
            await voice.disconnect(force=True)
        audit.info("discord call left")
        if why:
            self.notice(f"Left the call: {why}.")

    async def _connect_channel(self, channel: Any) -> Any:
        return await channel.connect(cls=voice_client_class(), timeout=_CONNECT_TIMEOUT_S, self_deaf=False)

    async def close(self) -> None:
        async with self._lock:
            await self._leave(None)
