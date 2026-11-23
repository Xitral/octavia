"""Texting her from the user's phone: DMs to a Discord bot of theirs become turns, and she texts back.

The user makes their own bot (docs/discord-bridge.md), puts its token in ``.env`` as ``DISCORD_BOT_TOKEN`` and turns
on ``discord_bridge``. In voice mode the bridge logs the bot in (discord.py, which reconnects by itself) and then:

* **Only the owner.** A DM from ``discord_bridge.owner_id`` (or ``.env``'s ``DISCORD_OWNER_ID``) is a turn. Anything
  else (another person, a bot, a server channel) is ignored without a word: logged at debug level, never answered,
  never shown to the brain. Without an owner the bridge doesn't start at all.
* **A turn in line.** The text goes through the assistant's queue (``Assistant.run_from``, source "discord"): it waits
  for a conversation or announcement at the PC to end, and is handled like typed chat, so preset replies, quick
  commands and "which model was that?" work the same. Nothing is said at the PC (unless ``speak_replies``); the DM
  shows "typing..." while she works, and her reply comes back as text, split at Discord's 2,000 characters.
* **Every risky call is asked in the DM.** The bridge is that turn's confirmer: "Want me to <...>? Reply yes or no.",
  then the owner's next DM is the answer (``parse_confirmation``), within ``confirm_timeout_s``; no answer is a no.
  Their words never approve anything in advance, and no earlier yes carries over (``_REMOTE_SOURCES`` in the
  assistant). The token never appears in a log line, a notice or a reply.
* **Files.** What the owner sends is kept in ``data/discord/inbox`` and listed in the turn's note
  (``attachments``); ``send_to_phone`` sends files back, after ``outgoing_problem`` said they may go. A voice message
  is written down by her own speech recognition (``voice_notes``), and its words are the turn's text.
* **Updates.** Her announcements are also texted when ``notify`` is "always", or "away" and nobody has touched the
  keyboard or mouse for ``away_after_min`` (``relay``).
* **A stop.** A stop in a text ("drop it", "stop", "cancel that and ..."), while an earlier text of theirs waits in
  line or runs, ends that one at once (``Assistant.stop_from``): it isn't queued behind what it stops. What else the
  text asks then runs as a turn of its own.
* **A pause.** At most ``max_turns_per_10_min`` turns in ten minutes; past that she says once that she's pausing.
* **Calls.** With ``calls`` (``bridge/discord_voice.VoiceCalls``), the bot also watches the voice channels of its
  servers for the owner, and joins them there: the call is her ears and voice until they leave. The bot then also
  gets server and voice-state events (``guilds``, ``voice_states``), but still never reads a server's messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from octavia import keys
from octavia.bridge import voice_notes
from octavia.bridge.attachments import INBOX, Saved, http_get, outgoing_problem, save_attachments, size_text, turn_note
from octavia.core.assistant import parse_confirmation
from octavia.core.events import EventBus, Notice
from octavia.core.stopping import stop_in
from octavia.core.turn_types import TEXTED_NOTE
from octavia.tools.permissions import ConfirmationRequest, Confirmer, NotAnswered
from octavia.tts.shown import shown_form

if TYPE_CHECKING:
    from octavia.bridge.discord_voice import VoiceCalls
    from octavia.config import AppConfig

log = logging.getLogger(__name__)
audit = logging.getLogger("octavia.audit")

SOURCE = "discord"
LIMIT = 2000  # Discord's most characters in one message
_WINDOW_S = 600.0  # the rate limit's ten minutes
_TYPING_EVERY_S = 8.0  # Discord shows "typing..." for about 10 s per trigger
_RETRY_S, _MAX_RETRY_S = 15.0, 600.0  # waiting to reconnect after a failure discord.py didn't handle itself
_CONFIRM_MARGIN_S = 15.0  # the executor waits this much longer than the question, for the messages around it
_ONLY_FILES = "(no message, just the attachment)"
_TEXTED_NOTE = TEXTED_NOTE  # told to the brain with every texted turn
_HEARD_NOTE = (
    "they sent a voice message: its words, written down by speech recognition on this PC (so a word may be misheard), "
    "are their message above; answer what they said"
)
_NO_WORDS_NOTE = "speech recognition made out no words in a voice message they sent"
_GLITCH = "Ugh, my brain just glitched. Try that again in a sec."
_ASK = "Want me to {}? Reply yes or no."
_REASK = "Sorry, was that a yes or a no?"
_NO_ANSWER = "No answer, so I haven't done it. Ask me again when you're ready."
_NOT_CLEAR = "I couldn't tell if that was a yes, so I haven't done it."
_DROPPED = "Okay, I dropped it."
_TOO_LATE = "That one had already gone through before your stop:"
_DROPPED_NOTE = (
    "the user texted a stop for what they'd asked you before (it was dropped, unanswered): this is what they want now"
)


class BridgeError(RuntimeError):
    """Something couldn't be sent; the message says why, in words for the user."""


class TurnLimit:
    """At most ``most`` turns in any ``window_s``: ``take`` counts one, False when that would be one too many."""

    def __init__(self, most: int, window_s: float = _WINDOW_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.most = most
        self.window_s = window_s
        self.clock = clock
        self._times: deque[float] = deque()

    def _drop_old(self) -> float:
        now = self.clock()
        while self._times and now - self._times[0] >= self.window_s:
            self._times.popleft()
        return now

    def take(self) -> bool:
        now = self._drop_old()
        if len(self._times) >= self.most:
            return False
        self._times.append(now)
        return True

    def wait_s(self) -> float:
        """How long until the next turn may go."""
        now = self._drop_old()
        return 0.0 if len(self._times) < self.most else max(0.0, self._times[0] + self.window_s - now)


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """``text`` in pieces Discord takes (at most ``limit`` characters each), cut between paragraphs, lines or
    sentences where it can, and a code block that's cut is closed and opened again so each piece shows right."""
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    while len(text) > limit:
        room = limit - 4  # for a ``` closing a code block cut here
        cut = _cut_at(text, room)
        piece, text = text[:cut].rstrip(), text[cut:].lstrip()
        if piece.count("```") % 2:  # inside a code block: close it here and reopen it in the next piece
            piece += "\n```"
            text = "```\n" + text
        pieces.append(piece)
    if text:
        pieces.append(text)
    return pieces


def _cut_at(text: str, room: int) -> int:
    """Where to cut ``text`` so its first piece is at most ``room`` long: the last paragraph break, line break,
    sentence end or space in the second half of that, else right at ``room``."""
    window = text[:room]
    for pattern in (r"\n\s*\n", r"\n", r"[.!?][)\"']?\s", r"\s"):
        found = [m.end() for m in re.finditer(pattern, window)]
        if found and found[-1] >= room // 2:
            return found[-1]
    return room


def owner_of(config: AppConfig) -> int:
    """The one Discord account she answers: ``discord_bridge.owner_id``, else ``.env``'s ``DISCORD_OWNER_ID``; 0
    when neither is set (or the .env one isn't a number)."""
    if config.discord_bridge.owner_id:
        return int(config.discord_bridge.owner_id)
    raw = keys.value("DISCORD_OWNER_ID")
    return int(raw) if raw.isdigit() else 0


def _idle_seconds() -> float:
    """How long since anyone touched this PC's keyboard or mouse; 0 when Windows can't say (never "away")."""
    try:
        from octavia.platform.windows import idle_seconds

        return idle_seconds()
    except Exception:
        return 0.0


def _failure(exc: BaseException) -> str | None:
    """What a failed login means for the user: "token" (Discord refused it), "intents" (the Message Content
    intent is off) or None (anything else, worth another try later)."""
    try:
        import discord
    except ImportError:  # pragma: no cover - a declared dependency
        return None
    if isinstance(exc, discord.LoginFailure):
        return "token"
    if isinstance(exc, discord.PrivilegedIntentsRequired):
        return "intents"
    code = getattr(exc, "code", None)
    if isinstance(exc, discord.ConnectionClosed) and code == 4004:
        return "token"
    if isinstance(exc, discord.ConnectionClosed) and code in (4013, 4014):
        return "intents"
    return None


def discord_client(bridge: DiscordBridge) -> Any:
    """The real client: DMs and their text only (``dm_messages``, ``message_content``), mentions never pinged, and
    TLS that trusts what Windows trusts. discord.py reconnects by itself after a dropped connection."""
    import discord

    logging.getLogger("discord").setLevel(logging.WARNING)  # its INFO lines are gateway chatter
    intents = discord.Intents.none()
    intents.dm_messages = True
    intents.message_content = True
    if bridge.calls is not None:  # which voice channel the owner is in: the servers and their voice states
        intents.guilds = True
        intents.voice_states = True
    options: dict[str, Any] = {"allowed_mentions": discord.AllowedMentions.none()}
    try:
        import aiohttp

        from octavia.net import tls_context

        options["connector"] = aiohttp.TCPConnector(ssl=tls_context())
    except Exception:
        log.debug("Using discord.py's own TLS settings", exc_info=True)
    client = discord.Client(intents=intents, **options)

    @client.event
    async def on_ready() -> None:
        await bridge.on_ready(client)

    @client.event
    async def on_message(message: Any) -> None:
        await bridge.on_message(message)

    @client.event
    async def on_voice_state_update(member: Any, before: Any, after: Any) -> None:
        await bridge.on_voice_state_update(member, before, after)

    return client


def discord_file(path: Path) -> Any:
    import discord

    return discord.File(str(path), filename=path.name)


class DiscordBridge(Confirmer):
    """The bot's side of the user's DMs (see the module docstring). ``assistant`` needs ``run_from``; the rest is
    for tests: ``client_factory`` makes the Discord client, ``idle`` says how long the PC has been untouched,
    ``fetch`` gets a URL's bytes, ``file_factory`` wraps a path for upload, ``clock`` drives the rate limit,
    ``voice_decoder`` makes the Opus decoder for voice messages (written down with ``assistant.stt``)."""

    def __init__(
        self,
        config: AppConfig,
        assistant: Any,
        events: EventBus,
        *,
        client_factory: Callable[[DiscordBridge], Any] = discord_client,
        idle: Callable[[], float] = _idle_seconds,
        fetch: Callable[[str], Awaitable[bytes]] | None = http_get,
        file_factory: Callable[[Path], Any] = discord_file,
        clock: Callable[[], float] = time.monotonic,
        calls: VoiceCalls | None = None,
        voice_decoder: Callable[[], Any] = voice_notes.opus_decoder,
    ) -> None:
        self.config = config
        self.voice_decoder = voice_decoder
        self.calls = calls  # voice calls with the owner (``bridge/discord_voice.py``), when they're on
        self.settings = config.discord_bridge
        self.assistant = assistant
        self.events = events
        self.client_factory = client_factory
        self.idle = idle
        self.fetch = fetch
        self.file_factory = file_factory
        self.inbox = config.resolve_path(INBOX)
        self.limit = TurnLimit(self.settings.max_turns_per_10_min, clock=clock)
        self.problem: str | None = "the bridge hasn't started"  # why she can't text right now, for the tools
        self.client: Any = None
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._announced = False  # said once that it's connected
        self._channel: Any = None  # the owner's DM channel
        self._answer: asyncio.Future[str] | None = None  # a question waiting for the owner's next DM
        self._paused_told = False
        self._background: set[asyncio.Task[Any]] = set()
        self._turns: set[asyncio.Future[Any]] = set()  # texted turns not answered yet
        self._dropped: set[asyncio.Future[Any]] = set()  # ...that a texted stop ended: no reply of their own
        self._preparing: set[asyncio.Future[None]] = set()  # texts whose files are still saved or written down

    # ------------------------------------------------------------------ setup
    @property
    def owner_id(self) -> int:
        return owner_of(self.config)

    @property
    def ready(self) -> bool:
        return self._connected and self.client is not None

    def notice(self, message: str, level: int = logging.INFO) -> None:
        self.events.emit(Notice(source="DISCORD", message=message, level=level))

    def start(self) -> bool:
        """Log the bot in, in the background. False (with a notice saying what to set) when it can't start: no
        owner, or no token. A refused token or a missing intent is said once when Discord answers, and it stops."""
        if not self.owner_id:
            self.problem = (
                "the Discord bridge has no owner: set discord_bridge.owner_id in config/local.yaml (or "
                "DISCORD_OWNER_ID in .env) to your own Discord user ID, then restart"
            )
            self.notice(f"Not starting: {self.problem}.", logging.WARNING)
            return False
        if not keys.is_set("DISCORD_BOT_TOKEN"):
            self.problem = keys.missing("DISCORD_BOT_TOKEN", cfg=self.config)
            self.notice(f"The Discord bridge is on but not starting: {self.problem}", logging.WARNING)
            return False
        self.problem = "the Discord bridge is still connecting"
        self._task = asyncio.create_task(self._run(), name="discord-bridge")
        return True

    async def _run(self) -> None:
        """Keep the bot logged in. discord.py rides out dropped connections itself; this only starts it again after
        a failure it gave up on (no network at startup), waiting longer each time, and stops for good when Discord
        refused the token or the intent: those need the user, and retrying would only spam the log."""
        delay = _RETRY_S
        warned = False
        while True:
            token = keys.value("DISCORD_BOT_TOKEN")  # read here only, never kept, logged or shown
            client = self.client = self.client_factory(self)
            try:
                await client.start(token)
                return  # closed (close)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                kind = _failure(exc)
                if kind == "token":
                    self.problem = (
                        "Discord refused the bot token (DISCORD_BOT_TOKEN in .env): reset it in the Developer Portal "
                        "(your application, Bot, Reset Token), paste the new one into .env and restart"
                    )
                    self.notice(self.problem, logging.ERROR)
                    return
                if kind == "intents":
                    self.problem = (
                        "Discord won't send the bot your messages: turn on its Message Content intent "
                        "(Developer Portal, your application, Bot, Privileged Gateway Intents) and restart"
                    )
                    self.notice(self.problem, logging.ERROR)
                    return
                self.problem = "the Discord bridge can't reach Discord right now"
                log.warning("Discord bridge: connecting failed (%s); trying again in %.0fs", type(exc).__name__, delay)
                if not warned:
                    warned = True
                    self.notice(f"Can't reach Discord yet; I'll keep trying ({type(exc).__name__}).", logging.WARNING)
            finally:
                self._connected = False
                with contextlib.suppress(Exception):
                    await client.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, _MAX_RETRY_S)

    async def close(self) -> None:
        if self.calls is not None:
            with contextlib.suppress(Exception):
                await self.calls.close()
        if self._answer is not None and not self._answer.done():
            self._answer.cancel()
        for task in list(self._background):
            task.cancel()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.close()
        self._connected = False
        self.problem = "the Discord bridge is stopped"

    async def on_ready(self, client: Any) -> None:
        self.client = client
        self._connected = True
        self.problem = None
        name = getattr(client, "user", None) or "the bot"
        if not self._announced:
            self._announced = True
            self.notice(f"Discord bridge connected: DM {name} from your phone.")
        else:
            log.debug("Discord bridge reconnected")
        if self.calls is not None:
            try:
                await self.calls.find_owner(client)  # already in a voice channel: join them there
            except Exception:
                log.warning("Couldn't look for the owner in the voice channels", exc_info=True)

    async def on_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        """Someone's voice state changed in one of the bot's servers: only the owner's moves matter."""
        if self.calls is None or getattr(member, "id", None) != self.owner_id or getattr(member, "bot", False):
            return
        if _channel_id(before) == _channel_id(after):
            return  # muted, deafened, started a stream: same channel
        await self.calls.owner_moved(getattr(after, "channel", None))

    # ---------------------------------------------------------------- incoming
    def _from_owner(self, message: Any) -> bool:
        """A DM the owner typed: not a bot's (her own included), not a webhook's, not in a server channel."""
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False) or getattr(message, "webhook_id", None):
            log.debug("Discord bridge: ignored a message from a bot")
            return False
        if getattr(message, "guild", None) is not None:
            log.debug("Discord bridge: ignored a message in a server channel")
            return False
        if getattr(author, "id", None) != self.owner_id:
            log.debug("Discord bridge: ignored a DM from someone else (user %s)", getattr(author, "id", "?"))
            return False
        return True

    async def on_message(self, message: Any) -> None:
        """One DM: the answer to a question she's waiting on, or a turn of its own."""
        if not self._from_owner(message):
            return
        self._channel = message.channel
        text = str(getattr(message, "content", "") or "").strip()
        busy = self._turns or self._preparing
        if text and busy and (stopped := await self._stop(message.channel, text)) is not None:
            if not stopped:
                return
            text, dropped = stopped, True
        else:
            dropped = False
        if self._answer is not None and not self._answer.done():
            self._answer.set_result(text)
            return
        if not self.limit.take():
            if not self._paused_told:
                self._paused_told = True
                minutes = max(1, round(self.limit.wait_s() / 60))
                await self._send_text(
                    message.channel,
                    f"I'm pausing for a bit: that's {self.limit.most} messages in 10 minutes. Text me again in about "
                    f"{minutes} minute{'s' if minutes != 1 else ''}.",
                    "pause",
                )
            return
        self._paused_told = False
        attachments = list(getattr(message, "attachments", None) or ())
        ticket: asyncio.Future[None] = asyncio.get_running_loop().create_future()  # a stop meanwhile cancels it
        self._preparing.add(ticket)
        try:
            saved = (
                await save_attachments(
                    attachments, self.inbox, int(self.settings.max_attachment_mb * 1_000_000), fetch=self.fetch
                )
                if attachments
                else []
            )
            heard = await self._hear(saved) if text or saved else []
        finally:
            self._preparing.discard(ticket)
        if ticket.cancelled():
            return  # a texted stop came while its files were saved or written down
        if not text and not saved:
            return  # a sticker, an empty message: nothing to answer
        said = [words for words in heard if words]
        if said:
            text = "\n\n".join([text, *said]) if text else "\n\n".join(said)
        extra = [_HEARD_NOTE] if said else []
        if len(said) < len(heard):
            extra.append(_NO_WORDS_NOTE)
        if dropped:
            extra.append(_DROPPED_NOTE)
        await self._turn(message.channel, text or _ONLY_FILES, saved, extra)

    async def _stop(self, channel: Any, text: str) -> str | None:
        """A text sent while an earlier one is unanswered: when it has a stop in it ("drop the task and make a fix"),
        the earlier ones end now (``Assistant.stop_from``) instead of this waiting in line behind them, and the user is
        told so. Returns the rest of the text to run as a turn ("" for nothing more), or None when it has no stop or
        there was nothing left to stop. While she waits for the answer to a question, only a text that is nothing but
        a stop is one: "wait, what will it send?" is their answer."""
        assistant = self.config.assistant
        kind, core, rest = stop_in(text, assistant.name, assistant.wake_phrase)
        asking = self._answer is not None and not self._answer.done()
        if kind is None or (asking and rest):
            return None
        try:
            ended, line = self.assistant.stop_from(SOURCE, kind, core)
        except Exception:
            log.exception("Stopping the texted turns failed")
            ended, line = [], None
        for ticket in list(self._preparing):
            ticket.cancel()
            ended.append(ticket)
        if not ended and not line:
            return None  # it all finished meanwhile: an ordinary text
        self._dropped.update(ended)
        if asking and self._answer is not None and not self._answer.done():
            self._answer.set_result("no")  # the question the stopped turn was waiting on: declined
        await self._send_text(channel, f"{_DROPPED} {line}" if line else _DROPPED, "stop")
        return rest

    async def _hear(self, saved: Sequence[Saved]) -> list[str]:
        """What the user says in each voice message they sent ("" for one with no words in it), written down by
        her own speech recognition (``voice_notes``). One she can't decode, or any while the speech model isn't
        there, is left out: the turn's note still lists the file."""
        stt = getattr(self.assistant, "stt", None)
        notes = [s.path for s in saved if s.kind == "audio" and s.path is not None]
        if stt is None or not notes:
            return []
        vocabulary = getattr(self.assistant, "vocabulary", None)
        heard: list[str] = []
        for path in notes:
            try:
                words = await voice_notes.transcribe(path, stt, self.voice_decoder)
            except asyncio.CancelledError:
                raise
            except ValueError:
                log.info("Discord bridge: %s isn't a voice message I can decode", path.name)
                continue
            except Exception:
                log.warning("Discord bridge: couldn't write down a voice message", exc_info=True)
                continue
            if words and vocabulary is not None:
                words = vocabulary.correct(words)
            audit.info("discord voice message heard file=%s chars=%d", path.name, len(words))
            heard.append(words)
        return heard

    async def _turn(self, channel: Any, text: str, saved: Sequence[Saved], extra: Sequence[str] = ()) -> None:
        notes = [_TEXTED_NOTE, *extra]
        if files := turn_note(saved):
            notes.append(files)
        typing = asyncio.create_task(self._keep_typing(channel), name="discord-typing")
        turn: asyncio.Future[Any] | None = None
        result: Any = None
        try:
            turn = self.assistant.run_from(SOURCE, text, "; ".join(notes), speak=self.settings.speak_replies)
            self._turns.add(turn)
            result = await turn
            reply = _reply_text(result)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if turn is None or turn not in self._dropped or (task is not None and task.cancelling()):
                raise
            reply = None  # taken back by a texted stop: its reply was that it's dropped
        except Exception:
            log.exception("A texted turn failed")
            reply = _GLITCH
        finally:
            typing.cancel()
            if turn is not None:
                self._turns.discard(turn)
        if turn is not None and turn in self._dropped:
            self._dropped.discard(turn)
            if reply is None or getattr(result, "barge_in", None) == "stop" or getattr(result, "interrupted", False):
                return  # stopped by a texted stop, which already answered
            reply = f"{_TOO_LATE} {reply}"  # a quick command already under way when the stop came: it's done
        if reply is not None:
            await self._send_text(channel, reply, "reply")

    async def _keep_typing(self, channel: Any) -> None:
        """Show "typing..." in the DM while she works, but not while she waits for the answer to a question."""
        while True:
            if self._answer is None or self._answer.done():
                try:
                    await channel.typing()
                except Exception:
                    log.debug("Couldn't show typing in the DM", exc_info=True)
            await asyncio.sleep(_TYPING_EVERY_S)

    # ---------------------------------------------------------------- outgoing
    async def _owner_channel(self) -> Any:
        """The owner's DM channel (the one they last wrote in, else opened now), or None when Discord won't say."""
        if self._channel is not None:
            return self._channel
        client = self.client
        if client is None or not self.owner_id:
            return None
        try:
            user = client.get_user(self.owner_id) or await client.fetch_user(self.owner_id)
            self._channel = await user.create_dm()
        except Exception as exc:
            log.warning("Discord bridge: couldn't open the DM with the owner (%s)", type(exc).__name__)
            return None
        return self._channel

    async def _send_text(self, channel: Any, text: str, kind: str) -> None:
        """Send ``text`` in as many messages as it takes; each send goes in the audit log (its length, not its
        words: those are in the conversation's transcript)."""
        for piece in split_message(text):
            await channel.send(piece)
            audit.info("discord send=%s to=owner chars=%d", kind, len(piece))

    async def send(self, text: str, files: Sequence[str | Path] = ()) -> str:
        """``send_to_phone``: a message and/or files to the owner's DM, each file checked first
        (``outgoing_problem``). Returns what was sent, in words; raises ``BridgeError`` saying why not."""
        if not self.ready:
            raise BridgeError(f"I can't text you right now: {self.problem or 'the Discord bridge is not connected'}.")
        max_bytes = int(self.settings.max_file_mb * 1_000_000)
        paths: list[Path] = []
        for raw in files:
            if (problem := outgoing_problem(raw, self.config, max_bytes)) is not None:
                raise BridgeError(problem)
            paths.append(Path(str(raw).strip().strip('"')).expanduser().resolve())
        if not text.strip() and not paths:
            raise BridgeError("There's nothing to send: give a message or a file.")
        channel = await self._owner_channel()
        if channel is None:
            raise BridgeError("Discord won't open a DM with you: send the bot a message first, then try again.")
        try:
            if text.strip():
                await self._send_text(channel, text, "send_to_phone")
            for path in paths:
                await channel.send(file=self.file_factory(path))
                audit.info("discord send=send_to_phone to=owner file=%s bytes=%d", path, path.stat().st_size)
        except Exception as exc:
            raise BridgeError(_send_failure(exc)) from exc
        sent = [f"{p.name} ({size_text(p.stat().st_size)})" for p in paths]
        what = (["the message"] if text.strip() else []) + sent
        return "Sent to your phone: " + (", ".join(what[:-1]) + " and " + what[-1] if len(what) > 1 else what[0]) + "."

    # ----------------------------------------------------------- announcements
    def away(self) -> bool:
        """Nobody has touched the keyboard or mouse here for ``away_after_min``."""
        return self.idle() >= self.settings.away_after_min * 60

    def relay(self, text: str, cue: str | None = None) -> None:
        """An announcement (``Assistant.relay``): also texted to the owner when ``notify`` says so ("always", or
        "away" while the PC sits untouched). Never waits: the text goes out in the background."""
        mode = self.settings.notify
        if mode == "off" or not self.ready or not text.strip():
            return
        if self.calls is not None and self.calls.on_call:
            return  # she says it in the call
        if mode == "away" and not self.away():
            return
        task = asyncio.get_running_loop().create_task(self._notify(text), name="discord-notify")
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _notify(self, text: str) -> None:
        channel = await self._owner_channel()
        if channel is None:
            return
        try:
            await self._send_text(channel, text, "notify")
        except Exception as exc:
            log.warning("Discord bridge: couldn't text an announcement (%s)", type(exc).__name__)

    # ----------------------------------------------------------- confirmation
    def timeout_s(self, request: ConfirmationRequest) -> float | None:
        return self.settings.confirm_timeout_s + _CONFIRM_MARGIN_S

    async def confirm(self, request: ConfirmationRequest) -> bool:
        """Ask in the DM and take the owner's next message as the answer: a yes or a no (``parse_confirmation``),
        one more question when it's unclear. Nothing within ``confirm_timeout_s``, or a second unclear reply, is no
        answer (``NotAnswered``), never a no: it isn't done, and the brain hears that it wasn't answered."""
        channel = await self._owner_channel()
        if channel is None:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.confirm_timeout_s
        try:
            asked = _ASK.format(request.description) + (f"\n`{request.detail}`" if request.detail else "")
            await self._send_text(channel, asked, "question")
            for attempt in range(2):
                answer = await self._next_answer(deadline - loop.time())
                if answer is None:
                    await self._send_text(channel, _NO_ANSWER, "question")
                    raise NotAnswered("no answer came in Discord")
                verdict = parse_confirmation(answer)
                if verdict is not None:
                    return verdict
                if attempt == 0:
                    await self._send_text(channel, _REASK, "question")
            await self._send_text(channel, _NOT_CLEAR, "question")
            raise NotAnswered("the reply in Discord wasn't a clear yes or no")
        except (asyncio.CancelledError, NotAnswered):
            raise
        except Exception:
            log.exception("Asking in Discord failed")
        return False

    async def _next_answer(self, timeout: float) -> str | None:
        """The owner's next DM, or None when none comes in ``timeout`` seconds."""
        if timeout <= 0:
            return None
        self._answer = asyncio.get_running_loop().create_future()
        try:
            async with asyncio.timeout(timeout):
                return await self._answer
        except TimeoutError:
            return None
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # the turn itself is being cancelled
            return None  # the bridge is closing: nobody will answer
        finally:
            self._answer = None


def _channel_id(voice_state: Any) -> int | None:
    """The id of the voice channel a voice state is in (None: in none)."""
    return getattr(getattr(voice_state, "channel", None), "id", None)


def _reply_text(result: Any) -> str:
    """Her answer, as the text to send: what she said (as shown: tts/shown.py), else a word about what went wrong."""
    text = shown_form(str(getattr(result, "text", "") or "")).strip()
    if text:
        if getattr(result, "interrupted", False):
            text += "\n\n(I got interrupted at the PC, so that may be cut short.)"
        return text
    if getattr(result, "error", None):
        return _GLITCH
    if getattr(result, "interrupted", False):
        return "I got interrupted at the PC before I could answer. Try again?"
    return "Done."


def _send_failure(exc: BaseException) -> str:
    """Why Discord didn't take a message, for the user."""
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    if status == 413 or code == 40005:
        return "Discord refused it: the file is too big for a bot to send."
    if code == 50007 or status == 403:
        return "Discord won't let the bot message you: send it a message first (or share a server with it)."
    return f"Discord didn't take it ({type(exc).__name__})."
