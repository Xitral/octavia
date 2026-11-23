"""The local voice worker: Qwen3-TTS 1.7B (voice clone) on the GPU, run by ``local_voice.LocalVoiceTTS`` with the Python
of a Qwen3-TTS-GGUF checkout (github.com/HaujetZhao/Qwen3-TTS-GGUF: the talker and predictor as GGUF on llama.cpp's
Vulkan build, the codec decoder in ONNX Runtime with DirectML).

It's a separate process so none of that loads into her own (a crash here is a lost sentence that Fish says instead,
not a dead assistant), and it imports nothing from octavia. Run as::

    python qwen_worker.py <engine_dir> <model_dir> <voice.wav> <voice.txt> <gpu 0|1> <first_frames> <temperature>
        <lang> <detail_temperature> <min_p> <seed: -1 for none>

Protocol. In, on stdin, one JSON object per line::

    {"op": "say", "id": 7, "text": "..."}     speak this, as request 7
    {"op": "stop", "id": 7}                    stop request 7 (the user cut in)

Out, on stdout, frames of a 9-byte header (kind byte, request id and payload length as little-endian uint32)
followed by the payload::

    R  ready: JSON {"sample_rate": 24000, "load_s": ..., "clone_s": ...} (id 0)
    S  request id has started (the ones before it are done)
    A  16-bit mono PCM for request id
    E  request id is finished
    X  request id failed (or id 0: the worker can't start): UTF-8 message

Requests are spoken one at a time in the order they came; a stop for one still waiting drops it.

Streaming: the codec decoder holds a few frames back (lookahead), so the first pieces are decoded every
``first_frames`` frames until sound comes out, then every ``chunk_size`` (12 frames, 1 s of speech). Measured on the
7900 XT (2026-10-26): first audio 0.3 to 0.5 s, 0.5 x real time, about 2 GB of video memory.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import struct
import sys
import threading
import time

HEADER = struct.Struct("<cII")
RATE = 24000


def main() -> int:
    engine_dir, model_dir, voice_wav, voice_txt = sys.argv[1:5]
    gpu, first_frames, temperature = sys.argv[5] == "1", int(sys.argv[6]), float(sys.argv[7])
    language = sys.argv[8] if len(sys.argv) > 8 else "english"
    detail_temperature = float(sys.argv[9]) if len(sys.argv) > 9 else temperature
    min_p = float(sys.argv[10]) if len(sys.argv) > 10 else 0.0
    seed = int(sys.argv[11]) if len(sys.argv) > 11 and int(sys.argv[11]) >= 0 else None
    out = sys.stdout.buffer
    write_lock = threading.Lock()

    def send(kind: bytes, rid: int, payload: bytes = b"") -> None:
        with write_lock:
            out.write(HEADER.pack(kind, rid, len(payload)) + payload)
            out.flush()

    try:
        sys.path.insert(0, engine_dir)
        with contextlib.redirect_stdout(sys.stderr):  # the engine prints; stdout carries only frames
            import numpy as np
            from qwen3_tts_gguf.inference import TTSConfig, TTSEngine
            from qwen3_tts_gguf.inference.schema.constants import map_language
            from qwen3_tts_gguf.inference.schema.result import Timing

            t = time.perf_counter()
            engine = TTSEngine(model_dir=model_dir, onnx_provider="DML" if gpu else "CPU", llm_use_gpu=gpu,
                               subprocess_decoder=False, verbose=False)  # fmt: skip
            if not engine.ready:
                raise RuntimeError(f"the Qwen3-TTS engine didn't load from {model_dir}")
            load_s = time.perf_counter() - t
            stream = engine.create_stream()
            t = time.perf_counter()
            voice = _voice(stream, voice_wav, voice_txt)
            clone_s = time.perf_counter() - t
            dec = engine.decoder._dec
            # One seed for every sentence: the samplers are made per sentence, so each starts from the same draw
            cfg = TTSConfig(temperature=temperature, sub_temperature=detail_temperature, min_p=min_p, seed=seed,
                            sub_seed=None if seed is None else seed + 1)  # fmt: skip
            lang = map_language(language)
    except Exception as exc:  # noqa: BLE001 - every start problem goes back to her as words
        send(b"X", 0, f"{type(exc).__name__}: {exc}".encode("utf-8", "replace"))
        return 1

    def speak(text: str, stop: threading.Event):
        """Float32 audio pieces as they're decoded; stops between frames once ``stop`` is set."""
        stream.talker.clear_memory()
        pdata = stream.prompt_builder.build_clone_prompt(text, voice, lang, False)
        state = voice.final_state
        buf: list = []
        target = first_frames
        frames = stream._run_engine_loop_gen(pdata, cfg, Timing())
        try:
            for codes, _ in frames:
                if stop.is_set():
                    return
                buf.append(codes)
                if len(buf) >= target:
                    audio, state = dec.decode(np.array(buf), state=state, is_final=False)
                    buf = []
                    if len(audio):  # held-back frames: small pieces until sound comes out
                        target = engine.chunk_size
                        yield audio
        finally:
            frames.close()  # frees its samplers now, not whenever it's collected (mid-way through the next sentence)
        audio, state = dec.decode(np.array(buf) if buf else np.zeros((0, 16), np.int64), state=state, is_final=True)
        if len(audio):
            yield audio

    with contextlib.redirect_stdout(sys.stderr):
        for _ in speak("Ready.", threading.Event()):  # the first run is slower; pay for it before she speaks
            pass
    send(b"R", 0, json.dumps({"sample_rate": RATE, "load_s": load_s, "clone_s": clone_s}).encode())

    work: queue.Queue[tuple[int, str] | None] = queue.Queue()
    stops: dict[int, threading.Event] = {}
    stops_lock = threading.Lock()

    def stop_event(rid: int) -> threading.Event:
        with stops_lock:
            return stops.setdefault(rid, threading.Event())

    def read_requests() -> None:
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("op") == "say":
                stop_event(int(msg["id"]))
                work.put((int(msg["id"]), str(msg.get("text", ""))))
            elif msg.get("op") == "stop":
                stop_event(int(msg["id"])).set()
        work.put(None)  # stdin closed: she's gone, so stop

    threading.Thread(target=read_requests, daemon=True).start()
    while (item := work.get()) is not None:
        rid, text = item
        stop = stop_event(rid)
        try:
            if not stop.is_set() and text.strip():
                send(b"S", rid)  # its turn: the one before it is done
                with contextlib.redirect_stdout(sys.stderr):
                    for piece in speak(text, stop):
                        send(b"A", rid, (np.clip(piece, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())
            send(b"E", rid)
        except Exception as exc:  # noqa: BLE001 - this sentence fails; the next may not
            send(b"X", rid, f"{type(exc).__name__}: {exc}".encode("utf-8", "replace"))
        finally:
            with stops_lock:
                stops.pop(rid, None)
    out.flush()
    os._exit(0)  # skip the llama.cpp bindings' teardown, which can crash on the way out


def _voice(stream, wav: str, txt: str):  # noqa: ANN001, ANN202 - the engine's types, imported late
    """Her cloned voice, kept beside the clip (``<clip>.qwen.json``) so a restart reads it back rather than cloning
    again (20 to 45 s); cloned again whenever the clip or its words are newer."""
    saved = f"{wav}.qwen.json"
    newest = max(os.path.getmtime(wav), os.path.getmtime(txt))
    if os.path.exists(saved) and os.path.getmtime(saved) >= newest:
        voice = stream.set_voice(saved)
        if voice:
            return voice
    with open(txt, encoding="utf-8") as f:
        words = f.read().strip()
    voice = stream.set_voice(wav, text=words)
    if not voice:
        raise RuntimeError(f"couldn't clone the voice from {wav}")
    with contextlib.suppress(Exception):  # only a speed-up
        voice.save(saved)
    return voice


if __name__ == "__main__":
    sys.exit(main())
