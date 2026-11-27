"""Settings picked up while she runs: no restart, no .env or YAML edit needed to try something.

``LiveSettings`` looks at her settings files about once a second (``config/local.yaml``, ``data/settings.json``,
``.env`` and ``data/voice_volume.json``). When one changes it builds the config again from the files, compares it
with the one she runs with, and for each setting that changed:

* ``LIVE``: read where it's used, from the config object she runs with, so the new value is simply put there.
* a hook the app registered (``hook``): the value is put there too, then the hook hands it to whatever copied it
  at startup (the gate that counts her remarks, the hotkeys, the log). A hook can say it couldn't (``False``): the
  old value goes back and the setting waits for a restart.
* anything else waits for a restart: a model loaded, a device opened, a feature started (or not) at startup.

What waits is written to ``data/settings_status.json`` for her settings window, which shows it and offers a restart:
it asks for one by writing ``data/settings_restart.json`` with her process id. The window's few actions (snoozing her
from the Quiet hours section) arrive the same way, in ``data/settings_action.json``, and run what the app registered
with ``action``; what ``state`` providers report (a snooze running, and until when) goes into the status. A file that doesn't load (a typo in
``local.yaml``) changes nothing: she keeps running with what she has and the status says why.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)

STATUS_FILE = Path("data") / "settings_status.json"
RESTART_FILE = Path("data") / "settings_restart.json"
ACTION_FILE = Path("data") / "settings_action.json"
ACTION_MAX_AGE_S = 30.0  # an action asked for longer ago than this (she was busy starting) is dropped
VOICE_VOLUME = "audio.output_volume"

# Read from the shared config at the moment of use: putting the new value in the config is all it takes.
LIVE = frozenset(
    {
        "assistant.user_name",  # the brain's prompt: from her next conversation
        "conversation.follow_up_window_s",
        "conversation.wake_ack",
        "conversation.confirm_listen_s",
        "conversation.narrate_work",
        "conversation.turn_stall_s",
        "conversation.acknowledge_early_ms",
        "conversation.early_start",
        "conversation.opener_wait_ms",
        "audio.earcons",
        "audio.working_sound",
        "audio.headphones",
        "wake.cooldown_s",
        "vad.no_speech_timeout_s",
        "barge_in.enabled",
        "barge_in.wake_phrase",
        "barge_in.voice",
        "brain.claude.model",  # these four when the brain next connects: her next conversation
        "brain.claude.fallback_model",
        "brain.claude.effort",
        "brain.claude.max_turns",
        "fast_chat.max_words",
        "fast_chat.timeout_s",
        "routing.fast_path",
        "permissions.submit_without_asking",
        "permissions.trusted_apps",
        "permissions.always_ask_before_sending",
        "ticktick.remind_before_min",
        "proactive.level",  # her overnight work reads these each night (``ProactiveService.settings``)
        "proactive.max_items",
        "proactive.night",
        "proactive.away_min",
        "proactive.look_ahead_days",
        "proactive.rank",
        "proactive.rank_model",
        "proactive.rank_effort",
        "proactive.projects",
        "proactive.steer_min_decisions",
        "proactive.steer_agreement",
        "emotion.note",
        "avatar.hide_after_s",
        "avatar.click_to_talk",
        "avatar.point",
        "avatar.rainboom",
        "avatar.victory_lap",
        "avatar.subtitles",  # read at each sentence she speaks
        "visuals.enabled",  # the panel tools look each time; the panel app reads its own few at start
        "visuals.stay_s",
        "visuals.idle_quit_s",
        "visuals.marks",  # the mark tools look each time
        "visuals.marks_stay_s",
        "visuals.web_images",  # the picture and page tools look each time
        "visuals.web_pages",
        "visuals.image_stay_s",
        "visuals.page_stay_s",
        "visuals.safe_search",
        "visuals.explainer_orb_points",  # read at each step an explainer points at
        "futures.experiments",
        "futures.retention_days",
        "self_scoring.enabled",  # read at every reply and every look for her next experiment on herself
        "self_scoring.experiments",
        "self_scoring.keep_habits",
        "self_scoring.lessons",
        "self_scoring.strategies",
        "inner_life.reflect_at",
        "inner_life.bring_up",
        "inner_life.web",
        "inner_life.self_notes",  # read at each prompt, each night and each note
        "inner_life.looking_back",
        "inner_life.dreams",
        "routines.offers",
        "study.nudges",  # read at each look for something to bring up
        "study.exam_days",
        "study.min_reviews",
        "self_improvement.auto_build",
        "self_improvement.auto_install",
        "self_improvement.max_builds_per_day",
        "self_improvement.announce",
        "discord_bridge.notify",
        "discord_bridge.away_after_min",
        "discord_bridge.speak_replies",
        "discord_bridge.mute_pc_on_call",
        "discord_bridge.call_wake_word",
        "deep_think.model",  # read as each deep think starts
        "deep_think.effort",
        "deep_think.max_per_day",
        "deep_think.max_second_opinions",
        "deep_think.web",
        "playbooks.enabled",  # read as each task ends
        "playbooks.model",
        "playbooks.effort",
        "playbooks.min_steps",
        "skills.enabled",  # read as each task starts and ends
        "skills.notice_repeats",
        "skills.min_tasks",
        "skills.min_steps",
        "gods_eye.enabled",  # read each time she shows a place
        "gods_eye.folder",
        "gods_eye.address",
        "fast_chat.context_turns",  # read at each quick answer
        "tools.search_engine",  # read at each web search
        "local_brain.route_max_words",  # the router and the shadow read these at each turn or tick
        "local_brain.route_min_confidence",
        "local_brain.answer_timeout_s",
        "local_brain.fallback_timeout_s",
        "local_brain.claude_retry_s",
        "local_brain.game_unload_s",
        "local_brain.game_reload_s",
        "addressee.threshold",  # read as each thing overheard is judged
        "addressee.repeat_window_s",
    }
)

# Applied by a hook the app registers (``App._live_hooks``); without one (what it updates isn't running), the
# value is only put in the config, which is all that's left to change.
HOOKED = frozenset(
    {
        VOICE_VOLUME,
        "conversation.session_timeout_s",
        "conversation.max_context_tokens",
        "conversation.acknowledge",
        "conversation.quick_replies",
        "conversation.backchannel",
        "audio.earcon_volume",
        "audio.sound_pack",
        "audio.directional",  # the speaker's panner (``audio/spatial.py``)
        "audio.directional_width",
        "wake.second_chance",
        "wake.second_chance_max_s",
        "vad.end_silence_ms",
        "vad.max_utterance_s",
        "vad.early_transcript_ms",
        "tts.fish_audio.model",  # these with her preset phrases made again in the new voice
        "tts.fish_audio.latency",
        "tts.fish_audio.speed",
        "tts.fish_audio.top_p",
        "tts.fish_audio.temperature",
        "tts.fish_audio.reference_id",
        "tts.fallback_after_s",
        "tts.provider",  # who answers and which voice speaks (``core/engines.py``)
        "tts.voice_effect",
        "brain.answers",
        "permissions.confirm_timeout_s",
        "hotkeys.enabled",
        "hotkeys.talk",
        "hotkeys.stop",
        "hotkeys.mute",
        "hotkeys.chat",
        "scheduler.quiet_hours",
        "ticktick.reminders",
        "ticktick.morning_summary",
        "futures.proactive",
        "futures.max_per_hour",
        "futures.max_per_day",
        "futures.offers",
        "futures.offers_per_day",
        "futures.quiet_hours",
        "futures.keep_request_words",
        "futures.week_free_hours_weekday",  # read each time the week is sampled
        "futures.week_free_hours_weekend",
        "usage_budget.enabled",
        "usage_budget.daily_share",
        "usage_budget.max_five_hour",
        "usage_budget.max_weekly",
        "self_improvement.builder_model",
        "self_improvement.builder_effort",
        "self_improvement.nightly_at",
        "self_improvement.morning_report",
        "system.new_code_quiet_s",
        "system.explorer_menu",
        "logging.level",
        "logging.show_replies",
        "screen_history.retention_days",
        "screen_history.ocr",
        "screen_history.exclude_sites",
        "screen_history.idle_after_s",
    }
)

Hook = Callable[[Any], "bool | None | Awaitable[bool | None]"]


def leaves(model: BaseModel, prefix: str = "") -> Iterator[tuple[str, BaseModel, str, Any]]:
    """Every setting in a config: (dotted key, the section holding it, its name there, its value)."""
    for name in type(model).model_fields:
        value = getattr(model, name)
        key = f"{prefix}{name}"
        if isinstance(value, BaseModel):
            yield from leaves(value, key + ".")
        else:
            yield key, model, name, value


def differences(running: BaseModel, fresh: BaseModel) -> dict[str, Any]:
    """``{key: new value}`` for each setting ``fresh`` gives differently from ``running``."""
    new = {key: value for key, _, _, value in leaves(fresh)}
    return {key: new[key] for key, _, _, value in leaves(running) if key in new and new[key] != value}


def _put(config: BaseModel, key: str, value: Any) -> None:
    """Put an already-validated value in the running config, as is: checking it field by field could trip over a
    rule across fields ("nightly_at and morning_report can't be the same") halfway through a change of both."""
    *parents, name = key.split(".")
    node: Any = config
    for part in parents:
        node = getattr(node, part)
    node.__dict__[name] = copy.deepcopy(value)


def _get(config: BaseModel, key: str) -> Any:
    node: Any = config
    for part in key.split("."):
        node = getattr(node, part)
    return node


def pid_alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:
        return False


def read_status(root: Path) -> dict[str, Any] | None:
    """What the running her last wrote: None when she isn't running (no file, or one from a process that's gone)."""
    try:
        data = json.loads((root / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("pid"), int) or not pid_alive(data["pid"]):
        return None
    return data


def ask_restart(root: Path, pid: int) -> None:
    """Ask the running her (``pid``) to restart. Raises OSError when it can't be written."""
    _write_json(root / RESTART_FILE, {"pid": pid, "at": time.time()})


def ask_action(root: Path, pid: int, action: str, **args: Any) -> None:
    """Ask the running her (``pid``) to do one of the settings window's actions. Raises OSError when it can't."""
    _write_json(root / ACTION_FILE, {"pid": pid, "action": action, "args": args, "at": time.time()})


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    replace(tmp, path)


def replace(tmp: Path, path: Path, tries: int = 6) -> None:
    """``tmp`` over ``path``, tried again for a moment: on Windows that fails while the other process (her, or her
    settings window) happens to be reading ``path``."""
    for attempt in range(tries):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


class LiveSettings:
    def __init__(
        self,
        config: Any,
        *,
        restart: Callable[[], Awaitable[Any]] | None = None,
        can_restart: Callable[[], bool] = lambda: False,
        every_s: float = 1.0,
    ) -> None:
        from octavia import keys
        from octavia.audio.voice_volume import SETTINGS_FILE

        self.config = config
        self.root: Path = config.root
        self._local = config.sources[1] if len(config.sources) > 1 else None
        self._restart = restart
        self._can_restart = can_restart
        self.every_s = every_s
        self.hooks: dict[str, Hook] = {}
        self.actions: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {}
        self.states: dict[str, Callable[[], Any]] = {}
        self._stated: dict[str, Any] = {}  # what the states said in the status last written
        self.voice_volume: Any = None  # the VoiceVolume she speaks with, when she speaks
        self._voice_file = config.resolve_path(SETTINGS_FILE)
        # Keys whose integration is set up at startup: what they were then, to tell whether one waits for a restart.
        self._env_started = {k.name: keys.value(k.name) for k in keys.KEYS if k.restart}
        self.pending: list[str] = []
        self.error: str | None = None
        # Choices in her settings window left out because they don't fit (``AppConfig.dropped_settings``).
        self.dropped: list[str] = list(getattr(config, "dropped_settings", []) or [])
        self._seen: dict[Path, tuple[float, int] | None] = {}
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def hook(self, key: str, fn: Hook) -> None:
        self.hooks[key] = fn

    def action(self, name: str, fn: Callable[[dict[str, Any]], Awaitable[Any]]) -> None:
        """What the window's ``name`` action does (it gets the action's arguments)."""
        self.actions[name] = fn

    def state(self, name: str, fn: Callable[[], Any]) -> None:
        """Something the window shows of her (``fn()`` must be JSON), put in the status under ``name``."""
        self.states[name] = fn

    def _read_states(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, fn in self.states.items():
            try:
                out[name] = fn()
            except Exception:
                log.debug("Couldn't read %s for the settings status", name, exc_info=True)
        return out

    # ---- files

    def _files(self) -> list[Path]:
        from octavia import config as config_module
        from octavia.core.userdir import state_root

        local = Path(self._local) if self._local else state_root(self.root) / "config" / "local.yaml"
        from octavia.core.userdir import state_root

        env = config_module.dotenv_file() or state_root(self.root) / ".env"
        from octavia import user_settings

        return [local, self.root / user_settings.FILE, env, self._voice_file]

    @staticmethod
    def _stamp(path: Path) -> tuple[float, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return st.st_mtime, st.st_size

    def _changed(self) -> bool:
        changed = False
        for path in self._files():
            stamp = self._stamp(path)
            if self._seen.get(path, stamp) != stamp:
                changed = True
            self._seen[path] = stamp
        return changed

    # ---- running

    def start(self) -> None:
        self._changed()
        self._write_status()
        self._task = asyncio.create_task(self._watch(), name="live-settings")

    async def _caught_up(self) -> None:
        """What changed while she was starting (the files were read at the beginning), applied once she's up."""
        try:
            await self.reload()
        except Exception:
            log.warning("Couldn't pick up a settings change", exc_info=True)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        with contextlib.suppress(OSError, ValueError):
            status = self.root / STATUS_FILE
            if json.loads(status.read_text(encoding="utf-8")).get("pid") == os.getpid():
                status.unlink()

    async def _watch(self) -> None:
        await self._caught_up()
        while True:
            await asyncio.sleep(self.every_s)
            try:
                await self._restart_if_asked()
                await self._act_if_asked()
                if self._changed():
                    await self.reload()
                elif self.states and self._read_states() != self._stated:
                    self._write_status()  # a snooze ran out, or the tray ended it
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Couldn't pick up a settings change", exc_info=True)

    async def _restart_if_asked(self) -> None:
        path = self.root / RESTART_FILE
        if not path.exists():
            return
        try:
            asked = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            asked = {}
        pid = asked.get("pid") if isinstance(asked, dict) else None
        if pid != os.getpid():
            if not isinstance(pid, int) or pid <= 0 or not pid_alive(pid):
                with contextlib.suppress(OSError):
                    path.unlink()  # for a run that's gone, or not a request at all
            return
        with contextlib.suppress(OSError):
            path.unlink()
        if self._restart is None:
            return
        log.info("Restarting, as asked in the settings window")
        await self._restart()

    async def _act_if_asked(self) -> None:
        path = self.root / ACTION_FILE
        if not path.exists():
            return
        try:
            asked = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            asked = {}
        if not isinstance(asked, dict):
            asked = {}
        pid = asked.get("pid")
        if pid != os.getpid():
            if not isinstance(pid, int) or pid <= 0 or not pid_alive(pid):
                with contextlib.suppress(OSError):
                    path.unlink()
            return
        with contextlib.suppress(OSError):
            path.unlink()
        name, args, at = asked.get("action"), asked.get("args"), asked.get("at")
        fn = self.actions.get(name) if isinstance(name, str) else None
        if fn is None or not isinstance(at, (int, float)) or time.time() - at > ACTION_MAX_AGE_S:
            log.info("Dropped a settings-window action: %s", name)
            return
        log.info("Settings window asked for %s", name)
        try:
            await fn(args if isinstance(args, dict) else {})
        except Exception:
            log.warning("Couldn't do %s for the settings window", name, exc_info=True)
        self._write_status()

    async def reload(self) -> list[str]:
        """Build the config from the files again and apply what changed. Returns the keys applied live."""
        from octavia.config import build_config

        async with self._lock:
            try:
                overrides = getattr(self.config, "overrides", None) or None
                fresh = await asyncio.to_thread(build_config, self._local, overrides, self.root)
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                log.warning("Settings changed but don't load, so nothing was applied: %s", exc)
                self._write_status()
                return []
            self.error = None
            self.dropped = list(getattr(fresh, "dropped_settings", []) or [])
            applied = [key for key, value in differences(self.config, fresh).items() if await self._apply(key, value)]
            if await self._voice_volume():
                applied.append(VOICE_VOLUME)
            self.pending = sorted(differences(self.config, fresh)) + [
                f"env:{name}" for name, was in self._env_started.items() if _env_value(name) != was
            ]
            self._write_status()
        if applied:
            log.info("Settings applied live: %s", ", ".join(applied))
        if self.pending:
            log.info("Settings waiting for a restart: %s", ", ".join(self.pending))
        return applied

    async def _apply(self, key: str, value: Any) -> bool:
        hook = self.hooks.get(key)
        if key not in LIVE and key not in HOOKED:
            return False
        old = _get(self.config, key)
        _put(self.config, key, value)
        if hook is None:
            return True
        try:
            result = hook(value)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            log.warning("Couldn't apply %s live; it waits for a restart", key, exc_info=True)
            result = False
        if result is False:
            _put(self.config, key, old)
            return False
        return True

    async def _voice_volume(self) -> bool:
        """Her voice volume, when the settings window (or anything but her) changed the file she keeps it in."""
        if self.voice_volume is None:
            return False
        from octavia.audio.voice_volume import VoiceVolume

        gain = VoiceVolume.load(self._voice_file, self.config.audio.output_volume).gain
        if abs(gain - self.voice_volume.gain) < 1e-6:
            return False
        self.voice_volume.set(gain)
        return True

    def _write_status(self) -> None:
        try:
            can_restart = bool(self._can_restart())
        except Exception:
            can_restart = False
        data = {
            "pid": os.getpid(),
            "pending": self.pending,
            "can_restart": can_restart and self._restart is not None,
            "error": self.error,
            "dropped": self.dropped,
            "at": time.time(),
        }
        self._stated = self._read_states()
        data.update(self._stated)
        try:
            _write_json(self.root / STATUS_FILE, data)
        except OSError:
            log.debug("Couldn't write %s", STATUS_FILE, exc_info=True)


def _env_value(name: str) -> str:
    from octavia import keys

    return keys.value(name)
