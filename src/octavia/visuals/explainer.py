"""Explainers: a panel that plays as she talks (docs/visuals.md, "Explainers").

An explainer is an ordinary panel (any blocks, a scene of her own elements most often) plus a timeline of steps.
Each step is a handful of the same edits ``update_visual`` makes (add, change, remove, arrange, highlight...), with
what lights up as it plays (``pulse``), what her pointer goes to (``point``) and a short caption. Nothing in it is
about any one topic: a search tree, a cell dividing or a proof is the same few pieces, put together by her.

She ties her sentences to the steps with beats: ``{{step-id}}`` in her reply (``tts/shown.py``), never said or
shown. The chunker carries a sentence's beats on its segment, and as that sentence starts playing
(``SpeechSegmentStarted``) the ``Director`` moves the panel to that step, so the picture changes as she says it. A
reply without beats still moves on, a step a sentence. What she says for each step is kept, so the user can pause,
step back and forth, and replay it with her voice (the controls on the panel, or ``explainer_control``); talking
over her pauses it where it is.

Every step's whole panel is worked out (and checked) when the explainer is made, so a step that doesn't fit is
refused before anything is shown, and going back is just showing an earlier one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import Field, ValidationError, model_validator

from octavia.core.events import BrainStarted, Event, EventBus, SpeakingFinished, SpeechSegmentStarted
from octavia.visuals.host import PanelEvent, PanelHost, Shown
from octavia.visuals.spec import ELEMENT_ID, MAX_EDITS, Edit, EditError, Visual, _Part, apply_edits

log = logging.getLogger(__name__)

MAX_STEPS = 24
ADDRESS = Annotated[str, Field(max_length=60)]
CONTROLS = ("play", "pause", "next", "back", "replay", "step")


class ExplainStep(_Part):
    """One step of an explainer: what changes on the panel as she says its sentence."""

    id: str = Field(pattern=ELEMENT_ID, description="Its beat: start the sentence for this step with {{id}}.")
    caption: str = Field(default="", max_length=160, description="A few words shown under the panel as it plays.")
    edits: list[Edit] = Field(
        default_factory=list,
        max_length=MAX_EDITS,
        description="What changes, as update_visual's edits: add, change (move, restyle, retext), remove, arrange, "
        "highlight, step_state, append_text... Moves ease, new things pop in, removed ones fade.",
    )
    pulse: list[ADDRESS] = Field(default_factory=list, max_length=8, description="Addresses that flash as it plays.")
    point: ADDRESS = Field(default="", description="The address her pointer goes to for this step (none: it rests).")
    hold_s: float = Field(default=3.5, ge=0.5, le=30, description="How long it plays when replayed without a voice.")


class Explainer(Visual):
    """A panel and the steps it plays through (index 0 of ``states()`` is the opening picture)."""

    setup: list[Edit] = Field(
        default_factory=list,
        max_length=MAX_EDITS,
        description="Edits made before it opens (arrange the opening picture: a tree, a row, a circle).",
    )
    steps: list[ExplainStep] = Field(min_length=1, max_length=MAX_STEPS)

    @model_validator(mode="after")
    def _plays(self) -> Explainer:
        ids = [s.id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError(
                f"step ids must be unique; listed twice: {', '.join(sorted({i for i in ids if ids.count(i) > 1}))}"
            )
        self.states()  # every step is checked now, not halfway through her explanation
        return self

    def opening(self) -> Visual:
        base = Visual.model_validate(
            {
                "title": self.title,
                "blocks": self.model_dump(by_alias=True)["blocks"],
                "width": self.width,
                "overlays": [o.model_dump() for o in self.overlays],
            }  # fmt: skip
        )
        if not self.setup:
            return base
        try:
            return apply_edits(base, self.setup)
        except (EditError, ValidationError) as exc:
            raise ValueError(f"setup: {exc}") from None

    def states(self) -> list[Visual]:
        """The whole panel at the opening and after each step."""
        out = [self.opening()]
        for step in self.steps:
            try:
                out.append(apply_edits(out[-1], step.edits) if step.edits else out[-1])
            except (EditError, ValidationError) as exc:
                raise ValueError(f"step {step.id!r}: {exc}") from None
        return out


class PlayerStep(_Part):
    id: str
    caption: str = ""


class Player(_Part):
    """What the panel's player bar shows: the steps, which one it's at (0: the opening), and whether it plays."""

    steps: list[PlayerStep]
    at: int = 0
    playing: bool = False
    voiced: bool = False  # it has her lines to replay


class StepView(Visual):
    """A panel as one step of an explainer shows it: the step's picture, its pointer and pulses, and the player."""

    pointer: str = ""
    pulse: list[str] = Field(default_factory=list)
    player: Player | None = None


@dataclass
class _Run:
    pid: str
    explainer: Explainer
    states: list[Visual]
    stay_s: float | None
    at: int = 0  # 0: the opening; k: after step k
    shown_at: int = -1
    lines: list[list[str]] = field(default_factory=list)  # what she said for each step
    listening: bool = True  # her reply is narrating it: sentences are taken as its lines
    auto: bool = True  # no beat yet: each sentence moves on a step
    playing: bool = False
    replaying: bool = False  # her voice is replaying it (a cut-in pauses it)
    timer: asyncio.Task[None] | None = None
    pointed: tuple[int, ...] = ()
    showing: asyncio.Task[None] | None = None

    @property
    def count(self) -> int:
        return len(self.explainer.steps)


class Director:
    """Plays explainers on her panels in step with her voice.

    ``speak(text)`` says a replay (her voice, with its beats); ``stop_speaking()`` stops her mid-sentence (pause);
    ``point(x, y, box, label)`` sends the orb to point at a spot of the screen (physical pixels). Any may be None.
    """

    def __init__(
        self,
        host: PanelHost,
        events: EventBus,
        *,
        speak: Callable[[str], Any] | None = None,
        stop_speaking: Callable[[], Any] | None = None,
        point: Callable[[int, int, tuple[int, int, int, int], str], Any] | None = None,
    ) -> None:
        self.host = host
        self.events = events
        self.speak = speak
        self.stop_speaking = stop_speaking
        self.point = point
        self.runs: dict[str, _Run] = {}
        self._unsubscribe = [
            events.subscribe(self._on_event, SpeechSegmentStarted, SpeakingFinished, BrainStarted),
            host.subscribe(self._on_panel),
        ]

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        for stop in self._unsubscribe:
            stop()
        for run in self.runs.values():
            self._stop_timer(run)

    # ------------------------------------------------------------------ starting one

    async def start(self, explainer: Explainer, *, panel_id: str | None = None, stay_s: float | None = None) -> Shown:
        """Shows ``explainer`` at its opening picture and listens for its beats. Raises what ``host.show`` raises."""
        states = explainer.states()
        pid = panel_id or self.host.new_id(explainer.title)
        if (old := self.runs.pop(pid, None)) is not None:
            self._stop_timer(old)
        run = _Run(pid, explainer, states, stay_s, lines=[[] for _ in explainer.steps])
        shown = await self.host.show(self._view(run, 0), panel_id=pid, stay_s=stay_s)
        run.shown_at = 0
        for other in self.runs.values():  # only the newest one follows her voice
            other.listening = other.auto = False
        self.runs[shown.id] = run
        self.host.drive(shown.id, lambda msg, pid=shown.id: self._on_page(pid, msg))
        return shown

    @property
    def active(self) -> _Run | None:
        """The explainer her voice drives: the newest one still up."""
        up = self.host.open_panels
        live = [r for r in self.runs.values() if r.pid in up]
        return live[-1] if live else None

    # ------------------------------------------------------------------ her voice

    def _on_event(self, event: Event) -> None:
        if isinstance(event, SpeechSegmentStarted):
            self._sentence(event)
        elif isinstance(event, SpeakingFinished):
            for run in self.runs.values():
                if run.replaying or run.listening:
                    run.playing = run.replaying = False
                    run.listening = run.auto = False
                    self._go(run, run.at)  # the bar shows it stopped (cut off, or simply done)
        elif isinstance(event, BrainStarted):
            for run in self.runs.values():  # a new turn: what she says now is about something else
                run.listening = run.auto = False

    def _sentence(self, event: SpeechSegmentStarted) -> None:
        run = self.active
        if run is None:
            return
        ids = [s.id for s in run.explainer.steps]
        named = [ids.index(b) for b in event.beats if b in ids]
        if named:
            run.auto = False
            target = named[-1] + 1
        elif run.listening and run.auto and run.at < run.count:
            target = run.at + 1
        elif run.listening or run.replaying:
            target = run.at
        else:
            return
        if run.listening and target > 0:
            run.lines[target - 1].append(str(event.text).strip())
        run.playing = run.listening or run.replaying
        self._go(run, target)

    # ------------------------------------------------------------------ the controls

    async def control(self, panel_id: str, action: str, step: str | int | None = None) -> str:
        """Plays, pauses, steps or replays an explainer; returns what it did, in words."""
        run = self.runs.get(panel_id)
        if run is None or panel_id not in self.host.open_panels:
            raise KeyError(panel_id)
        if action not in CONTROLS:
            raise ValueError(f"no control {action!r} (one of {', '.join(CONTROLS)})")
        if action == "pause":
            self._halt(run)
            self._go(run, run.at)
            return f"Paused at step {run.at} of {run.count}."
        if action in ("next", "back", "step"):
            self._halt(run)
            if action == "step":
                ids = [s.id for s in run.explainer.steps]
                if isinstance(step, int) or (isinstance(step, str) and step.isdigit()):
                    target = int(step)
                elif step in ids:
                    target = ids.index(step) + 1
                else:
                    raise ValueError(f"no step {step!r} (steps: {', '.join(ids)}, or a number from 0)")
            else:
                target = run.at + (1 if action == "next" else -1)
            self._go(run, max(0, min(run.count, target)))
            return f"At step {max(0, min(run.count, target))} of {run.count}."
        # play / replay
        self._halt(run)
        start = 0 if action == "replay" or run.at >= run.count else run.at
        self._go(run, start)
        rest = run.explainer.steps[start:]
        lines = [(s, " ".join(run.lines[start + k]) or s.caption) for k, s in enumerate(rest)]
        if self.speak is not None and any(line for _, line in lines):
            run.replaying = run.playing = True
            text = " ".join(f"{{{{{s.id}}}}} {line}" for s, line in lines if line)
            result = self.speak(text)
            if asyncio.iscoroutine(result):
                task = asyncio.get_running_loop().create_task(result)
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
            self._go(run, start)
            return "Playing it again with my voice." if action == "replay" else "Playing on with my voice."
        run.playing = True
        run.timer = asyncio.get_running_loop().create_task(self._autoplay(run))
        self._go(run, start)
        return "Playing it without a voice."

    def _halt(self, run: _Run) -> None:
        self._stop_timer(run)
        if run.replaying and self.stop_speaking is not None:
            with contextlib.suppress(Exception):
                self.stop_speaking()
        if run.listening and run.playing and self.stop_speaking is not None:
            with contextlib.suppress(Exception):
                self.stop_speaking()  # pausing her live explanation stops her too
        run.playing = run.replaying = run.listening = run.auto = False

    def _stop_timer(self, run: _Run) -> None:
        if run.timer is not None and not run.timer.done():
            run.timer.cancel()
        run.timer = None

    async def _autoplay(self, run: _Run) -> None:
        while run.at < run.count and run.playing:
            await asyncio.sleep(run.explainer.steps[run.at].hold_s if run.at else 1.0)
            if not run.playing:
                return
            self._go(run, run.at + 1)
        run.playing = False
        self._go(run, run.at)

    # ------------------------------------------------------------------ the page

    def _on_page(self, pid: str, msg: dict[str, Any]) -> None:
        run = self.runs.get(pid)
        if run is None:
            return
        if msg.get("type") == "control":
            action = str(msg.get("action") or "")
            step = msg.get("step")
            task = asyncio.get_running_loop().create_task(self.control(pid, action, step if step is not None else None))
            task.add_done_callback(
                lambda t: t.cancelled() or (t.exception() and log.info("Explainer control: %s", t.exception()))
            )
        elif msg.get("type") == "pointer" and self.point is not None:
            rect = msg.get("rect")
            if not isinstance(rect, dict) or run.at == 0:
                return
            try:
                x, y, w, h = (int(round(float(rect[k]))) for k in ("x", "y", "w", "h"))
            except (KeyError, TypeError, ValueError):
                return
            where = (x, y, w, h)
            if run.pointed == (run.at, *where):
                return
            run.pointed = (run.at, *where)
            step = run.explainer.steps[run.at - 1]
            with contextlib.suppress(Exception):
                self.point(x + w // 2, y + h // 2, (x, y, x + w, y + h), step.caption or step.point)

    def _on_panel(self, event: PanelEvent) -> None:
        run = self.runs.pop(event.id, None)
        if run is not None:
            self._halt(run)

    # ------------------------------------------------------------------ showing a step

    def _view(self, run: _Run, at: int) -> StepView:
        state = run.states[at]
        step = run.explainer.steps[at - 1] if at else None
        player = Player(
            steps=[PlayerStep(id=s.id, caption=s.caption) for s in run.explainer.steps],
            at=at,
            playing=run.playing,
            voiced=any(run.lines),
        )
        return StepView.model_validate(
            {
                **state.model_dump(by_alias=True),
                "pointer": step.point if step else "",
                "pulse": list(step.pulse) if step and at != run.shown_at else [],
                "player": player.model_dump(),
            }
        )

    def _go(self, run: _Run, at: int) -> None:
        """Moves the panel to step ``at`` (shown in the background; a newer move wins over one still on its way)."""
        run.at = at
        if run.showing is None or run.showing.done():
            run.showing = asyncio.get_running_loop().create_task(self._show_latest(run))

    async def _show_latest(self, run: _Run) -> None:
        shown: tuple[int, bool, bool] | None = None
        while shown != (run.at, run.playing, any(run.lines)) and run.pid in self.host.open_panels:
            shown = (run.at, run.playing, any(run.lines))
            view = self._view(run, run.at)
            try:
                await self.host.show(view, panel_id=run.pid, stay_s=run.stay_s, sound=False)
            except Exception as exc:  # the panel went (closed under it) or the app failed: nothing to move
                log.info("An explainer step couldn't be shown: %s", exc)
                return
            run.shown_at = shown[0]
