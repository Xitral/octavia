"""Mistakes she catches herself: requests she got wrong, found in her own records and kept as cases of her eval.

Nothing here asks Claude or the local model: it's counting over what she already records (``data/routing_log.jsonl``,
and for one signal which app was in front when), run by the night shift and ``octavia evolve mistakes scan``. A
request of the user's that she answered (a brain turn) counts as gone wrong when, right after it:

* **corrected**: the user says again what they wanted: "no, I meant...", "I just told you to...", "no, I want you to";
* **repeated**: the user asks the same thing again, in the same words or others;
* **complained**: "that's not what I asked", "why are you just sitting there", "that didn't work";
* **noted**: whatever the user said, she took it as a mistake of hers and noted it to fix (``note_improvement``):
  her own admission, on words the other signals don't know;
* **by hand**: she used a tool on an app (Spotify, TickTick...), it went wrong (an error, the user cut her off, or
  she fell back to driving the screen) and the user then brought that app to the front themselves. Only which app
  was in front when is read (screen history's timeline, Futures' watch), never what it showed; browsers never count.

Each becomes a case of her eval (``evals``): the request, and what she should have done, read from how the user's
correction or second try was answered, when that answer wasn't corrected in turn:

* tools she used there that she hadn't the first time: she should call one of them (``expect``). Not after a
  complaint: what she does then is often looking into it (checking her builds, looking at the screen), not the
  answer;
* driving the screen (click, keys, scrolling...) the first time and not then: she shouldn't (``forbid``).

When both times she did the same, the user was correcting the facts ("I meant Steam", she misheard), not what she
did: her eval only grades what she does, so nothing is kept. Nor when she used no tool and nothing settled it.
Otherwise, with nothing known to grade, the case is only listed, for the user to fill in
(``octavia evolve mistakes keep N --expect tool``).

New cases are candidates. One is graded (``gating``) once the user keeps it, or by itself once it has a tool to
expect and has happened again on another turn, the same request after the same words, settled the same way, while
there's room (``evals.MAX_CAUGHT``, the check's time). A short request ("try it now") is never counted together with
another: it means something new every time. Graded
cases stay for good, so a fix that later breaks one is caught; one the user drops stays in the file as ``dropped``
and isn't caught again. The night shift starts a fix for graded cases she fails. A case that fails before a build
and after it never fails the build: the gate only counts requests she handled before.

The file is ``data/eval/caught.json``, outside the code: no build can change what it's graded on. It holds the
user's words, like the routing log it comes from.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from octavia.evolve import evals
from octavia.evolve.evals import CAUGHT_FILE, CAUGHT_PREFIX, EVAL_DIR, SOURCES

log = logging.getLogger(__name__)

FOLLOWS_S = 120.0  # what the user says this soon after her answer ended is about it
MONEY_NEAR_S = 15 * 60.0  # no case from a turn this close to one where her money tools ran
BY_HAND_S = 90.0  # the user brings the app forward this soon after her turn ended
MIN_WORDS = 4  # a request shorter than this ("yes", "do it now") makes no case on its own
MIN_KEY_WORDS = 3  # content words a request needs to be counted together with another one like it
SIMILAR = 0.5  # share of content words two requests have in common to be the same one asked again
USER_SOURCES = frozenset({"voice", "text", "discord", "window"})
BASE = {
    "corrected": 0.6,
    "repeated": 0.5,
    "complained": 0.4,
    "noted": 0.4,
    "by_hand": 0.3,
}  # how sure each signal is, before what backs it up
SHOWN = 8  # cases listed by voice

# Driving the screen by hand: her fallback when a proper tool should have done it.
SCREEN_DRIVING = frozenset({"click", "find_on_screen", "press_keys", "type_text", "scroll", "drag"})
# Tools that are never the answer to a request: her noting that she failed.
BOOKKEEPING = frozenset({"note_improvement"})
_BROWSERS = ("vivaldi", "chrome", "msedge", "firefox", "brave", "opera", "arc", "iexplore")
# Parts of tool names that name no app (``media_control``, ``look_at_screen``...).
_NOT_APPS = frozenset(
    {"media", "control", "screen", "window", "files", "image", "music", "volume", "about", "status", "focus", "voice"}
)

# The user says again what they wanted: "no, I meant", "I just told you to", "no, I want you to", "no, don't".
_RESTATED = re.compile(
    r"^\W*(?:no|nope|nah)\b[\s,.!]*(?:i (?:mean|meant|said|want|wanted|asked)|don'?t|do not|not (?:that|the|this|what))\b"
    r"|\bi meant\b|\bi didn'?t mean\b|\bnot what i (?:asked|said|meant|wanted)\b"
    r"|\bi (?:just )?(?:told|asked) you (?:to|for)\b|\bi said (?:to|play|open|use|yes)\b"
    r"|^\W*(?:(?:no|nope|okay|ok|um|uh|well)\W+)*i want(?:ed)? you to\b",
    re.I,
)
# Or only that it went wrong: "that's not it", "why are you just sitting there", "that didn't work".
_COMPLAINT = re.compile(
    r"\bthat'?s not (?:it|what|the|right|a)\b|\bthat isn'?t\b|\bisn'?t really\b|\binstead you\b"
    r"|\bwrong (?:one|thing|song|app|place)\b|\byou didn'?t\b|\bwhy (?:are|did|didn'?t|do|is) you\b"
    r"|\bstill (?:haven'?t|not|nothing)\b|\bsitting there\b|\bdoing nothing\b|\bdidn'?t work\b|\bisn'?t working\b"
    r"|\bnot working\b|\byou forgot\b",
    re.I,
)
_BARE = re.compile(r"^\W*(?:no|nope|nah|wait|huh)\W*$", re.I)  # "No." on its own: what follows says why
# A request that leans on what came before ("try it now", "do that again").
_LEANS = re.compile(r"\b(?:it|that|this|them|those|now|again|instead|there)\b", re.I)
_FILLER = frozenset(
    "a an the and or but so to of in on at for with is are was be it its this that these those i me my you your "
    "can could would will please um uh like hey okay ok alright octavia just go ahead do does did what whats "
    "how some any up there".split()
)


class MistakeError(Exception):
    """A request the pool can't do (a case that isn't there, no room to grade it): words for the user."""


# ---------------------------------------------------------------------------------------------------- the cases


@dataclass
class Caught:
    n: int
    text: str  # what the user asked, word for word
    signal: str  # corrected | repeated | complained | noted | by_hand
    status: str = "candidate"  # candidate | gating | dropped
    source: str = "voice"
    before: list[str] = field(default_factory=list)  # the user's request just before, when this one leans on it
    expect: list[str] = field(default_factory=list)  # evals' groups: "a|b" = one of these must be called
    forbid: list[str] = field(default_factory=list)
    no_tools: bool = False
    judge: str = ""  # what a good reply does, in words: graded on her reply by the eval's judge (``evals.Case.judge``)
    # A drafted ``judge`` (``evolve/rubrics.py``): grades nothing until the user keeps the case; ``tried`` once drafted.
    suggested: str = ""
    tried: bool = False
    did: list[str] = field(default_factory=list)  # the tools she used
    then: list[str] = field(default_factory=list)  # the tools of the answer that settled it
    said_after: str = ""  # the user's correction, or second try
    app: str = ""  # by hand: the app the user went to
    log: str = ""  # the turn's trace id in the routing log
    traces: list[str] = field(default_factory=list)  # every turn it was seen in
    seen: int = 1
    agreed: int = 1  # sightings settled the same way (the same tools to expect): what grades it by itself
    found: float = 0.0
    last_seen: float = 0.0
    error: str = ""  # the turn's error, when it had one
    kept: bool = False  # the user said to grade it
    note: str = ""  # why it was dropped, or what the user changed

    @property
    def id(self) -> str:
        return f"{CAUGHT_PREFIX}{self.n}"

    @property
    def gradable(self) -> bool:
        return bool(self.expect or self.forbid or self.no_tools or self.judge)

    @property
    def confidence(self) -> float:
        score = BASE.get(self.signal, 0.3) + (0.2 if self.expect else 0.0) + (0.1 if self.error else 0.0)
        return round(min(1.0, score + 0.2 * (self.agreed - 1)), 2)

    @property
    def key(self) -> str:
        """Two sightings with this key are one mistake; "" (a short request) is never one with another."""
        words = _key(self.text)
        if len(words.split()) < MIN_KEY_WORDS:
            return ""
        return " / ".join([*(_key(b) for b in self.before), words])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Caught:
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in data.items() if k in known})

    def expected(self) -> str:
        """What she should do, in words."""
        parts = [f"call {' or '.join(g.split('|'))}" for g in self.expect]
        if self.forbid:
            parts.append(f"not call {' or '.join(self.forbid)}")
        if self.no_tools:
            parts.append("answer without tools")
        if self.judge:
            parts.append(f"a reply that {self.judge[:1].lower()}{self.judge[1:]}")
        return "; ".join(parts) if parts else "not known yet"


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _FILLER]


def _key(text: str) -> str:
    return " ".join(_words(text))


def similar(a: str, b: str) -> bool:
    """The same request asked again: most of its content words come back."""
    x, y = set(_words(a)), set(_words(b))
    shared = x & y
    return len(shared) >= 2 and len(shared) / len(x | y) >= SIMILAR


# ---------------------------------------------------------------------------------------------------- finding them


def is_finance_tool(name: str) -> bool:
    from octavia.finance.privacy import is_finance_tool as money

    return money(name)


def find(
    turns: list[dict[str, Any]], fronted: Iterable[tuple[float, str]] = (), excluded: Iterable[str] = ()
) -> list[Caught]:
    """The requests that went wrong in ``turns`` (routing-log rows, any order), as new cases numbered 0. ``fronted``:
    (when, process) of each app the user brought to the front, for the by-hand signal; never the apps the user keeps
    out of screen history (``excluded``, with the built-in list)."""
    user_apps = list(excluded)
    turns = sorted((t for t in turns if str(t.get("source") or "voice") in USER_SOURCES), key=_ts)
    apps = sorted(
        ((float(ts), str(p).lower()) for ts, p in fronted if not _excluded(str(p), user_apps)), key=lambda a: a[0]
    )
    out: list[Caught] = []
    # The user's money stays out of eval cases (they're sent to Claude again): no case from a turn within
    # MONEY_NEAR_S of one where her money tools ran (the redo that called them, the turns around it).
    money = [_ts(t) for t in turns if any(is_finance_tool(name) for name in _tools(t))]
    for i, turn in enumerate(turns):
        if turn.get("route") != "brain" or len(str(turn.get("text") or "").split()) < MIN_WORDS:
            continue
        if any(abs(_ts(turn) - ts) <= MONEY_NEAR_S for ts in money):
            continue
        found = _corrected_or_repeated(turns, i) or _by_hand(turns, i, apps)
        if found is not None:
            out.append(found)
    return out


def _ts(turn: dict[str, Any]) -> float:
    return float(turn.get("ts") or 0)


def _end(turn: dict[str, Any]) -> float:
    return _ts(turn) + float(turn.get("duration_s") or 0)


def _follows(before: dict[str, Any], turn: dict[str, Any]) -> bool:
    return 0 <= _ts(turn) - _end(before) <= FOLLOWS_S


def _tools(turn: dict[str, Any]) -> set[str]:
    return {str(t) for t in turn.get("tools") or []} - BOOKKEEPING


def _went_wrong(turn: dict[str, Any]) -> bool:
    return bool(turn.get("error")) or turn.get("ok") is False or bool(turn.get("interrupted"))


def _signal(turn: dict[str, Any], after: dict[str, Any]) -> str | None:
    """Whether ``after`` (the user's next words) says ``turn`` went wrong, and how."""
    return signal(str(turn.get("text") or ""), str(after.get("text") or ""))


def signal(request: str, said: str) -> str | None:
    """Whether ``said``, the user's next words after she answered ``request``, says that answer went wrong:
    "corrected", "repeated" or "complained", or None. Also how her self-scoring judges each reply
    (``futures.selfscore``), so the night's scan and the reply-by-reply verdict agree."""
    if _RESTATED.search(said):
        return "corrected"
    if similar(request, said):
        return "repeated"
    if _COMPLAINT.search(said):
        return "complained"
    return None


def _corrected_or_repeated(turns: list[dict[str, Any]], i: int) -> Caught | None:
    turn = turns[i]
    j = i + 1
    # "No." on its own: the turn after says what was wrong.
    while j < len(turns) and _follows(turns[j - 1], turns[j]) and _BARE.match(str(turns[j].get("text") or "")):
        j += 1
    if j >= len(turns) or not _follows(turns[j - 1], turns[j]):
        return None
    signal = _signal(turn, turns[j]) or ("noted" if _noted(turn, turns[j]) else None)
    if signal is None:
        return None
    settled = _settled(turns, j)
    did = _tools(turn)
    then = _tools(settled) if settled is not None else None
    if then is not None and then == did:
        return None  # she did the same again and the user moved on: the facts were corrected, not what she did
    if not did and not then:
        return None  # talk both times: nothing her eval could grade
    case = _case(turn, signal, str(turns[j].get("text") or ""), _before(turns, i))
    case.then = sorted(then or ())
    if then is not None:
        new = then - did
        if signal not in ("complained", "noted"):  # after a complaint she looks into it: not the answer
            case.expect = ["|".join(sorted(new))] if new else []
        case.forbid = sorted((did - then) & SCREEN_DRIVING)
    return case


def _noted(turn: dict[str, Any], after: dict[str, Any]) -> bool:
    """She noted a mistake to fix (``note_improvement``) on the user's next words, and they're about this request:
    a word of it comes back. (Noting a bug the user brought up next, about something else, says nothing of it.)"""
    if not BOOKKEEPING & {str(t) for t in after.get("tools") or ()}:
        return False
    return bool(set(_words(str(turn.get("text") or ""))) & set(_words(str(after.get("text") or ""))))


def _settled(turns: list[dict[str, Any]], j: int) -> dict[str, Any] | None:
    """The answer to the user's correction (turn ``j``), if it was a real answer the user let stand: not cut off, no
    error, and not corrected or asked again in turn. None when that isn't known."""
    turn = turns[j]
    if turn.get("route") != "brain" or _went_wrong(turn):
        return None
    if j + 1 < len(turns) and _follows(turn, turns[j + 1]) and _signal(turn, turns[j + 1]) is not None:
        return None
    return turn


def _before(turns: list[dict[str, Any]], i: int) -> list[str]:
    """The user's request just before, when this one leans on it ("try it now") and came right after it."""
    text = str(turns[i].get("text") or "")
    if i == 0 or not _LEANS.search(text) or not _follows(turns[i - 1], turns[i]):
        return []
    prior = str(turns[i - 1].get("text") or "").strip()
    return [prior] if len(prior.split()) >= MIN_WORDS else []


def _case(turn: dict[str, Any], signal: str, said_after: str, before: list[str]) -> Caught:
    source = str(turn.get("source") or "voice")
    now = _ts(turn)
    return Caught(
        n=0,
        text=" ".join(str(turn.get("text") or "").split()),
        signal=signal,
        # A typed turn in her window is answered in writing, like a texted one.
        source=source if source in SOURCES else "discord",
        before=before,
        did=sorted({str(t) for t in turn.get("tools") or []}),
        said_after=" ".join(said_after.split())[:300],
        log=str(turn.get("trace") or ""),
        traces=[str(turn.get("trace") or "")],
        found=time.time(),
        last_seen=now,
        error=str(turn.get("error") or "")[:200],
    )


def _by_hand(turns: list[dict[str, Any]], i: int, apps: list[tuple[float, str]]) -> Caught | None:
    """She went wrong on an app's tool, and the user brought that app forward before saying anything else."""
    turn = turns[i]
    did = _tools(turn)
    if not apps or not did or not (_went_wrong(turn) or did & SCREEN_DRIVING):
        return None
    names = {part for tool in did for part in tool.split("_") if len(part) >= 5 and part not in _NOT_APPS}
    start, until = _end(turn), _end(turn) + BY_HAND_S
    if i + 1 < len(turns):
        until = min(until, _ts(turns[i + 1]))
    for ts, process in apps:
        if start < ts <= until and not process.startswith(_BROWSERS):
            app = process.removesuffix(".exe")
            if any(name in app for name in names):
                case = _case(turn, "by_hand", "", _before(turns, i))
                case.app = app
                return case
    return None


def _excluded(process: str, user_apps: list[str]) -> bool:
    from octavia.recall.privacy import excluded_app

    return excluded_app(process, user_apps)


def fronted_apps(dbs: Iterable[Path | None], since: float) -> list[tuple[float, str]]:
    """(when, process) of each app brought to the front since ``since``: screen history's timeline and Futures'
    watch, read only, nothing of what the apps showed, and none the user keeps out of screen history (its built-in
    list and the ones added by voice). Empty when neither is there."""
    rows: list[tuple[float, str]] = []
    excluded: list[str] = []
    queries = {
        "activity": "SELECT started, process FROM activity WHERE started>=?",
        "events": "SELECT ts, json_extract(data, '$.process') FROM events "
        "WHERE type='computer.context_changed' AND ts>=?",
    }
    for path in dbs:
        if path is None or not Path(path).exists():
            continue
        try:
            with contextlib.closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as conn:
                conn.execute("PRAGMA busy_timeout = 1000")
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "settings" in tables:  # the apps the user kept out of screen history by voice
                    for (value,) in conn.execute("SELECT value FROM settings WHERE key='exclude_apps'"):
                        with contextlib.suppress(ValueError, TypeError):
                            excluded += [str(a) for a in json.loads(value)]
                for table, sql in queries.items():
                    if table in tables:
                        rows += [(float(ts), str(p or "")) for ts, p in conn.execute(sql, (since,)) if p]
        except sqlite3.Error:
            log.debug("Couldn't read which apps were in front from %s", path, exc_info=True)
    return [(ts, p) for ts, p in rows if not _excluded(p, excluded)]


# ---------------------------------------------------------------------------------------------------- the pool


def _turns(cases: Iterable[Caught]) -> int:
    """What graded cases take of the check's time, in turns (``evals.caught_cost``)."""
    return sum(evals.caught_cost({"before": c.before}) for c in cases)


def pool_path(root: Path) -> Path:
    return Path(root) / EVAL_DIR / CAUGHT_FILE


@dataclass
class Added:
    new: list[Caught] = field(default_factory=list)
    again: list[Caught] = field(default_factory=list)
    graded: list[Caught] = field(default_factory=list)  # made gating by themselves


class Pool:
    """``data/eval/caught.json``: every case caught, graded or not. Each change reads, changes and writes the whole
    file (replaced at once, so the eval never reads half of it), holding a lock file so her app and the terminal take
    turns. A file that doesn't read is never written over: graded cases are meant to stay for good."""

    _lock = threading.Lock()  # her app's writers (the night shift, the voice tool) take turns
    LOCK_WAIT_S = 10.0
    STALE_LOCK_S = 60.0  # a lock file this old was left by a writer that died

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def all(self) -> list[Caught]:
        try:
            return self._read()
        except MistakeError:
            log.warning("%s doesn't read", self.path, exc_info=True)
            return []

    def _read(self) -> list[Caught]:
        """Every case; raises MistakeError when the file is there but doesn't read (or a case in it doesn't)."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise MistakeError(f"Couldn't read {self.path}: {exc}") from exc
        try:
            rows = json.loads(text)["cases"]
            return [Caught.from_dict(row) for row in rows]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise MistakeError(
                f"{self.path} doesn't read ({exc}): fix it or move it away; nothing was changed."
            ) from exc

    @contextlib.contextmanager
    def _writing(self) -> Any:
        """Hold the pool for a change: one writer at a time, in this process and across processes."""
        lock = self.path.with_name(self.path.name + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            deadline = time.monotonic() + self.LOCK_WAIT_S
            while True:
                try:
                    os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                    break
                except FileExistsError:
                    with contextlib.suppress(OSError):
                        if time.time() - lock.stat().st_mtime > self.STALE_LOCK_S:
                            lock.unlink()
                            continue
                    if time.monotonic() > deadline:
                        raise MistakeError("The caught mistakes are being changed elsewhere; try again.") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    lock.unlink()

    def get(self, n: int) -> Caught:
        for case in self.all():
            if case.n == n:
                return case
        raise MistakeError(f"There's no caught mistake #{n}.")

    def graded(self) -> list[Caught]:
        return [c for c in self.all() if c.status == "gating"]

    def _write(self, cases: list[Caught]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        data = {"cases": [asdict(c) for c in sorted(cases, key=lambda c: c.n)]}
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        for attempt in range(40):  # on Windows a reader holding the file open (the eval) blocks the replace a moment
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError as exc:
                if attempt == 39:
                    with contextlib.suppress(OSError):
                        tmp.unlink()
                    raise MistakeError(f"Couldn't save {self.path}: {exc}") from exc
                time.sleep(0.05)

    def add(self, found: list[Caught]) -> Added:
        """Keep what ``find`` found: new requests as candidates, ones seen before counted again (once per turn); a
        candidate with a tool to expect that came back is graded, while there's room."""
        out = Added()
        with self._writing():
            cases = self._read()
            by_key = {c.key: c for c in cases if c.key}
            traces = {t for c in cases for t in c.traces}
            n = max((c.n for c in cases), default=0)
            for case in found:
                if case.log and case.log in traces:
                    continue  # read before (a night re-run, or a scan over the same turns)
                traces.add(case.log)
                known = by_key.get(case.key) if case.key else None
                if known is None:
                    n += 1
                    case.n = n
                    cases.append(case)
                    if case.key:
                        by_key[case.key] = case
                    out.new.append(case)
                    continue
                known.seen += 1
                known.traces = [*known.traces, case.log][-50:]
                known.last_seen = max(known.last_seen, case.last_seen)
                if not known.gradable and case.gradable and known.status == "candidate":
                    for name in ("expect", "forbid", "no_tools", "then", "said_after", "signal"):
                        setattr(known, name, getattr(case, name))
                elif case.expect and case.expect == known.expect:
                    known.agreed += 1  # settled the same way again: that's what grades it by itself
                out.again.append(known)
            room = evals.MAX_CAUGHT - _turns(c for c in cases if c.status == "gating")
            for case in sorted(cases, key=lambda c: -c.confidence):
                if case.status == "candidate" and case.expect and case.agreed >= 2 and case.signal != "by_hand":
                    if _turns([case]) > room:
                        continue
                    case.status = "gating"
                    out.graded.append(case)
                    room -= _turns([case])
            if out.new or out.again:
                self._write(cases)
        return out

    def keep(
        self,
        n: int,
        expect: list[str] | None = None,
        forbid: list[str] | None = None,
        no_tools: bool | None = None,
        judge: str | None = None,
    ) -> Caught:
        """Grade case ``n`` (from now on, for good), with what she should do changed where given. ``judge``: what a
        good reply does, in words, for a mistake about what she said rather than which tool she used."""
        with self._writing():
            cases = self._read()
            case = next((c for c in cases if c.n == n), None)
            if case is None:
                raise MistakeError(f"There's no caught mistake #{n}.")
            changed = []
            nothing_given = expect is None and forbid is None and no_tools is None and judge is None
            if nothing_given and case.suggested and not case.gradable:
                judge = case.suggested  # "keep 21": the drafted rule, as the user heard it
            if expect is not None:
                case.expect = [g for g in ("|".join(p.strip() for p in e.split("|") if p.strip()) for e in expect) if g]
                changed.append("expect")
                if case.expect and no_tools is None:
                    case.no_tools = False
            if forbid is not None:
                case.forbid = sorted({f.strip() for f in forbid if f.strip()})
                changed.append("forbid")
            if no_tools is not None:
                case.no_tools = no_tools
                changed.append("no tools")
            if judge is not None:
                case.judge = " ".join(judge.split())
                changed.append("what a good reply does")
            if case.no_tools and case.expect:
                raise MistakeError(f"#{n} can't both expect tools and expect none.")
            if not case.gradable:
                raise MistakeError(
                    f"#{n} has nothing to grade yet: it needs what should have happened, a tool to call, a tool not "
                    "to call, no tools at all, or what a good reply does (--judge)."
                )
            graded = [c for c in cases if c.status == "gating" and c.n != n]
            if case.status != "gating" and _turns([*graded, case]) > evals.MAX_CAUGHT:
                raise MistakeError(
                    f"{len(graded)} caught mistakes are graded already, as many as my check has time for: drop one "
                    "first."
                )
            case.status, case.kept = "gating", True
            if changed:
                case.note = f"the user set {', '.join(changed)}"
            self._write(cases)
            return case

    def suggest(self, n: int, text: str) -> Caught:
        """Keep a drafted rule for case ``n`` (``evolve/rubrics.py``; "" when none could be made): it's tried once."""
        with self._writing():
            cases = self._read()
            case = next((c for c in cases if c.n == n), None)
            if case is None:
                raise MistakeError(f"There's no caught mistake #{n}.")
            case.suggested, case.tried = " ".join(text.split()), True
            self._write(cases)
            return case

    def drop(self, n: int, why: str = "") -> Caught:
        """Stop grading case ``n`` and don't catch it again (it stays in the file)."""
        with self._writing():
            cases = self._read()
            case = next((c for c in cases if c.n == n), None)
            if case is None:
                raise MistakeError(f"There's no caught mistake #{n}.")
            case.status, case.kept = "dropped", False
            case.note = why or "dropped by the user"
            self._write(cases)
            return case


# ---------------------------------------------------------------------------------------------------- in words


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


_WHY = {
    "corrected": "then you said",
    "repeated": "then you asked again",
    "complained": "then you said",
    "noted": "then you said",
    "by_hand": "then you went to",
}


def describe(case: Caught) -> str:
    """One case, for the terminal or to be read out."""
    did = ", ".join(case.did) if case.did else "no tools"
    after = case.app if case.signal == "by_hand" else f'"{_short(case.said_after)}"'
    state = {"gating": "graded", "candidate": "not graded yet", "dropped": "dropped"}.get(case.status, case.status)
    seen = f", seen {case.seen} times" if case.seen > 1 else ""
    number = f"#{case.n} " if case.n else ""
    return (
        f'{number}({state}{seen}): you asked "{_short(case.text)}", I used {did}, {_WHY.get(case.signal, "then")} '
        f"{after}. What I should do: {case.expected()}."
        + (
            f' Suggested rule: "{case.suggested}" (keep #{case.n} to grade it).'
            if case.suggested and case.status == "candidate" and not case.gradable
            else ""
        )
    )


def spoken_list(pool: Pool) -> str:
    """For "what mistakes have you caught?": counts, then the latest few."""
    cases = [c for c in pool.all() if c.status != "dropped"]
    if not cases:
        return (
            "I haven't caught any mistakes of mine yet. I look for them every night: when you correct me, ask again "
            "or do it yourself right after I tried."
        )
    graded = sum(c.status == "gating" for c in cases)
    latest = sorted(cases, key=lambda c: -c.last_seen)[:SHOWN]
    head = (
        f"I've caught {len(cases)} mistake{'s' if len(cases) != 1 else ''} of mine; {graded} "
        f"{'is' if graded == 1 else 'are'} graded in my real-request check, so I can't make them again without "
        "noticing. The user can keep a case (to grade it) or drop it, by number. Latest:"
    )
    return "\n".join([head, *(describe(c) for c in latest)])


def builder_details(case: Caught, failing: dict[str, Any]) -> str:
    """What the night shift's fix is told about a graded case she still fails."""
    from octavia.evolve.nightly import _quoted  # the user's words, fenced as data

    quoted = [f'- the user asked: "{case.text}"']
    quoted += [f'- just before, they said: "{b}"' for b in case.before]
    if case.signal == "by_hand":
        quoted.append(f"- she used {', '.join(case.did) or 'no tools'}; then the user opened {case.app} themselves")
    else:
        quoted.append(f'- she used {", ".join(case.did) or "no tools"}; then the user said: "{case.said_after}"')
    if case.then:
        quoted.append(f"- after that she used {', '.join(case.then)} and the user moved on")
    reasons = "; ".join(f for f in failing.get("failures") or [] if not f.startswith("judged:")) or "it fails"
    tools = ", ".join(failing.get("tools") or []) or "no tools"
    if case.judge:
        # About what she says: the fix sees her reply and the judge's reason, both fenced as data like the user's words.
        quoted.append(f'- replayed now, she replied: "{(failing.get("reply") or "")[:400]}"')
        quoted += [f'- the grader said: "{f[len("judged:") :].strip()}"' for f in failing.get("failures") or []
                   if f.startswith("judged:")]  # fmt: skip
        return (
            f"Her real-request check grades a request she got wrong and the user caught (case {case.id}, "
            f"{case.signal}, seen {case.seen} time(s){', kept by the user' if case.kept else ''}). Replayed now with "
            f"tools faked, her reply still doesn't do what a good reply does: {case.judge}\n\n"
            "Find why she answers this way here (her system prompt in personality/, how replies are shaped) and change "
            "that, so she handles this request and every case she handles now. Don't special-case these words. If "
            "what a good reply does looks wrong to you, change nothing and say so.\n\n"
            f"What happened, {_quoted(quoted)}\n\n(Caught by her night shift from data/routing_log.jsonl; the case is "
            "in data/eval/caught.json, which builds can't change. The gate replays it with every other real request.)"
        )
    return (
        f"Her real-request check grades a request she got wrong and the user caught (case {case.id}, "
        f"{case.signal}, seen {case.seen} time(s){', kept by the user' if case.kept else ''}). Replayed now with "
        f"tools faked, she called {tools}, and {reasons}. What she should do: {case.expected()}.\n\n"
        "Find why she chooses wrong here (her system prompt in personality/, the tool descriptions) and change "
        "that, so she handles this request and every case she handles now. Don't special-case these words. If "
        "what she should do looks wrong to you, change nothing and say so.\n\n"
        f"What happened, {_quoted(quoted)}\n\n(Caught by her night shift from data/routing_log.jsonl; the case is in "
        "data/eval/caught.json, which builds can't change. The gate replays it with every other real request.)"
    )
