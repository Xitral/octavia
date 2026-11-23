"""The guided setup window (``octavia welcome --window``): "forming OCTAVIA" in its own look, orb and all.

The window is a small app on the avatar's Electron (``avatar/welcome``, ``avatar/renderer/welcome``); this process
is its other half (``Session``): it sends the page what it needs once (the devices, the voice presets, this PC) and
answers as the user goes: a Fish key checked, a voice preview made with that key, a town looked up, and at the end
the choices saved (``welcome.write``, the same as the console wizard) and Claude signed in. Without the avatar's
Electron, ``octavia welcome`` runs the console wizard instead.
"""

from __future__ import annotations

import base64
import json
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from octavia import welcome
from octavia.tts import presets

APP = "avatar/welcome"
READY_TIMEOUT_S = 30.0
WAKE_TIMEOUT_S = 300.0
LAUNCHED = 0  # exit codes of the window: it saved and started OCTAVIA...
SAVED = 3  # ...it saved, OCTAVIA wasn't started from it...
NOTHING = 1  # ...nothing was saved


def launch(root: Path) -> str | None:
    """Starts OCTAVIA on its own (windowless, as the Start menu does); an error message, or None."""
    scripts = root / ".venv" / "Scripts"
    launcher = scripts / "octaviaw.exe"
    command = [str(launcher)] if launcher.exists() else [str(scripts / "pythonw.exe"), "-m", "octavia", "run"]
    if not Path(command[0]).exists():
        return "its program wasn't found. Start it from the Start menu: OCTAVIA."
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(command, cwd=root, creationflags=flags, close_fds=True)  # noqa: S603
    except OSError as exc:
        return f"it couldn't be started ({exc})."
    return None


def app_command(root: Path, electron: str, app_config: Path) -> list[str] | None:
    from octavia.avatar.bridge import electron_path

    exe = electron_path(root, electron)
    if not exe.is_file() or not (root / APP / "main.js").is_file():
        return None
    return [str(exe), str(root / APP), "--config", str(app_config)]


class Session:
    """Answers the page. ``send`` goes to the window; ``services`` are welcome.py's (replaceable in tests)."""

    def __init__(self, root: Path, send: Any, services: welcome.Services | None = None) -> None:
        self.root = root
        self.send = send
        self.services = services or welcome.default_services()
        self.choices: welcome.Choices | None = None
        self.done = False
        self.launched = False  # "Start OCTAVIA" started it (the installer then doesn't)
        self._raw: dict[tuple[str, str], bytes] = {}  # (voice, line): the words as said, before any effect

    def model(self) -> dict[str, Any]:
        try:
            devices = self.services.devices()
        except Exception:
            devices = []
        try:
            hw = self.services.hardware()
        except Exception:
            hw = {}
        if hw.get("ram_gb") and hw["ram_gb"] < 8:
            hw["note"] = "Under 8 GB of memory: turn off screen history if it feels slow."
        return {
            "type": "model",
            "devices": {
                "inputs": welcome._device_names(devices, "input")[:30],
                "outputs": welcome._device_names(devices, "output")[:30],
            },
            "presets": [{"name": p.name, "voice_id": p.voice_id, "about": p.about} for p in presets.PRESETS],
            # Another model than Claude (welcome.OTHER_MODELS): name, base URL ("" = the user gives it), key variable,
            # a default model name.
            "models": [
                {"name": name, "base_url": url, "key_env": key_env, "model": model}
                for name, url, key_env, model in welcome.OTHER_MODELS
            ],
            "hardware": hw,
        }

    def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "check-key":
            args = (str(msg.get("key", "")), str(msg.get("effect", "console")))
            threading.Thread(target=self._check_key, args=args, daemon=True).start()
        elif kind == "preview":
            args = (str(msg.get("key", "")), str(msg.get("voice_id", "")), str(msg.get("effect", "console")))
            threading.Thread(target=self._preview, args=args, daemon=True).start()
        elif kind == "open":
            from octavia.platform import welcome_guide

            welcome_guide.open_page(str(msg.get("target", "")), str(msg.get("canvas_url", "")))
        elif kind == "check":
            threading.Thread(target=self._check, args=(msg,), daemon=True).start()
        elif kind == "say":
            threading.Thread(target=self._say, args=(msg,), daemon=True).start()
        elif kind == "stuck":
            threading.Thread(target=self._stuck, args=(msg,), daemon=True).start()
        elif kind == "geocode":
            threading.Thread(target=self._geocode, args=(str(msg.get("place", "")),), daemon=True).start()
        elif kind == "save":
            self._save(msg.get("choices") or {})
        elif kind == "sign-in":
            threading.Thread(target=self._sign_in, daemon=True).start()
        elif kind == "done":
            threading.Thread(target=self._wake, daemon=True).start()

    # ---- answers
    def _wake(self) -> None:
        """Starts OCTAVIA and waits until it says it's up (``data/ready.json``), then the window closes."""
        started = time.time()
        error = launch(self.root)
        if error is None:
            ready = self.root / "data" / "ready.json"
            while time.time() - started < WAKE_TIMEOUT_S:
                try:
                    if ready.stat().st_mtime >= started - 1:
                        self.launched = True
                        self.send({"type": "awake", "ok": True})
                        time.sleep(3.0)  # long enough to read it
                        break
                except OSError:
                    pass
                time.sleep(0.5)
            else:
                error = "it's taking unusually long. It may still finish: look for its orb, or start it from the Start menu."
        if error is not None:
            self.send({"type": "awake", "ok": False, "message": error})
            time.sleep(6.0)
        self.done = True
        self.send({"type": "quit"})

    def _greeting(self, choices: welcome.Choices) -> None:
        """Its first words, in the chosen voice, for when it's awake (the page plays them then)."""
        key = choices.env.get("FISH_AUDIO_API_KEY", "")
        tts = choices.config.get("tts", {})
        voice = tts.get("fish_audio", {}).get("reference_id") or presets.DEFAULT.voice_id
        who = choices.config.get("assistant", {})
        if not key or presets.by_id(voice) is None:
            return
        text = presets.greeting(
            str(who.get("name", "")), str(who.get("user_name", "")), str(who.get("wake_phrase", ""))
        )
        try:
            audio = presets.with_effect(self._spoken(key, voice, text), str(tts.get("voice_effect", "console")))
        except presets.PreviewError:
            return
        self.send({"type": "greeting", "audio": base64.b64encode(audio).decode()})

    def _spoken(self, key: str, voice_id: str, text: str = presets.PREVIEW_LINE) -> bytes:
        """The raw words in a voice, made once per voice and line (the effect is put on afterwards, so changing
        it costs no new request)."""
        cached = self._raw.get((voice_id, text))
        if cached is None:
            cached = presets.preview(key, voice_id, text, fmt="pcm")
            self._raw[(voice_id, text)] = cached
        return cached

    # ---- the guided connections (welcome_guide.py)
    def _check(self, msg: dict[str, Any]) -> None:
        from octavia.platform import welcome_guide

        what = str(msg.get("what", ""))
        ok, said = welcome_guide.check(what, str(msg.get("value", "")), str(msg.get("extra", "")))
        self.send({"type": "checked", "what": what, "ok": ok, "message": said})

    def _say(self, msg: dict[str, Any]) -> None:
        """A guide's line in the chosen voice and effect (nothing without a Fish key: the page shows the words)."""
        key, voice = str(msg.get("key", "")), str(msg.get("voice_id", ""))
        text = str(msg.get("text", ""))[:300]
        if not key or not text or presets.by_id(voice) is None:
            return
        try:
            audio = presets.with_effect(self._spoken(key, voice, text), str(msg.get("effect", "console")))
        except presets.PreviewError:
            return
        self.send({"type": "said", "id": msg.get("id"), "audio": base64.b64encode(audio).decode()})

    def _stuck(self, msg: dict[str, Any]) -> None:
        from octavia.platform import welcome_guide

        keys = {k: str(v) for k, v in (msg.get("keys") or {}).items() if k in ALLOWED_ENV}
        answer = welcome_guide.stuck(
            self.root, str(msg.get("guide", "")), str(msg.get("step", "")), str(msg.get("question", "")), keys
        )
        self.send({"type": "answer", "text": answer})

    def _check_key(self, key: str, effect: str = "console") -> None:
        try:
            raw = self._spoken(key, presets.DEFAULT.voice_id)
        except presets.PreviewError as exc:
            self.send({"type": "key-checked", "ok": False, "message": f"That key didn't work: {exc}."})
            return
        self.send({"type": "key-checked", "ok": True})
        audio = presets.with_effect(raw, effect)
        self.send({"type": "preview", "voice_id": presets.DEFAULT.voice_id, "effect": effect,
                   "audio": base64.b64encode(audio).decode()})  # fmt: skip

    def _preview(self, key: str, voice_id: str, effect: str = "console") -> None:
        if presets.by_id(voice_id) is None:
            return
        try:
            raw = self._spoken(key, voice_id)
        except presets.PreviewError as exc:
            self.send({"type": "preview", "voice_id": voice_id, "message": f"Couldn't make the preview: {exc}."})
            return
        audio = presets.with_effect(raw, effect)
        self.send(
            {"type": "preview", "voice_id": voice_id, "effect": effect, "audio": base64.b64encode(audio).decode()}
        )

    def _geocode(self, place: str) -> None:
        if place.strip() and self.services.places is not None:
            matches = self.services.places(place)
            if matches:
                rows = [
                    {"label": lab, "latitude": round(lat, 4), "longitude": round(lon, 4)} for lab, lat, lon in matches
                ]
                self.send({"type": "geocoded", "found": True, **rows[0], "others": rows[1:]})
                return
        found = self.services.geocode(place) if place.strip() else None
        if found is None:
            self.send({"type": "geocoded", "found": False})
            return
        label, lat, lon = found
        self.send(
            {"type": "geocoded", "found": True, "label": label, "latitude": round(lat, 4), "longitude": round(lon, 4)}
        )

    def _save(self, raw: dict[str, Any]) -> None:
        config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
        env = {str(k): str(v) for k, v in (raw.get("env") or {}).items() if isinstance(v, str) and v.strip()}
        choices = welcome.Choices(config=_allowed(config), env={k: v for k, v in env.items() if k in ALLOWED_ENV})
        try:
            from octavia.config import AppConfig, deep_merge, file_values

            AppConfig.model_validate(deep_merge(file_values(self.root), choices.config))  # refuse what won't load
            welcome.write(self.root, choices)
        except Exception as exc:
            self.send({"type": "saved", "ok": False, "message": f"Couldn't save: {exc}"})
            return
        self.choices = choices
        notes = []
        if "FISH_AUDIO_API_KEY" not in choices.env:
            notes.append("No Fish Audio key yet: it speaks with Windows' voice until you add one in Settings.")
        if config.get("brain", {}).get("claude", {}).get("auth") == "api_key" and "ANTHROPIC_API_KEY" not in env:
            notes.append("Add your Anthropic API key in Settings before you talk to it.")
        notes.append("Everything here can be changed later: right-click its tray icon, Settings.")
        brain = config.get("brain", {})
        sign_in = brain.get("provider", "claude") == "claude" and brain.get("claude", {}).get("auth") == "subscription"
        self.send({"type": "saved", "ok": True, "notes": notes, "sign_in": sign_in})
        threading.Thread(target=self._greeting, args=(choices,), daemon=True).start()

    def _sign_in(self) -> None:
        from octavia.config import load_config

        try:
            code = self.services.login(load_config(root=self.root))
        except Exception as exc:
            self.send({"type": "signed-in", "ok": False, "message": f"Couldn't sign in: {exc}"})
            return
        self.send({"type": "signed-in", "ok": code == 0})


# What the page may set: these sections' keys only, and these .env names.
ALLOWED_CONFIG = {
    "brain": {"claude": {"auth"}, "openai_compatible": {"base_url", "model", "api_key_env"}},
    "audio": {"input_device", "output_device", "headphones"},
    "tts": {"provider", "fallback", "voice_effect", "fish_audio"},
    "assistant": {"name", "wake_phrase", "user_name", "home", "home_latitude", "home_longitude"},
    "discord_bridge": {"enabled"},
    "hive": {"share"},
}
ALLOWED_ENV = {
    "ANTHROPIC_API_KEY", "FISH_AUDIO_API_KEY", "DISCORD_BOT_TOKEN", "DISCORD_OWNER_ID", "TICKTICK_API_TOKEN",
    "CANVAS_BASE_URL", "CANVAS_API_TOKEN", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
} | {key_env for _, _, key_env, _ in welcome.OTHER_MODELS if key_env}  # fmt: skip


def _allowed(config: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for section, keys in ALLOWED_CONFIG.items():
        given = config.get(section)
        if not isinstance(given, dict):
            continue
        if isinstance(keys, dict):
            inner = {k: {kk: vv for kk, vv in (given.get(k) or {}).items() if kk in v} for k, v in keys.items()}
            inner = {k: v for k, v in inner.items() if v}
            if section == "brain" and given.get("provider") in ("claude", "openai_compatible"):
                inner["provider"] = given["provider"]
            if inner:
                out[section] = inner
            continue
        kept = {k: v for k, v in given.items() if k in keys}
        if section == "tts" and isinstance(kept.get("fish_audio"), dict):
            kept["fish_audio"] = {k: v for k, v in kept["fish_audio"].items() if k == "reference_id"}
        if kept:
            out[section] = kept
    return out


def run(root: Path, electron: str = "", hidden: bool = False) -> int | None:
    """The window, until it's closed: 0 when it saved, 1 when closed without; None when it can't open (no Electron),
    so the console wizard runs instead."""
    config_file = Path(tempfile.mkdtemp(prefix="octavia-welcome-")) / "welcome.json"
    icon = root / "assets" / "octavia.png"
    config_file.write_text(
        json.dumps({"title": "OCTAVIA", "icon": str(icon) if icon.is_file() else None, "hidden": hidden})
    )
    command = app_command(root, electron, config_file)
    if command is None:
        return None
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )  # fmt: skip
    lock = threading.Lock()

    def send(msg: dict[str, Any]) -> None:
        with lock:
            try:
                proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))  # type: ignore[union-attr]
                proc.stdin.flush()  # type: ignore[union-attr]
            except (OSError, ValueError):
                pass

    inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def read() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                inbox.put(msg)
        inbox.put(None)

    threading.Thread(target=read, daemon=True).start()
    session = Session(root, send)
    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        try:
            msg = inbox.get(timeout=max(0.1, deadline - time.monotonic()))
        except queue.Empty:
            proc.kill()
            return None
        if msg is None:
            return None
        if msg.get("type") == "ready":
            break
        if msg.get("type") == "already-open":
            return 1
    send(session.model())
    while True:
        msg = inbox.get()
        if msg is None or msg.get("type") == "closed":
            break
        session.handle(msg)
    proc.wait(timeout=10)
    if session.launched:
        return LAUNCHED
    return SAVED if session.choices is not None else NOTHING
