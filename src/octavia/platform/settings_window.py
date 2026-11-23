"""Her settings window: every setting worth changing, found by section or by search, saved as it's changed.

Opened from the tray's Settings or ``octavia settings``, as its own process (``python -m
octavia.platform.settings_window ROOT [CONFIG] [--section ID]``), like her other windows. The window itself is drawn
by a small app on the avatar's Electron (``avatar/settings``, ``avatar/renderer/settings``) in the look of her orb and
panels; this process is its other half (``Backend``, ``Electron``): it sends the app every setting once and answers
each change. Without the avatar's Electron the plain Tk window (``SettingsWindow``) opens instead. What both show
comes from ``octavia.settings_catalog``; each change is checked against the whole config first (a bad value is refused with
the setting's own name and never saved), then kept where that kind of setting lives:

* config settings in ``data/settings.json`` (``octavia.user_settings``), never in ``config/default.yaml``; a value
  set back to what the files give is dropped from there again;
* API keys in ``.env`` (``keys.set_value``), masked until the eye is clicked;
* her voice volume in ``data/voice_volume.json``, where "talk louder" keeps it.

While she runs she picks the change up by herself (``core/live_settings.py``). What she can only use after a restart
is marked, and a bar offers the restart; she says in ``data/settings_status.json`` what's waiting.
"""

from __future__ import annotations

import contextlib
import copy
import enum
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any

from octavia import settings_catalog as catalog
from octavia.settings_catalog import Setting

MODULE = "octavia.platform.settings_window"
DEFAULT_DEVICE = "Windows default"
DEFAULT_CHOICE = "Default"

# The classic window: near-black, her blue, a pale violet for what waits (never amber).
BACKGROUND = "#121317"
SIDEBAR = "#17181d"
CARD = "#1c1e24"
CARD_EDGE = "#262931"
FIELD = "#24272e"
FIELD_EDGE = "#323640"
TEXT = "#e7e8ea"
MUTED = "#8b8f98"
FAINT = "#5f636c"
ACCENT = "#7fc4ff"
BUTTON = "#2c6fd6"
AMBER = "#b4b0e0"  # what waits for a restart
RED = "#ff8a80"
GREEN = "#7ee2a8"
SELECTED = "#22262f"

_TIME = re.compile(r"(?:[01]?\d|2[0-3]):[0-5]\d")


# ---------------------------------------------------------------- values: config <-> what the window shows


def get(config: Any, key: str) -> Any:
    node = config
    for part in key.split("."):
        node = getattr(node, part)
    return node


def env_file(config: Any) -> Path:
    from octavia import config as config_module
    from octavia.core.userdir import state_root

    return config_module.dotenv_file() or state_root(config.root) / ".env"


def current(config: Any, setting: Setting) -> Any:
    """What she runs with for ``setting`` (the files plus earlier choices; her voice volume as she uses it)."""
    if setting.env:
        from octavia import keys

        return keys.value(setting.env)
    if setting.key == "audio.output_volume":
        from octavia.audio.voice_volume import SETTINGS_FILE, VoiceVolume

        return VoiceVolume.load(config.resolve_path(SETTINGS_FILE), config.audio.output_volume).gain
    return get(config, setting.key)


def to_text(setting: Setting, value: Any) -> str:
    """``value`` for an entry box."""
    kind = catalog.kind_of(setting)
    if value is None:
        return ""
    if kind == "list":
        return ", ".join(str(v) for v in value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def parse(setting: Setting, raw: Any) -> Any:
    """What was typed or picked, as the config takes it. Raises ValueError with a plain reason."""
    kind = catalog.kind_of(setting)
    if kind in ("bool", "percent", "volume"):
        return raw
    if kind in ("input", "output"):
        return None if raw in (DEFAULT_DEVICE, "", None) else str(raw)
    if kind == "choice":
        if setting.key == "barge_in.voice":
            return {"auto": None, "on": True, "off": False}[raw]
        return None if raw in (None, DEFAULT_CHOICE) else str(raw)
    text = "" if raw is None else str(raw).strip()
    if kind in ("int", "float"):
        if not text:
            if catalog.optional(setting):
                return None
            raise ValueError("needs a number")
        cleaned = text.rstrip("%").replace(",", ".").replace(" ", "")
        try:
            # An int as typed, digit for digit: a Discord user ID is past what a float keeps exactly.
            number: float = int(cleaned) if kind == "int" and re.fullmatch(r"[+-]?\d+", cleaned) else float(cleaned)
        except ValueError:
            raise ValueError("needs a number") from None
        if not math.isfinite(number):
            raise ValueError("needs a number")  # "inf" parses, and an endless timeout would never end
        if kind == "int" and not isinstance(number, int):
            if not number.is_integer():
                raise ValueError("needs a whole number")
            number = int(number)
        lo, hi = catalog.bounds(setting)
        if (lo is not None and number < lo) or (hi is not None and number > hi):
            if lo is not None and hi is not None:
                where = f"between {_n(lo)} and {_n(hi)}"
            else:
                where = f"at least {_n(lo)}" if lo is not None else f"at most {_n(hi)}"
            raise ValueError(f"must be {where}")
        return number
    if kind == "list":
        return [part.strip() for part in text.split(",") if part.strip()]
    if kind == "time":
        if text and not _TIME.fullmatch(text):
            raise ValueError("needs a time like 09:00, or nothing")
        return text
    if kind == "span":
        compact = text.replace(" ", "")
        if compact and not re.fullmatch(rf"{_TIME.pattern}-{_TIME.pattern}", compact):
            raise ValueError("needs hours like 22:00-07:00, or nothing")
        return compact
    if kind == "hotkey":
        return " ".join(text.split()).lower()
    return text


def _n(number: float | None) -> str:
    return "" if number is None else (str(int(number)) if float(number).is_integer() else str(number))


def check(root: Path, path: str | None, changed: dict[str, Any]) -> str | None:
    """Why these config values can't be kept (a hotkey that won't parse, quiet hours that aren't), or None: the
    whole config is built with them, so nothing that would stop her from starting gets through."""
    from octavia import user_settings
    from octavia.config import AppConfig, file_values

    for key, value in changed.items():
        if key.startswith("hotkeys.") and value:
            try:
                from octavia.platform.windows import parse_hotkey

                parse_hotkey(value)
            except ImportError:
                pass
            except ValueError as exc:
                return f"{_label(key)}: {exc}"
    data = user_settings.apply(copy.deepcopy(file_values(root, path)), root)
    for key, value in changed.items():
        user_settings.put(data, key, value)
    try:
        AppConfig.model_validate(data)
    except ValueError as exc:
        errors = getattr(exc, "errors", None)
        if callable(errors):
            first = errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            message = str(first.get("msg", exc)).removeprefix("Value error, ")
            return f"{_label(where)}: {message}"
        return str(exc)
    return None


def _label(key: str) -> str:
    setting = catalog.by_key(key)
    return setting.label if setting is not None else key


_ABSENT = object()


def from_files(root: Path, path: str | None, key: str) -> Any:
    """What the config files alone give for ``key`` (the built-in default when neither file names it)."""
    from octavia.config import AppConfig, file_values

    node: Any = file_values(root, path)
    for part in key.split("."):
        node = node.get(part, _ABSENT) if isinstance(node, dict) else _ABSENT
    return get(AppConfig(), key) if node is _ABSENT else node


def _same(a: Any, b: Any) -> bool:
    return a == b  # 12 == 12.0 already; through float, two 18-digit IDs could look the same


def save(config: Any, path: str | None, setting: Setting, value: Any) -> str | None:
    """Keep ``value`` for ``setting`` where it lives. Returns why it can't be kept, or None once it is."""
    from octavia import user_settings

    root = config.root
    try:
        if setting.env:
            from octavia import keys

            keys.set_value(env_file(config), setting.env, value)
            return None
        if setting.key == "audio.output_volume":
            from octavia.audio.voice_volume import SETTINGS_FILE, VoiceVolume

            VoiceVolume(value, config.resolve_path(SETTINGS_FILE), config.audio.output_volume).save()
            return None
        problem = check(root, path, {setting.key: value})
        if problem:
            return problem
        if _same(from_files(root, path, setting.key), value):
            user_settings.forget(root, [setting.key])  # back to what the files say: it follows them again
        else:
            user_settings.save(root, {setting.key: value}, path=path)
    except (OSError, ValueError) as exc:
        return f"Couldn't save: {exc}"
    return None


def reset(config: Any, setting: Setting) -> str | None:
    """Drop the window's choice for ``setting``: it follows the config files again."""
    from octavia import user_settings

    try:
        user_settings.forget(config.root, [setting.key])
    except OSError as exc:
        return f"Couldn't save: {exc}"
    return None


def device_names(kind: str, host_api: str | None) -> list[str]:
    from octavia.audio import devices

    api = (host_api or devices.DEFAULT_HOST_API or "").lower()
    key = "inputs" if kind == "input" else "outputs"
    try:
        listed = devices.list_devices()
    except Exception:
        return []
    return sorted({d["name"] for d in listed if d[key] > 0 and api in d["host_api"].lower()}, key=str.lower)


_MODIFIERS = {"control_l", "control_r", "alt_l", "alt_r", "shift_l", "shift_r", "win_l", "win_r", "super_l",
              "super_r", "meta_l", "meta_r", "caps_lock", "num_lock"}  # fmt: skip
_NAMED = {"prior": "pageup", "next": "pagedown", "return": "enter", "escape": "esc", "minus": "-", "plus": "+",
          "comma": ",", "period": ".", "slash": "/", "semicolon": ";"}  # fmt: skip


def _win_down() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        return bool((user32.GetKeyState(0x5B) | user32.GetKeyState(0x5C)) & 0x8000)
    except (AttributeError, OSError):
        return False


def hotkey_of(event: Any) -> str | None:
    """A key press as a hotkey ("ctrl+alt+j"), or None for a modifier on its own."""
    name = (event.keysym or "").lower()
    if name in _MODIFIERS:
        return None
    code = getattr(event, "keycode", None)
    if sys.platform == "win32" and isinstance(code, int) and (0x30 <= code <= 0x39 or 0x41 <= code <= 0x5A):
        name = chr(code).lower()  # Ctrl+Shift+1 is "1", not the "exclam" Shift makes of it
    state = int(getattr(event, "state", 0) or 0)
    parts = [m for m, bit in (("ctrl", 0x4), ("alt", 0x20000), ("shift", 0x1), ("win", 0x40)) if state & bit]
    if "win" not in parts and _win_down():
        parts.append("win")
    if "alt" not in parts and state & 0x8 and sys.platform != "win32":
        parts.insert(1 if parts[:1] == ["ctrl"] else 0, "alt")
    return "+".join([*parts, _NAMED.get(name, name)])


# ---------------------------------------------------------------- the window's side: what the page is told


class Backend:
    """Everything the window does, apart from drawing it: the map of settings it's sent, a change checked and kept
    (or refused, with why), a reset, a key revealed, a search, a restart or snooze asked of her. ``handle`` takes a
    message from the page and returns the answers; ``status`` is what's sent every few seconds."""

    def __init__(self, config: Any, path: str | None) -> None:
        self.config = config
        self.path = path
        self.values: dict[str, Any] = {}
        for setting in catalog.all_settings():
            try:
                self.values[setting.key] = current(config, setting)
            except Exception:
                self.values[setting.key] = None
        self._devices: dict[str, list[str]] = {}
        self._usage_at = 0.0
        self._usage: dict[str, Any] | None = None

    # ---- the map

    def model(self, start: str | None = None) -> dict[str, Any]:
        status = self.status()
        return {
            "type": "model",
            "name": self.config.assistant.name,
            "start": start,
            "sounds": {"volume": float(self.config.audio.earcon_volume)},
            "sections": [
                {
                    "id": s.id,
                    "title": s.title,
                    "glyph": s.glyph,
                    "about": s.about,
                    "area": s.area,
                    "groups": [
                        {
                            "title": g.title,
                            "about": g.about,
                            "advanced": g.advanced,
                            "when": list(g.when) if g.when else None,
                            "keys": [k for k in g.keys if catalog.by_key(k) is not None],
                        }
                        for g in s.groups
                    ],
                }
                for s in catalog.SECTIONS
            ],
            "settings": [self.describe(s) for s in catalog.all_settings()],
            **status,
        }

    def describe(self, setting: Setting) -> dict[str, Any]:
        """One setting as the page draws it: its control, what it shows now, and its marks."""
        kind = catalog.kind_of(setting)
        value = self.values.get(setting.key)
        out: dict[str, Any] = {
            "key": setting.key,
            "label": setting.label,
            "about": setting.about,
            "section": setting.section,
            "group": setting.group,
            "restart": catalog.needs_restart(setting),
            "changed": self._changed(setting),
            "unit": setting.unit,
        }
        if kind == "bool":
            out |= {"kind": "toggle", "value": bool(value)}
        elif kind == "choice":
            options = list(catalog.choices_of(setting))
            if setting.key != "barge_in.voice" and catalog.optional(setting):
                options.append(DEFAULT_CHOICE)
            shown = _choice_key(setting, value)
            if shown not in options:
                options.append(shown)  # a value from local.yaml the list doesn't name (a full model id)
            out |= {
                "kind": "choice",
                "value": shown,
                "options": [{"value": o, "label": catalog.shown_choice(setting, o)} for o in options],
            }
        elif kind in ("input", "output"):
            names = [DEFAULT_DEVICE, *self.devices(kind)]
            shown = DEFAULT_DEVICE if value is None else str(value)
            if shown not in names:
                names.append(shown)
            out |= {"kind": "device", "value": shown, "options": [{"value": n, "label": n} for n in names]}
        elif kind in ("percent", "volume"):
            top = 200 if kind == "volume" else round(100 * (catalog.bounds(setting)[1] or 1.0))
            out |= {"kind": "slider", "value": round(float(value or 0) * 100), "max": top}
        elif kind == "secret":
            out |= {"kind": "secret", "value": "", "set": bool(value)}
        elif kind == "hotkey":
            out |= {"kind": "hotkey", "value": value or ""}
        else:
            lo, hi = catalog.bounds(setting)
            width = "short" if kind in ("int", "float", "time") else "mid" if kind == "span" else "wide"
            out |= {
                "kind": "number" if kind in ("int", "float") else "text",
                "value": to_text(setting, value),
                "width": width,
                "min": lo,
                "max": hi,
                "step": setting.step or (1 if kind == "int" else None),
                "placeholder": {"list": "separate with commas", "time": "hh:mm", "span": "22:00-07:00"}.get(kind, ""),
            }
        return out

    def _changed(self, setting: Setting) -> bool:
        if setting.env or setting.key == "audio.output_volume":
            return False
        from octavia import user_settings

        return setting.key in user_settings.read(self.config.root)

    def devices(self, kind: str) -> list[str]:
        if kind not in self._devices:
            self._devices[kind] = device_names(kind, self.config.audio.host_api)
        return self._devices[kind]

    # ---- her side

    def status(self) -> dict[str, Any]:
        from octavia.core.live_settings import read_status

        return {"status": read_status(self.config.root), "usage": self.usage()}

    def usage(self, max_age_s: float = 20.0) -> dict[str, Any] | None:
        """Where the Claude limits stand and what her own work used today, for the Usage section's meters."""
        now = time.time()
        if self._usage is not None and now - self._usage_at < max_age_s:
            return self._usage
        try:
            from octavia.core.usage import Budget, BudgetSettings

            cfg = self.config
            budget = Budget(
                BudgetSettings.from_config(cfg.usage_budget),
                cfg.resolve_path("data/usage"),
                api_key=cfg.brain.claude.auth == "api_key",
            )
            settings = budget.settings
            meters = []
            latest = None if budget.api_key else budget.latest()
            if latest is not None:
                for title, window, limit in (
                    ("Your 5-hour window", latest.five_hour, settings.max_five_hour),
                    ("Your weekly limit", latest.weekly, settings.max_weekly),
                ):
                    if window is not None:
                        used = window.utilization if window.current(now) else 0.0
                        meters.append({"title": title, "used": used, "limit": limit, "note": f"{used:.0%} used"})
            if settings.enabled and not budget.api_key:
                today = budget.used_today(now)
                share = settings.daily_share or 1e-9
                meters.append(
                    {
                        "title": "Her background work today",
                        "used": min(1.0, today / share),
                        "limit": None,
                        "note": f"{today:.1%} of your week, of {settings.daily_share:.0%} allowed",
                    }
                )
            self._usage = {"meters": meters, "text": usage_words(budget, latest, now)}
        except Exception as exc:
            self._usage = {"meters": [], "text": f"Couldn't read her usage: {exc}"}
        self._usage_at = now
        return self._usage

    # ---- what the page asks

    def handle(self, msg: dict[str, Any]) -> list[dict[str, Any]]:
        kind = msg.get("type")
        key = msg.get("key")
        setting = catalog.by_key(key) if isinstance(key, str) else None
        if kind == "set" and setting is not None:
            return [self.set(setting, msg.get("raw"), msg.get("id"))]
        if kind == "reset" and setting is not None:
            return [self.reset(setting, msg.get("id"))]
        if kind == "reveal" and setting is not None and setting.env:
            from octavia import keys

            return [{"type": "secret", "key": setting.key, "value": keys.value(setting.env)}]
        if kind == "search":
            query = str(msg.get("query") or "")[:200]
            return [
                {
                    "type": "found",
                    "id": msg.get("id"),
                    "keys": [s.key for s in catalog.search(query)][:60],
                    "sections": catalog.sections_found(query),
                }
            ]
        if kind == "restart":
            return [self.restart()]
        if kind == "action" and msg.get("action") in ("snooze", "focus", "wake"):
            return [self.act(str(msg["action"]), msg.get("minutes"))]
        if kind == "action" and msg.get("action") == "finance_purge":
            return [self.purge_money()]
        if kind == "action" and msg.get("action") == "forget":
            return [self.forget(msg.get("minutes"))]
        return []

    def set(self, setting: Setting, raw: Any, ask: Any = None) -> dict[str, Any]:
        if catalog.kind_of(setting) in ("percent", "volume"):
            try:
                raw = round(float(raw) / 100, 2)
            except (TypeError, ValueError):
                return self._refused(setting, f"{setting.label} needs a number.", ask)
        try:
            value = parse(setting, raw)
        except ValueError as exc:
            return self._refused(setting, f"{setting.label} {exc}.", ask)
        problem = save(self.config, self.path, setting, value)
        if problem:
            return self._refused(setting, problem, ask)
        self.values[setting.key] = current(self.config, setting) if setting.env else value
        return self._kept(setting, ask)

    def reset(self, setting: Setting, ask: Any = None) -> dict[str, Any]:
        problem = reset(self.config, setting)
        if problem:
            return self._refused(setting, problem, ask)
        self.values[setting.key] = from_files(self.config.root, self.path, setting.key)
        return self._kept(setting, ask)

    def _kept(self, setting: Setting, ask: Any) -> dict[str, Any]:
        name = self.config.assistant.name
        running = self.status()["status"] is not None
        if not running:
            message, tone = f"Kept. {name} uses it when she next starts.", "idle"
        elif catalog.needs_restart(setting):
            message, tone = "Kept. It takes effect after a restart.", "restart"
        else:
            message, tone = f"Kept. {name} is using it now.", "live"
        return {"type": "saved", "id": ask, "key": setting.key, "setting": self.describe(setting), "message": message,
                "tone": tone}  # fmt: skip

    def _refused(self, setting: Setting, why: str, ask: Any) -> dict[str, Any]:
        return {"type": "refused", "id": ask, "key": setting.key, "error": why, "setting": self.describe(setting)}

    def restart(self) -> dict[str, Any]:
        from octavia.core.live_settings import ask_restart, read_status

        status = read_status(self.config.root)
        try:
            if status is None:
                raise OSError("she isn't running")
            ask_restart(self.config.root, int(status["pid"]))
        except (OSError, ValueError, KeyError) as exc:
            return {"type": "refused", "key": "", "error": f"Couldn't ask her to restart: {exc}"}
        return {"type": "status", **self.status()}

    def act(self, action: str, minutes: Any) -> dict[str, Any]:
        from octavia.core.live_settings import ask_action, read_status

        status = read_status(self.config.root)
        try:
            if status is None:
                raise OSError("she isn't running")
            ask_action(self.config.root, int(status["pid"]), action, minutes=minutes)
        except (OSError, ValueError, KeyError) as exc:
            return {"type": "refused", "key": "", "error": f"Couldn't ask her: {exc}"}
        return {"type": "status", **self.status()}

    def purge_money(self) -> dict[str, Any]:
        """Delete all money data: by her, when she runs (she holds its database open), else directly."""
        from octavia.core.live_settings import ask_action, read_status
        from octavia.finance import privacy

        status = read_status(self.config.root)
        try:
            if status is not None:
                ask_action(self.config.root, int(status["pid"]), "finance_purge")
            else:
                privacy.purge(self.config.root)
        except (OSError, ValueError, KeyError) as exc:
            return {"type": "refused", "key": "", "error": f"Couldn't delete it: {exc}"}
        return {"type": "money_purged", "running": status is not None}

    def forget(self, minutes: Any) -> dict[str, Any]:
        """Forget the last ``minutes`` (the window's second click is the yes): by her, when she runs (she restarts
        into it), else here (``forget_while_off``)."""
        from octavia.core import forget
        from octavia.core.live_settings import read_status

        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 0
        if minutes not in forget.CHOICES:
            return {"type": "refused", "key": "", "error": "That isn't one of the choices."}
        status = read_status(self.config.root)
        if status is not None:
            return {"type": "forgotten", "message": ask_her_to_forget(self.config, int(status["pid"]), minutes)}
        return {"type": "forgotten", "message": forget_while_off(self.config, minutes)}


def ask_her_to_forget(config: Any, pid: int, minutes: int, wait_s: float = 8.0) -> str:
    """Ask the running her (``pid``) to forget, and wait a moment to see her take it: she leaves the pending forget
    at once (``App.forget_recent``). Returns what to show."""
    from octavia.core import forget
    from octavia.core.live_settings import ACTION_FILE, ask_action

    pending = config.root / forget.PENDING_FILE
    before = pending.stat().st_mtime if pending.exists() else None
    try:
        ask_action(config.root, pid, "forget", minutes=minutes)
    except OSError as exc:
        return f"Couldn't ask her: {exc}"
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if pending.exists() and pending.stat().st_mtime != before:
            return f"{config.assistant.name} is forgetting {forget.label(minutes)} and restarting into it."
        if not (config.root / ACTION_FILE).exists():  # taken, but she didn't leave it: she says why
            time.sleep(0.5)
            if pending.exists() and pending.stat().st_mtime != before:
                continue
            return f"{config.assistant.name} couldn't forget it just now; she says why."
        time.sleep(0.1)
    return f"{config.assistant.name} didn't take it (she may be busy starting). Nothing was forgotten; try again."


def forget_while_off(config: Any, minutes: int) -> str:
    """Forget the last ``minutes`` while she isn't running: here and now, holding her single-instance lock so she
    can't start halfway through. When the lock is held (her supervisor is between two starts of her), it's left for
    the start that's coming (``core/forget.request``). Returns what to show."""
    from octavia.core import forget

    what = forget.label(minutes)
    guard = None
    if sys.platform == "win32":
        from octavia.cli import _path_hash
        from octavia.platform.windows import SingleInstance

        guard = SingleInstance(f"Local\\OctaviaAssistant-{_path_hash(config.root)}")
        if guard.already_running:  # she's starting (her supervisor holds it): her start already looked for one
            guard.close()
            return f"{config.assistant.name} is starting up. Nothing was forgotten; try again in a minute."
    try:
        now = time.time()
        result = forget.forget(config.root, minutes, now - minutes * 60, now)
    finally:
        if guard is not None:
            guard.close()
    if not result.ok:
        return f"Couldn't forget {what}: {result.error}."
    if not result.total:
        return f"Nothing was kept from {what}."
    records = f"{result.total} record" + ("" if result.total == 1 else "s")
    return f"Forgot {what}: {records}. A copy of what was there is in {result.backup}."


def usage_words(budget: Any, latest: Any, now: float) -> str:
    """A few plain sentences under the meters: when the windows reset, whether her work is held back, what she
    skipped. (``Budget.describe`` says all of it, every skipped job named: too long for a window.)"""
    from octavia.core.usage import _when

    if budget.api_key:
        return "She runs on your Anthropic API credits, so your subscription's limits don't hold her back."
    if latest is None:
        return "No reading from Claude yet; one comes with her next Claude reply."
    parts = []
    resets = []
    for name, window in (("the 5-hour window", latest.five_hour), ("the weekly limit", latest.weekly)):
        if window is not None and window.current(now) and window.resets_at:
            resets.append(f"{name} {_when(window.resets_at, now)}")
    if resets:
        parts.append("Resets: " + " and ".join(resets) + ".")
    if not budget.settings.enabled:
        parts.append("The budget is off, so nothing holds her background work back.")
    else:
        reason = budget.reason(now)
        parts.append(f"Her background work is held back: {reason}." if reason else "Her background work may run.")
    skipped = {str(s.get("job") or "") for s in budget.skips(now - 86400)}
    if skipped:
        parts.append(f"She skipped {len(skipped)} job{'s' if len(skipped) != 1 else ''} in the last day.")
    return " ".join(parts)


def _choice_key(setting: Setting, value: Any) -> str:
    if setting.key == "barge_in.voice":
        return "auto" if value is None else ("on" if value else "off")
    if isinstance(value, enum.Enum):  # a permission's Decision: "allow", not "Decision.ALLOW"
        value = value.value
    return DEFAULT_CHOICE if value is None else str(value)


# ---------------------------------------------------------------- running it: her Electron window, Tk if it can't

APP = "avatar/settings"
WINDOW_STATE = "data/settings_window/window.json"
STATUS_EVERY_S = 2.0
READY_TIMEOUT_S = 20.0


def app_command(config: Any, app_config: Path) -> list[str] | None:
    """How to start the window's app, or None when the avatar's Electron isn't installed."""
    from octavia.avatar.bridge import electron_path

    exe = electron_path(config.root, config.avatar.electron)
    if not exe.is_file() or not (config.root / APP / "main.js").is_file():
        return None
    return [str(exe), str(config.root / APP), "--config", str(app_config)]


def _icon(config: Any) -> str | None:
    """Her raven emblem as a PNG for the window's taskbar button (made once)."""
    target = config.resolve_path("data/settings_window/icon.png")
    if target.is_file():
        return str(target)
    try:
        from PIL import Image

        from octavia.platform.tray import emblem

        target.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(emblem(64)).save(target)
        return str(target)
    except Exception:
        return None


class Electron:
    """The window's app, talked to over its stdin and stdout. ``run`` answers it until the window closes."""

    def __init__(
        self, backend: Backend, command: list[str], *, start: str | None = None, log_file: Path | None = None
    ) -> None:
        import queue

        self.backend = backend
        self.log_file = log_file  # where the app's own complaints go (its stderr)
        self.command = command
        self.start = start
        self.inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.proc: Any = None

    def launch(self) -> None:
        import subprocess
        import threading

        errors: Any = subprocess.DEVNULL
        if self.log_file is not None:
            with contextlib.suppress(OSError):
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                errors = self.log_file.open("ab")
        self.proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if errors is not subprocess.DEVNULL:
            errors.close()  # the app has its own handle on it now
        threading.Thread(target=self._read, name="settings-window", daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.inbox.put(msg)
        self.inbox.put(None)

    def send(self, msg: dict[str, Any]) -> bool:
        try:
            self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def wait_ready(self, timeout: float = READY_TIMEOUT_S) -> str:
        """ "ready", "already-open" (another settings window was brought forward) or "failed"."""
        import queue

        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            try:
                msg = self.inbox.get(timeout=left)
            except queue.Empty:
                break
            if msg is None:
                return "failed"
            if msg.get("type") in ("ready", "already-open"):
                return str(msg["type"])
        return "failed"

    def run(self, until: Any = None) -> None:
        """Send the map, then answer the page and keep it told her status, until the window closes (or ``until()``
        says to stop)."""
        import queue

        self.send(self.backend.model(self.start))
        last = self.backend.status()
        next_status = time.monotonic() + STATUS_EVERY_S
        while until is None or not until():
            try:
                msg = self.inbox.get(timeout=max(0.05, next_status - time.monotonic()))
            except queue.Empty:
                msg = {}
            if msg is None or msg.get("type") == "closed":
                break
            if msg:
                for answer in self.backend.handle(msg):
                    self.send(answer)
            if time.monotonic() >= next_status:
                now = self.backend.status()
                if now != last:
                    self.send({"type": "status", **now})
                    last = now
                next_status = time.monotonic() + STATUS_EVERY_S

    def close(self) -> None:
        if self.proc is None:
            return
        self.send({"type": "quit"})
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def open_window(config: Any, path: str | None, *, start: str | None = None) -> bool:
    """Her settings window in her own look. False when it can't be shown that way (no Electron): Tk then."""
    app_config = config.resolve_path("data/settings_window/app.json")
    try:
        app_config.parent.mkdir(parents=True, exist_ok=True)
        app_config.write_text(
            json.dumps(
                {
                    "title": f"{config.assistant.name}: settings",
                    "icon": _icon(config),
                    "statePath": str(config.resolve_path(WINDOW_STATE)),
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        return False
    command = app_command(config, app_config)
    if command is None:
        return False
    log_file = config.resolve_path("data/settings_window/app.log")
    with contextlib.suppress(OSError):
        if log_file.stat().st_size > 1_000_000:
            log_file.unlink()  # only the latest runs' complaints matter
    window = Electron(Backend(config, path), command, start=start, log_file=log_file)
    try:
        window.launch()
    except OSError:
        return False
    ready = window.wait_ready()
    if ready == "already-open":
        window.close()
        return True
    if ready != "ready":
        window.close()
        return False
    try:
        window.run()
    finally:
        window.close()
    return True


# ---------------------------------------------------------------- the classic window (Tk), when Electron can't


class SettingsWindow:
    def __init__(self, root: Any, config: Any, path: str | None, *, poll_ms: int = 2000) -> None:
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter import ttk

        self.tk = tk
        self.root = root
        self.config = config
        self.path = path
        self.poll_ms = poll_ms
        self.values: dict[str, Any] = {}
        for setting in catalog.all_settings():
            try:
                self.values[setting.key] = current(config, setting)
            except Exception:
                self.values[setting.key] = None
        self.rows: dict[str, dict[str, Any]] = {}
        self.section = catalog.SECTIONS[0].id
        self.status: dict[str, Any] | None = None
        self._restarting: tuple[int, float] | None = None  # (the pid asked, when)
        self._poll: str | None = None
        self._banner_text: tuple[Any, ...] | None = None
        name = config.assistant.name

        families = set(tkfont.families(root))
        text_family = next((f for f in ("Segoe UI Variable Text", "Segoe UI") if f in families), "TkDefaultFont")
        head_family = next((f for f in ("Segoe UI Variable Display", "Segoe UI") if f in families), text_family)
        self.glyphs = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families), None)
        self.fonts = {
            "body": (text_family, 10),
            "label": (text_family, 10, "bold"),
            "small": (text_family, 9),
            "tiny": (text_family, 7, "bold"),
            "title": (head_family, 20, "bold"),
            "brand": (head_family, 13, "bold"),
            "nav": (text_family, 10),
            "group": (text_family, 8, "bold"),
            "glyph": (self.glyphs or text_family, 11),
        }

        root.title(f"{name}: settings")
        root.configure(background=BACKGROUND)
        root.geometry("1060x720")
        root.minsize(800, 520)
        style = ttk.Style(root)
        with contextlib.suppress(tk.TclError):
            style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=TEXT, arrowcolor=MUTED,
                        bordercolor=FIELD_EDGE, lightcolor=FIELD, darkcolor=FIELD, padding=(8, 4))  # fmt: skip
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)],
                  bordercolor=[("focus", ACCENT)], arrowcolor=[("active", TEXT)])  # fmt: skip
        style.configure("Horizontal.TScale", background=ACCENT, troughcolor=FIELD, bordercolor=CARD,
                        lightcolor=ACCENT, darkcolor=ACCENT)  # fmt: skip
        style.configure("Vertical.TScrollbar", background=CARD, troughcolor=BACKGROUND, bordercolor=BACKGROUND,
                        arrowcolor=MUTED, lightcolor=CARD, darkcolor=CARD, gripcount=0)  # fmt: skip
        root.option_add("*TCombobox*Listbox.background", FIELD)
        root.option_add("*TCombobox*Listbox.foreground", TEXT)
        root.option_add("*TCombobox*Listbox.selectBackground", BUTTON)
        root.option_add("*TCombobox*Listbox.font", self.fonts["body"])

        # ---- sidebar
        side = tk.Frame(root, background=SIDEBAR, width=236)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        brand = tk.Frame(side, background=SIDEBAR)
        brand.pack(fill="x", padx=18, pady=(18, 14))
        badge = tk.Canvas(brand, width=36, height=36, background=SIDEBAR, highlightthickness=0)
        badge.create_oval(2, 2, 34, 34, fill="#1a2c42", outline=ACCENT, width=1)
        badge.create_text(18, 18, text=(name[:1] or "O").upper(), fill=ACCENT, font=self.fonts["brand"])
        badge.pack(side="left")
        names = tk.Frame(brand, background=SIDEBAR)
        names.pack(side="left", padx=(10, 0))
        tk.Label(names, text=name, background=SIDEBAR, foreground=TEXT, font=self.fonts["brand"]).pack(anchor="w")
        tk.Label(names, text="Settings", background=SIDEBAR, foreground=MUTED, font=self.fonts["small"]).pack(
            anchor="w"
        )

        search = tk.Frame(side, background=FIELD, highlightthickness=1, highlightbackground=FIELD_EDGE,
                          highlightcolor=ACCENT)  # fmt: skip
        search.pack(fill="x", padx=14, pady=(0, 14))
        tk.Label(search, text="\ue721" if self.glyphs else "\u2315", background=FIELD, foreground=MUTED,
                 font=self.fonts["glyph"]).pack(side="left", padx=(9, 2))  # fmt: skip
        self.query = tk.StringVar()
        self.search = tk.Entry(search, textvariable=self.query, background=FIELD, foreground=TEXT, relief="flat",
                               insertbackground=TEXT, font=self.fonts["body"], highlightthickness=0)  # fmt: skip
        self.search.pack(side="left", fill="x", expand=True, ipady=6, padx=(2, 8))
        self._hint(self.search, "Search settings")
        self.query.trace_add("write", lambda *_: self._searched())

        self.nav: dict[str, tuple[Any, ...]] = {}
        navs = tk.Frame(side, background=SIDEBAR)
        navs.pack(fill="both", expand=True, padx=8)
        for section in catalog.SECTIONS:
            self._nav_item(navs, section)

        # ---- main
        main = tk.Frame(root, background=BACKGROUND)
        main.pack(side="left", fill="both", expand=True)
        foot = tk.Frame(main, background=BACKGROUND)
        foot.pack(fill="x", side="bottom", padx=28, pady=(8, 14))
        self.message = tk.Label(foot, text="Changes are saved as you make them.", background=BACKGROUND,
                                foreground=FAINT, font=self.fonts["small"], anchor="w")  # fmt: skip
        self.message.pack(side="left", fill="x", expand=True)
        self._button(foot, "Done", self.close, primary=False).pack(side="right")

        self.banner = tk.Frame(main, background=BACKGROUND)
        self.banner.pack(fill="x", padx=(28, 24), pady=(16, 0))
        header = tk.Frame(main, background=BACKGROUND)
        header.pack(fill="x", padx=28, pady=(8, 6))
        self.title = tk.Label(header, text="", background=BACKGROUND, foreground=TEXT, font=self.fonts["title"],
                              anchor="w")  # fmt: skip
        self.title.pack(anchor="w")
        self.subtitle = tk.Label(header, text="", background=BACKGROUND, foreground=MUTED, font=self.fonts["body"],
                                 anchor="w")  # fmt: skip
        self.subtitle.pack(anchor="w", pady=(2, 0))

        holder = tk.Frame(main, background=BACKGROUND)
        holder.pack(fill="both", expand=True, padx=(28, 6), pady=(8, 0))
        self.canvas = tk.Canvas(holder, background=BACKGROUND, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, background=BACKGROUND)
        self._body_id = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._body_id, width=e.width - 16))
        root.bind_all("<MouseWheel>", self._wheel, add="+")

        root.bind("<Escape>", lambda _: self.close())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Control-f>", lambda _: self.search.focus_set())
        self.show(self.section)
        self.refresh_status()

    # ---- pieces

    def _hint(self, entry: Any, text: str) -> None:
        """Grey placeholder text in an empty entry."""

        def show(_: Any = None) -> None:
            if not entry.get():
                entry._hinted = True
                entry.insert(0, text)
                entry.configure(foreground=FAINT)

        def hide(_: Any = None) -> None:
            if getattr(entry, "_hinted", False):
                entry.delete(0, "end")
                entry._hinted = False
                entry.configure(foreground=TEXT)

        entry.bind("<FocusIn>", hide, add="+")
        entry.bind("<FocusOut>", show, add="+")
        show()

    def _typed_query(self) -> str:
        text = self.query.get().strip()
        return "" if getattr(self.search, "_hinted", False) and text == "Search settings" else text

    def _button(self, parent: Any, text: str, command: Any, primary: bool = True, colour: str = BUTTON) -> Any:
        bg = colour if primary else "#262931"
        hover = {BUTTON: "#3b7fe6", AMBER: "#c8c4ee"}.get(colour, "#30343e") if primary else "#30343e"
        button = self.tk.Label(parent, text=text, background=bg, foreground="#15161a" if colour == AMBER else TEXT,
                               font=self.fonts["label"], padx=16, pady=6, cursor="hand2")  # fmt: skip
        button.bind("<Button-1>", lambda _: command())
        button.bind("<Enter>", lambda _: button.configure(background=hover))
        button.bind("<Leave>", lambda _: button.configure(background=bg))
        return button

    def _nav_item(self, parent: Any, section: catalog.Section) -> None:
        tk = self.tk
        item = tk.Frame(parent, background=SIDEBAR, cursor="hand2")
        item.pack(fill="x", pady=1)
        mark = tk.Frame(item, background=SIDEBAR, width=3)
        mark.pack(side="left", fill="y", pady=5)
        glyph = tk.Label(item, text=section.glyph if self.glyphs else "\u2022", width=2, background=SIDEBAR,
                         foreground=MUTED, font=self.fonts["glyph"])  # fmt: skip
        glyph.pack(side="left", padx=(10, 6), pady=4)
        label = tk.Label(item, text=section.title, background=SIDEBAR, foreground=MUTED, font=self.fonts["nav"],
                         anchor="w")  # fmt: skip
        label.pack(side="left", fill="x", expand=True)
        self.nav[section.id] = (item, mark, glyph, label)

        def pick(_: Any) -> None:
            if self._typed_query():
                self.query.set("")
                self.root.focus_set()
                self.search.event_generate("<FocusOut>")
            self.show(section.id)

        for widget in (item, glyph, label):
            widget.bind("<Button-1>", pick)
            widget.bind("<Enter>", lambda _, s=section.id: self._nav_hover(s, True))
            widget.bind("<Leave>", lambda _, s=section.id: self._nav_hover(s, False))

    def _nav_hover(self, section: str, on: bool) -> None:
        if section == self.section and not self._typed_query():
            return
        item, _, glyph, label = self.nav[section]
        for widget in (item, glyph, label):
            widget.configure(background="#1d1f25" if on else SIDEBAR)

    def _paint_nav(self) -> None:
        searching = bool(self._typed_query())
        for sid, (item, mark, glyph, label) in self.nav.items():
            on = sid == self.section and not searching
            for widget in (item, glyph, label):
                widget.configure(background=SELECTED if on else SIDEBAR)
            mark.configure(background=ACCENT if on else SIDEBAR)
            glyph.configure(foreground=ACCENT if on else MUTED)
            label.configure(foreground=TEXT if on else MUTED)

    def _wheel(self, event: Any) -> None:
        with contextlib.suppress(Exception):
            if str(event.widget).startswith(str(self.canvas)):
                self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def flush(self) -> None:
        """Keep what's typed but not yet saved (the box still has the cursor) before it's closed or redrawn."""
        for row in list(self.rows.values()):
            if "flush" in row:
                with contextlib.suppress(Exception):
                    row["flush"]()

    def close(self) -> None:
        self.flush()
        self.root.destroy()

    # ---- what's shown

    def show(self, section: str) -> None:
        self.flush()
        self.section = section
        about = next(s for s in catalog.SECTIONS if s.id == section)
        self.title.configure(text=about.title)
        self.subtitle.configure(text=about.about)
        self._render([s for s in catalog.all_settings() if s.section == section], grouped=False)
        self._paint_nav()

    def _searched(self) -> None:
        query = self._typed_query()
        if not query:
            self.show(self.section)
            return
        self.flush()
        found = [s for s in catalog.all_settings() if catalog.matches(s, query)]
        self.title.configure(text="Search")
        self.subtitle.configure(
            text=f"{len(found)} setting{'s' if len(found) != 1 else ''} for \u201c{query}\u201d"
            if found
            else f"Nothing matches \u201c{query}\u201d. Try another word."
        )
        self._render(found, grouped=True)
        self._paint_nav()

    def _render(self, settings: list[Setting], grouped: bool) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        self.rows.clear()
        last = None
        for setting in settings:
            if grouped and setting.section != last:
                last = setting.section
                title = next(s.title for s in catalog.SECTIONS if s.id == setting.section)
                self.tk.Label(self.body, text=title.upper(), background=BACKGROUND, foreground=FAINT,
                              font=self.fonts["group"], anchor="w").pack(fill="x", pady=(14, 4))  # fmt: skip
            self._row(setting)
        self.canvas.yview_moveto(0)

    def _row(self, setting: Setting) -> None:
        tk = self.tk
        card = tk.Frame(self.body, background=CARD, highlightthickness=1, highlightbackground=CARD_EDGE)
        card.pack(fill="x", pady=3)
        inner = tk.Frame(card, background=CARD)
        inner.pack(fill="x", padx=18, pady=12)
        control = tk.Frame(inner, background=CARD)
        control.pack(side="right", padx=(18, 0))
        text = tk.Frame(inner, background=CARD)
        text.pack(side="left", fill="x", expand=True)
        top = tk.Frame(text, background=CARD)
        top.pack(anchor="w", fill="x")
        tk.Label(top, text=setting.label, background=CARD, foreground=TEXT, font=self.fonts["label"]).pack(side="left")
        if catalog.needs_restart(setting):
            tk.Label(top, text="RESTART", background="#24233a", foreground=AMBER, font=self.fonts["tiny"],
                     padx=6, pady=1).pack(side="left", padx=(8, 0))  # fmt: skip
        reset = tk.Label(top, text="Reset", background=CARD, foreground=ACCENT, font=self.fonts["small"],
                         cursor="hand2")  # fmt: skip
        reset.bind("<Button-1>", lambda _: self._reset(setting))
        about = None
        if setting.about:
            about = tk.Label(text, text=setting.about, background=CARD, foreground=MUTED, font=self.fonts["small"],
                             anchor="w", justify="left", wraplength=460)  # fmt: skip
            about.pack(anchor="w", pady=(3, 0))
        error = tk.Label(text, text="", background=CARD, foreground=RED, font=self.fonts["small"], anchor="w",
                         justify="left", wraplength=460)  # fmt: skip

        def fit(_: Any = None) -> None:  # the description wraps in the room the control leaves it
            room = max(200, inner.winfo_width() - control.winfo_reqwidth() - 30)
            for label in (about, error):
                if label is not None and int(label.cget("wraplength")) != room:
                    label.configure(wraplength=room)

        inner.bind("<Configure>", fit)
        row: dict[str, Any] = {"card": card, "error": error, "reset": reset, "control": control}
        self.rows[setting.key] = row
        self._control(setting, control, row)
        self._show_reset(setting)

    def _show_reset(self, setting: Setting) -> None:
        row = self.rows.get(setting.key)
        if row is None or setting.env or setting.key == "audio.output_volume":
            return
        from octavia import user_settings

        if setting.key in user_settings.read(self.config.root):
            row["reset"].pack(side="left", padx=(10, 0))
        else:
            row["reset"].pack_forget()

    # ---- controls

    def _control(self, setting: Setting, parent: Any, row: dict[str, Any]) -> None:
        kind = catalog.kind_of(setting)
        value = self.values.get(setting.key)
        if kind == "bool":
            self._toggle(setting, parent, bool(value), row)
        elif kind == "choice":
            self._choice(setting, parent, value, row)
        elif kind in ("input", "output"):
            names = [DEFAULT_DEVICE, *device_names(kind, self.config.audio.host_api)]
            shown = DEFAULT_DEVICE if value is None else str(value)
            if shown not in names:
                names.append(shown)
            self._combo(setting, parent, names, shown, row, width=34)
        elif kind in ("percent", "volume"):
            self._slider(setting, parent, float(value or 0), row)
        elif kind == "secret":
            self._secret(setting, parent, value or "", row)
        elif kind == "hotkey":
            self._hotkey(setting, parent, value or "", row)
        else:
            width = 8 if kind in ("int", "float", "time") else 13 if kind == "span" else 30
            self._entry(setting, parent, to_text(setting, value), row, width)

    def _toggle(self, setting: Setting, parent: Any, on: bool, row: dict[str, Any]) -> None:
        canvas = self.tk.Canvas(parent, width=46, height=24, background=CARD, highlightthickness=0, cursor="hand2")
        canvas.pack()
        state = {"on": on}

        def paint() -> None:
            canvas.delete("all")
            fill = ACCENT if state["on"] else "#3a3e48"
            canvas.create_oval(1, 1, 23, 23, fill=fill, outline=fill)
            canvas.create_oval(23, 1, 45, 23, fill=fill, outline=fill)
            canvas.create_rectangle(12, 1, 34, 23, fill=fill, outline=fill)
            x = 34 if state["on"] else 12
            canvas.create_oval(x - 8, 4, x + 8, 20, fill="#10161e" if state["on"] else "#c9ccd2", outline="")

        def flip(_: Any = None) -> None:
            state["on"] = not state["on"]
            paint()
            if not self.commit(setting, state["on"]):
                state["on"] = not state["on"]
                paint()

        canvas.bind("<Button-1>", flip)
        row["get"] = lambda: state["on"]
        row["flip"] = flip
        row["set"] = lambda v: (state.update(on=bool(v)), paint())
        paint()

    def _choice_key(self, setting: Setting, value: Any) -> str:
        return _choice_key(setting, value)

    def _choice(self, setting: Setting, parent: Any, value: Any, row: dict[str, Any]) -> None:
        options = list(catalog.choices_of(setting))
        shown = self._choice_key(setting, value)
        if setting.key != "barge_in.voice" and catalog.optional(setting):
            options.append(DEFAULT_CHOICE)
        if shown not in options:
            options.append(shown)  # a value from local.yaml the list doesn't name (a full model id)
        names = {o: catalog.shown_choice(setting, o) for o in options}
        if len(options) <= 3 and sum(len(n) for n in names.values()) <= 32:
            self._segments(setting, parent, options, names, shown, row)
            return
        back = {n: o for o, n in names.items()}
        self._combo(setting, parent, list(names.values()), names[shown], row, width=24,
                    convert=lambda n: back.get(n, n), show=lambda v: names.get(self._choice_key(setting, v), str(v)))  # fmt: skip

    def _segments(self, setting: Setting, parent: Any, options: list[str], names: dict[str, str], value: str,
                  row: dict[str, Any]) -> None:  # fmt: skip
        tk = self.tk
        strip = tk.Frame(parent, background=FIELD, highlightthickness=1, highlightbackground=FIELD_EDGE)
        strip.pack()
        state = {"value": value}
        labels: dict[str, Any] = {}

        def paint() -> None:
            for option, label in labels.items():
                on = option == state["value"]
                label.configure(background=BUTTON if on else FIELD, foreground=TEXT if on else MUTED)

        def pick(option: str) -> None:
            if option == state["value"]:
                return
            before = state["value"]
            state["value"] = option
            paint()
            if not self.commit(setting, option):
                state["value"] = before
                paint()

        for option in options:
            label = tk.Label(strip, text=names[option], font=self.fonts["small"], padx=12, pady=5, cursor="hand2")
            label.pack(side="left")
            label.bind("<Button-1>", lambda _, o=option: pick(o))
            labels[option] = label
        row["pick"] = pick
        row["set"] = lambda v: (state.update(value=self._choice_key(setting, v)), paint())
        paint()

    def _combo(self, setting: Setting, parent: Any, values: list[str], shown: str, row: dict[str, Any], width: int,
               convert: Any = None, show: Any = None) -> Any:  # fmt: skip
        from tkinter import ttk

        var = self.tk.StringVar(value=shown)
        combo = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=width,
                             font=self.fonts["body"])  # fmt: skip
        combo.pack()
        state = {"value": shown}

        def picked(_: Any = None) -> None:
            chosen = var.get()
            if chosen == state["value"]:
                return
            if self.commit(setting, convert(chosen) if convert else chosen):
                state["value"] = chosen
            else:
                var.set(state["value"])

        combo.bind("<<ComboboxSelected>>", picked)
        row["choose"] = lambda text: (var.set(text), picked())

        def put(value: Any) -> None:  # what the row shows, and what a pick is compared with
            var.set(show(value) if show else DEFAULT_DEVICE if value is None else str(value))
            state["value"] = var.get()

        row["set"] = put
        return combo

    def _slider(self, setting: Setting, parent: Any, value: float, row: dict[str, Any]) -> None:
        from tkinter import ttk

        tk = self.tk
        top = 200 if catalog.kind_of(setting) == "volume" else round(100 * (catalog.bounds(setting)[1] or 1.0))
        var = tk.IntVar(value=round(value * 100))
        frame = tk.Frame(parent, background=CARD)
        frame.pack()
        shown = tk.Label(frame, text=f"{var.get()}%", width=5, background=CARD, foreground=TEXT,
                         font=self.fonts["body"], anchor="e")  # fmt: skip
        state = {"saved": var.get()}

        def moved(v: str) -> None:
            var.set(round(float(v)))
            shown.configure(text=f"{var.get()}%")

        scale = ttk.Scale(frame, from_=0, to=top, variable=var, orient="horizontal", length=200, command=moved)
        scale.pack(side="left")
        shown.pack(side="left", padx=(8, 0))

        def released(_: Any = None) -> None:
            if var.get() == state["saved"]:
                return
            if self.commit(setting, round(var.get() / 100, 2)):
                state["saved"] = var.get()
            else:
                var.set(state["saved"])
                shown.configure(text=f"{var.get()}%")

        scale.bind("<ButtonRelease-1>", released)
        scale.bind("<KeyRelease>", released)
        row["slide"] = lambda percent: (var.set(percent), released())
        row["set"] = lambda v: (var.set(round(float(v) * 100)), shown.configure(text=f"{var.get()}%"),
                                state.update(saved=var.get()))  # fmt: skip

    def _field(self, parent: Any, var: Any, width: int, **kw: Any) -> Any:
        return self.tk.Entry(parent, textvariable=var, width=width, background=FIELD, foreground=TEXT,
                             insertbackground=TEXT, relief="flat", font=self.fonts["body"], highlightthickness=1,
                             highlightbackground=FIELD_EDGE, highlightcolor=ACCENT, **kw)  # fmt: skip

    def _entry(self, setting: Setting, parent: Any, text: str, row: dict[str, Any], width: int) -> None:
        tk = self.tk
        frame = tk.Frame(parent, background=CARD)
        frame.pack()
        var = tk.StringVar(value=text)
        entry = self._field(frame, var, width)
        entry.pack(side="left", ipady=4)
        if setting.unit:
            tk.Label(frame, text=setting.unit, background=CARD, foreground=MUTED, font=self.fonts["small"]).pack(
                side="left", padx=(6, 0)
            )
        state = {"saved": text}

        def done(_: Any = None) -> None:
            if var.get().strip() == state["saved"].strip():
                return
            if self.commit(setting, var.get()):
                state["saved"] = to_text(setting, self.values.get(setting.key))
                var.set(state["saved"])

        entry.bind("<Return>", done)
        entry.bind("<FocusOut>", done)
        row["flush"] = done
        row["type"] = lambda text: (var.set(text), done())
        row["set"] = lambda v: (var.set(to_text(setting, v)), state.update(saved=var.get()))

    def _secret(self, setting: Setting, parent: Any, value: str, row: dict[str, Any]) -> None:
        tk = self.tk
        frame = tk.Frame(parent, background=CARD)
        frame.pack()
        var = tk.StringVar(value=value)
        entry = self._field(frame, var, 28, show="\u2022")
        entry.pack(side="left", ipady=4)
        state = {"saved": value, "shown": False}
        eye = tk.Label(frame, text="\ue7b3" if self.glyphs else "show", background=CARD, foreground=MUTED,
                       font=self.fonts["glyph"], cursor="hand2")  # fmt: skip
        eye.pack(side="left", padx=(8, 0))
        dot = tk.Label(frame, text="", background=CARD, font=self.fonts["small"], width=8, anchor="w")
        dot.pack(side="left", padx=(6, 0))

        def paint() -> None:
            on = bool(state["saved"])
            dot.configure(text="\u25cf Set" if on else "\u25cb Not set", foreground=GREEN if on else FAINT)

        def reveal(_: Any = None) -> None:
            state["shown"] = not state["shown"]
            entry.configure(show="" if state["shown"] else "\u2022")
            if self.glyphs:
                eye.configure(text="\ued1a" if state["shown"] else "\ue7b3")
            else:
                eye.configure(text="hide" if state["shown"] else "show")
            eye.configure(foreground=ACCENT if state["shown"] else MUTED)

        def done(_: Any = None) -> None:
            if var.get() == state["saved"]:
                return
            if self.commit(setting, var.get()):
                from octavia import keys

                state["saved"] = keys.value(setting.env or "")
                var.set(state["saved"])
                paint()

        eye.bind("<Button-1>", reveal)
        entry.bind("<Return>", done)
        entry.bind("<FocusOut>", done)
        row["flush"] = done
        row["entry"] = entry
        row["reveal"] = reveal
        row["type"] = lambda text: (var.set(text), done())
        paint()

    def _hotkey(self, setting: Setting, parent: Any, value: str, row: dict[str, Any]) -> None:
        tk = self.tk
        var = tk.StringVar(value=value or "Off")
        box = tk.Label(parent, textvariable=var, width=16, background=FIELD, foreground=TEXT if value else MUTED,
                       font=self.fonts["body"], pady=5, cursor="hand2", highlightthickness=1,
                       highlightbackground=FIELD_EDGE, takefocus=1)  # fmt: skip
        box.pack()
        state = {"saved": value, "listening": False}

        def paint() -> None:
            var.set(_pretty(state["saved"]) or "Off")
            box.configure(foreground=TEXT if state["saved"] else MUTED, highlightbackground=FIELD_EDGE)

        def listen(_: Any = None) -> None:
            state["listening"] = True
            var.set("Press keys\u2026")
            box.configure(foreground=ACCENT, highlightbackground=ACCENT)
            box.focus_set()

        def key(event: Any) -> str | None:
            if not state["listening"]:
                return None
            name = (event.keysym or "").lower()
            if name == "escape":
                state["listening"] = False
                paint()
                return "break"
            combo = "" if name in ("backspace", "delete") else hotkey_of(event)
            if combo is None:
                return "break"
            state["listening"] = False
            if self.commit(setting, combo):
                state["saved"] = combo
            paint()
            return "break"

        box.bind("<Button-1>", listen)
        box.bind("<KeyPress>", key)
        box.bind("<FocusOut>", lambda _: (state.update(listening=False), paint()))
        row["set"] = lambda v: (state.update(saved=v or ""), paint())
        row["press"] = key
        row["listen"] = listen
        paint()

    # ---- saving

    def commit(self, setting: Setting, raw: Any) -> bool:
        """Check and keep a change. True once it's kept; False (the reason shown on the row) when it isn't."""
        try:
            value = parse(setting, raw)
        except ValueError as exc:
            return self._refused(setting, f"{setting.label} {exc}.")
        problem = save(self.config, self.path, setting, value)
        if problem:
            return self._refused(setting, problem)
        self.values[setting.key] = value
        row = self.rows.get(setting.key)
        if row is not None:
            row["error"].pack_forget()
        self._show_reset(setting)
        self._saved(setting)
        return True

    def _refused(self, setting: Setting, why: str) -> bool:
        row = self.rows.get(setting.key)
        if row is not None:
            row["error"].configure(text=why)
            row["error"].pack(anchor="w", pady=(4, 0))
        self.message.configure(text=why, foreground=RED)
        return False

    def _saved(self, setting: Setting) -> None:
        name = self.config.assistant.name
        if self.status is None:
            text, colour = f"Saved. {name} uses it when she next starts.", MUTED
        elif catalog.needs_restart(setting):
            text, colour = "Saved. It takes effect after a restart.", AMBER
        else:
            text, colour = f"Saved. {name} is using it now.", GREEN
        self.message.configure(text=text, foreground=colour)
        self._schedule(1200)  # her word on what waits, once she's seen the change

    def _reset(self, setting: Setting) -> None:
        problem = reset(self.config, setting)
        if problem:
            self._refused(setting, problem)
            return
        self.values[setting.key] = from_files(self.config.root, self.path, setting.key)
        row = self.rows.get(setting.key, {})
        if "set" in row:
            row["set"](self.values[setting.key])
        row.get("error") is not None and row["error"].pack_forget()
        self._show_reset(setting)
        self._saved(setting)

    # ---- her side: what waits for a restart

    def _schedule(self, ms: int) -> None:
        if self._poll is not None:
            with contextlib.suppress(Exception):
                self.root.after_cancel(self._poll)
        self._poll = self.root.after(ms, self.refresh_status)

    def refresh_status(self) -> None:
        from octavia.core.live_settings import read_status

        self.status = read_status(self.config.root)
        if self._restarting is not None:
            pid, asked = self._restarting
            if (self.status is not None and self.status.get("pid") != pid) or time.monotonic() - asked > 45:
                self._restarting = None  # she's back, or she didn't restart (a build she wouldn't cut off)
        self._paint_banner()
        self._schedule(self.poll_ms)

    def waiting(self) -> list[str]:
        """What she says waits for a restart, by the names the window shows."""
        out = []
        for key in (self.status or {}).get("pending") or []:
            setting = catalog.by_key(key)
            out.append(setting.label if setting is not None else key)
        return out

    def _paint_banner(self) -> None:
        name = self.config.assistant.name
        status = self.status
        waiting = self.waiting()
        look = (status is None, (status or {}).get("error"), tuple(waiting), (status or {}).get("can_restart"),
                self._restarting is not None)  # fmt: skip
        if look == self._banner_text:
            return  # unchanged: no flicker every poll
        self._banner_text = look
        for child in self.banner.winfo_children():
            child.destroy()
        if status is None:
            if self._restarting:
                self._strip(f"Restarting {name}\u2026", AMBER, "#1f1e33")
            else:
                self._strip(f"{name} isn't running. Your changes are kept, and she uses them when she starts.",
                            MUTED, "#191b20")  # fmt: skip
            return
        if status.get("error"):
            self._strip(f"Your settings files don't load, so she kept what she had: {status['error']}", RED,
                        "#2a1a1a")  # fmt: skip
            return
        if not waiting:
            return
        if self._restarting:
            self._strip(f"Restarting {name}\u2026", AMBER, "#1f1e33")
            return
        shown = ", ".join(waiting[:3]) + (f" and {len(waiting) - 3} more" if len(waiting) > 3 else "")
        many = len(waiting) != 1
        strip = self._strip(f"{len(waiting)} change{'s' if many else ''} {'wait' if many else 'waits'} for a "
                            f"restart: {shown}.", AMBER, "#1f1e33")  # fmt: skip
        if status.get("can_restart"):
            self._button(strip, "Restart now", self.restart, colour=AMBER).pack(side="right", padx=10, pady=6)
        else:
            self.tk.Label(strip, text=f"Quit {name} and start her again.", background="#1f1e33", foreground=MUTED,
                          font=self.fonts["small"]).pack(side="right", padx=12)  # fmt: skip

    def _strip(self, text: str, colour: str, background: str) -> Any:
        tk = self.tk
        strip = tk.Frame(self.banner, background=background, highlightthickness=1, highlightbackground=colour)
        strip.pack(fill="x")
        tk.Label(strip, text=text, background=background, foreground=colour, font=self.fonts["small"],
                 anchor="w", justify="left", wraplength=520).pack(side="left", padx=12, pady=9)  # fmt: skip
        return strip

    def restart(self) -> None:
        from octavia.core.live_settings import ask_restart

        if self.status is None:
            return
        try:
            pid = int(self.status["pid"])
            ask_restart(self.config.root, pid)
        except (OSError, ValueError, KeyError) as exc:
            self.message.configure(text=f"Couldn't ask her to restart: {exc}", foreground=RED)
            return
        self._restarting = (pid, time.monotonic())
        self._paint_banner()
        self.message.configure(text=f"Asked {self.config.assistant.name} to restart.", foreground=MUTED)


def _pretty(combo: str) -> str:
    """``ctrl+alt+j`` as Ctrl + Alt + J."""
    return " + ".join(part.capitalize() if len(part) > 1 else part.upper() for part in combo.split("+") if part)


def main(argv: list[str]) -> int:
    if sys.platform == "win32":
        import ctypes

        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
    import tkinter as tk

    from octavia.config import load_config

    positional = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] != "--section")]
    root_dir = Path(positional[0]) if positional else None
    path = positional[1] if len(positional) > 1 and positional[1] else None
    config = load_config(path, root=root_dir)

    start = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--section"), None)
    if "--classic" not in argv and open_window(config, path, start=start):
        return 0
    root = tk.Tk()
    SettingsWindow(root, config, path)
    root.lift()
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
