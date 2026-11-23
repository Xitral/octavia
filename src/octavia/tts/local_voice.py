"""Her cloned voice made on this PC: Qwen3-TTS 1.7B on the GPU, run by a worker process (``qwen_worker.py``).

Fish Audio's free tier is most of the wait before she speaks (0.8 to 1.2 s to the first audio, 3.5 to 7 s at busy
moments, and slower than real time on busy evenings, which is where mid-sentence pauses come from). Qwen3-TTS, cloned
from Fish-spoken clips of her voice with their words, measured 0.3 to 0.5 s to the first audio and 0.5 x real time on
the 7900 XT with about 2 GB of video memory (2026-10-26), and the user heard it as almost just like Fish. Kyutai Pocket
TTS (CPU, 0.1 s) was tried first and rejected by ear: it didn't sound like her.

The worker runs in a Python of its own (``tts.local.python``, from a Qwen3-TTS-GGUF checkout): none of its libraries
load into hers, and a crash there costs one sentence, which her backup voice says. It's put in a job that Windows ends
with her, like llama-server. Frames come back on its stdout (see the worker's docstring); one reader thread hands them
to each sentence's queue.

Loading takes 5 to 50 s. ``start`` begins it without waiting, so her startup isn't held up; until it's ready every
sentence raises ``NotReady`` and the backup (Fish) says it, with nothing announced. A worker that died is started
again on the next sentence, at most once a minute. While a game is full screen (``game_unload_s``) the worker is
ended so the game has the GPU, Fish speaks meanwhile, and it's loaded again once no game has been in front for
``game_reload_s``, as the local brain does; the GPU lent to a fine-tune (``localbrain.shadow.gpu_lent``) counts as a
game.

Fish's tone cues can't be performed, so anything in short square brackets is dropped, and there's no ``style_hint``
(the brain isn't told about cues it can't use).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import struct
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import IO

from octavia.audio.types import AudioFormat
from octavia.config import LocalVoiceConfig
from octavia.tts.base import STARTED, NotReady, TTSError, TTSProvider
from octavia.tts.windows_voice import strip_tags

log = logging.getLogger(__name__)

SAMPLE_RATE = 24000  # Qwen3-TTS 12 Hz codec's rate; checked against what the worker reports
HEADER = struct.Struct("<cII")
WORKER = Path(__file__).with_name("qwen_worker.py")
RESTART_EVERY_S = 60.0
GAME_POLL_S = 5.0
_NO_WINDOW = 0x08000000
_BELOW_NORMAL = 0x00004000  # her own loop and the mic come first


def _game_in_front() -> bool:
    from octavia.localbrain.shadow import game_in_front

    return game_in_front()


class LocalVoiceTTS(TTSProvider):
    name = "local"
    waits_its_turn = True  # the worker makes one sentence at a time: it says STARTED when this one's turn comes

    def __init__(
        self,
        cfg: LocalVoiceConfig,
        voice: Path,
        words: Path,
        game: Callable[[], bool] = _game_in_front,
        log_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.voice = voice  # the clip of her voice
        self.words = words  # what's said in it
        self.format = AudioFormat(sample_rate=SAMPLE_RATE)
        self.allowed_tags: frozenset[str] = frozenset()
        self.pause_tags: frozenset[str] = frozenset()
        self.inline_tags: frozenset[str] = frozenset()
        self._game = game
        self.log_path = log_path  # the worker's own output (its engine prints and any traceback)
        self._proc: subprocess.Popen[bytes] | None = None
        self._job: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = asyncio.Event()
        self._queues: dict[int, asyncio.Queue[tuple[bytes, bytes]]] = {}
        self._next_id = 0
        self._write_lock = threading.Lock()
        self._started_at = 0.0
        self._watch: asyncio.Task[None] | None = None
        self.unloaded_for_game = False  # ended so a game has the GPU; loaded again after it
        self.start_error = ""  # why the worker couldn't start, in full (voice_engine, the log)
        self.load_s: float | None = None
        self._key: str | None = None

    # -- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        c = self.cfg
        for what, path in (("tts.local.python", c.python), ("tts.local.engine_dir", c.engine_dir),
                           ("tts.local.model_dir", c.model_dir)):  # fmt: skip
            if not path:
                raise TTSError(f"The local voice needs {what} (docs/octavia-architecture.md, Local voice)")
            if not Path(path).exists():
                raise TTSError(f"The local voice's {what} isn't there: {path}")
        for path in (self.voice, self.words):
            if not path.is_file():
                raise TTSError(f"The local voice's clip or its words aren't there: {path}")
        self._loop = asyncio.get_running_loop()
        self._launch()
        if c.game_unload_s > 0:
            self._watch = asyncio.create_task(self._watch_games(), name="local-voice-games")

    def _launch(self) -> None:
        self._ready.clear()
        self.start_error = ""
        self._started_at = time.monotonic()
        c = self.cfg
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        args = [c.python, "-I", str(WORKER), c.engine_dir, c.model_dir, str(self.voice), str(self.words),
                "1" if c.gpu else "0", str(c.first_frames), str(c.temperature), c.language,
                str(c.detail_temperature), str(c.min_p), str(c.seed)]  # fmt: skip
        flags = (_NO_WINDOW | _BELOW_NORMAL) if sys.platform == "win32" else 0
        errors: int | IO[bytes] = subprocess.DEVNULL
        if self.log_path is not None:
            with contextlib.suppress(OSError):
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                errors = self.log_path.open("wb")  # one start's output: the last one is what explains a failure
        try:
            self._proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, env=env,
                creationflags=flags, cwd=c.engine_dir,
            )  # fmt: skip
        finally:
            if not isinstance(errors, int):
                errors.close()  # the worker has its own handle
        from octavia.localbrain.server import _die_with_us

        self._job = _die_with_us(self._proc)
        threading.Thread(target=self._read, args=(self._proc,), name="local-voice-reader", daemon=True).start()
        log.info("Local voice starting (%s)", self.voice.name)

    async def wait_ready(self, timeout: float | None = None) -> bool:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._ready.wait(), timeout or self.cfg.start_timeout_s)
        return self._ready.is_set()

    async def _end_worker(self) -> None:
        proc, self._proc = self._proc, None
        self._ready.clear()
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()  # type: ignore[union-attr]  # the worker stops when its input ends
            try:
                await asyncio.to_thread(proc.wait, 3.0)
            except Exception:
                proc.kill()
        from octavia.localbrain.server import _close_handle

        _close_handle(self._job)
        self._job = None

    async def close(self) -> None:
        watch, self._watch = self._watch, None
        if watch is not None:
            watch.cancel()
            await asyncio.wait({watch})
        await self._end_worker()
        self._fail_all("the local voice closed")

    # -- games ---------------------------------------------------------------------
    async def _watch_games(self) -> None:
        gaming_since: float | None = None
        clear_since: float | None = None
        while True:
            await asyncio.sleep(GAME_POLL_S)
            try:
                gaming = await asyncio.to_thread(self._game)
            except Exception:  # the watch must outlive anything: it's what gives a game the GPU back
                log.debug("Local voice: the game check failed once", exc_info=True)
                continue
            now = time.monotonic()
            if gaming:
                clear_since = None
                gaming_since = gaming_since or now
                if not self.unloaded_for_game and now - gaming_since >= self.cfg.game_unload_s:
                    self.unloaded_for_game = True
                    log.info("Local voice: a game is full screen, so it's unloaded and Fish speaks")
                    await self._end_worker()
            else:
                gaming_since = None
                clear_since = clear_since or now
                if self.unloaded_for_game and now - clear_since >= self.cfg.game_reload_s:
                    self.unloaded_for_game = False
                    log.info("Local voice: no game for a while, loading again")
                    self._launch()

    # -- the worker's output -----------------------------------------------------
    def _read(self, proc: subprocess.Popen[bytes]) -> None:
        """Runs on its own thread: frames from the worker, handed to the event loop."""
        out = proc.stdout
        assert out is not None
        try:
            while header := out.read(HEADER.size):
                if len(header) < HEADER.size:
                    break
                kind, rid, size = HEADER.unpack(header)
                payload = out.read(size) if size else b""
                self._post(self._frame, proc, kind, rid, payload)
        except (OSError, ValueError):
            pass
        self._post(self._worker_ended, proc)

    def _post(self, fn, *args) -> None:  # noqa: ANN001 - a loop callback
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):  # the loop closed in the meantime
                loop.call_soon_threadsafe(fn, *args)

    def _frame(self, proc: subprocess.Popen[bytes], kind: bytes, rid: int, payload: bytes) -> None:
        if proc is not self._proc:
            return  # an old worker's last words
        if rid == 0:
            if kind == b"R":
                info = json.loads(payload)
                if info.get("sample_rate") != SAMPLE_RATE:
                    self.start_error = f"the local voice speaks at {info.get('sample_rate')} Hz, not {SAMPLE_RATE}"
                    log.warning(self.start_error)
                    proc.kill()  # ends as a stopped worker: a failure, said, not a voice forever loading
                    return
                self.load_s = info.get("load_s")
                self._ready.set()
                log.info("Local voice ready in %.1f s (voice %.1f s)", time.monotonic() - self._started_at,
                         info.get("clone_s") or 0.0)  # fmt: skip
            elif kind == b"X":
                self.start_error = payload.decode("utf-8", "replace")
                log.warning("Local voice couldn't start: %s", self.start_error)
            return
        queue = self._queues.get(rid)
        if queue is not None:
            queue.put_nowait((kind, payload))

    def _worker_ended(self, proc: subprocess.Popen[bytes]) -> None:
        if proc is not self._proc:
            return  # an old one, already replaced or ended on purpose
        self._ready.clear()
        code = proc.poll()
        log.warning("Local voice worker ended (exit %s)%s", code, f": {self.start_error}" if self.start_error else "")
        self._fail_all("the local voice worker stopped")

    def _fail_all(self, why: str) -> None:
        for queue in self._queues.values():
            queue.put_nowait((b"X", why.encode()))

    # -- speaking ------------------------------------------------------------------
    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _send(self, msg: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise TTSError("the local voice worker isn't running")
        with self._write_lock:
            proc.stdin.write((json.dumps(msg) + "\n").encode())
            proc.stdin.flush()

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        text = strip_tags(text)
        if not text:
            return
        if self.unloaded_for_game:
            raise NotReady("the local voice is unloaded while a game runs")
        if not self._alive() and self._loop is not None:
            if time.monotonic() - self._started_at < RESTART_EVERY_S:
                why = f": {self.start_error}" if self.start_error else ""
                raise TTSError(f"the local voice worker stopped{why}")
            self._launch()
        if not self._ready.is_set():
            if time.monotonic() - self._started_at > self.cfg.start_timeout_s:
                raise TTSError(f"the local voice didn't finish loading in {self.cfg.start_timeout_s:.0f} s")
            raise NotReady("the local voice is still loading")
        self._next_id += 1
        rid = self._next_id
        queue: asyncio.Queue[tuple[bytes, bytes]] = asyncio.Queue()
        self._queues[rid] = queue
        finished = False
        try:
            try:
                self._send({"op": "say", "id": rid, "text": text})
            except OSError as exc:
                raise TTSError(f"can't reach the local voice worker: {exc}") from exc
            while True:
                kind, payload = await queue.get()
                if kind == b"A":
                    yield payload
                elif kind == b"S":
                    yield STARTED  # its turn came: the backup's first-audio clock starts now
                elif kind == b"E":
                    finished = True
                    return
                else:
                    finished = True
                    raise TTSError(f"the local voice: {payload.decode('utf-8', 'replace')}")
        finally:
            self._queues.pop(rid, None)
            if not finished:  # cut in (or a failure here): the worker stops making this sentence
                with contextlib.suppress(Exception):
                    self._send({"op": "stop", "id": rid})

    def cache_key(self) -> str:
        if self._key is not None:
            return self._key
        digest = hashlib.sha1()
        for path in (self.voice, self.words):
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"missing")
        c = self.cfg
        sampling = f"{c.temperature}:{c.detail_temperature}:{c.min_p}:{c.seed}"
        self._key = f"local:qwen:{Path(c.model_dir).name}:{sampling}:{digest.hexdigest()[:12]}:{SAMPLE_RATE}"
        return self._key
