"""``octavia welcome``: the first-run setup wizard (the public build's installer runs it; anyone can run it again).

It asks, one step at a time, and writes what was chosen to ``config/local.yaml`` (choices) and ``.env`` (keys):

1. How she thinks: your own Claude account (signs in through the browser), an Anthropic API key, or another model
   when this build has a backend for one.
2. Microphone and speakers.
3. Her voice: Fish Audio with your key (the public default voice and the "console" effect), Windows' own voice, or
   the GPU voice when it's installed.
4. Her name, the wake word, what she calls you and her persona file.
5. Home: your town (looked up once, for the weather, directions and maps).
6. Optional connections: Discord (your own bot, step by step), TickTick, Canvas, Gmail, calendars.
7. A hardware check: what this PC can run.

Every question has a default (Enter keeps it), and nothing is written until the end, after a summary and a yes. The
console is behind ``Prompter``, so the tests drive it with scripted answers.
"""

from __future__ import annotations

import copy
import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from octavia.config import AppConfig, deep_merge

PUBLIC_VOICE = "f88993cf418c4fb798f1551bc51f0ef8"  # Fish Audio's public "Expressive Female Voice"
DONE_MARK = Path("data") / "welcome.done"

DISCORD_STEPS = """\
Your own Discord bot, so you can text and call it from your phone:
  1. Open https://discord.com/developers/applications and click "New Application" (any name, e.g. OCTAVIA).
  2. Bot (left menu): "Reset Token", copy it. Under "Privileged Gateway Intents" turn on "Message Content Intent".
  3. OAuth2, URL Generator: tick "bot", then "Send Messages", "Read Message History", "Attach Files", "Connect" and
     "Speak". Open the URL it makes and add the bot to a server of yours (calls happen in its voice channels).
  4. Your own user ID: Discord settings, Advanced, turn on Developer Mode; then right-click your name, "Copy User ID".
It answers only that account: everyone else's messages are ignored and on a call only your voice is decoded."""


class Prompter(Protocol):
    def say(self, text: str) -> None: ...
    def ask(self, question: str, default: str = "") -> str: ...
    def secret(self, question: str) -> str: ...


class ConsolePrompter:
    def say(self, text: str) -> None:
        print(text)

    def ask(self, question: str, default: str = "") -> str:
        shown = f" [{default}]" if default else ""
        try:
            answer = input(f"{question}{shown}: ").strip()
        except EOFError:
            answer = ""
        return answer or default

    def secret(self, question: str) -> str:
        import getpass

        try:
            return getpass.getpass(f"{question} (hidden, Enter to skip): ").strip()
        except EOFError:
            return ""


@dataclass
class Choices:
    """What the wizard will write."""

    config: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # said at the end

    def set(self, dotted: str, value: Any) -> None:
        node = self.config
        *parents, last = dotted.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = value


@dataclass
class Services:
    """The outside world, replaceable in tests."""

    devices: Callable[[], list[dict[str, Any]]]
    geocode: Callable[[str], tuple[str, float, float] | None]
    login: Callable[[AppConfig], int]
    hardware: Callable[[], dict[str, Any]]
    providers: Callable[[], tuple[str, ...]]
    places: Callable[[str], list[tuple[str, float, float]]] | None = None  # a few matches to choose from


# ------------------------------------------------------------------------------------------------ helpers
def _yes(answer: str) -> bool:
    return answer.strip().lower() in {"y", "yes", "yeah", "yep", "sure", "ok"}


def _pick(p: Prompter, question: str, options: list[str], default: int = 1) -> int:
    """A numbered choice: the 1-based number picked (``default`` on Enter or anything unreadable)."""
    for i, option in enumerate(options, 1):
        p.say(f"  {i}. {option}")
    answer = p.ask(question, str(default))
    try:
        n = int(answer)
    except ValueError:
        return default
    return n if 1 <= n <= len(options) else default


def _list_devices() -> list[dict[str, Any]]:
    import sounddevice as sd

    apis = [str(api.get("name", "")) for api in sd.query_hostapis()]
    out = []
    for d in sd.query_devices():
        row = dict(d)
        index = row.get("hostapi")
        row["hostapi_name"] = apis[index] if isinstance(index, int) and 0 <= index < len(apis) else ""
        out.append(row)
    return out


# Canada's provinces and territories by their postal abbreviations (the US states are the weather tool's).
_PROVINCES = {
    "ab": "alberta", "bc": "british columbia", "mb": "manitoba", "nb": "new brunswick",
    "nl": "newfoundland and labrador", "ns": "nova scotia", "nt": "northwest territories", "nu": "nunavut",
    "on": "ontario", "pe": "prince edward island", "qc": "quebec", "sk": "saskatchewan", "yt": "yukon",
}  # fmt: skip


def _region_fits(result: dict[str, Any], qualifier: str) -> bool:
    """Whether a result fits one part after the town's name: a state, province, region or country, written out or
    abbreviated ("NC", "ON", "UK", "USA")."""
    from octavia.tools.extensions.weather import _COUNTRY_ALIASES, _US_STATES

    q = qualifier.lower().replace(".", "").strip()
    wanted = {q, _US_STATES.get(q, q), _PROVINCES.get(q, q), _COUNTRY_ALIASES.get(q, q)}
    fields = [result.get(k) for k in ("admin1", "admin2", "country", "country_code")]
    have = {f.lower() for f in fields if isinstance(f, str)}
    return bool(wanted & have) or any(len(w) > 3 and any(w in h for h in have) for w in wanted)


def _split_place(place: str) -> tuple[str, list[str]]:
    """The town and what narrows it: "Boone, NC" and "Boone NC" both give ("Boone", ["NC"])."""
    from octavia.tools.extensions.weather import _COUNTRY_ALIASES, _US_STATES

    parts = [p.strip() for p in place.split(",") if p.strip()]
    if not parts:
        return "", []
    name, rest = parts[0], parts[1:]
    words = name.split()
    if not rest and len(words) > 1:
        last = words[-1].lower().strip(".")
        if last in _US_STATES or last in _PROVINCES or last in _COUNTRY_ALIASES or len(last) == 2:
            name, rest = " ".join(words[:-1]), [words[-1]]
    return name, rest


def find_places(place: str, limit: int = 5) -> list[tuple[str, float, float]]:
    """Towns matching what was typed, best first: (label, latitude, longitude). [] when none or offline."""
    import httpx

    from octavia.net import tls_context

    name, rest = _split_place(place)
    if not name:
        return []
    try:
        r = httpx.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 20, "language": "en"},
            timeout=10,
            verify=tls_context(),
        )
        r.raise_for_status()
        results = r.json().get("results") or []
    except Exception:
        return []
    fitting = [res for res in results if all(_region_fits(res, q) for q in rest)]
    out = []
    for res in fitting[:limit]:
        region = res.get("admin1") or ""
        country = res.get("country") or ""
        label = ", ".join(x for x in (res.get("name"), region, country if res.get("country_code") != "US" else "") if x)
        out.append((label, float(res["latitude"]), float(res["longitude"])))
    return out


def _geocode(place: str) -> tuple[str, float, float] | None:
    found = find_places(place, limit=1)
    return found[0] if found else None


def _hardware() -> dict[str, Any]:
    info: dict[str, Any] = {"cpus": os.cpu_count() or 0, "system": f"{platform.system()} {platform.release()}"}
    try:
        import psutil

        info["ram_gb"] = round(psutil.virtual_memory().total / 2**30)
    except Exception:
        info["ram_gb"] = 0
    gpus: list[str] = []
    if platform.system() == "Windows":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_VideoController).Name"],
                capture_output=True, text=True, timeout=20, check=False,
            ).stdout  # fmt: skip
            gpus = [line.strip() for line in out.splitlines() if line.strip()]
        except (OSError, subprocess.SubprocessError):
            pass
    info["gpus"] = gpus
    info["disk_free_gb"] = round(shutil.disk_usage(Path.cwd()).free / 2**30)
    return info


def _providers() -> tuple[str, ...]:
    """The brain backends this build has (``brain.provider``)."""
    from typing import get_args

    from octavia.config import BrainConfig

    annotation = BrainConfig.model_fields["provider"].annotation
    return tuple(get_args(annotation)) or ("claude",)


def default_services() -> Services:
    from octavia.brain.claude_auth import login

    return Services(_list_devices, _geocode, login, _hardware, _providers, find_places)


# ------------------------------------------------------------------------------------------------ the steps
def step_brain(p: Prompter, s: Services, c: Choices) -> None:
    p.say("\n1. How it thinks")
    options = [
        "My Claude account, Pro or Max (recommended: a browser opens to sign in)",
        "An Anthropic API key (billed per use)",
    ]
    others = [x for x in s.providers() if x not in ("claude",)]
    if others:
        options.append(f"Another model ({', '.join(others)}): set it up in config/local.yaml, see docs")
    n = _pick(p, "Pick one", options)
    if n == 1:
        c.set("brain.claude.auth", "subscription")
    elif n == 2:
        key = p.secret("Your Anthropic API key (console.anthropic.com, API Keys)")
        c.set("brain.claude.auth", "api_key")
        if key:
            c.env["ANTHROPIC_API_KEY"] = key
        else:
            c.notes.append("Claude: add ANTHROPIC_API_KEY to .env before starting it.")
    elif "openai_compatible" in others:
        step_other_model(p, c)
    else:
        c.notes.append(f"Brain: set brain.provider to one of {', '.join(others)} in config/local.yaml.")


# Servers that speak OpenAI's chat API: (shown as, base URL, the .env variable for its key or "" for none, a model
# to suggest). The model names are only suggestions: whatever the server lists works.
OTHER_MODELS = (
    ("OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-5.1"),
    ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-2.5-pro"),
    ("xAI Grok", "https://api.x.ai/v1", "XAI_API_KEY", "grok-4"),
    ("Mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", "mistral-large-latest"),
    ("DeepSeek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
    ("Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", ""),
    ("OpenRouter (many models, one key)", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", ""),
    ("Ollama on this PC (no key)", "http://localhost:11434/v1", "", ""),
    ("LM Studio on this PC (no key)", "http://localhost:1234/v1", "", ""),
    ("Another server (its base URL)", "", "OPENAI_API_KEY", ""),
)


def step_other_model(p: Prompter, c: Choices) -> None:
    """Its brain on another model, through any OpenAI-compatible API (``brain.openai_compatible``)."""
    n = _pick(p, "Which one", [name for name, *_ in OTHER_MODELS])
    name, url, key_env, model = OTHER_MODELS[n - 1]
    if not url:
        url = p.ask("Its base URL (ending in /v1 for most)", "http://localhost:8080/v1")
    model = p.ask("The model's name there", model)
    c.set("brain.provider", "openai_compatible")
    c.set("brain.openai_compatible.base_url", url)
    c.set("brain.openai_compatible.api_key_env", key_env)
    if model:
        c.set("brain.openai_compatible.model", model)
    else:
        c.notes.append("Brain: set brain.openai_compatible.model in config/local.yaml to the model's name.")
    if key_env:
        key = p.secret(f"Your {name} API key")
        if key:
            c.env[key_env] = key
        else:
            c.notes.append(f"Brain: add {key_env} to .env before starting it.")
    c.notes.append(
        "Brain: on a model other than Claude, its web search, self-improvement builds and objectives are off, "
        "and how well it does depends on the model: `python -m octavia.evolve.evals run` measures it."
    )


# Windows lists every device once per audio system, MME's names cut at 31 characters, plus stand-ins for "whatever
# the default is": only WASAPI's (the one she uses, full names) are shown, without the stand-ins.
_NOT_DEVICES = re.compile(r"sound mapper|primary sound (capture )?driver", re.IGNORECASE)


def _device_names(devices: list[dict[str, Any]], kind: str) -> list[str]:
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    usable = [d for d in devices if d.get(key, 0) > 0 and str(d.get("name", "")).strip()]
    wasapi = [d for d in usable if "wasapi" in str(d.get("hostapi_name", "")).lower()]
    names: list[str] = []
    for d in wasapi or usable:
        name = " ".join(str(d["name"]).split())
        if _NOT_DEVICES.search(name) or name in names:
            continue
        names.append(name)
    # What's left of a cut name next to its full one (no WASAPI list to choose from): the full one only.
    return [n for n in names if not any(other != n and other.startswith(n) for other in names)]


def step_audio(p: Prompter, s: Services, c: Choices) -> None:
    p.say("\n2. Microphone and speakers")
    try:
        devices = s.devices()
    except Exception as exc:  # no audio library, no devices
        p.say(f"  Couldn't list the audio devices ({type(exc).__name__}): it'll use Windows' defaults.")
        return
    for kind, key, label in (
        ("input", "audio.input_device", "Microphone"),
        ("output", "audio.output_device", "Speakers"),
    ):
        names = _device_names(devices, kind)
        if not names:
            continue
        n = _pick(p, f"{label} (Enter: Windows' default)", ["Windows' default", *names[:20]])
        if n > 1:
            c.set(key, names[n - 2])
    headphones = p.ask("Do you use headphones? (lets you interrupt it just by talking) y/n", "y")
    c.set("audio.headphones", _yes(headphones))
    c.notes.append("Audio: `uv run octavia listen-test` shows what it hears.")


def step_voice(p: Prompter, s: Services, c: Choices, gpu_voice: bool) -> None:
    p.say("\n3. Its voice")
    options = [
        "Fish Audio with my own key (best: its default voice, free tier available at fish.audio)",
        "Windows' built-in voice (no key, more robotic)",
    ]
    if gpu_voice:
        options.append("The voice made on my GPU (local, no key)")
    n = _pick(p, "Pick one", options)
    if n == 1:
        key = p.secret("Your Fish Audio API key (fish.audio, API Keys)")
        if key:
            c.env["FISH_AUDIO_API_KEY"] = key
        else:
            c.notes.append("Voice: add FISH_AUDIO_API_KEY to .env; until then Windows' voice speaks.")
        c.set("tts.provider", "fish_audio")
        from octavia.tts import presets

        n = _pick(p, "Which voice", [f"{v.name}: {v.about}" for v in presets.PRESETS], 1)
        c.set("tts.fish_audio.reference_id", presets.PRESETS[n - 1].voice_id)
        c.set("tts.fallback", "windows")
        effect = _pick(p, "Voice effect", ["console (its default)", "none", "radio", "room"], 1)
        c.set("tts.voice_effect", ["console", "off", "radio", "room"][effect - 1])
    elif n == 2:
        c.set("tts.provider", "fish_audio")
        c.set("tts.fallback", "windows")
        c.set("tts.fallback_after_s", 0.5)
        c.notes.append("Voice: Windows speaks. Add a Fish Audio key to .env any time for its real voice.")
    else:
        c.set("tts.provider", "local")


def step_identity(p: Prompter, c: Choices) -> None:
    p.say("\n4. Who it is")
    name = p.ask("Its name", "Octavia")
    c.set("assistant.name", name)
    c.set("assistant.wake_phrase", p.ask("The word that wakes it", name.lower()))
    c.set("assistant.user_name", p.ask("What it calls you (Enter: nothing in particular)", ""))
    p.say("  Its personality is in personality/persona.md: rewrite it any time, it's plain text.")
    persona = p.ask("Persona file", "personality/persona.md")
    c.set("assistant.personality_file", persona)


def step_home(p: Prompter, s: Services, c: Choices) -> None:
    p.say("\n5. Home")
    place = p.ask("Your town, e.g. 'Boone, North Carolina' (Enter to skip: it'll ask when it matters)", "")
    if not place:
        return
    found = s.geocode(place)
    if found is None:
        p.say("  Couldn't look it up, so it's kept as typed.")
        c.set("assistant.home", place)
        return
    label, lat, lon = found
    if _yes(p.ask(f"  Found {label} ({lat:.3f}, {lon:.3f}). Right? y/n", "y")):
        c.set("assistant.home", place)
        c.set("assistant.home_latitude", round(lat, 4))
        c.set("assistant.home_longitude", round(lon, 4))
    else:
        c.set("assistant.home", place)


def step_integrations(p: Prompter, c: Choices) -> None:
    p.say("\n6. Optional connections (Enter skips each one; `uv run octavia settings` has them all later)")
    if _yes(p.ask("Discord: text and call it from your phone? y/n", "n")):
        p.say(DISCORD_STEPS)
        token = p.secret("The bot's token")
        owner = p.ask("Your Discord user ID (digits)", "")
        if token:
            c.env["DISCORD_BOT_TOKEN"] = token
        if owner.isdigit():
            c.env["DISCORD_OWNER_ID"] = owner
        if token and owner.isdigit():
            c.set("discord_bridge.enabled", True)
        else:
            c.notes.append("Discord: needs both the token and your user ID; run `uv run octavia welcome` again.")
    if _yes(p.ask("TickTick (your to-dos)? y/n", "n")):
        if token := p.secret("TickTick API token (TickTick on the web: Settings, Account & Security, API Token)"):
            c.env["TICKTICK_API_TOKEN"] = token
    if _yes(p.ask("Canvas (coursework and grades)? y/n", "n")):
        url = p.ask("Your school's Canvas address, e.g. https://yourschool.instructure.com", "")
        token = p.secret("Canvas access token (Canvas: Account, Settings, '+ New Access Token')")
        if url:
            c.env["CANVAS_BASE_URL"] = url
        if token:
            c.env["CANVAS_API_TOKEN"] = token
    if _yes(p.ask("Gmail (new-mail alerts, reading your inbox)? y/n", "n")):
        if address := p.ask("Your Gmail address", ""):
            c.env["GMAIL_ADDRESS"] = address
        if password := p.secret("A Gmail app password (myaccount.google.com/apppasswords)"):
            c.env["GMAIL_APP_PASSWORD"] = password
    if _yes(p.ask("Calendars (private iCal addresses)? y/n", "n")):
        if urls := p.ask("The addresses, separated by spaces", ""):
            c.env["CALENDAR_ICS_URLS"] = urls
    p.say(
        "  The ETHER (Entangled Thought Harmonics and Emergent Resonance), where every OCTAVIA shares what it learns:\n"
        "  fixes its self-improvement makes, short lessons and requests it got wrong can go to the\n"
        "  maintainer, so every OCTAVIA gets better from each other's. Each is checked for anything personal first\n"
        "  (kept back if it holds any), and a person reviews it before anyone gets it. Updates come signed either way.\n"
        "  Sharing means the maintainer may use, change, include and relicense what you send in OCTAVIA (README, License)."
    )
    if _yes(p.ask("Share what it learns with the ETHER? y/n", "n")):
        c.set("hive.share", "on")


def step_hardware(p: Prompter, s: Services, c: Choices) -> bool:
    """Says what this PC can run; True when a GPU voice is worth offering."""
    p.say("\n7. This PC")
    try:
        hw = s.hardware()
    except Exception:
        hw = {}
    gpus = hw.get("gpus") or []
    p.say(f"  {hw.get('system', '?')}, {hw.get('cpus', '?')} CPU threads, {hw.get('ram_gb', '?')} GB RAM, "
          f"{hw.get('disk_free_gb', '?')} GB free here")  # fmt: skip
    p.say(f"  Graphics: {', '.join(gpus) if gpus else 'none found'}")
    strong = [g for g in gpus if any(w in g.lower() for w in ("nvidia", "geforce", "rtx", "radeon rx", "arc a"))]
    if hw.get("ram_gb", 0) and hw["ram_gb"] < 8:
        c.notes.append("This PC has under 8 GB of RAM: turn off screen history and the local model if it's slow.")
    if strong:
        p.say("  A dedicated GPU: the local voice and a local model are possible (docs/local-brain.md).")
    else:
        p.say("  No dedicated GPU: everything still works, with the cloud voice and model.")
    return bool(strong)


# ------------------------------------------------------------------------------------------------ writing
def write(root: Path, choices: Choices) -> list[str]:
    """Writes the choices; returns what was written."""
    from octavia import keys

    written = []
    from octavia.core.userdir import state_root

    home = state_root(root)
    local = home / "config" / "local.yaml"
    existing: dict[str, Any] = {}
    if local.exists():
        existing = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
        backup = local.with_name("local.yaml.bak")
        shutil.copyfile(local, backup)
        written.append(f"{backup} (your old settings, as they were)")
    merged = deep_merge(existing, copy.deepcopy(choices.config))
    local.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Your settings (from `octavia welcome`; edit freely, or use `uv run octavia settings`).\n"
        "# Every option and its default is in config/default.yaml. Keys and tokens go in .env, never here.\n\n"
    )
    local.write_text(header + yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8")
    written.append("config/local.yaml")
    env = home / ".env"
    if choices.env and not env.exists() and (root / ".env.example").exists():
        shutil.copyfile(root / ".env.example", env)
    for name, value in choices.env.items():
        keys.set_value(env, name, value)
    if choices.env:
        written.append(f".env ({', '.join(sorted(choices.env))})")
    mark = root / DONE_MARK
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text("1\n", encoding="utf-8")
    return written


def needed(root: Path) -> bool:
    """Whether the first start should run the wizard: nothing set up here yet."""
    from octavia.core.userdir import state_root

    home = state_root(root)
    return not (root / DONE_MARK).exists() and not (home / "config" / "local.yaml").exists()


def run(root: Path, prompter: Prompter | None = None, services: Services | None = None) -> int:
    from octavia.config import load_config

    p = prompter or ConsolePrompter()
    s = services or default_services()
    p.say("Hi! Let's set OCTAVIA up. Enter keeps the suggestion in [brackets]; Ctrl+C stops without saving.")
    c = Choices()
    step_brain(p, s, c)
    step_audio(p, s, c)
    gpu = step_hardware(p, s, c)
    step_voice(p, s, c, gpu_voice=gpu and _local_voice_installed(root))
    step_identity(p, c)
    step_home(p, s, c)
    step_integrations(p, c)
    p.say("\nReady to save:")
    for line in yaml.safe_dump(c.config, sort_keys=False).splitlines():
        p.say(f"  {line}")
    if c.env:
        p.say(f"  .env: {', '.join(sorted(c.env))} (values not shown)")
    if not _yes(p.ask("Save these? y/n", "y")):
        p.say("Nothing was saved.")
        return 1
    for item in write(root, c):
        p.say(f"  wrote {item}")
    if c.config.get("brain", {}).get("claude", {}).get("auth") == "subscription":
        p.say("\nSigning in to Claude: a browser window opens.")
        try:
            config = load_config(root=root)
        except Exception as exc:
            p.say(f"  Couldn't read the settings back ({exc}); run `uv run octavia login` after fixing them.")
        else:
            if s.login(config) != 0:
                c.notes.append("Claude isn't signed in yet: run `uv run octavia login`.")
    p.say("")
    for note in c.notes:
        p.say(f"- {note}")
    p.say("\nAll set. Start it with: uv run octavia   (and say its name)")
    return 0


def _local_voice_installed(root: Path) -> bool:
    return bool(os.environ.get("OCTAVIA_LOCAL_TTS")) or (root / "models" / "local-tts").exists()
