"""llama.cpp's ``llama-server`` as a child process: the local model, on this PC's GPU, on 127.0.0.1 only.

It runs at below-normal priority with no window. ``start`` waits until the model is loaded and then reads her prompt
once (``warm``), so later requests only read what's new: about 0.1 s instead of ~16 s on the 7900 XT. ``stop`` ends
the process and gives its GPU memory back. Nothing here is on a turn's path: the shadow worker calls it.

The read prompt is also kept on disk (``local_brain.slot_cache_dir``, llama-server's slot save and restore): a game
sends the model away many times a day, and taking the prompt back from the file takes about a second where reading it
again took ~17 s. The file is named by a hash of the warm-up request and the model, so a changed prompt is never
restored; when a restore fails, the prompt is read as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import hashlib
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from octavia.config import LocalBrainConfig

log = logging.getLogger(__name__)

_LOAD_TIMEOUT_S = 300.0  # a first load from a cold disk took 74 s for the 9B
_BELOW_NORMAL = 0x00004000
_NO_WINDOW = 0x08000000
_KILL_ON_JOB_CLOSE = 0x2000
# Each user of the model has a slot of its own (``id_slot``), so each keeps its prompt cached: the shadow's ~42k
# tokens would take ~16 s to read again after an addressee check. The slots share one KV cache (``-kvu``) of
# ``context`` tokens, so this costs no more GPU memory than one slot. ``--no-cache-idle-slots``: by default the server
# moves an idle slot out to RAM when the other one gets work, and for this model (a hybrid with recurrent layers) it
# then reads the whole prompt again anyway (measured: 40k tokens again, 15.7 s; kept: 15 tokens, 0.16 s).
SLOTS = {"shadow": 0, "addressee": 1}
# A saved slot keeps the recurrent layers' checkpoints too (the server log says "restored 3 context checkpoint(s)"), so
# a restored prompt is read on from where it was. Measured with a 60k-token prompt: restored in 1.0 s from a 2.1 GB
# file, then 14 new tokens read, where reading it all took 28 s.
_CACHE_PREFIX = "shadow-"
_CACHE_TIMEOUT_S = 180.0  # a cold disk reading two gigabytes


def _die_with_us(proc: subprocess.Popen[bytes]) -> int | None:
    """Put the process in a job that Windows ends when this process does, however it ends: a crashed Octavia mustn't
    leave the model holding the GPU's memory (and its port, so the next start would refuse to start)."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes as w

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", w.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", w.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", w.DWORD),
            ("SchedulingClass", w.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", Basic),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = w.HANDLE
    kernel32.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        log.debug("Local brain: no job object (%d)", ctypes.get_last_error())
        return None
    info = Extended()
    info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) or not (
        kernel32.AssignProcessToJobObject(job, int(proc._handle))  # type: ignore[attr-defined]
    ):
        log.warning("Local brain: llama-server may outlive a crash (%d)", ctypes.get_last_error())
        _close_handle(job)
        return None
    return job  # open as long as the process should live: closing it ends the process


def _close_handle(handle: int | None) -> None:
    if handle and sys.platform == "win32":
        from ctypes import wintypes as w

        ctypes.WinDLL("kernel32").CloseHandle(w.HANDLE(handle))


def local_client(**kwargs: Any) -> httpx.AsyncClient:
    """An HTTP client for the model server on this PC (plain http to 127.0.0.1). ``verify=False`` because it never
    speaks TLS: by default every new client loads Windows' certificate store, about 200 ms with the event loop stuck,
    and the health check made one each second while the model loaded (2,041 stalls in three weeks, Nov 2026)."""
    return httpx.AsyncClient(verify=False, **kwargs)


class LlamaServer:
    def __init__(self, config: LocalBrainConfig, log_path: Path, cache_dir: Path | None = None) -> None:
        self.config = config
        self.log_path = log_path
        self.cache_dir = cache_dir  # where her read prompt is kept between loads (None: read it again each time)
        self.url = f"http://127.0.0.1:{config.port}"
        self.ready = False
        self._proc: subprocess.Popen[bytes] | None = None
        self._job: int | None = None
        self._lock = asyncio.Lock()  # one start at a time; ``stop`` doesn't wait for it
        self.transport: httpx.AsyncBaseTransport | None = None  # tests answer for the server

    def missing(self) -> str | None:
        """Why it can't run (a file that isn't there), or None."""
        for what, path in (("server", self.config.server), ("model", self.config.model)):
            if not path or not Path(path).is_file():
                return f"local_brain.{what} not found: {path or '(unset)'}"
        return None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def start(self, warm: dict[str, Any] | None = None) -> bool:
        """Start it and wait until it answers; then send ``warm`` (a request body) once so her prompt is cached.
        False when it couldn't start: the port is someone else's, the process died, or it took too long."""
        async with self._lock:
            if self.running:
                return self.ready
            if await self._answers(any_reply=True):  # something else (a benchmark) has the port: never talk to it
                log.warning("Local brain: port %d is already in use; not starting", self.config.port)
                return False
            cmd = [
                self.config.server, "-m", self.config.model, "--host", "127.0.0.1", "--port", str(self.config.port),
                "-c", str(self.config.context), "-ngl", "999", "--device", self.config.device, "--jinja",
                "-np", str(len(SLOTS)), "-kvu", "--no-cache-idle-slots", "--no-webui",
            ]  # fmt: skip
            if self.cache_dir is not None:
                try:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    cmd += ["--slot-save-path", str(self.cache_dir)]
                except OSError:
                    log.warning("Local brain: no prompt cache folder at %s", self.cache_dir, exc_info=True)
                    self.cache_dir = None
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            flags = _BELOW_NORMAL | _NO_WINDOW if sys.platform == "win32" else 0
            with open(self.log_path, "ab") as out:
                self._proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, creationflags=flags
                )
            try:
                self._job = _die_with_us(self._proc)
            except Exception:
                log.warning("Local brain: llama-server may outlive a crash", exc_info=True)
            started = time.monotonic()
            while time.monotonic() - started < _LOAD_TIMEOUT_S:
                if not self.running:
                    if self._proc is not None:  # not stopped on purpose (a game came up): it died
                        log.warning("Local brain: llama-server exited while loading (see %s)", self.log_path)
                        await self._kill()
                    return False
                if await self._answers():
                    break
                await asyncio.sleep(1.0)
            else:
                log.warning("Local brain: the model didn't load in %.0f s", _LOAD_TIMEOUT_S)
                await self._kill()
                return False
            log.info("Local brain: model loaded in %.1f s", time.monotonic() - started)
        if warm is not None:
            await self._warm(warm)
        self.ready = self.running
        return self.ready

    async def _warm(self, warm: dict[str, Any]) -> None:
        """Her prompt into the shadow's slot: from the file an earlier load saved when there's one for this very prompt
        and model, otherwise read (and then saved for next time)."""
        name = await asyncio.to_thread(self.cache_name, warm)
        if name is not None and self.cache_dir is not None and await asyncio.to_thread((self.cache_dir / name).is_file):
            if await self._restore(name):
                return
        started = time.monotonic()
        try:
            async with local_client(timeout=180, transport=self.transport) as client:
                await client.post(f"{self.url}/v1/chat/completions", json=warm)
            log.info("Local brain: her prompt read in %.1f s", time.monotonic() - started)
        except httpx.HTTPError as exc:
            log.warning("Local brain: warming up failed: %s", exc)
            return
        if name is not None and self.running:
            await self._save(name)

    async def _restore(self, name: str) -> bool:
        started = time.monotonic()
        try:
            reply = await self._slot("restore", name)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Local brain: couldn't restore her prompt from disk (%s); reading it again", exc)
            await asyncio.to_thread(self._forget, name)  # a broken file mustn't fail every load
            return False
        log.info(
            "Local brain: her prompt restored from disk in %.1f s (%s tokens)",
            time.monotonic() - started,
            reply.get("n_restored", "?"),
        )
        await asyncio.to_thread(self._prune, name)
        return True

    async def _save(self, name: str) -> None:
        started = time.monotonic()
        try:
            reply = await self._slot("save", name)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Local brain: couldn't save her prompt to disk: %s", exc)
            await asyncio.to_thread(self._forget, name)  # half a file would only fail the next restore
            return
        log.info(
            "Local brain: her prompt saved to disk in %.1f s (%s tokens, %.1f GB)",
            time.monotonic() - started,
            reply.get("n_saved", "?"),
            (reply.get("n_written") or 0) / 1e9,
        )
        await asyncio.to_thread(self._prune, name)

    async def _slot(self, action: str, name: str) -> dict[str, Any]:
        async with local_client(timeout=_CACHE_TIMEOUT_S, transport=self.transport) as client:
            response = await client.post(
                f"{self.url}/slots/{SLOTS['shadow']}", params={"action": action}, json={"filename": name}
            )
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}: {response.text[:200]}")
        reply = response.json()
        return reply if isinstance(reply, dict) else {}

    def cache_name(self, warm: dict[str, Any]) -> str | None:
        """The file her read prompt is kept in: named by a hash of the warm-up request (her prompt and tools), the
        settings the server starts with, and the model and server files themselves, so nothing stale is restored."""
        if self.cache_dir is None:
            return None
        files = []
        for path in (self.config.model, self.config.server):
            try:
                stat = Path(path).stat()
            except OSError:
                return None
            files.append([str(path), stat.st_size, stat.st_mtime_ns])
        started_with = [self.config.context, self.config.device, len(SLOTS)]
        key = json.dumps([warm, started_with, files], sort_keys=True, ensure_ascii=False)
        return f"{_CACHE_PREFIX}{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}.bin"

    def _prune(self, keep: str) -> None:
        """Delete the files kept for other prompts or models: each is a gigabyte or two."""
        if self.cache_dir is None:
            return
        for path in self.cache_dir.glob(f"{_CACHE_PREFIX}*.bin"):
            if path.name != keep:
                try:
                    path.unlink()
                except OSError:
                    continue
                log.info("Local brain: deleted an old prompt file (%s)", path.name)

    def _forget(self, name: str) -> None:
        if self.cache_dir is not None:
            with contextlib.suppress(OSError):
                (self.cache_dir / name).unlink(missing_ok=True)

    async def stop(self) -> None:
        """End it now, even in the middle of loading (a game wants the memory): the load then gives up."""
        await self._kill()

    async def _kill(self) -> None:
        self.ready = False
        proc, self._proc = self._proc, None
        job, self._job = self._job, None
        try:
            if proc is None or proc.poll() is not None:
                return
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 15)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()
        finally:
            _close_handle(job)

    async def _answers(self, any_reply: bool = False) -> bool:
        """It's up (``/health`` is 200; 503 while still loading). ``any_reply``: anything at all answers there."""
        try:
            async with local_client(timeout=2) as client:
                status = (await client.get(f"{self.url}/health")).status_code
                return any_reply or status == 200
        except httpx.HTTPError:
            return False
