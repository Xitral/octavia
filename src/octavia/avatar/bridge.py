"""Octavia's side of the desktop avatar: starts the avatar app and keeps it in step with the assistant.

The app (``avatar/``, Electron) draws the character. This module starts it as a child process and talks to it over
its stdin and stdout, one JSON object per line (docs/avatar-protocol.md), so there's no network port to secure:

* what the assistant is doing (``state``), and when the character should pop in and out (``show``/``hide``: in
  ``conversation`` mode it comes when you talk to it and leaves as soon as the assistant is back to waiting for the
  wake word, or once it has been idle ``hide_after_s`` otherwise);
* out of the way when the user asks (``send_away``, from the hide_avatar tool): hidden in either mode, and nothing
  brings her back, not talking nor pointing, until they ask (``come_back``) or the time they gave is up;
* the mood of each sentence the moment it starts playing (``segment``, from ``emotion.mood_for``);
* a camera pose and flash the moment the assistant takes a screenshot (``snapshot``);
* what she's busy with (``activity``): ``search``, a magnifying glass up to her eye, while she searches or looks
  something up (``is_search``: the screen, the web, her memory, the user's files or calendar) and while the brain
  studies a screenshot; ``tasks``, reading a clipboard, while one of TickTick's tools runs; ``web``, spinning a
  globe, while she searches or reads the web; ``files``, flipping through a folder, while she looks through the
  user's files. It ends when she starts talking (a line she queued before the call came back doesn't count), when
  she asks for a yes or the turn ends, when she points, or after ``ACTIVITY_MAX_S``. ``morning``, a mug of hot
  chocolate for the morning rundown (its announcement's cue), lingers after the talking, sips and all, until the
  next turn, a hide, or ``ACTIVITY_MAX_S``;
* one-shot moments with a prop (``moment``, ``moment_for``): ticking a task off on her clipboard when one is
  completed, clicking a stopwatch on when a timer (or a focus session) is set, holding it up ringing when a timer
  goes off and ringing a bell before a reminder (from the announcement's cue, ``AnnouncementStarted``), and a sonic
  rainboom when something big is done (an improvement installed, or a turn that got ``RAINBOOM_ACTIONS`` or more
  things done with nothing failing; at most once every ``RAINBOOM_COOLDOWN_S``, and ``avatar.rainboom`` turns it
  off); a wrench twirled as a build of hers starts; headphones for a beat when she starts the user's music; an
  umbrella or sunglasses as she tells them the weather (``weather_moment``: rain or snow now, or a clear day); and a
  megaphone for a shouted sentence (``emotion.shouts``);
* a victory lap round the screen (``lap``) when the user ticks off the last task due today (``TasksCleared``),
  while she's there and not pointing, unless ``avatar.victory_lap`` is off;
* a jab at the spot she points at as the assistant clicks it (``press``, from the click tool);
* props she makes out of simple shapes, built by the app as they're made or thrown away (``prop-define``,
  ``prop-remove``, from the avatar_props tools), and the prop the user asked her to hold (``prop-hold``), told again
  to an app that restarts. Unless the user asked her to keep holding it, she puts it away (still saved) once the
  conversation is over, when a later request calls any other tool, or after ``PROP_IDLE_S`` with no talk of it;
* a glance at the window the user switches to (``glance``, the middle of its frame) and a jump (``startle``) at a
  big window popping up by itself while the assistant is idle, or at a bang in the room (``LoudNoise``). While she's
  on screen, a look at the window in front every ``FRONT_POLL_S`` (``foreground.ForegroundWatch``); not at her own
  windows, nor while she points; ``avatar.glance`` and ``avatar.startle`` turn them off;
* sitting on top of the window in front (``perch``, its frame) in ``always`` mode while a build of hers runs and the
  assistant is idle, when it has ``PERCH_ROOM_PX`` above it; home again (``unperch``) when the build ends, the
  assistant does anything, or that window stops being in front or moves; then not again for ``PERCH_REST_S``, unless
  ``avatar.perch`` is off;
* impatience (the ``impatient`` gesture: arms crossed, tapping) when a call she's waiting on runs longer than
  ``IMPATIENT_S`` with nothing in her hands to show for it;
* a mood that carries over (``mood``): thanked or praised she stays a little happy for ``MOOD_S`` after the turn,
  teased she stays smug (``emotion.user_mood_for``, from what the user said);
* flying over to something on the screen and pointing at it (``point``, from the point_at_screen and
  point_sequence tools through ``point()``), and back home (``unpoint``) once the conversation moves on: the turn's
  reply is over and ``avatar.point_linger_s`` more have passed (``point-ending`` tells the app, so the ring can count
  down), the user wakes her or cuts her off for a new request, a new turn starts, any tool other than those two is
  called (a click, a key, a game control), or ``POINT_MAX_S`` passed. A hide ends pointing in the app by itself;
* with ``avatar.form: orb`` (a glowing orb instead of the character, no model needed), whether any of the turn's
  tools is running (``working``), for the orb's working look;
* lip sync (``mouth``, about 50 a second) from the audio exactly as it's handed to the speaker. The player's render
  tap only queues each block; a worker thread analyses it (``lipsync.VisemeAnalyzer``), so the audio thread never
  waits on the analysis.

The config file the app starts with also lists the props she can hold (``avatar.props_dir`` plus the built-ins,
``scan_props``), which one each activity uses, and how she floats and naps (``avatar.hover``,
``avatar.nap_after_min``). Clicking the character starts listening, like the wake word. While the pointer rests on
her she fades and lets clicks through; the app says so (``hover``), and until it leaves, Ctrl is looked at every
``CTRL_POLL_S`` and the app told when it goes down or up (``keys``), so holding Ctrl lets her take the click (or a
drag) again. The app is restarted if it crashes, a few times; the assistant works the same without it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import queue
import re
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from octavia.avatar import foreground
from octavia.avatar.emotion import mood_for, shouts, user_mood_for
from octavia.avatar.lipsync import VisemeAnalyzer
from octavia.avatar.subtitles import subtitle
from octavia.config import AppConfig, AvatarConfig
from octavia.core.events import (
    AnnouncementStarted,
    AssistantState,
    BrainStarted,
    CameraShutter,
    ErrorOccurred,
    Event,
    EventBus,
    FastChatStarted,
    ImprovementChanged,
    Interrupted,
    LoudNoise,
    Notice,
    PhrasePlaying,
    PresetReplied,
    ScreenshotTaken,
    SpeakingFinished,
    SpeakingStarted,
    SpeechSegmentAudio,
    SpeechSegmentStarted,
    SpeechWordStarted,
    StateChanged,
    TasksCleared,
    ToolCallFinished,
    ToolCallStarted,
    Transcript,
    WakeDetected,
    WakeListening,
)
from octavia.tools.extensions.ticktick import PREFIX as TICKTICK
from octavia.tools.extensions.ticktick import categorize

log = logging.getLogger(__name__)

NOTICE_SOURCE = "AVATAR"
# How long the shutter sound waits for her camera's flash before it plays anyway (she was flying somewhere, say).
SNAPSHOT_WAIT_S = 1.2
APP_DIR = "avatar"  # the Electron app, at the project root
STATE_FILE = "data/avatar/window.json"  # where the app keeps its position after you drag it
# The orb's own: the character's spot is for a tall window that may hang off the screen on purpose, which put the
# orb two thirds down the left edge. With none saved, the orb starts in the bottom right corner.
ORB_STATE_FILE = "data/avatar/window-orb.json"


def state_file(settings: AvatarConfig) -> str:
    """Where the app keeps the window's position for this form (each form remembers its own)."""
    return ORB_STATE_FILE if settings.form == "orb" else STATE_FILE


CONFIG_FILE = "data/avatar/config.json"  # what the app is started with
_BUSY = frozenset(
    {
        AssistantState.LISTENING,
        AssistantState.TRANSCRIBING,
        AssistantState.THINKING,
        AssistantState.SPEAKING,
        AssistantState.CONFIRMING,
    }
)
_TURN = frozenset({AssistantState.THINKING, AssistantState.SPEAKING, AssistantState.CONFIRMING})  # a turn runs
_RESTARTS = 5  # crashes (each within a minute of starting) before giving up
_RESTART_DELAY_S = 2.0
_CLOSE_WAIT_S = 3.0
_QUEUE_BLOCKS = 200  # about 2 s of audio: past that the analysis is behind, and blocks are dropped
_PHRASE_TAIL_S = 0.3  # lip sync stays open this much past a phrase's length (output latency, the last frame)
POINT_MAX_S = 90.0  # she stops pointing after this long even if nothing else ends it (always mode never hides)
ACTIVITY_MAX_S = 20.0  # an activity she's told about ends by itself after this long
RAINBOOM_ACTIONS = 5  # things done in one turn, none failing, that make it big enough for a sonic rainboom
RAINBOOM_COOLDOWN_S = 15 * 60.0  # rare stays special
_RAINBOOM_PENDING_S = 120.0  # an improvement installed while she's away still gets its rainboom when she's back
FRONT_POLL_S = 0.4  # how often she looks at the window in front, while she's on screen
GLANCE_GAP_S = 1.2  # at most one glance this often: flicking through windows isn't a glance at each
STARTLE_GAP_S = 20.0  # she jumps at most this often
PERCH_ROOM_PX = 220  # she sits on a window only with this much room above it on its monitor (physical px)...
PERCH_WIDTH_PX = 360  # ...and when it's this wide
PERCH_REST_S = 60.0  # after she leaves a window she sat on, the next is this long off at least
CTRL_POLL_S = 0.05  # how often Ctrl is looked at, only while the pointer rests on her
BUILD_MAX_S = 45 * 60.0  # a build she hasn't heard the end of by then is taken to be over
IMPATIENT_S = 8.0  # a call she's waiting on runs this long with nothing to show for it: she gets impatient
MOOD_S = 60.0  # how long a mood from the user's words lasts after the turn
PROP_IDLE_S = 90.0  # a prop she wasn't asked to keep holding is put away after this long with no talk of it
# The avatar_props tools (by name: that module builds on this one). A call of one is talk of her prop.
_PROP_TOOLS = frozenset({"make_avatar_prop", "hold_avatar_prop", "list_avatar_props", "remove_avatar_prop"})
_LABEL_CHARS = 60  # a point's label ("the apple") is cut to this
# The tools that mean she's searching or looking something up, so she holds up her magnifying glass ("search"). The
# brain studies a screenshot once look_at_screen has taken it (taking it is the camera's), so that one starts
# "search" when it finished; the rest start it the moment they do: looking closer at the screenshot, the screen's
# labels, the web (Claude's own WebSearch and WebFetch too), her memory, the user's files and calendar.
_SEARCH_AFTER = frozenset({"look_at_screen"})
_SEARCH_DURING = frozenset(
    {
        "zoom_screen",
        "find_on_screen",
        "WebSearch",
        "WebFetch",
        "open_web_search",
        "show_image",
        "recall",
        "find_files",
        "open_file",
        "get_calendar_events",
    }
)
# Any other tool, a new one or another of Claude's own, is a search when its name says so, anywhere in it and in any
# case: "search_notes", "lookupWord", "fetch_page". Except Claude Code's ToolSearch, should the CLI ever use it: that
# loads the descriptions of her own tools, it doesn't look anything up for the user.
_SEARCH_NAME = re.compile(r"search|find|look[\s_-]*up|recall|fetch|browse|query", re.IGNORECASE)
_NOT_SEARCH = _SEARCH_AFTER | {"ToolSearch"}
# The tools that point (through point()). A call of any other tool means the user has moved on, so she stops pointing.
_POINTING_TOOLS = frozenset({"point_at_screen", "point_sequence"})
_MORE = TICKTICK + "more"  # TickTick's less-used tools, through one: its `tool` argument names which
# TickTick's tools (without the prefix) that tick something off, and the one that starts a focus session.
_TICKS = frozenset({"complete_task", "complete_tasks_in_project", "complete_checklist_item", "upsert_habit_checkins"})
_FOCUS = frozenset({"create_focus"})
_TIMERS = frozenset({"set_timer"})
# The web and the user's files have props of their own (a globe, a folder) instead of the magnifier.
_WEB_TOOLS = frozenset({"WebSearch", "WebFetch", "open_web_search", "show_image", "show_page"})
_FILE_TOOLS = frozenset({"find_files", "open_file"})
# Activities that outlast her talking and the turn's end (the morning rundown's mug): only a new turn, a hide, pointing
# or ACTIVITY_MAX_S ends them.
_LINGER = frozenset({"morning"})
# Starting the user's music: Spotify, or media_control resuming or changing tracks.
_MUSIC_ACTIONS = frozenset({"play", "play_pause", "next", "previous"})
# The weather tool's "Now (7:45 AM): 62°F ..., light rain, ..." line, and what she holds up for it.
_WEATHER = "get_weather"
_NOW = re.compile(r"Now \((\d{1,2}):\d{2}\s*([AP]M)\):([^.]*)", re.IGNORECASE)
_WET = re.compile(r"\b(?:rain|drizzle|showers?|thunderstorms?|snow|hail)\b", re.IGNORECASE)
_SUNNY = re.compile(r"\b(?:clear|mostly clear|sunny)\b", re.IGNORECASE)
_DAYLIGHT = range(7, 19)  # sunglasses from 7 in the morning to 7 at night
# What an announcement's cue (AnnouncementStarted) plays: a ringing stopwatch for a timer, a bell for a reminder.
_CUE_MOMENTS = {"timer": "timer_done", "reminder": "bell"}
# A tool whose name says it only looks something up doesn't get anything done (for the rainboom).
_READ_NAME = re.compile(r"^(?:get|list|read|see|check|show|describe|identify)_", re.IGNORECASE)


def is_search(tool: str) -> bool:
    """Whether a call of ``tool`` means she's searching, so she holds her magnifying glass up from its start.
    TickTick's tools don't: they have her clipboard (``activity_for``)."""
    if tool.startswith(TICKTICK):
        return False
    return tool not in _NOT_SEARCH and (tool in _SEARCH_DURING or _SEARCH_NAME.search(tool) is not None)


def activity_for(tool: str) -> str | None:
    """What she holds up while a call of ``tool`` runs: her clipboard (``tasks``) for TickTick, a globe (``web``) for
    the web, a folder (``files``) for the user's files, her magnifying glass (``search``) for any other search, or
    nothing."""
    if tool.startswith(TICKTICK):
        return "tasks"
    if tool in _WEB_TOOLS:
        return "web"
    if tool in _FILE_TOOLS:
        return "files"
    return "search" if is_search(tool) else None


def weather_moment(summary: str) -> str | None:
    """What she holds up as she tells the user the weather, from ``get_weather``'s result: an ``umbrella`` for rain or
    snow right now, ``sunglasses`` for a clear sky in the daytime, or None."""
    now = _NOW.search(summary)
    if now is None:
        return None
    hour = int(now.group(1)) % 12 + (12 if now.group(2).upper() == "PM" else 0)
    if _WET.search(now.group(3)):
        return "umbrella"
    return "sunglasses" if _SUNNY.search(now.group(3)) and hour in _DAYLIGHT else None


def _ticktick_tool(tool: str, args: dict[str, Any]) -> str | None:
    """The TickTick tool a call runs, without the prefix (through ``ticktick_more`` too); None when it isn't one, or
    only asks ``ticktick_more`` for a tool's description."""
    if not tool.startswith(TICKTICK):
        return None
    if tool != _MORE:
        return tool[len(TICKTICK) :]
    inner = args.get("tool")
    return inner.strip() if isinstance(inner, str) and not args.get("describe") else None


def moment_for(tool: str, args: dict[str, Any]) -> str | None:
    """The moment a call of ``tool`` plays once it has gone through: ``tick`` for a TickTick task (or checklist item,
    or habit) done, ``timer_start`` for a timer or a TickTick focus session started, ``music`` for the user's music
    started; None for anything else."""
    if tool in _TIMERS:
        return "timer_start"
    if tool == "play_on_spotify" or (tool == "media_control" and args.get("action") in _MUSIC_ACTIONS):
        return "music"
    inner = _ticktick_tool(tool, args)
    if inner in _TICKS:
        return "tick"
    return "timer_start" if inner in _FOCUS else None


def _does_something(tool: str, args: dict[str, Any]) -> bool:
    """Whether a call of ``tool`` gets something done (a click, a key, a task changed), rather than looking something
    up or pointing: those count toward a sonic rainboom."""
    if tool in _POINTING_TOOLS or tool in _SEARCH_AFTER or is_search(tool) or _READ_NAME.match(tool):
        return False
    inner = _ticktick_tool(tool, args)
    if tool.startswith(TICKTICK):
        return inner is not None and inner != "setup" and categorize(inner) != "read"
    return True


def electron_path(root: Path, configured: str = "") -> Path:
    """The Electron executable: ``avatar.electron`` when set, else the one npm installed in ``avatar/``."""
    if configured.strip():
        return Path(configured.strip())
    package = root / APP_DIR / "node_modules" / "electron"
    try:  # the electron package writes the executable's path, relative to dist/, here
        name = (package / "path.txt").read_text(encoding="utf-8").strip()
    except OSError:
        name = "electron.exe" if sys.platform == "win32" else "electron"
    return package / "dist" / name


# ---------------------------------------------------------------------- props

# The app makes these from simple shapes; a file of the same name wins.
PROP_BUILTINS = (
    "camera",
    "magnifier",
    "clipboard",
    "stopwatch",
    "bell",
    "globe",
    "folder",
    "mug",
    "wrench",
    "headphones",
    "sunglasses",
    "megaphone",
    "umbrella",
)
# The hand a built-in goes in unless its settings say: the right, but the clipboard, the folder and the globe go in her
# left, so her right can tick things off, spin the globe or rest on her hip (the app says the same). Headphones and
# sunglasses she wears on her head, whatever the setting.
_BUILTIN_HANDS = {"clipboard": "left", "folder": "left", "globe": "left"}
# What each prop setting (avatar.props.<key>) is held for.
PROP_ACTIVITIES = {
    "camera": "taking a screenshot",
    "search": "looking closely at your screen or searching for something",
    "tasks": "working with your TickTick tasks",
    "timer": "setting a timer, and when one goes off",
    "reminder": "reminding you of a task",
    "web": "searching or reading the web",
    "files": "looking through your files",
    "morning": "the morning rundown",
    "build": "starting to build an improvement of hers",
    "music": "starting your music",
    "shout": "shouting",
    "rain": "telling you it's raining",
    "sun": "telling you it's sunny",
}
_PROP_FILES = (".glb", ".gltf")  # in this order of preference when a prop has both
# A file's longest side unless its settings say otherwise. A built-in keeps its own true-to-life size unless given
# another one (the app treats this default as "not given").
PROP_DEFAULT_SIZE_M = 0.18
_PROP_SIZE_MIN_M = 0.02  # the app's limits: smaller is a speck,
_PROP_SIZE_MAX_M = 2.0  # bigger than she is is surely a mistake (a size in centimetres, say)
_PROP_NAME_MAX = 64  # the app's limit
_MB = 1024 * 1024  # as Windows counts them
_PROP_BYTES_MAX = 64 * _MB  # the app's limit (MAX_PROP_BYTES in avatar/main.js): it won't load a bigger file
_PROP_GRIP_MAX_M = 1.0
_GLB_HEAD = struct.Struct("<4sIIII")  # magic, version, whole length; then the first chunk's length and type
_GLB_JSON = 0x4E4F534A  # "JSON": a .glb's first chunk is always its JSON
_DRACO = "KHR_draco_mesh_compression"
# Compression the app can't undo: each needs a decoder the page isn't allowed to run. Meshopt goes by two names
# (EXT_ and the newer KHR_), and the loader refuses both without a decoder. A file that merely offers meshopt or KTX2
# still has the plain data, which the loader falls back to; one that offers Draco it refuses anyway.
_COMPRESSION = (_DRACO, "EXT_meshopt_compression", "KHR_meshopt_compression", "KHR_texture_basisu")


@dataclass(frozen=True)
class Prop:
    """Something she can hold: a model file from avatar.props_dir, one the app makes itself (``builtin``), or one she
    made out of simple shapes (``parts``, ``made_props``)."""

    name: str
    file: Path | None = None
    builtin: str | None = None
    hand: str = "right"
    size: float = PROP_DEFAULT_SIZE_M  # its longest side once scaled, in metres
    grip: tuple[float, float, float] = (0.0, 0.0, 0.0)  # where it sits in the hand, metres (the hand bone's frame)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)  # degrees, same frame
    style: str = "toon"  # toon = shaded like her; original = its own materials
    parts: tuple[dict[str, Any], ...] = ()  # a made prop's shapes

    def to_app(self) -> dict[str, Any]:
        """This prop as the app's config file lists it (docs/avatar-protocol.md)."""
        app: dict[str, Any] = {
            "file": str(self.file) if self.file is not None else None,
            "builtin": self.builtin,
            "hand": self.hand,
            "size": self.size,
            "grip": list(self.grip),
            "rotation": list(self.rotation),
            "style": self.style,
        }
        if self.parts:
            app["parts"] = [dict(part) for part in self.parts]
        return app


_DEFAULT_PROP = Prop("")


@dataclass(frozen=True)
class PropScan:
    """The props she can hold, which one each activity uses, and what's wrong with any of them."""

    folder: Path
    props: dict[str, Prop]
    activities: dict[str, str | None]  # activity -> the prop it uses (None = nothing)
    problems: list[str]


def prop_name(stem: str) -> str:
    """A prop's name from its file name: lowercase, anything but a-z, 0-9, ``_`` and ``-`` turned into ``-``."""
    return re.sub(r"[^a-z0-9_-]+", "-", stem.lower()).strip("-")


def _mentions_prop(text: str, name: str) -> bool:
    """Whether a request is about the prop ``name`` she holds: a word of its name ("sword", "swords"), or her props."""
    words = [re.escape(word) for word in re.split(r"[-_]+", name) if len(word) > 2] or [re.escape(name)]
    return re.search(rf"\b(?:{'|'.join(words)})|\bprops?\b", text, re.IGNORECASE) is not None


def scan_props(config: AppConfig) -> PropScan:
    """The props in ``avatar.props_dir``, the built-ins and the ones she made, and which one each activity uses
    (``avatar.props``). A file or built-in wins over a made prop of the same name."""
    from octavia.avatar.made_props import made_props  # it builds on this module

    folder = config.resolve_path(config.avatar.props_dir)
    props, problems = find_props(folder)
    made, bad = made_props(config)
    problems += bad
    for name, prop in made.items():
        if name in props:
            problems.append(f"the prop {name!r} she made is left out: a file or built-in prop has that name")
        else:
            props[name] = prop
    activities: dict[str, str | None] = {}
    for activity in PROP_ACTIVITIES:
        wanted = getattr(config.avatar.props, activity)
        name = prop_name(wanted)
        if wanted and name not in props:
            problems.append(
                f"avatar.props.{activity} is {wanted!r}, but there's no prop by that name, so she holds nothing "
                "(`octavia avatar props` lists them)"
            )
        activities[activity] = name if name in props else None
    return PropScan(folder, props, activities, problems)


def find_props(folder: Path) -> tuple[dict[str, Prop], list[str]]:
    """The built-ins plus each ``<name>.glb`` (or self-contained ``<name>.gltf``) in ``folder``, and what's wrong.

    A file the app couldn't load is left out. A ``<name>.json`` beside a file holds its settings (hand, size, grip,
    rotation, style); a bad or unknown one is named and the default used, since a typo shouldn't cost the whole
    prop. A settings file named like a built-in, with no model file, tunes the built-in.
    """
    props = {name: Prop(name, builtin=name, hand=_BUILTIN_HANDS.get(name, "right")) for name in PROP_BUILTINS}
    try:
        files = sorted(p for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []
    except OSError as exc:
        return props, [f"couldn't read the props folder {folder}: {exc.strerror or exc}"]
    settings_files = {p.stem.lower(): p for p in files if p.suffix.lower() == ".json"}
    models = sorted(
        (p for p in files if p.suffix.lower() in _PROP_FILES),
        key=lambda p: (prop_name(p.stem), _PROP_FILES.index(p.suffix.lower()), p.name),
    )
    problems: list[str] = []
    claimed: set[str] = set()  # settings files that belong to a model file (used or not)
    from_files: dict[str, Path] = {}
    for path in models:
        name = prop_name(path.stem)
        claimed.add(path.stem.lower())
        if not name:
            problems.append(f"{path.name} is left out: its name needs a letter or digit")
            continue
        if len(name) > _PROP_NAME_MAX:
            problems.append(f"{path.name} is left out: its name is longer than {_PROP_NAME_MAX} characters")
            continue
        if name in from_files:
            problems.append(f"{path.name} is left out: {from_files[name].name} is also called {name!r}")
            continue
        problem = _model_problem(path)
        if problem:
            problems.append(problem)
            continue
        from_files[name] = path
        settings, bad = _prop_settings(settings_files.get(path.stem.lower()))
        problems += bad
        props[name] = Prop(name, file=path.resolve(), **settings)
    for stem, path in settings_files.items():
        name = prop_name(stem)
        if stem in claimed:
            continue
        if name in PROP_BUILTINS and name not in from_files:
            settings, bad = _prop_settings(path)
            problems += bad
            props[name] = Prop(name, builtin=name, **{"hand": _BUILTIN_HANDS.get(name, "right"), **settings})
        else:
            problems.append(f"{path.name} isn't used: there's no {path.stem}.glb beside it")
    return props, problems


def _model_problem(path: Path) -> str | None:
    """Why the app couldn't load this model file (it only gets the file's bytes), or None."""
    glb = path.suffix.lower() == ".glb"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"{path.name} is left out: couldn't read it ({exc.strerror or exc})"
    # Before reading anything: a huge .gltf would otherwise be read whole just to be refused.
    if size > _PROP_BYTES_MAX:
        return (
            f"{path.name} is left out: it's too big ({math.ceil(size / _MB)} MB; "
            f"a prop can be at most {_PROP_BYTES_MAX // _MB} MB)"
        )
    try:
        data = _glb_json(path) if glb else json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        return f"{path.name} is left out: couldn't read it ({exc.strerror or exc})"
    except ValueError as exc:  # not a whole GLB, bad JSON, or not UTF-8 text
        why = str(exc) if glb else "it isn't a glTF file inside"
        return f"{path.name} is left out: {why}"
    if not isinstance(data, dict):
        return f"{path.name} is left out: it isn't a {'GLB' if glb else 'glTF'} file inside"
    compressed = ", ".join(_compression(data))
    if compressed:
        return f"{path.name} is left out: it's compressed ({compressed}); export it again without compression"
    if glb:
        return None
    # A .gltf usually comes with a .bin and textures beside it, which the app can't fetch: only embedded data works.
    for key in ("buffers", "images"):
        items = data.get(key)
        for item in items if isinstance(items, list) else []:
            uri = item.get("uri") if isinstance(item, dict) else None
            if isinstance(uri, str) and not uri.startswith("data:"):
                return f"{path.name} is left out: it needs separate files ({uri}); use the one-file .glb version"
    return None


def _glb_json(path: Path) -> Any:
    """A .glb's JSON chunk, parsed; ValueError (saying what's wrong, for the user) when it isn't a whole GLB."""
    with path.open("rb") as file:
        head = file.read(_GLB_HEAD.size)
        if len(head) < _GLB_HEAD.size or head[:4] != b"glTF":
            raise ValueError("it isn't a GLB file inside")
        _, _, total, length, kind = _GLB_HEAD.unpack(head)
        if total > os.fstat(file.fileno()).st_size:
            raise ValueError("it's cut short (did the download finish?)")
        # The lengths come from the file itself: read the chunk only when it fits inside it.
        if kind != _GLB_JSON or not 0 < length <= total - _GLB_HEAD.size:
            raise ValueError("it isn't a GLB file inside")
        try:
            return json.loads(file.read(length).decode("utf-8-sig"))
        except ValueError:
            raise ValueError("it isn't a GLB file inside") from None


def _compression(data: dict[str, Any]) -> list[str]:
    """The compression in a glTF's JSON that stops the app loading it (see _COMPRESSION)."""

    def names(key: str) -> list[Any]:
        value = data.get(key)
        return value if isinstance(value, list) else []

    required, used = names("extensionsRequired"), names("extensionsUsed")
    return [ext for ext in _COMPRESSION if ext in required or (ext == _DRACO and ext in used)]


def _prop_settings(path: Path | None) -> tuple[dict[str, Any], list[str]]:
    """The settings in a prop's .json file, and a line for each one that's wrong (its default is used instead)."""
    if path is None:
        return {}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))  # -sig: Notepad may start the file with a BOM
    except OSError as exc:
        return {}, [f"couldn't read {path.name} ({exc.strerror or exc}), so its prop uses the defaults"]
    except ValueError as exc:
        return {}, [f"{path.name} isn't valid JSON ({exc}), so its prop uses the defaults"]
    if not isinstance(data, dict):
        return {}, [f'{path.name} should be an object like {{"hand": "left"}}, so its prop uses the defaults']
    settings: dict[str, Any] = {}
    problems: list[str] = []
    for key, value in data.items():
        check = _PROP_CHECKS.get(key)
        if check is None:
            problems.append(f"{path.name}: {key!r} isn't a setting (they are {', '.join(_PROP_CHECKS)})")
            continue
        try:
            settings[key] = check(value)
        except ValueError as exc:
            default = getattr(_DEFAULT_PROP, key)
            shown = list(default) if isinstance(default, tuple) else default
            problems.append(f"{path.name}: {key} {exc}, so it's {json.dumps(shown)}")
    return settings, problems


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("must be a number")
    return float(value)


def _choice(*options: str) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if isinstance(value, str) and value.strip().lower() in options:
            return value.strip().lower()
        raise ValueError("must be " + " or ".join(json.dumps(o) for o in options))

    return check


def _size(value: Any) -> float:
    size = _number(value)
    if not _PROP_SIZE_MIN_M <= size <= _PROP_SIZE_MAX_M:
        raise ValueError(f"must be in metres, from {_PROP_SIZE_MIN_M:g} to {_PROP_SIZE_MAX_M:g}")
    return size


def _xyz(limit: float | None, unit: str) -> Callable[[Any], tuple[float, float, float]]:
    def check(value: Any) -> tuple[float, float, float]:
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError(f"must be three numbers [x, y, z] in {unit}")
        x, y, z = (_number(v) for v in value)
        if limit is not None and max(abs(x), abs(y), abs(z)) > limit:
            raise ValueError(f"must be in {unit}, each at most {limit:g} either way")
        return (x, y, z)

    return check


_PROP_CHECKS: dict[str, Callable[[Any], Any]] = {
    "hand": _choice("right", "left"),
    "size": _size,
    "grip": _xyz(_PROP_GRIP_MAX_M, "metres"),
    "rotation": _xyz(None, "degrees"),
    "style": _choice("toon", "original"),
}


def app_config(
    config: AppConfig,
    model: Path,
    props: PropScan,
    *,
    position: dict[str, int] | None = None,
    preview_prop: str | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    """The app's config file (docs/avatar-protocol.md), for Octavia's app and for ``octavia avatar preview``."""
    settings = config.avatar
    return {
        "model": str(model),
        "height": settings.height,
        "framing": settings.framing,
        "position": position,
        "statePath": str(config.resolve_path(state_file(settings))),
        "credit": settings.credit,
        "hideFromCapture": settings.hide_from_capture,
        "pointHeight": settings.point_height,
        "trail": settings.trail,
        "props": {name: prop.to_app() for name, prop in sorted(props.props.items())},
        "activityProps": dict(props.activities),
        "previewProp": preview_prop,
        "hover": settings.hover,
        "napAfterS": round(settings.nap_after_min * 60),
        "form": settings.form,
        "orbSize": settings.orb_size,
        "debug": debug,
    }


# What hold_prop says while she's the orb (avatar.form): it has no hands, so nothing would show.
ORB_HOLDS_NO_PROPS = "She's the orb right now, which can't hold props: they show when her look is the character."


def needs_model(settings: AvatarConfig) -> bool:
    """Whether the app needs the VRM file: the orb is drawn by the app itself."""
    return settings.form != "orb"


def _box(box: Any) -> list[int] | None:
    """(left, top, right, bottom) as whole pixels, the right way round; raises ValueError/TypeError when it isn't."""
    if box is None:
        return None
    left, top, right, bottom = (round(float(v)) for v in box)
    return [min(left, right), min(top, bottom), max(left, right), max(top, bottom)]


def _prop_message(name: str, prop: dict[str, Any] | None) -> dict[str, Any]:
    """A made prop for the app: built (``prop-define``), or thrown away when ``prop`` is None (``prop-remove``)."""
    if prop is None:
        return {"type": "prop-remove", "name": name}
    return {"type": "prop-define", "name": name, "prop": prop}


def ctrl_held() -> bool:
    """Whether Ctrl is held on this PC; never anywhere else."""
    if sys.platform != "win32":
        return False
    from octavia.platform import windows as win

    return win.ctrl_held()


class AvatarBridge:
    """Starts the avatar app and keeps it in step with the assistant. Create one, then ``await start()``."""

    def __init__(
        self,
        config: AppConfig,
        events: EventBus,
        *,
        sample_rate: int = 44100,
        spawn: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        front: Callable[[], foreground.Front | None] | None = None,
        ctrl: Callable[[], bool] | None = None,
    ) -> None:
        """``spawn`` stands in for ``asyncio.create_subprocess_exec``, ``front`` for ``foreground.read_front`` and
        ``ctrl`` for ``ctrl_held`` (tests)."""
        self.config = config
        self.settings = config.avatar
        self.events = events
        self.sample_rate = sample_rate
        self.on_click: Callable[[], None] | None = None  # the app points this at "start listening"
        # The text of her pop-up panels up now: a sentence they already show gets no subtitle (avatar/subtitles.py).
        self.panel_text: Callable[[], Iterable[str]] | None = None
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._clock = clock
        self._loop: asyncio.AbstractEventLoop | None = None
        self._proc: Any = None
        self._ready = False
        self._closed = False
        self._visible = False  # what the app was last told
        # The user sent her away (send_away): hidden, in either mode, until they ask her back or the time is up.
        self._away = False
        self._away_timer: asyncio.TimerHandle | None = None
        self._snapshot_at: float | None = None  # when the last snapshot went to the app, until its shutter came back
        self._state = AssistantState.STARTING
        self._hide_task: asyncio.Task[None] | None = None
        # Pointing (point()): until point_linger_s after the turn, a new turn or request, a call of any other tool,
        # POINT_MAX_S, or a hide.
        self._pointing = False
        self._point_timer: asyncio.TimerHandle | None = None
        self._activity: str | None = None  # the activity the app was last told about
        self._activity_timer: asyncio.TimerHandle | None = None
        self._busy: dict[str, str] = {}  # this turn's calls still running that hold something up: call id -> activity
        self._calls: dict[str, tuple[str, dict[str, Any]]] = {}  # this turn's calls still running: id -> (tool, args)
        self._working = False  # what the orb was last told about those calls ("working")
        # The sonic rainboom: what this turn got done (and whether anything failed), when the last one went off, and
        # until when one is owed for an improvement installed while she was away.
        self._done = 0
        self._failed = False
        self._last_rainboom = -math.inf
        self._rainboom_owed_until = -math.inf
        self._owed_moment: str | None = None  # a moment for her next sentence this turn (the weather's umbrella)
        self._waits: dict[str, asyncio.TimerHandle | None] = {}  # calls she may get impatient waiting on, by call id
        self._carry: tuple[str, float] | None = None  # the mood the user's words leave her in, for after the turn
        # Props she made or threw away since the app's config was written (name -> its settings, None once gone), and
        # the prop she was asked to hold (name, hand): told again to an app that restarts.
        self._prop_changes: dict[str, dict[str, Any] | None] = {}
        self._held_prop: tuple[str, str | None] | None = None
        # Whether the user asked her to keep holding it, the turn it was taken up in, and when it's put away.
        self._prop_kept = False
        self._prop_turn = 0
        self._prop_timer: asyncio.TimerHandle | None = None
        self._turn = 0  # counts turns, so a prop knows a later request from its own
        self._front = foreground.ForegroundWatch(front or foreground.read_front)
        self._last_glance = -math.inf
        self._last_startle = -math.inf
        self._building: dict[int, float] = {}  # builds of hers running: improvement id -> when it started
        self._perched: tuple[int, foreground.Rect] | None = None  # the window she sits on, and its frame then
        self._last_perch = -math.inf  # when she last left a window she sat on
        # While the pointer rests on her (the app says "hover") she lets clicks through, unless Ctrl is held: the app
        # never has the keyboard, so Ctrl is watched here and the app told when it changes ("keys").
        self._read_ctrl = ctrl or ctrl_held
        self._ctrl = False  # what the app was last told (it starts every hover at False)
        self._ctrl_watch: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._supervisor: asyncio.Task[None] | None = None
        self._unsubscribe: Callable[[], None] | None = None
        # Lip sync: the render tap queues audio blocks while speech plays; the worker turns them into mouth frames.
        self._speaking = False
        self._phrase_until = 0.0  # a short phrase ("On it!") plays until then (time.monotonic)
        self._blocks: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=_QUEUE_BLOCKS)
        self._worker: threading.Thread | None = None
        self._analyzer = VisemeAnalyzer(sample_rate)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> bool:
        """Start the app; False (and a notice saying why) when it can't run: not installed, no model."""
        self._loop = asyncio.get_running_loop()
        root = self.config.root
        exe = electron_path(root, self.settings.electron)
        model = self.config.resolve_path(self.settings.model)
        if not exe.is_file():
            self._notice("The avatar app isn't installed; run `uv run octavia avatar setup` once.", logging.WARNING)
            return False
        if needs_model(self.settings) and not model.is_file():
            self._notice(f"No avatar model at {model} (avatar.model).", logging.WARNING)
            return False
        props = await asyncio.to_thread(scan_props, self.config)  # a .gltf prop is parsed: keep it off the loop
        if props.problems:
            self._notice("Avatar props: " + "; ".join(props.problems) + ".", logging.WARNING)
        config_path = self.config.resolve_path(CONFIG_FILE)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        app = app_config(
            self.config, model, props, position=self._saved_position(), debug=log.isEnabledFor(logging.DEBUG)
        )
        config_path.write_text(json.dumps(app), encoding="utf-8")
        self._command = [str(exe), str(root / APP_DIR), "--config", str(config_path)]
        self._unsubscribe = self.events.subscribe(self._on_event)
        if self.settings.lip_sync:
            self._worker = threading.Thread(target=self._lip_sync_loop, name="avatar-lipsync", daemon=True)
            self._worker.start()
        self._supervisor = asyncio.create_task(self._supervise(), name="avatar")
        if self.settings.glance or self.settings.startle or self.settings.perch:
            self._tasks.add(asyncio.create_task(self._watch_front(), name="avatar-front"))
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._cancel_hide()
        self._cancel_timer(self._away_timer)
        self._cancel_timer(self._prop_timer)
        self._forget_moves()
        self._hover(False)
        self._speaking = False
        if self._worker is not None:
            with contextlib.suppress(queue.Full):
                self._blocks.put_nowait(None)
        proc = self._proc
        if proc is not None and proc.returncode is None:
            self._send({"type": "quit"})
            with contextlib.suppress(Exception):
                if proc.stdin is not None:
                    proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), _CLOSE_WAIT_S)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        if self._supervisor is not None:
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    @property
    def app_pid(self) -> int | None:
        """The app's process id while it runs, else None.

        It's Electron's main process, which owns all her windows: the screen tools use it to tell them from the
        user's own (her window can be over the very thing they're asked about).
        """
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return None
        pid = getattr(proc, "pid", None)
        return pid if isinstance(pid, int) else None

    def _saved_position(self) -> dict[str, int] | None:
        """Where the user last dragged the window (the app saves it)."""
        try:
            data = json.loads(self.config.resolve_path(state_file(self.settings)).read_text(encoding="utf-8"))
            return {"x": int(data["x"]), "y": int(data["y"])}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    async def _supervise(self) -> None:
        """Run the app; restart it after a crash (a few times, then leave it off: the assistant doesn't need it)."""
        crashes = 0
        while not self._closed:
            started = self._clock()
            try:
                self._proc = await self._spawn(
                    *self._command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                self._notice(f"Couldn't start the avatar: {exc}", logging.WARNING)
                return
            self._ready, self._visible = False, False
            self._forget_moves()  # a new app isn't pointing or holding anything
            self._hover(False)  # ...nor has the pointer on her
            readers = [
                asyncio.create_task(self._read_stdout(self._proc.stdout), name="avatar-stdout"),
                asyncio.create_task(self._read_stderr(self._proc.stderr), name="avatar-stderr"),
            ]
            code = await self._proc.wait()
            await asyncio.gather(*readers, return_exceptions=True)
            self._ready = False
            if self._closed:
                return
            crashes = crashes + 1 if self._clock() - started < 60.0 else 1
            if crashes > _RESTARTS:
                self._notice("The avatar keeps crashing, so it's off until the next start.", logging.WARNING)
                return
            log.warning("The avatar app exited (code %s); restarting it", code)
            await asyncio.sleep(_RESTART_DELAY_S * crashes)

    async def _read_stdout(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            try:
                message = json.loads(text)
            except ValueError:
                message = None
            if isinstance(message, dict):
                self._on_message(message)
            elif text:
                log.debug("avatar: %s", text)

    async def _read_stderr(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                log.debug("avatar stderr: %s", text)

    def _on_message(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "ready":
            self._ready = True
            self._send({"type": "state", "state": self._state.value})
            self._working = False  # a restarted app starts out idle
            self._sync_working()
            for name, prop in self._prop_changes.items():
                self._send(_prop_message(name, prop))
            if self._held_prop is not None:
                self._send({"type": "prop-hold", "name": self._held_prop[0], "hand": self._held_prop[1]})
            if self._should_show():
                self._show("always" if self.settings.mode == "always" else "talk")
        elif kind == "click":
            if self.settings.click_to_talk and self.on_click is not None:
                try:
                    self.on_click()
                except Exception:
                    log.warning("Clicking the avatar failed", exc_info=True)
        elif kind == "shutter":
            self._snapshot_at = None
            self.events.emit(CameraShutter())
        elif kind == "hidden":
            # Nothing to update: _hide already counted her gone, and a "hidden" that crossed a newer "show" is stale
            # (believing it would leave her on screen with no hide ever scheduled).
            log.debug("Avatar hidden")
        elif kind == "moved":
            log.debug("Avatar moved to %s, %s", message.get("x"), message.get("y"))
        elif kind == "hover":
            self._hover(message.get("over") is True)
        elif kind == "error":
            self._notice(f"Avatar: {message.get('message', 'something went wrong')}", logging.WARNING)

    # ------------------------------------------------------------------ sending

    def _send(self, message: dict[str, Any]) -> bool:
        """Write one message to the app; False when it was dropped (not running, or not ready yet except quit)."""
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None:
            return False
        if not self._ready and message.get("type") != "quit":
            return False
        try:
            proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            return False  # it's gone: the supervisor restarts it
        return True

    def _send_mouth(self, message: dict[str, Any]) -> None:
        if self._lip_sync_open():  # frames still being analysed when speech ended must not reopen the mouth
            self._send(message)

    def _send_mood(self, emotion: str, intensity: float, hold: float) -> None:
        """A mood outside speech, only while she's there: one from while she was away would greet the user
        later, out of the blue."""
        if self._on_screen():
            self._send({"type": "emotion", "emotion": emotion, "intensity": intensity, "hold": hold})

    def _call_threadsafe(self, callback: Callable[..., None], *args: Any) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):  # closing
                loop.call_soon_threadsafe(callback, *args)

    # ------------------------------------------------------------------ what the assistant does

    def _on_event(self, event: Event) -> None:
        if isinstance(event, StateChanged):
            # Straight from idle to speaking, with no wake word or click first, is an announcement.
            self._on_state(
                event.new,
                announcing=event.old is AssistantState.IDLE and event.new is AssistantState.SPEAKING,
                remote=event.remote is not None,
            )
        elif isinstance(event, WakeListening) and self._state is AssistantState.IDLE:
            # The conversation is over: lingering on screen until a timeout reads as her still listening. In always
            # mode she stays on screen, but goes back to her spot rather than pointing at an old answer.
            self._hide()
            self._unpoint()
            self._put_away_prop()
        elif isinstance(event, WakeDetected):
            self._unpoint()  # a new request: what she pointed at belonged to the last one
            if not event.during_speech:
                self._show("wake")
        elif isinstance(event, Interrupted):
            self._unpoint()  # cut off (talked over, the talk or stop hotkey): the reply she pointed for is over
            self._send({"type": "subtitles-end", "interrupted": True})
        elif isinstance(event, TasksCleared):
            self._victory_lap()
        elif isinstance(event, LoudNoise):
            self._startle()
        elif isinstance(event, Transcript):
            self._carry = user_mood_for(event.text)  # what they just said: a thank-you, a tease
        elif isinstance(event, BrainStarted | PresetReplied | FastChatStarted):
            # A new turn (typed, or a follow-up with no wake word): the markers belonged to the last one. A new point
            # in this turn brings them back.
            self._turn += 1
            if isinstance(event, BrainStarted | FastChatStarted) and self._held_prop is not None:
                if _mentions_prop(event.text, self._held_prop[0]):
                    self._prop_turn = self._turn  # a request about it: it's still that conversation
                    self._prop_talked_of()
            self._unpoint()
            self._done, self._failed, self._owed_moment = 0, False, None
            if self._activity in _LINGER:
                self._end_activity()  # the morning's mug was for the rundown, not for this
        elif isinstance(event, SpeechSegmentStarted):
            # She's talking now, not peering. A line that starts while a search is still running was queued before
            # its answer came back ("Let me look that up."), so she keeps searching through it.
            if not self._busy and self._activity not in _LINGER:
                self._end_activity()
            self._show("announce")  # speech outside a conversation (an announcement) brings it in too
            self._send_segment(event.text, shown=event.shown, index=event.index)
            if self._clock() < self._rainboom_owed_until:
                self._rainboom()
            elif shouts(event.text):
                self._moment("megaphone")
            elif self._owed_moment is not None:
                self._moment(self._owed_moment)  # the weather's umbrella or sunglasses, as she tells it
            self._owed_moment = None
        elif isinstance(event, SpeechSegmentAudio):
            # How long a sentence's audio is, for pacing its subtitle's words (docs/subtitles.md).
            if self.settings.subtitles:
                self._send({"type": "segment-audio", "index": event.index, "seconds": round(event.seconds, 3)})
        elif isinstance(event, SpeechWordStarted):
            # She's reached this word, by her audio as it plays (tts/word_times.py): her subtitles light it.
            if self.settings.subtitles:
                self._send({"type": "segment-word", "index": event.index, "word": event.word, "of": event.of})
        elif isinstance(event, AnnouncementStarted):
            moment = _CUE_MOMENTS.get(event.cue or "")
            if moment is not None:
                self._show("announce")  # it's coming anyway (she speaks next): the moment goes as she arrives
                self._moment(moment)
            elif event.cue == "morning":
                self._show("announce")
                self._start_activity("morning")
        elif isinstance(event, PhrasePlaying):
            self._phrase_until = time.monotonic() + event.seconds + _PHRASE_TAIL_S
            self._send_segment(event.text, hold_s=event.seconds)
        elif isinstance(event, SpeakingStarted):
            self._speaking = True
        elif isinstance(event, SpeakingFinished):
            self._speaking = False
            self._send({"type": "mouth", "level": 0.0, "v": [0.0] * 5})
            # Her subtitles fade once she's done; at once when she was cut off (talked over, the stop hotkey).
            self._send({"type": "subtitles-end", "interrupted": event.interrupted})
        elif isinstance(event, ToolCallStarted):
            if event.name not in _POINTING_TOOLS:
                self._unpoint()  # she's moved on (clicking, typing, playing): the markers would only be in the way
            if event.name in _PROP_TOOLS:
                self._prop_talked_of()
            elif self._turn > self._prop_turn:
                self._put_away_prop()  # a later request that does something else: the prop was for the last one
            self._calls[event.call_id] = (event.name, dict(event.args or {}))
            self._sync_working()
            activity = activity_for(event.name)
            if activity is not None:
                self._busy[event.call_id] = activity
                self._start_activity(activity)
            elif event.name not in _POINTING_TOOLS and event.name not in _SEARCH_AFTER:
                self._waits[event.call_id] = self._later(IMPATIENT_S, lambda: self._impatient(event.call_id))
        elif isinstance(event, ToolCallFinished):
            self._busy.pop(event.call_id, None)
            self._cancel_timer(self._waits.pop(event.call_id, None))
            name, args = self._calls.pop(event.call_id, (event.name, {}))
            self._sync_working()
            if not event.ok:
                self._failed = True
                self._send_mood("sad", 0.35, 2)
                return
            if event.name in _SEARCH_AFTER:
                self._start_activity("search")  # the brain studies the picture now
            if _does_something(name, args):
                self._done += 1
            moment = moment_for(name, args)
            if moment is not None:
                self._moment(moment)
            if name == _WEATHER:
                self._owed_moment = weather_moment(event.summary)
        elif isinstance(event, ErrorOccurred) and not event.fatal:
            self._send_mood("sad", 0.4, 2)
        elif isinstance(event, ImprovementChanged):
            self._on_improvement(event)
        elif isinstance(event, ScreenshotTaken) and self._on_screen():
            # Her camera pose and a flash; she's out of the picture herself. The shutter sound waits for her flash.
            self._snapshot_at = self._clock() if self._send({"type": "snapshot"}) else None

    def shows_snapshot(self) -> bool:
        """Whether she's pressing her camera's shutter for the screenshot just taken: then the shutter sound waits for
        the app's ``shutter`` (``CameraShutter``) to land on her flash."""
        at = self._snapshot_at
        return at is not None and self._clock() - at < SNAPSHOT_WAIT_S

    def _on_improvement(self, event: ImprovementChanged) -> None:
        """A build of hers starts: a wrench twirled (she may sit on a window while it runs). Anything else ends it: home
        from a window she sat on once no build is left, and an improvement ready or installed makes her excited (an
        install gets a rainboom)."""
        if event.status == "building":
            self._building[event.improvement_id] = self._clock()
            self._moment("wrench")  # while she's there to twirl it
            return
        if self._building.pop(event.improvement_id, None) is not None and not self._building:
            self._unperch()
        if event.status in ("ready", "installed"):
            self._send_mood("excited", 0.7, 3)
            if event.status == "installed":
                self._rainboom_owed_until = self._clock() + _RAINBOOM_PENDING_S
                self._rainboom()

    def _sync_working(self) -> None:
        """Tells the orb whether any of the turn's tools is running, for its working look (the character shows her
        props instead, so she isn't told)."""
        working = bool(self._calls)
        if self.settings.form != "orb" or working == self._working:
            return
        if self._send({"type": "working", "on": working}):
            self._working = working

    def _send_segment(
        self, text: str, *, shown: str | None = None, hold_s: float | None = None, index: int | None = None
    ) -> None:
        """A sentence starts playing. ``subtitle`` is what her subtitles show for it (None: none, which also takes down
        the last one); ``hold`` for a short phrase outside a reply, which no ``subtitles-end`` follows; ``index`` its
        place in the reply, which its ``segment-audio`` (how long it lasts, for pacing the words) names."""
        mood = mood_for(text)
        message: dict[str, Any] = {
            "type": "segment",
            "text": text,
            "emotion": mood.emotion,
            "intensity": round(mood.intensity, 2),
            "gesture": mood.gesture,
            "subtitle": self._subtitle(text, shown),
        }
        if hold_s is not None:
            message["hold"] = round(hold_s, 2)
        if index is not None:
            message["index"] = index
        self._send(message)

    def _subtitle(self, text: str, shown: str | None) -> str | None:
        if not self.settings.subtitles:
            return None
        try:
            panels = list(self.panel_text()) if self.panel_text is not None else []
        except Exception:  # the panels' state is only a nicety here: a subtitle too many beats none
            log.debug("Couldn't read her panels for the subtitles", exc_info=True)
            panels = []
        return subtitle(text, shown, panels)

    def _on_state(self, state: AssistantState, *, announcing: bool = False, remote: bool = False) -> None:
        """``remote``: the turn was texted from the user's phone and is answered there, so it doesn't bring her in."""
        turn_over = self._state in _TURN and state not in _TURN
        self._state = state
        self._send({"type": "state", "state": state.value})
        if turn_over and self._done >= RAINBOOM_ACTIONS and not self._failed:
            self._rainboom()  # a long task, all of it done: as she finishes telling you
        if turn_over and self._carry is not None:
            emotion, intensity = self._carry
            self._carry = None
            if self._on_screen():
                self._send({"type": "mood", "emotion": emotion, "intensity": intensity, "seconds": MOOD_S})
        if state not in _TURN:
            # The turn is over: a call it cut off never reports back.
            self._busy.clear()
            self._calls.clear()
            self._sync_working()
            for timer in self._waits.values():
                self._cancel_timer(timer)
            self._waits.clear()
            self._done, self._failed, self._owed_moment = 0, False, None
        # The brain is done searching (or waits for a yes first). Speaking starts with a sentence, which decides.
        if state not in (AssistantState.THINKING, AssistantState.SPEAKING) and self._activity not in _LINGER:
            self._end_activity()
        if state is not AssistantState.IDLE:
            self._unperch()  # she's needed: home first
        if state in _BUSY and remote:
            pass  # answered on the phone: not brought in (and if she's there anyway, she leaves as she would)
        elif state in _BUSY:
            self._cancel_hide()
            self._show("announce" if announcing else "talk")
        elif state is AssistantState.STOPPED:
            self._hide()
        else:
            self._hide_when_idle()
        if turn_over and self._pointing:
            self._linger()

    def _should_show(self) -> bool:
        return not self._away and (self.settings.mode == "always" or self._state in _BUSY)

    def _on_screen(self) -> bool:
        """Whether she's there to act something out: shown, or always on screen, and not sent away."""
        return not self._away and (self._visible or self.settings.mode == "always")

    @property
    def away(self) -> bool:
        """The user sent her away (``send_away``) and she hasn't come back yet."""
        return self._away

    def send_away(self, minutes: float | None = None) -> str | None:
        """The user wants her out of the way: she leaves (in ``always`` mode too) and nothing brings her back, not
        talking to her nor pointing, until ``come_back`` or, with ``minutes``, until that time is up. None once done,
        else why not. Safe to call from any thread."""
        problem = self._running_problem()
        if problem:
            return problem
        if not self._on_loop():
            self._call_threadsafe(self.send_away, minutes)
            return None
        self._away = True
        self._cancel_timer(self._away_timer)
        self._away_timer = self._later(minutes * 60, self._time_up) if minutes else None
        self._unperch()
        self._hide()
        return None

    def come_back(self) -> str | None:
        """She's wanted on screen again after ``send_away``: she pops in now (in conversation mode she leaves again
        once the assistant has been idle a while, as usual). None once done, else why not. Safe from any thread."""
        problem = self._running_problem()
        if problem:
            return problem
        if not self._on_loop():
            self._call_threadsafe(self.come_back)
            return None
        self._return()
        self._show("always" if self.settings.mode == "always" else "talk")
        return None

    def _time_up(self) -> None:
        """The time she was sent away for is up: back only where she'd be anyway (always mode, or mid-conversation),
        not popping in on an idle desktop just to leave again."""
        self._away_timer = None
        self._return()
        if self._should_show():
            self._show("always" if self.settings.mode == "always" else "talk")

    def _return(self) -> None:
        self._away = False
        self._cancel_timer(self._away_timer)
        self._away_timer = None

    def define_prop(self, name: str, prop: dict[str, Any]) -> str | None:
        """A prop she made (``made_props``; ``prop`` as ``Prop.to_app`` gives it), new or changed: the app builds it at
        once (``prop-define``), and again should it restart. None once sent, else why it isn't yet (it will be when the
        app is ready). Safe to call from any thread."""
        return self._prop_change(name, prop)

    def remove_prop(self, name: str) -> str | None:
        """A prop she made is thrown away (``prop-remove``); out of her hand if she holds it. Like ``define_prop``."""
        if self._held_prop is not None and self._held_prop[0] == name:
            self._held_prop = None
            self._cancel_timer(self._prop_timer)
            self._prop_timer = None
        return self._prop_change(name, None)

    def hold_prop(self, name: str | None, hand: str | None = None, keep: bool = False) -> str | None:
        """She holds prop ``name`` (any she has) in ``hand`` (None: the prop's own) whenever no activity or moment needs
        her hands, or None to put it down (``prop-hold``). With ``keep`` (the user asked her to keep holding it) until
        another one; otherwise she puts it away herself once the conversation about it is over (``_put_away_prop``).
        None once sent, else why not. Safe to call from any thread."""
        if name and not needs_model(self.settings):
            return ORB_HOLDS_NO_PROPS
        problem = self._running_problem()
        if problem:
            return problem
        if not self._on_loop():
            self._call_threadsafe(self.hold_prop, name, hand, keep)
            return None
        self._held_prop = (name, hand) if name else None
        self._prop_kept, self._prop_turn = keep, self._turn
        self._cancel_timer(self._prop_timer)
        self._prop_timer = None
        if name:
            self._prop_talked_of()
        return None if self._send({"type": "prop-hold", "name": name, "hand": hand}) else "The avatar isn't responding."

    def _prop_talked_of(self) -> None:
        """Her prop came up again: the time to put it away counts from now."""
        if self._held_prop is None or self._prop_kept:
            return
        self._cancel_timer(self._prop_timer)
        self._prop_timer = self._later(PROP_IDLE_S, self._prop_time_up)

    def _prop_time_up(self) -> None:
        self._prop_timer = None
        self._put_away_prop()

    def _put_away_prop(self) -> None:
        """Out of her hand, unless the user asked her to keep holding it; it stays saved, to be held again later."""
        if self._held_prop is None or self._prop_kept:
            return
        log.debug("Putting the %s away", self._held_prop[0])
        self._held_prop = None
        self._cancel_timer(self._prop_timer)
        self._prop_timer = None
        self._send({"type": "prop-hold", "name": None, "hand": None})

    @property
    def held_prop(self) -> str | None:
        """The prop she was asked to hold (``hold_prop``), or None."""
        return self._held_prop[0] if self._held_prop is not None else None

    def _prop_change(self, name: str, prop: dict[str, Any] | None) -> str | None:
        if not self._on_loop():
            self._call_threadsafe(self._prop_change, name, prop)
            return self._running_problem()
        self._prop_changes[name] = prop
        self._send(_prop_message(name, prop))
        return self._running_problem()

    def _running_problem(self) -> str | None:
        proc = self._proc
        if self._closed or proc is None or proc.returncode is not None:
            return "The avatar isn't running."
        if not self._ready:
            return "The avatar is still starting."
        return None

    def _show(self, reason: str) -> None:
        self._cancel_hide()
        if self._away:
            return
        if not self._visible and self._ready:
            self._visible = True
            self._send({"type": "show", "reason": reason})
        self._hide_when_idle()

    def _hide_when_idle(self) -> None:
        """In conversation mode she leaves once the assistant has been idle ``hide_after_s``.

        Becoming idle schedules that, but a show while it already is idle (a wake word that led nowhere, a late
        announcement or point) comes with no state change: without scheduling it here she'd stay on screen for good.
        """
        if self._visible and self.settings.mode == "conversation" and self._state not in _BUSY:
            self._schedule_hide()

    def _hide(self) -> None:
        self._cancel_hide()
        if not self._visible or (self.settings.mode == "always" and not self._away):
            return
        self._visible = False
        self._send({"type": "hide"})
        self._forget_moves()  # the app ends pointing and any activity as she leaves

    def _schedule_hide(self) -> None:
        self._cancel_hide()
        self._hide_task = asyncio.get_running_loop().create_task(self._hide_later(), name="avatar-hide")
        self._tasks.add(self._hide_task)
        self._hide_task.add_done_callback(self._tasks.discard)

    async def _hide_later(self) -> None:
        await asyncio.sleep(self.settings.hide_after_s)
        if self._state not in _BUSY:
            self._hide()

    def _cancel_hide(self) -> None:
        task, self._hide_task = self._hide_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    # ------------------------------------------------------------------ pointing and activities

    @property
    def can_point(self) -> bool:
        """She can fly over and point right now: the app is running and ready, and ``avatar.point`` is on."""
        return self._point_problem() is None

    def point(self, x: int, y: int, *, box: tuple[int, int, int, int] | None = None, label: str = "") -> str | None:
        """Fly to and point at (x, y), physical pixels on the Windows desktop; None when sent, else why she can't.

        ``box`` (left, top, right, bottom, same pixels) is the thing's extent when it has a clear one, so she stands
        clear of all of it; ``label`` is a short name for it ("the apple"). Pointing again just moves her to the new
        spot. Safe to call from any thread: the message itself is always sent from the event loop.
        """
        problem = self._point_problem()
        if problem:
            return problem
        try:
            message = {
                "type": "point",
                "x": round(float(x)),
                "y": round(float(y)),
                "box": _box(box),
                "label": " ".join(str(label).split())[:_LABEL_CHARS],
            }
        except (TypeError, ValueError, OverflowError):
            return "Those coordinates aren't pixel positions."
        if self._on_loop():
            return self._point_now(message)
        self._call_threadsafe(self._point_now, message)
        return None

    def press(self) -> None:
        """The spot she's pointing at is being clicked: she jabs at it and the ring flashes (``press``). Nothing when
        she isn't pointing. Safe to call from any thread."""
        if not self._on_loop():
            self._call_threadsafe(self.press)
            return
        if self._pointing:
            self._send({"type": "press"})

    def _point_problem(self) -> str | None:
        if not self.settings.point:
            return "Pointing is turned off (avatar.point)."
        problem = self._running_problem()
        if problem:
            return problem
        if self._away:
            return "The user sent the avatar away (show_avatar brings her back)."
        return None

    def _point_now(self, message: dict[str, Any]) -> str | None:
        problem = self._point_problem()  # again: from another thread, things may have changed on the way
        if problem:
            return problem
        self._cancel_hide()
        self._end_activity(tell_app=False)  # pointing ends any activity: the app ends it by itself
        if not self._send(message):
            return "The avatar app isn't responding."
        self._visible = True  # the app shows her if she was away
        self._pointing = True
        if self._perched is not None:  # she flies over from the window she sat on, and home after
            self._perched, self._last_perch = None, self._clock()
        self._cancel_timer(self._point_timer)
        self._point_timer = self._later(POINT_MAX_S, self._unpoint)
        self._hide_when_idle()
        return None

    def _linger(self) -> None:
        """The reply is over: she keeps pointing ``point_linger_s`` more, long enough to find the spot, then flies
        home, rather than pointing on at an old answer while the user moves on. The app is told how long is left, so
        the ring can fade out over it."""
        seconds = self.settings.point_linger_s
        if seconds <= 0:
            self._unpoint()
            return
        self._cancel_timer(self._point_timer)
        self._point_timer = self._later(seconds, self._unpoint)
        self._send({"type": "point-ending", "seconds": seconds})

    def _unpoint(self) -> None:
        """Stop pointing and fly home; she stays on screen."""
        self._cancel_timer(self._point_timer)
        self._point_timer = None
        if self._pointing:
            self._pointing = False
            self._send({"type": "unpoint"})

    def _start_activity(self, name: str) -> None:
        """Tell the app she's busy with ``name`` (its pose and prop), only while she's there; it ends by itself after
        ACTIVITY_MAX_S, in case nothing else ends it."""
        if not self._on_screen():
            return
        if self._activity != name:
            if not self._send({"type": "activity", "name": name}):
                return
            self._activity = name
        self._cancel_timer(self._activity_timer)
        self._activity_timer = self._later(ACTIVITY_MAX_S, self._end_activity)

    def _end_activity(self, *, tell_app: bool = True) -> None:
        self._cancel_timer(self._activity_timer)
        self._activity_timer = None
        if self._activity is not None:
            self._activity = None
            if tell_app:
                self._send({"type": "activity", "name": None})

    def _moment(self, name: str) -> bool:
        """Play a one-shot moment in the app (``tick``, ``timer_start``...), only while she's there. Whether it went."""
        if not self._on_screen():
            return False
        return self._send({"type": "moment", "name": name})

    async def _watch_front(self) -> None:
        """A look at the window in front every ``FRONT_POLL_S`` (``_look_front``) for as long as the bridge runs."""
        while True:
            await asyncio.sleep(FRONT_POLL_S)
            self._look_front()

    def _look_front(self) -> None:
        """While she's on screen: she glances at the window the user switched to, and a big new one popping up by
        itself makes her jump (only while the assistant is idle: what it opens itself is no surprise). Not at her own
        windows, and not while she points. And she may sit on the window in front, or leave it (``_consider_perch``)."""
        if not (self._ready and self._on_screen()):
            self._front.reset()  # what's in front when she's back isn't a switch she saw
            return
        try:
            switch = self._front.look()
        except Exception:
            log.debug("Couldn't look at the window in front", exc_info=True)
            return
        self._consider_perch()
        if switch is None or switch.front.pid == self.app_pid or self._pointing:
            return
        if switch.popped_up and self._state is AssistantState.IDLE:
            self._startle()
        now = self._clock()
        if self.settings.glance and now - self._last_glance >= GLANCE_GAP_S:
            self._last_glance = now
            x, y = switch.front.centre
            self._send({"type": "glance", "x": x, "y": y})

    def _hover(self, over: bool) -> None:
        """The pointer came to rest on her (Ctrl is watched from now on) or left her (no longer)."""
        if over and self._ctrl_watch is None and not self._closed:
            self._ctrl = False
            self._ctrl_watch = asyncio.create_task(self._watch_ctrl(), name="avatar-ctrl")
            self._tasks.add(self._ctrl_watch)
            self._ctrl_watch.add_done_callback(self._tasks.discard)
        elif not over and self._ctrl_watch is not None:
            task, self._ctrl_watch = self._ctrl_watch, None
            task.cancel()

    async def _watch_ctrl(self) -> None:
        """Tells the app whenever Ctrl goes down or up (``keys``), every ``CTRL_POLL_S``, until ``_hover(False)``."""
        while True:
            try:
                held = bool(self._read_ctrl())
            except Exception:
                log.debug("Couldn't read Ctrl", exc_info=True)
                held = False
            if held != self._ctrl and self._send({"type": "keys", "ctrl": held}):
                self._ctrl = held
            await asyncio.sleep(CTRL_POLL_S)

    def _consider_perch(self) -> None:
        """Sat on a window: home again once it stops being in front, moves, or there's no reason to sit any more.
        Otherwise: sits on the window in front, if now's the time and it has room for her."""
        front = self._front.front
        if self._perched is not None:
            if not self._may_sit() or front is None or (front.hwnd, front.frame) != self._perched:
                self._unperch()
            return
        if not self._may_sit() or self._clock() - self._last_perch < PERCH_REST_S:
            return
        if front is None or front.pid == self.app_pid:
            return
        left, top, right, bottom = front.frame
        if top - front.monitor[1] < PERCH_ROOM_PX or right - left < PERCH_WIDTH_PX:
            return  # maximised, or too near the top of the screen, or too small to sit on
        self._perched = (front.hwnd, front.frame)
        self._send({"type": "perch", "left": left, "top": top, "right": right, "bottom": bottom})

    def _may_sit(self) -> bool:
        """Whether it's a time to sit on a window: it's on, she's always on screen, a build of hers runs (one she's
        heard nothing of for ``BUILD_MAX_S`` is over), the assistant is idle and she isn't pointing."""
        now = self._clock()
        for improvement, started in list(self._building.items()):
            if now - started > BUILD_MAX_S:
                del self._building[improvement]
        return (
            self.settings.perch
            and self.settings.mode == "always"
            and bool(self._building)
            and self._state is AssistantState.IDLE
            and not self._pointing
        )

    def _unperch(self) -> None:
        """Home from the window she sits on, if she does."""
        if self._perched is None:
            return
        self._perched = None
        self._last_perch = self._clock()
        self._send({"type": "unperch"})

    def _startle(self) -> None:
        """Something made her jump (a bang, a big window popping up by itself): a jolt and a surprised face, or, if
        she's napping, she wakes with a start. Only while she's there and not pointing, at most every
        ``STARTLE_GAP_S``, unless ``avatar.startle`` is off."""
        now = self._clock()
        if not self.settings.startle or self._pointing or now - self._last_startle < STARTLE_GAP_S:
            return
        if self._on_screen():
            self._last_startle = now
            self._send({"type": "startle"})

    def _victory_lap(self) -> None:
        """The last task due today is done: a lap round the screen, if it's on, she's there, and she isn't busy
        pointing at something."""
        if not self.settings.victory_lap or self._pointing or not self._on_screen():
            return
        self._send_mood("excited", 0.9, 4)
        self._send({"type": "lap"})

    def _impatient(self, call_id: str) -> None:
        """A call she's waiting on is taking a while: arms crossed, tapping, unless she has something to show for it
        now (a search's prop), or it's done, or the turn is."""
        self._waits.pop(call_id, None)
        if call_id not in self._calls or self._activity is not None or self._state is not AssistantState.THINKING:
            return
        if self._on_screen():
            self._send({"type": "gesture", "name": "impatient"})
            self._send_mood("angry", 0.3, 3)

    def _rainboom(self) -> None:
        """A sonic rainboom, if it's on, she's there to do it, and the last one isn't too recent. One that can't go off
        yet stays owed (``_rainboom_owed_until``) for the next thing she says."""
        if not self.settings.rainboom:
            return
        now = self._clock()
        if now - self._last_rainboom < RAINBOOM_COOLDOWN_S:
            self._rainboom_owed_until = -math.inf  # it's been one just now: that one counts
            return
        if not self._moment("rainboom"):
            return  # she isn't there: it stays owed, if it was
        self._last_rainboom = now
        self._rainboom_owed_until = -math.inf

    def _forget_moves(self) -> None:
        """The app has stopped pointing (or sitting on a window) and dropped any activity by itself: it hid her, or
        it's a new app."""
        self._pointing = False
        self._perched = None
        self._cancel_timer(self._point_timer)
        self._point_timer = None
        self._end_activity(tell_app=False)

    def _later(self, seconds: float, callback: Callable[[], None]) -> asyncio.TimerHandle | None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return None
        return loop.call_later(seconds, callback)

    @staticmethod
    def _cancel_timer(timer: asyncio.TimerHandle | None) -> None:
        if timer is not None:
            timer.cancel()

    def _on_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:  # no loop in this thread
            return False

    # ------------------------------------------------------------------ lip sync

    def _lip_sync_open(self) -> bool:
        """Speech plays: a reply (between SpeakingStarted and SpeakingFinished) or a short phrase."""
        return self._speaking or time.monotonic() < self._phrase_until

    def on_render(self, block: np.ndarray) -> None:
        """The speaker's render tap (audio thread): queue the block while speech plays. Never blocks."""
        if self._worker is None or not self._lip_sync_open():
            return
        with contextlib.suppress(queue.Full):  # the analysis fell behind: skip rather than stall the audio
            self._blocks.put_nowait(block)

    def _lip_sync_loop(self) -> None:
        """Worker thread: audio blocks in, mouth frames out (about 50 a second while speaking)."""
        was_speaking = False
        closed_sent = True  # a silent frame is sent once, to close the mouth, not 50 times a second
        while True:
            try:
                block = self._blocks.get(timeout=0.25)
            except queue.Empty:
                if was_speaking and not self._lip_sync_open():
                    self._analyzer.reset()
                    was_speaking, closed_sent = False, True
                continue
            if block is None:
                return
            was_speaking = True
            try:
                frames = self._analyzer.feed(block)
            except Exception:
                log.debug("Lip sync analysis failed for a block", exc_info=True)
                continue
            for frame in frames:
                message = {
                    "type": "mouth",
                    "level": round(float(frame.level), 3),
                    "v": [round(float(w), 3) for w in frame.weights],
                }
                silent = message["level"] == 0 and not any(message["v"])
                # Silence inside a reply (a tool call runs) isn't talking: the renderer's own silence timeout
                # then lets the talking body language rest.
                if silent and closed_sent:
                    continue
                closed_sent = silent
                self._call_threadsafe(self._send_mouth, message)

    def _notice(self, message: str, level: int = logging.INFO) -> None:
        self.events.emit(Notice(source=NOTICE_SOURCE, message=message, level=level))
