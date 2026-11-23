"""`octavia doctor` (setup checks) and `octavia listen-test` (live tuning view)."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from octavia.config import AppConfig, current_env

OK, WARN, FAIL = "ok  ", "warn", "FAIL"


class _Report:
    def __init__(self) -> None:
        self.failures = 0

    def line(self, status: str, what: str, detail: str = "") -> None:
        if status == FAIL:
            self.failures += 1
        print(f"  [{status}] {what}" + (f": {detail}" if detail else ""))


async def run_doctor(config: AppConfig, live: bool = False) -> int:
    r = _Report()
    print(f"{config.assistant.name} doctor")
    print(f"  project: {config.root}")
    print(f"  config:  {', '.join(config.sources) or '(built-in defaults)'}")

    print("\nPython")
    r.line(OK if sys.version_info[:2] == (3, 12) else WARN, "version", sys.version.split()[0])

    print("\nModels")
    from octavia.setup_models import asset_present, required_assets

    for asset in required_assets(config):
        present = asset_present(asset, config)
        r.line(OK if present else FAIL, asset.name, asset.dest if present else "missing; run `uv run octavia setup`")
    try:
        from octavia.stt import create_stt

        stt = create_stt(config)
        check = getattr(stt, "check_source", None)
        if check is not None:
            check()
        present = getattr(stt, "files_present", lambda: True)()
        where = str(getattr(stt, "model_dir", ""))
        r.line(
            OK if present else FAIL,
            "Speech recognition model",
            where if present else "missing; run `uv run octavia setup`",
        )
    except Exception as exc:
        r.line(FAIL, "Speech recognition model", str(exc))
    if config.emotion.enabled:  # optional: without it she only hears the words, not the tone
        from octavia.emotion.model import EmotionModel

        where = config.resolve_path(config.emotion.model_dir)
        present = EmotionModel(where).files_present()
        r.line(
            OK if present else WARN,
            "Speech emotion model",
            str(where) if present else "missing (voice tone off); run `uv run octavia setup`",
        )

    print("\nAudio")
    await _check_audio(config, r)

    print("\nKeys (.env)")
    _check_keys(config, r)

    print("\nVoice (Fish Audio)")
    fish = config.tts.fish_audio
    key = (current_env(fish.api_key_env) or "").strip()  # checked with the other keys above
    voice = fish.reference_id or (current_env(fish.reference_id_env) or "").strip()
    r.line(OK if voice else WARN, "voice", voice or "none chosen; the model's default voice will be used")
    r.line(OK, "model", fish.model)
    if config.tts.provider in ("local", "local_only"):
        print("\nVoice (local, Qwen3-TTS)")
        local = config.tts.local
        found = True
        for what, path in (("python", local.python), ("engine", local.engine_dir), ("model", local.model_dir),
                           ("voice clip", str(config.resolve_path(local.voice))),
                           ("voice words", str(config.resolve_path(local.voice_text)))):  # fmt: skip
            here = bool(path) and Path(path).exists()
            found = found and here
            r.line(OK if here else FAIL, what, path or "not set (tts.local)")
        if live and found:
            await _live_tts(config, r)
    elif live and key:
        await _live_tts(config, r)

    print("\nBrain (Claude)")
    await _check_claude(config, r, live)

    print("\nHotkeys")
    if sys.platform == "win32" and config.hotkeys.enabled:
        try:
            from octavia.platform.windows import parse_hotkey

            for name in ("talk", "stop", "mute", "chat"):
                combo = getattr(config.hotkeys, name)
                if combo:
                    parse_hotkey(combo)
                    r.line(OK, name, combo)
        except Exception as exc:
            r.line(FAIL, "hotkeys", str(exc))
    else:
        r.line(OK, "hotkeys", "disabled")

    print("\nOctavia Futures")
    _check_futures(config, r)

    print("\nDiscord bridge")
    await _check_discord(config, r, live)

    print()
    if r.failures:
        print(f"{r.failures} problem(s) found.")
        return 1
    print("Everything looks ready. Start with: uv run octavia")
    return 0


def _check_keys(config: AppConfig, r: _Report) -> None:
    """Every key Octavia reads (``octavia.keys``): which file, set or not, and what's wrong. Never a value. A key
    that's simply not there is fine: most are optional (``octavia env`` lists them all)."""
    from octavia import keys

    file = keys.env_file()
    r.line(OK if file is not None and file.is_file() else WARN, ".env", str(file) if file else "none loaded")
    connected = []
    for finding in keys.check(config):
        if finding.status == "ok":
            connected.append(finding.name)
        elif finding.status == "problem":
            r.line(FAIL, finding.name, finding.detail)
        elif finding.status == "unknown":
            r.line(WARN, finding.name, finding.detail)
    r.line(OK, "set", ", ".join(connected) or "none")


def _check_futures(config: AppConfig, r: _Report) -> None:
    """Her evidence-based model: off, or its database opens at the right schema version (without creating one)."""
    settings = config.futures
    if not settings.enabled:
        r.line(OK, "futures", "off (futures.enabled)")
        return
    path = config.resolve_path(settings.database)
    if not path.exists():
        r.line(OK, "database", f"{path} (made at the first start)")
        return
    try:
        from octavia.futures.store import SCHEMA_VERSION, FuturesStore

        store = FuturesStore(path)
    except Exception as exc:
        r.line(WARN, "database", f"can't be used ({exc}); Octavia runs without Futures until it's fixed or removed")
        return
    try:
        events = store.one("SELECT COUNT(*) AS n FROM events")
        r.line(OK, "database", f"{path}, schema v{store.schema_version} of {SCHEMA_VERSION}, {events['n']} events")
    finally:
        store.close()
    r.line(OK, "speaking up on her own", "on" if settings.proactive else "off")


async def _check_discord(config: AppConfig, r: _Report, live: bool) -> None:
    """Texting her from the phone (docs/discord-bridge.md): on or off, the token there (never shown), an owner set.
    Only ``--live`` logs the bot in, to see that Discord takes the token and lets it read DMs."""
    from octavia import keys
    from octavia.bridge.discord_bridge import owner_of

    settings = config.discord_bridge
    token = keys.is_set("DISCORD_BOT_TOKEN")
    owner = owner_of(config)
    if not settings.enabled:
        r.line(OK, "bridge", "off (discord_bridge.enabled)" + ("; DISCORD_BOT_TOKEN is set" if token else ""))
        return
    r.line(OK, "bridge", f"on; announcements texted: {settings.notify}")
    r.line(OK if token else FAIL, "token", "DISCORD_BOT_TOKEN set" if token else "DISCORD_BOT_TOKEN missing in .env")
    r.line(
        OK if owner else FAIL,
        "owner",
        f"user {owner}" if owner else "none: set discord_bridge.owner_id (or DISCORD_OWNER_ID); it won't start without",
    )
    if not live or not token:
        return
    try:
        import discord

        client = discord.Client(intents=discord.Intents.none())
        try:
            await asyncio.wait_for(client.login(keys.value("DISCORD_BOT_TOKEN")), 20)
            flags = (await client.application_info()).flags
            r.line(OK, "live login", f"signed in as {client.user}")
            reads = flags.gateway_message_content or flags.gateway_message_content_limited
            r.line(
                OK if reads else FAIL,
                "Message Content intent",
                "on"
                if reads
                else "off: turn it on (Developer Portal, your application, Bot, Privileged Gateway Intents)",
            )
        finally:
            await client.close()
    except Exception as exc:  # never the exception's text: only what kind of failure it was
        refused = type(exc).__name__ == "LoginFailure"
        r.line(FAIL, "live login", "Discord refused the token" if refused else type(exc).__name__)


async def _check_audio(config: AppConfig, r: _Report) -> None:
    try:
        from octavia.audio.devices import choose_device

        mic = choose_device(config.audio.input_device, "input", config.audio.host_api)
        r.line(OK, "microphone", f"{mic.name} ({mic.host_api})")
        spk = choose_device(config.audio.output_device, "output", config.audio.host_api)
        r.line(OK, "speaker", f"{spk.name} ({spk.host_api})")
    except Exception as exc:
        r.line(FAIL, "devices", str(exc))
        return
    try:
        from octavia.audio.microphone import MicrophoneSource

        source = MicrophoneSource(config.audio.input_device, config.audio.host_api)
        await source.start()
        try:
            peak = await source.measure_level(1.0)
        finally:
            await source.close()
        if peak <= 2 / 32768:
            r.line(FAIL, "mic level", "digital silence: the mic is muted, at zero gain, or not the one you use")
        elif peak < 0.002:
            r.line(WARN, "mic level", f"very quiet (peak {peak:.4f}); speak during the test or raise the gain")
        else:
            r.line(OK, "mic level", f"peak {peak:.3f}")
    except TimeoutError:
        r.line(
            FAIL,
            "mic level",
            "the device opened but sent no audio for 3 s (idle, disconnected, or its app is closed). "
            "Pick another with audio.input_device; `octavia devices` lists them.",
        )
    except Exception as exc:
        r.line(FAIL, "microphone", str(exc) or type(exc).__name__)


async def _live_tts(config: AppConfig, r: _Report) -> None:
    from octavia.app import create_tts

    try:
        tts = create_tts(config)
        await tts.start()
        if (wait_ready := getattr(tts, "wait_ready", None)) is not None and not await wait_ready():
            await tts.close()
            raise RuntimeError(getattr(tts, "start_error", "") or "the voice didn't finish loading")
        started = time.perf_counter()
        first = None
        total = 0
        async for chunk in tts.synthesize("Systems check. All good."):
            first = first or time.perf_counter() - started
            total += len(chunk)
        await tts.close()
        seconds = total / tts.format.bytes_per_second
        r.line(
            OK if total else FAIL, "live synthesis", f"first audio after {first or 0:.2f}s, {seconds:.1f}s of speech"
        )
    except Exception as exc:
        r.line(FAIL, "live synthesis", str(exc))


async def _check_claude(config: AppConfig, r: _Report, live: bool) -> None:
    from octavia.brain.claude_auth import auth_status

    cfg = config.brain.claude
    cli = config.claude_cli_path() or _bundled_cli()
    r.line(OK if cli and Path(cli).exists() else FAIL, "Claude Code CLI", str(cli))
    if cfg.auth == "subscription":
        for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            if os.environ.get(var):
                r.line(WARN, var, "is set; it will be ignored so your subscription is used")
        status = auth_status(config)
        if status is None:
            r.line(WARN, "Claude login", "couldn't query the CLI")
        elif status.get("loggedIn"):
            r.line(OK, "Claude login", f"{status.get('authMethod', 'signed in')} ({status.get('configDirectory', '')})")
        else:
            r.line(FAIL, "Claude login", "not signed in. Run: uv run octavia login")
    else:
        r.line(OK if os.environ.get("ANTHROPIC_API_KEY") else FAIL, "ANTHROPIC_API_KEY", "api_key mode")
    r.line(OK, "model", f"{cfg.model} (effort {cfg.effort})")
    if not live:
        return
    from octavia.brain.base import ResponseDone, TextDelta, UserTurn
    from octavia.brain.claude import ClaudeBrain
    from octavia.core.events import EventBus
    from octavia.tools.base import ToolContext
    from octavia.tools.executor import ToolExecutor
    from octavia.tools.permissions import PermissionPolicy
    from octavia.tools.registry import ToolRegistry

    events = EventBus()
    executor = ToolExecutor(ToolRegistry(), PermissionPolicy(config.permissions), ToolContext(config, events), events)

    async def prompt() -> str:
        return "You are a connectivity check. Reply with exactly: ok"

    work = config.resolve_path(cfg.work_dir) / "doctor"
    config_dir = config.resolve_path(cfg.config_dir) if cfg.config_dir else None
    brain = ClaudeBrain(
        cfg,
        [],
        executor,
        prompt,
        work_dir=work,
        events=events,
        config_dir=config_dir,
        cli_path=config.claude_cli_path(),
    )
    started = time.perf_counter()
    try:
        await brain.start()
        connected = time.perf_counter() - started
        first = None
        done = None
        t0 = time.perf_counter()
        async for ev in brain.respond(UserTurn(text="Say ok.")):
            if isinstance(ev, TextDelta) and first is None:
                first = time.perf_counter() - t0
            if isinstance(ev, ResponseDone):
                done = ev
        if done and not done.error:
            r.line(
                OK, "live request", f"connected in {connected:.1f}s, first token {first or 0:.2f}s, reply {done.text!r}"
            )
        else:
            r.line(FAIL, "live request", done.error if done else "no response")
    except Exception as exc:
        r.line(FAIL, "live request", str(exc))
    finally:
        await brain.close()


def _bundled_cli() -> str | None:
    from octavia.brain.claude import bundled_cli_path

    path = bundled_cli_path()
    return str(path) if path else None


async def listen_test(config: AppConfig, seconds: float) -> int:
    """Print wake detections, speech start/end and transcripts, to tune thresholds."""
    from octavia.app import create_stt, create_vad, create_wake
    from octavia.audio.microphone import MicrophoneSource
    from octavia.vad.endpointer import Endpointer, EndpointerSettings, EndpointKind

    vad, stt, wake = create_vad(config), create_stt(config), create_wake(config)
    for component in (vad, stt, wake):
        await asyncio.to_thread(component.load)
    v = config.vad
    endpointer = Endpointer(
        EndpointerSettings(
            threshold=v.threshold,
            neg_threshold=v.neg_threshold,
            min_speech_ms=v.min_speech_ms,
            end_silence_ms=v.end_silence_ms,
            pre_roll_ms=v.pre_roll_ms,
            max_utterance_s=v.max_utterance_s,
            no_speech_timeout_s=3600,
        )
    )
    source = MicrophoneSource(config.audio.input_device, config.audio.host_api)
    await source.start()
    print(f"Listening for {seconds:.0f}s on {source.choice.name if source.choice else 'default mic'}.")
    print(f'Say "{config.assistant.wake_phrase}" and some sentences. Ctrl+C to stop.\n')
    deadline = time.monotonic() + seconds
    peak = 0.0
    last_meter = 0.0
    try:
        async for frame in source.frames():
            if time.monotonic() > deadline:
                break
            detection = wake.process(frame)
            if detection is not None:
                score = f" (score {detection.score:.2f})" if detection.score is not None else ""
                print(f"\n[WAKE] detected{score}")
            prob = vad.probability(frame)
            peak = max(peak, float(abs(frame).max()))
            event = endpointer.process(frame, prob)
            if event is not None and event.kind is EndpointKind.SPEECH_STARTED:
                print("\n[VAD] speech started")
            elif event is not None and event.kind is EndpointKind.UTTERANCE:
                print(f"[VAD] speech ended ({event.duration_s:.1f}s), transcribing...")
                result = await stt.transcribe(event.audio)
                print(f'[STT] "{result.text}"  ({result.latency_s:.2f}s)')
                endpointer.reset(no_speech_timeout_s=3600)
                vad.reset()
            now = time.monotonic()
            if now - last_meter > 0.25:
                bar = "#" * int(min(1.0, prob) * 20)
                sys.stdout.write(f"\r  speech {prob:4.2f} [{bar:<20}] level {peak:5.3f}   ")
                sys.stdout.flush()
                last_meter, peak = now, 0.0
    finally:
        await source.close()
        print()
    return 0
