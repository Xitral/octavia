"""Audio device discovery and selection (PortAudio via sounddevice).

Devices are chosen by name (substring, case-insensitive) within a host API
because indices change between sessions. On Windows, WASAPI is preferred:
with ``auto_convert`` it accepts any sample rate and gives ~25 ms output
latency, versus ~100 ms for MME.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

log = logging.getLogger(__name__)

DEFAULT_HOST_API = "Windows WASAPI" if sys.platform == "win32" else None
# SteelSeries Sonar splits the default output into channels. Her voice belongs on Chat: the Gaming channel runs
# virtual surround with a room reverb over everything, which smears and crackles a voice.
SONAR_PREFIX = "steelseries sonar - "
SONAR_VOICE_CHANNEL = "steelseries sonar - chat"
WINDOWS_DEFAULT = ""  # as a device ref: the system default exactly, without moving off a Sonar channel
_LISTING_SUFFIX = re.compile(r"\s*\((?:in|out)[^()]*\bHz\)\s*$", re.IGNORECASE)


class AudioDeviceError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceChoice:
    index: int
    name: str
    host_api: str
    extra_settings: Any  # sd.WasapiSettings or None


def _sd():  # imported lazily so tests and text mode don't need PortAudio
    import sounddevice as sd

    return sd


def list_devices() -> list[dict[str, Any]]:
    sd = _sd()
    apis = sd.query_hostapis()
    out = []
    for i, dev in enumerate(sd.query_devices()):
        out.append(
            {
                "index": i,
                "name": dev["name"],
                "host_api": apis[dev["hostapi"]]["name"],
                "inputs": dev["max_input_channels"],
                "outputs": dev["max_output_channels"],
                "default_rate": dev["default_samplerate"],
            }
        )
    return out


def _host_api_index(name: str | None) -> int | None:
    if not name:
        return None
    sd = _sd()
    for i, api in enumerate(sd.query_hostapis()):
        if api["name"].lower() == name.lower() or name.lower() in api["name"].lower():
            return i
    raise AudioDeviceError(f"Host API {name!r} not found. Available: {[a['name'] for a in sd.query_hostapis()]}")


def choose_device(ref: int | str | None, kind: Literal["input", "output"], host_api: str | None) -> DeviceChoice:
    sd = _sd()
    api_name = host_api if host_api is not None else DEFAULT_HOST_API
    api_index = _host_api_index(api_name)
    apis = sd.query_hostapis()
    devices = sd.query_devices()
    channels_key = "max_input_channels" if kind == "input" else "max_output_channels"

    if isinstance(ref, int):
        if not 0 <= ref < len(devices) or devices[ref][channels_key] < 1:
            raise AudioDeviceError(f"Device #{ref} is not a valid {kind} device")
        index = ref
    elif isinstance(ref, str) and ref.strip():
        # Tolerate names pasted straight from `octavia devices`, e.g. "Mic (Foo)  (in, 48000 Hz)".
        needle = _LISTING_SUFFIX.sub("", ref).strip().lower()
        candidates = [
            i
            for i, d in enumerate(devices)
            if d[channels_key] > 0 and needle in d["name"].lower() and (api_index is None or d["hostapi"] == api_index)
        ]
        if not candidates:
            raise AudioDeviceError(
                f"No {kind} device matching {ref!r}"
                + (f" on {api_name}" if api_index is not None else "")
                + ". Run `octavia devices` to see the options."
            )
        exact = [i for i in candidates if devices[i]["name"].lower() == needle]
        index = (exact or candidates)[0]
        if len(candidates) > 1 and not exact:
            log.warning(
                "Several %s devices match %r (%s); using %s. Use a longer part of the name to pick another.",
                kind,
                ref,
                ", ".join(devices[i]["name"] for i in candidates),
                devices[index]["name"],
            )
    else:
        if api_index is not None:
            index = apis[api_index][f"default_{kind}_device"]
        else:
            index = sd.default.device[0 if kind == "input" else 1]
        if index is None or index < 0:
            raise AudioDeviceError(f"No default {kind} device")
        if kind == "output" and ref is None:
            index = _sonar_voice_channel(devices, index)

    api = apis[devices[index]["hostapi"]]["name"]
    extra = sd.WasapiSettings(auto_convert=True) if "WASAPI" in api else None
    return DeviceChoice(index=index, name=devices[index]["name"], host_api=api, extra_settings=extra)


def _sonar_voice_channel(devices: Any, index: int) -> int:
    """The default output, or Sonar's Chat channel (on the same host API) when the default is another Sonar
    channel. Set ``audio.output_device`` to keep her on a particular one."""
    name = devices[index]["name"].lower()
    if not name.startswith(SONAR_PREFIX) or name.startswith(SONAR_VOICE_CHANNEL):
        return index
    for i, d in enumerate(devices):
        if (
            d["max_output_channels"] > 0
            and d["hostapi"] == devices[index]["hostapi"]
            and d["name"].lower().startswith(SONAR_VOICE_CHANNEL)
        ):
            log.info("The default speaker is %s; her voice goes to %s instead", devices[index]["name"], d["name"])
            return i
    return index


_com_state = threading.local()


def ensure_com() -> None:
    """Initialize COM on the calling thread (Windows). Call before opening or starting a stream.

    PortAudio initializes COM only on the thread that imported sounddevice. Opening a
    WASAPI stream from any other thread (asyncio.to_thread workers, recovery threads)
    then fails with "Unanticipated host error -9999 ... GLE 0x490" on some devices
    (verified with the Voicemod virtual mic), or works only by luck when an earlier
    call happened to initialize COM on that pooled thread.
    """
    if sys.platform != "win32" or getattr(_com_state, "done", False):
        return
    import ctypes

    try:
        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        ole32.CoInitializeEx.restype = ctypes.c_long
        # 0 = COINIT_MULTITHREADED. RPC_E_CHANGED_MODE (already an STA thread) is fine too.
        ole32.CoInitializeEx(None, 0)
    except Exception:
        log.debug("CoInitializeEx failed", exc_info=True)
    _com_state.done = True


_reinit_hooks: list[Callable[[], None]] = []
_reinit_lock = threading.RLock()


def add_reinit_hook(hook: Callable[[], None]) -> Callable[[], None]:
    """Register a stream to reopen after PortAudio is re-initialized. Returns an unregister function.

    Pa_Terminate closes *every* open stream in the process, so whoever re-initializes
    (the mic watchdog, the speaker watchdog) must bring all the others back too.
    Hooks run on the re-initializing thread and must not touch their old stream
    object: PortAudio has already freed it.
    """
    with _reinit_lock:
        _reinit_hooks.append(hook)

    def remove() -> None:
        with _reinit_lock:
            if hook in _reinit_hooks:
                _reinit_hooks.remove(hook)

    return remove


def reinitialize_portaudio() -> None:
    """Re-scan devices (PortAudio only enumerates at init), then reopen every registered stream.

    Uses private sounddevice API. Raises if PortAudio can't be re-initialized.
    """
    sd = _sd()
    ensure_com()
    with _reinit_lock:
        try:
            sd._terminate()
        except Exception:
            log.debug("PortAudio terminate failed", exc_info=True)
        sd._initialize()
        for hook in list(_reinit_hooks):
            try:
                hook()
            except Exception:
                log.warning("Reopening an audio stream after PortAudio re-init failed", exc_info=True)
