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
            "hardware": hw,
        }

    def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "check-key":
            threading.Thread(target=self._check_key, args=(str(msg.get("key", "")),), daemon=True).start()
        elif kind == "preview":
            args = (str(msg.get("key", "")), str(msg.get("voice_id", "")))
            threading.Thread(target=self._preview, args=args, daemon=True).start()
        elif kind == "geocode":
            threading.Thread(target=self._geocode, args=(str(msg.get("place", "")),), daemon=True).start()
        elif kind == "save":
            self._save(msg.get("choices") or {})
        elif kind == "sign-in":
            threading.Thread(target=self._sign_in, daemon=True).start()
        elif kind == "done":
            self.done = True

    # ---- answers
    def _check_key(self, key: str) -> None:
        try:
            audio = presets.preview(key, presets.DEFAULT.voice_id)
        except presets.PreviewError as exc:
            self.send({"type": "key-checked", "ok": False, "message": f"That key didn't work: {exc}."})
            return
        self.send({"type": "key-checked", "ok": True})
        self.send({"type": "preview", "voice_id": presets.DEFAULT.voice_id, "audio": base64.b64encode(audio).decode()})

    def _preview(self, key: str, voice_id: str) -> None:
        if presets.by_id(voice_id) is None:
            return
        try:
            audio = presets.preview(key, voice_id)
        except presets.PreviewError as exc:
            self.send({"type": "preview", "voice_id": voice_id, "message": f"Couldn't make the preview: {exc}."})
            return
        self.send({"type": "preview", "voice_id": voice_id, "audio": base64.b64encode(audio).decode()})

    def _geocode(self, place: str) -> None:
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
        sign_in = config.get("brain", {}).get("claude", {}).get("auth") == "subscription"
        self.send({"type": "saved", "ok": True, "notes": notes, "sign_in": sign_in})

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
    "brain": {"claude": {"auth"}},
    "audio": {"input_device", "output_device", "headphones"},
    "tts": {"provider", "fallback", "voice_effect", "fish_audio"},
    "assistant": {"name", "wake_phrase", "user_name", "home", "home_latitude", "home_longitude"},
    "discord_bridge": {"enabled"},
    "hive": {"share"},
}
ALLOWED_ENV = {
    "ANTHROPIC_API_KEY", "FISH_AUDIO_API_KEY", "DISCORD_BOT_TOKEN", "DISCORD_OWNER_ID", "TICKTICK_API_TOKEN",
    "CANVAS_BASE_URL", "CANVAS_API_TOKEN", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
}  # fmt: skip


def _allowed(config: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for section, keys in ALLOWED_CONFIG.items():
        given = config.get(section)
        if not isinstance(given, dict):
            continue
        if isinstance(keys, dict):
            inner = {k: {kk: vv for kk, vv in (given.get(k) or {}).items() if kk in v} for k, v in keys.items()}
            inner = {k: v for k, v in inner.items() if v}
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
        if session.done:
            send({"type": "quit"})
    proc.wait(timeout=10)
    return 0 if session.choices is not None else 1
