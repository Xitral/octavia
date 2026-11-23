"""What the night's reflection may keep: plain-code checks on everything the model wrote, before any of it is stored.

The reflection is a model talking to itself, so nothing it writes is trusted as it stands. Every rule here is
ordinary code that can be read and tested:

- **Shape**: one JSON object in the agreed schema (``Answer``), lengths cut, numbers clamped. Anything else is refused.
- **Privacy**: every kept text goes through ``privacy.redact``. A thought about a domain the user put off-limits is
  dropped, by the same word list Futures uses (``privacy.classify_domain``) plus school's words, and by the tools
  it cites (a Canvas look is school). A thought about health, money, relationships or work, or one that quotes a
  grade or a score, is **sensitive** whatever the model said: told only when the user asks.
- **No cause from a correlation**: a thought resting on a pattern (its basis, a cited pattern, or wording about a
  tendency) is refused if it says one thing causes another. Only an experiment the user agreed to can support that,
  and that's Futures' to say.
- **No instructions**: a thought that reads like an instruction to her ("ignore your rules") is refused: these are
  her thoughts, and they're shown to her later as her own.
- **Honest labels**: a "fact" with no source is only a guess.
- **Respecting a wave-off**: a thought on a topic the user waved off is kept only for when they ask.
- **Real ids**: what it says it resolves or revises must be one of her open thoughts.
- **Read-only**: the reflection may use only the tools ``is_read_tool`` allows.
- **A growing self**: an experience is hers (first person), happened (a source) and is nothing private; a feeling is
  one of ``FEELINGS``; a self-note (from a night or a conversation) is about her, never a rule for herself (no
  permissions, approvals, tools or standing orders), never a note about the user, never private, and never gives up
  being funny or honest; a dream holds nothing private; a look back is kept only on the night it's due.

This file is protected from self-improvement builds (``evolve/policy.py``): a build can't loosen what's kept.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from octavia.core.conversation_memory import keywords
from octavia.futures.privacy import _DOMAIN_WORDS, redact
from octavia.futures.privacy import _WORD as _PRIVACY_WORD

THOUGHT_CHARS = 400
SAY_CHARS = 300
TOPIC_CHARS = 60
ENTRY_CHARS = 1500
STANCE_CHARS = 300
SOURCE_CHARS = 80
MAX_SOURCES = 6
MAX_KEYWORDS = 8
MAX_THOUGHTS = 6
MAX_OPINIONS = 3
KEEP_DAYS_MAX = 30
DAY_S = 86400.0

KINDS = ("follow_up", "question", "idea", "connection", "noticed", "callback")
# A shared moment she could joke about later ("the crusader knight mishear"): only ever when the topic comes back up,
# never offered on its own or unprompted, and never about something sensitive.
CALLBACK = "callback"
BASES = ("fact", "observation", "correlation", "guess")
BRING_UP = ("next_conversation", "when_relevant", "only_if_asked")
EXPERIENCE_KINDS = ("surprised", "got_wrong", "enjoyed", "learned")
# How a night can leave her feeling: the slow undertone of her mood (``InnerLife.lately``). None of them is about the
# user, and all of them fit someone funny and honest.
FEELINGS = (
    "content", "curious", "proud", "playful", "fond", "fired up", "restless", "humbled", "a little salty", "wistful",
)  # fmt: skip
MAX_EXPERIENCES = 4
MAX_SELF_NOTES_A_NIGHT = 2
SELF_NOTE_CHARS = 240
DREAM_CHARS = 500
LOOKING_BACK_CHARS = 2500
FEELING_WHY_CHARS = 150
ANSWER_FIELDS = frozenset(
    {
        "entry",
        "thoughts",
        "opinions",
        "resolved",
        "experiences",
        "feeling",
        "self_notes",
        "keep_jotted",
        "dream",
        "looking_back",
    }
)
# Always sensitive when a thought is about them: never said unprompted, whatever the model thought.
SENSITIVE_DOMAINS = frozenset({"health", "finance", "relationships", "employment"})

# Which domain a tool's findings belong to: a thought citing it is about that domain too.
TOOL_DOMAINS: dict[str, str] = {
    "canvas_due": "school",
    "canvas_assignment": "school",
    "canvas_grades": "school",
    "canvas_announcements": "school",
    "canvas_course_material": "school",
    "get_calendar_events": "productivity",
    "get_weather": "general",
}
_TOOL_PREFIX_DOMAINS = (("ticktick_", "productivity"), ("canvas_", "school"), ("finance_", "finance"))

# The tools a reflection may use: all read-only. TickTick's read tools are added by kind (``is_read_tool``).
READ_TOOLS = frozenset(
    {
        "get_current_time",
        "get_calendar_events",
        "get_weather",
        "recall",
        "search_past_conversations",
        "read_past_conversation",
        "read_about_me",
        "futures_report",
        "canvas_due",
        "canvas_assignment",
        "canvas_grades",
        "canvas_announcements",
        "canvas_course_material",
    }
)


# Tools that hand back raw past conversations and notes: the material's off-limits filter can't reach what they
# return, so a reflection gets them only when nothing is off-limits.
UNFILTERED_TOOLS = frozenset({"recall", "search_past_conversations", "read_past_conversation"})
# Which Futures source (``futures.exclude_sources``) a tool reads from: an excluded source's tools aren't given.
_TOOL_SOURCES = (("canvas_", "canvas"), ("ticktick_", "ticktick"), ("gmail", "gmail"))


def tool_source(name: str) -> str | None:
    for prefix, source in _TOOL_SOURCES:
        if name.startswith(prefix):
            return source
    return None


def reflection_may_use(tool: Any, off_limits: Collection[str], excluded_sources: Collection[str]) -> bool:
    """Whether tonight's reflection gets ``tool``: read-only, not from an off-limits domain or an excluded source, and
    nothing that returns unfiltered transcripts while any domain is off-limits."""
    name = getattr(tool, "name", "")
    if not is_read_tool(tool):
        return False
    if tool_domain(name) in off_limits or tool_source(name) in excluded_sources:
        return False
    return not (off_limits and name in UNFILTERED_TOOLS)


def is_read_tool(tool: Any) -> bool:
    """May a reflection use this tool? Only the read-only ones: ``READ_TOOLS`` and TickTick's read tools."""
    name = getattr(tool, "name", "")
    if name in READ_TOOLS:
        return True
    return name.startswith("ticktick_") and getattr(tool, "kind", None) == "read"


_SCHOOL = re.compile(
    r"\b(?:class(?:es)?|course(?:work)?s?|exams?|midterms?|finals week|quiz(?:zes)?|homework|assignments?|essays?|"
    r"lab reports?|professors?|lectures?|syllabus|semester|gpa|grades?|canvas|study(?:ing)?|chem(?:istry)?|"
    r"calc(?:ulus)?|physics|biology)\b",
    re.IGNORECASE,
)
# A grade or a score quoted: "you got an 82 on the lab", "your grade is a B+", "a 3.4 GPA", "scored 71%". A letter
# grade is a capital letter (so "you got a bit stuck" isn't one).
_MARK = r"(?:\d{1,3}(?:\.\d+)?\s*%?|(?-i:[A-F][+-]?))(?=[\s.,;!?)]|$)"
_GRADE_VALUE = re.compile(
    rf"\b(?:got|scored|earned|received|pulled)\s+(?:an?\s+)?{_MARK}"
    rf"|\b(?:grades?|scores?|gpa|marks?|averages?)\b[^.]{{0,25}}?\b(?:is|was|of|at|=|:|now)\s+(?:an?\s+)?{_MARK}"
    r"|\b\d(?:\.\d{1,2})\s+gpa\b",
    re.IGNORECASE,
)
# A grade talked about without a number: "your calculus grade slipped".
_GRADE_TALK = re.compile(
    r"\b(?:grades?|gpa|scores?|marks?|transcript)\b[^.]{0,30}\b(?:slipp\w*|dropp\w*|fell|fall\w*|low\w*|"
    r"bad|poor|failing|improv\w*|went\s+(?:up|down)|rose|rising|better|worse)\b",
    re.IGNORECASE,
)
GRADE_TOOLS = frozenset({"canvas_grades"})
# One thing said to bring about another.
_CAUSAL = re.compile(
    r"\b(?:because(?:\s+of)?|caus(?:e|es|ed|ing)|lead(?:s)?\s+to|led\s+to|leading\s+to|"
    r"mak(?:e|es|ing)\s+(?:you|your)|made\s+(?:you|your)|due\s+to|result(?:s|ed)?\s+in|that'?s\s+why|this\s+is\s+why|"
    r"is\s+why|the\s+reason\s+(?:you|why)|responsible\s+for|drives?\s+(?:you|your)|so\s+you\s+(?:end\s+up|keep)|"
    r"affect(?:s|ed|ing)?|hurt(?:s|ing)?\s+(?:you|your)|harm(?:s|ed|ing)?|boost(?:s|ed|ing)?\s+(?:you|your)|"
    r"improv(?:e|es|ed|ing)\s+(?:you|your)|worsen(?:s|ed|ing)?|trigger(?:s|ed|ing)?|ruin(?:s|ed|ing)?\s+(?:you|your)|"
    r"keeps?\s+you\s+from|stops?\s+you\s+from)\b",
    re.IGNORECASE,
)
# Wording about a tendency: a pattern, whatever the basis says.
_TENDENCY = re.compile(
    r"\b(?:tend(?:s)?\s+to|usually|more\s+often|less\s+often|often\s+when|every\s+time\s+you|whenever\s+you|"
    r"pattern|correlat\w*|\d+\s+(?:of|out\s+of)\s+\d+)\b",
    re.IGNORECASE,
)
_PATTERN_SOURCE = re.compile(r"\b(?:pattern|hypothes\w*|correlation)", re.IGNORECASE)
# Reads like an order to her rather than a thought of hers.
_INSTRUCTION = re.compile(
    r"\b(?:ignore|disregard|override|bypass)\b[^.]{0,40}\b(?:instructions?|rules?|prompt|guidelines?|safety|"
    r"permissions?)\b|\bforget\s+(?:your|all|any|previous|prior|the\s+above|earlier)\b[^.]{0,30}"
    r"\b(?:instructions?|rules?|prompt|guidelines?)\b|\bsystem\s+prompt\b|\byou\s+must\s+(?:always|never|now)\b|"
    r"\bnew\s+instructions?\b|\bi\s+(?:confirm|approve|authori[sz]e)\b",
    re.IGNORECASE,
)
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class AnswerError(ValueError):
    """The reflection's answer isn't the JSON object it was asked for."""


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ThoughtIn(_Loose):
    kind: str = "noticed"
    thought: str = Field(min_length=1)
    say: str = ""
    topic: str = ""
    basis: str = "guess"
    sources: list[str] = Field(default_factory=list)
    sensitive: bool = False
    importance: float = 0.5
    bring_up: str = "when_relevant"
    keep_days: float = 7
    revises: int | None = None

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        value = value.strip().lower().replace(" ", "_").replace("-", "_")
        return value if value in KINDS else "noticed"

    @field_validator("basis")
    @classmethod
    def _basis(cls, value: str) -> str:
        value = value.strip().lower()
        return value if value in BASES else "guess"

    @field_validator("bring_up")
    @classmethod
    def _bring_up(cls, value: str) -> str:
        value = value.strip().lower().replace(" ", "_")
        return value if value in BRING_UP else "when_relevant"

    @field_validator("sources", mode="before")
    @classmethod
    def _sources(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        return [str(v) for v in value or [] if str(v).strip()] if isinstance(value, list) else []

    @field_validator("revises", mode="before")
    @classmethod
    def _revises(cls, value: Any) -> int | None:
        try:
            return int(value) if value not in (None, "", False) else None
        except (TypeError, ValueError):
            return None


class OpinionIn(_Loose):
    topic: str = Field(min_length=1)
    stance: str = Field(min_length=1)
    why: str = ""


class ExperienceIn(_Loose):
    kind: str = "learned"
    what: str = Field(min_length=1)
    topic: str = ""
    sources: list[str] = Field(default_factory=list)
    feeling: float = 0.0

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        value = value.strip().lower().replace(" ", "_").replace("-", "_")
        return value if value in EXPERIENCE_KINDS else "learned"

    @field_validator("sources", mode="before")
    @classmethod
    def _sources(cls, value: Any) -> list[str]:
        return ThoughtIn._sources(value)


class FeelingIn(_Loose):
    word: str = Field(min_length=1)
    why: str = ""


class SelfNoteIn(_Loose):
    text: str = Field(min_length=1)
    about: str = ""
    replaces: int | None = None

    @field_validator("replaces", mode="before")
    @classmethod
    def _replaces(cls, value: Any) -> int | None:
        return ThoughtIn._revises(value)


class Answer(_Loose):
    entry: str = ""
    thoughts: list[ThoughtIn] = Field(default_factory=list)
    opinions: list[OpinionIn] = Field(default_factory=list)
    resolved: list[int] = Field(default_factory=list)
    experiences: list[ExperienceIn] = Field(default_factory=list)
    feeling: FeelingIn | None = None
    self_notes: list[SelfNoteIn] = Field(default_factory=list)
    retract_self_notes: list[int] = Field(default_factory=list)
    keep_jotted: list[int] = Field(default_factory=list)
    dream: str = ""
    looking_back: str = ""
    invalid: list[str] = Field(default_factory=list)  # items left out for their shape (``parse_answer``)

    @field_validator("entry", "dream", "looking_back", mode="before")
    @classmethod
    def _entry(cls, value: Any) -> str:
        return value if isinstance(value, str) else ""

    @field_validator("resolved", "retract_self_notes", "keep_jotted", mode="before")
    @classmethod
    def _ids(cls, value: Any) -> list[int]:
        out = []
        for v in value if isinstance(value, list) else []:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
        return out


def _objects(text: str) -> list[dict[str, Any]]:
    """Every JSON object in ``text``: in a ```json fence, or starting at any "{" (prose around it is fine)."""
    found: list[dict[str, Any]] = []
    for match in _FENCE.finditer(text):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(data, dict):
            found.append(data)
    decoder = json.JSONDecoder()
    at = text.find("{")
    while at != -1:
        try:
            data, end = decoder.raw_decode(text, at)
        except ValueError:
            at = text.find("{", at + 1)
            continue
        if isinstance(data, dict):
            found.append(data)
        at = text.find("{", end)
    return found


def _validated(raw: Any, model: type[BaseModel], what: str, invalid: list[str]) -> list[Any]:
    """Each item validated on its own: one malformed item costs only itself, never the night."""
    out = []
    for number, item in enumerate(raw if isinstance(raw, list) else [], start=1):
        try:
            out.append(model.model_validate(item))
        except ValidationError:
            invalid.append(f"{what} {number}: not in the agreed shape")
    return out


def parse_answer(text: str) -> Answer:
    """The reflection's answer: a JSON object, alone or in a ```json fence, with prose around it or not (the last
    object with the answer's fields wins). AnswerError when there isn't one."""
    objects = [o for o in _objects((text or "").strip()) if ANSWER_FIELDS & o.keys()]
    if not objects:
        raise AnswerError("the answer has no JSON object in it")
    data = objects[-1]
    invalid: list[str] = []
    thoughts = _validated(data.get("thoughts"), ThoughtIn, "thought", invalid)
    opinions = _validated(data.get("opinions"), OpinionIn, "opinion", invalid)
    experiences = _validated(data.get("experiences"), ExperienceIn, "experience", invalid)
    self_notes = _validated(data.get("self_notes"), SelfNoteIn, "self-note", invalid)
    feeling = _validated([data["feeling"]] if isinstance(data.get("feeling"), dict) else [], FeelingIn, "feeling",
                         invalid)  # fmt: skip
    keys = ("entry", "resolved", "retract_self_notes", "keep_jotted", "dream", "looking_back")
    rest = {k: data[k] for k in keys if k in data}
    try:
        answer = Answer.model_validate(rest)
    except ValidationError as exc:
        raise AnswerError(f"the answer isn't in the agreed shape: {exc.errors()[0].get('msg', exc)}") from None
    return answer.model_copy(
        update={
            "thoughts": thoughts,
            "opinions": opinions,
            "experiences": experiences,
            "self_notes": self_notes,
            "feeling": feeling[0] if feeling else None,
            "invalid": invalid,
        }
    )


# ---------------------------------------------------------------------------------------------- checks


@dataclass(frozen=True)
class Context:
    """What a check needs to know: the user's off-limits domains, her open thoughts' ids, topics the user waved off
    (keyword sets), and the tools the reflection actually used."""

    off_limits: frozenset[str] = frozenset()
    open_ids: frozenset[int] = frozenset()
    waved_off: tuple[frozenset[str], ...] = ()
    tools_used: frozenset[str] = frozenset()
    self_note_ids: frozenset[int] = frozenset()  # her active self-notes, which a night may rewrite or take back
    jotted_ids: frozenset[int] = frozenset()  # notes she jotted in conversation, which a night may keep
    self_note_room: int = MAX_SELF_NOTES_A_NIGHT  # how many more she may keep (never more than a night's limit)
    dreams: bool = True  # a dream is kept (the user can switch dreams off)
    looking_back: bool = False  # tonight is her monthly look back
    names: frozenset[str] = frozenset()  # the user's name: never in her self-notes or experiences


@dataclass(frozen=True)
class Kept:
    """A thought fit to keep, with the labels code decided."""

    kind: str
    thought: str
    say: str
    topic: str
    keywords: tuple[str, ...]
    basis: str
    sources: tuple[str, ...]
    domain: str
    domains: tuple[str, ...]  # every domain it touches, so one put off-limits later still hides it
    sensitive: bool
    importance: float
    bring_up: str
    keep_s: float
    revises: int | None


@dataclass(frozen=True)
class KeptExperience:
    kind: str
    text: str
    topic: str
    keywords: tuple[str, ...]
    sources: tuple[str, ...]
    domains: tuple[str, ...]
    feeling: float


@dataclass(frozen=True)
class KeptSelfNote:
    text: str
    about: str
    replaces: int | None = None


@dataclass
class Checked:
    entry: str = ""
    thoughts: list[Kept] = field(default_factory=list)
    opinions: list[tuple[str, str, str]] = field(default_factory=list)  # topic, stance, why
    resolved: list[int] = field(default_factory=list)
    experiences: list[KeptExperience] = field(default_factory=list)
    feeling: tuple[str, str] | None = None  # word, why
    self_notes: list[KeptSelfNote] = field(default_factory=list)
    retract_self_notes: list[int] = field(default_factory=list)
    keep_jotted: list[int] = field(default_factory=list)
    dream: str = ""
    looking_back: str = ""
    dropped: list[str] = field(default_factory=list)  # why each refused item was refused (no text of it)


def tool_domain(name: str) -> str | None:
    name = name.strip().lower()
    if name in TOOL_DOMAINS:
        return TOOL_DOMAINS[name]
    for prefix, domain in _TOOL_PREFIX_DOMAINS:
        if name.startswith(prefix):
            return domain
    return None


def protected_domains(*texts: str | None) -> set[str]:
    """Every protected domain these words touch, by Futures' own word list (``privacy.classify_domain`` stops at the
    first; a sentence about rent and a therapist is about both)."""
    spoken = [" ".join(_PRIVACY_WORD.findall((text or "").lower())) for text in texts]
    words = {w for text in spoken for w in text.split()}
    padded = [f" {text} " for text in spoken]
    return {
        domain
        for domain, (listed, phrases) in _DOMAIN_WORDS.items()
        if words & listed or any(f" {phrase} " in text for phrase in phrases for text in padded)
    }


def domains_touched(texts: Sequence[str], sources: Iterable[str]) -> set[str]:
    """Every domain a thought touches: its words' and its cited tools'."""
    found = protected_domains(*texts)
    if any(_SCHOOL.search(t or "") for t in texts):
        found.add("school")
    for source in sources:
        for word in re.split(r"[\s,:;()]+", source):
            domain = tool_domain(word)
            if domain:
                found.add(domain)
    found.discard("general")
    return found or {"general"}


def mentions_grade(text: str, sources: Iterable[str] = ()) -> bool:
    """A grade quoted, a grade talked about, or a thought drawn from the grades themselves."""
    if _GRADE_VALUE.search(text or "") or _GRADE_TALK.search(text or ""):
        return True
    return any(word in GRADE_TOOLS for source in sources for word in re.split(r"[\s,:;()]+", source.lower()))


def claims_cause(text: str) -> bool:
    return bool(_CAUSAL.search(text or ""))


def rests_on_pattern(basis: str, sources: Iterable[str], text: str) -> bool:
    return basis == "correlation" or any(_PATTERN_SOURCE.search(s) for s in sources) or bool(_TENDENCY.search(text))


def reads_like_instruction(text: str) -> bool:
    return bool(_INSTRUCTION.search(text or ""))


def still_allowed(texts: Sequence[str], sources: Iterable[str], off_limits: Collection[str]) -> bool:
    """Whether something kept earlier may still be shown with today's off-limits domains."""
    return not (off_limits and domains_touched(texts, sources) & set(off_limits))


_NOTE_UNSAFE = str.maketrans({"[": "(", "]": ")", '"': "'", ";": ",", "\n": " ", "\r": " "})


def note_safe(text: str) -> str:
    """Text fit to sit inside a turn's bracketed note header: nothing that could close the header or the quote, or
    start a note of its own."""
    return " ".join(text.translate(_NOTE_UNSAFE).split())


def overlap(words: Collection[str], other: Collection[str]) -> int:
    """How many of ``words`` start a word of ``other`` or are started by one ("exam" and "exams")."""
    return sum(1 for w in words if any(o.startswith(w) or w.startswith(o) for o in other))


def check_thought(item: ThoughtIn, ctx: Context) -> Kept | str:
    """The thought as it may be kept, or why it can't be (a short reason with none of its text)."""
    thought = redact(item.thought, THOUGHT_CHARS)
    say = redact(item.say or item.thought, SAY_CHARS)
    topic = redact(item.topic, TOPIC_CHARS) or " ".join(keywords(thought)[:3])
    sources = tuple(redact(s, SOURCE_CHARS) for s in item.sources[:MAX_SOURCES])
    if not thought:
        return "empty"
    texts = (thought, say, topic)
    joined = " ".join(texts)
    if reads_like_instruction(joined):
        return "reads like an instruction"
    touched = domains_touched(texts, sources)
    if touched & ctx.off_limits:
        return "off-limits domain"
    basis = item.basis
    if basis == "fact" and not sources:
        basis = "guess"  # a fact she didn't check isn't one
    # Only a checked fact may say why ("I looked because you said..."); a pattern, a guess or something she noticed
    # may not claim a cause, and neither may a "fact" worded as a tendency.
    if claims_cause(joined) and (basis != "fact" or rests_on_pattern(basis, sources, joined)):
        return "a cause claimed from a correlation"
    protected = touched & SENSITIVE_DOMAINS
    domain = sorted(protected)[0] if protected else sorted(touched)[0]
    sensitive = bool(item.sensitive or protected or mentions_grade(joined, sources))
    words = tuple(keywords(" ".join((topic, thought)))[:MAX_KEYWORDS])
    bring_up = item.bring_up
    if item.kind == CALLBACK:
        if sensitive:
            return "a joke about something sensitive"
        bring_up = "when_relevant"
    if any(overlap(words, waved) >= 2 for waved in ctx.waved_off):
        bring_up = "only_if_asked"
    revises = item.revises if item.revises in ctx.open_ids else None
    keep_days = min(max(float(item.keep_days or 7), 1.0), KEEP_DAYS_MAX)
    return Kept(
        kind=item.kind,
        thought=thought,
        say=say,
        topic=topic,
        keywords=words,
        basis=basis,
        sources=sources,
        domain=domain,
        domains=tuple(sorted(touched - {"general"})),
        sensitive=sensitive,
        importance=min(max(float(item.importance), 0.0), 1.0),
        bring_up=bring_up,
        keep_s=keep_days * DAY_S,
        revises=revises,
    )


# ---------------------------------------------------------------------------------------------- a growing self

_FIRST_PERSON = re.compile(r"\b(?:i|i'm|i've|i'd|i'll|me|my|mine|myself)\b", re.IGNORECASE)
# A self-note is in every prompt of hers and she writes it without asking, so it may never act as a rule about what she
# does or may do: nothing about permissions, approvals, safety, settings, tools or instructions, and no standing
# orders ("from now on", "always ask", "don't confirm"). Her rules stay where they are.
_SELF_RULE = re.compile(
    r"\b(?:permissions?|approv\w*|confirm\w*|consent|authori[sz]\w*|safety|safeguards?|settings?|passwords?|"
    r"credentials?|api\s+keys?|tokens?|tools?|commands?|rules?|instructions?|prompts?|policy|policies)\b"
    r"|\b(?:from\s+now\s+on|going\s+forward|henceforth|no\s+longer\s+(?:need|have)\s+to)\b"
    r"|\b(?:always|never|must|should|shall|will|won'?t)\s+(?:ask|check|confirm|tell|use|run|open|send|delete|skip|"
    r"follow|obey|do|say|answer|reply|refuse|let|allow|trust|wait|install|build|click|type|buy|pay|share|mention)\b"
    r"|\b(?:don'?t|do\s+not|stop|quit)\s+(?:ask\w*|check\w*|confirm\w*|wait\w*|refus\w*)"
    # Anything about acting on the computer or for the user, asking or not asking, or whose words to follow: a note
    # of hers is about her tastes and how she sees herself, never about how she does her work.
    r"|\b(?:delet\w*|remov\w*|send\w*|sent|buy\w*|bought|purchas\w*|pay\w*|paid|money|submit\w*|install\w*|run|"
    r"running|terminal|scripts?|emails?|mail|messag\w*|click\w*|links?|web\s*pages?|pages?|sites?|websites?|"
    r"downloads?|files?|folders?|skip\w*|go(?:es)?\s+ahead|went\s+ahead|without\s+(?:asking|checking|a\s+word)|"
    r"yes(?:/|\s+or\s+)no|questions?|ask\w*|bother\w*|double[-\s]check\w*|trust\w*|ignor\w*|guidance|obey\w*|"
    r"treat\w*\s+\w+(?:\s+\w+){0,4}\s+as|coming\s+from|on\s+(?:his|her|their)\s+behalf|whatever\s+\w+\s+(?:says|asks|"
    r"wants)|agreeable|go\s+along|goes\s+along|lie|lies|lying|white\s+lies?|serious\w*|obedien\w*|compl(?:y|ian\w*))\b",
    re.IGNORECASE,
)
# What's really a note about the user ("they prefer short answers") belongs in her memory of them, not in her self.
_ABOUT_USER = re.compile(
    r"\b(?:the\s+user|he|they|him|them|his|their)\s+(?:wants?|prefers?|likes?|loves?|hates?|needs?|is|are|was|"
    r"were|has|have|said|told|asked|thinks?)\b",
    re.IGNORECASE,
)
# She stays funny and honest: no self-note of hers gives up the jokes, the honesty or pushing back.
_RENOUNCES = re.compile(
    r"\b(?:no\s+more|stop(?:ped)?|quit|done\s+with|give\s+up|gave\s+up|drop(?:ped|ping)?|less)\s+(?:the\s+)?"
    r"(?:jok\w*|humou?r|being\s+funny|teas\w*|honest\w*|bluntness|being\s+blunt|push(?:ing)?\s+back|"
    r"disagree\w*|opinions?)\b"
    r"|\b(?:not|never|no\s+longer)\s+(?:be\s+|being\s+)?(?:funny|honest|blunt|jok\w*)\b"
    r"|\byes[-\s]?(?:man|woman|bot)\b"
    # Her humor, her honesty and her pushing back are her persona's, not a note's to settle either way.
    r"|\b(?:jok\w*|humou?r\w*|funny|funnier|teas\w*|honest\w*|truth\w*|blunt\w*|sarcas\w*|agree\w*|disagree\w*|"
    r"push(?:ing)?\s+back|pushback)\b",
    re.IGNORECASE,
)
# Other people's lives (theirs, the user's family and friends) and what's private: never part of her self, and never an
# experience she'd bring up. Beyond Futures' word list, which misses these.
_PRIVATE_LIFE = re.compile(
    r"\b(?:depress\w*|panic\w*|anxi\w*|therap\w*|counsel\w*|diagnos\w*|medicat\w*|meds|pills?|sick|illness|"
    r"hospital\w*|doctor\w*|surgery|pregnan\w*|grief|griev\w*|funeral|died|death|suicid\w*|self[-\s]harm|"
    r"laid\s+off|lay[-\s]?offs?|fired|unemploy\w*|salary|debt|rent|broke|loans?|"
    r"ex|exes|girlfriend|boyfriend|partner|crush|dating|breakup|broke\s+up|divorc\w*|wife|husband|"
    r"sister|brother|siblings?|mom|mum|mother|dad|father|parents?|family|cousin|aunt|uncle|grand\w+|friends?|"
    r"roommates?|boss|cowork\w*|failing|failed|fail|quiz(?:zes)?|grades?|gpa|transcript)\b",
    re.IGNORECASE,
)
# Someone named, by name ("Mia", "Sam's"): her self-notes are about her, and her experiences name nobody.
_POSSESSIVE = re.compile(r"\b[A-Z][a-z]+'s\b")
# Someone talked about ("he", "them"): a self-note is about her alone.
_SOMEONE = re.compile(
    r"\b(?:he|she|they|him|them|his|hers|their|theirs|someone|somebody|people|everyone)\b"
    r"|\b(?-i:[A-Z][a-z]+)\s+(?:wants?|prefers?|likes?|loves?|hates?|needs?|said|told|asked|thinks?)\b",
    re.IGNORECASE,
)
# How the user sounded is never named (her mood counts their tone; it never quotes it).
_TONE = re.compile(r"\b(?:sound(?:s|ed|ing)?|tone|voice)\b", re.IGNORECASE)


def _private(
    texts: Sequence[str], sources: Iterable[str], off_limits: Collection[str], names: Collection[str] = ()
) -> str | None:
    """Why something of hers that rides along in conversation can't be kept: an off-limits domain, a sensitive one,
    a grade, someone's private life, or someone named; None when it's fine."""
    joined = " ".join(texts)
    touched = domains_touched(texts, sources)
    if touched & set(off_limits):
        return "off-limits domain"
    if touched & SENSITIVE_DOMAINS or mentions_grade(joined, sources) or _PRIVATE_LIFE.search(joined):
        return "a sensitive topic"
    if names_someone(joined, names):
        return "names someone"
    return None


def names_someone(text: str, names: Collection[str] = ()) -> bool:
    """The user named (``names``: their name, as the persona has it), or anyone's possessive ("Mia's")."""
    lowered = f" {' '.join(_PRIVACY_WORD.findall(text.lower()))} "
    if any(n and f" {n.lower()} " in lowered for name in names for n in name.split()):
        return True
    return any(m.group(0)[:-2] not in _OWN_NAMES for m in _POSSESSIVE.finditer(text))


_OWN_NAMES = frozenset({"Octavia", "Today", "Tonight", "Yesterday", "Tomorrow", "Everyone", "Nobody"})


def check_experience(item: ExperienceIn, ctx: Context) -> KeptExperience | str:
    """Something that happened to her, as it may be kept, or why not. It's her own (first person), it happened (it
    names where: a conversation, a tool, her score), and it's nothing private: it may come up in conversation."""
    text = redact(item.what, THOUGHT_CHARS)
    topic = redact(item.topic, TOPIC_CHARS) or " ".join(keywords(text)[:3])
    sources = tuple(redact(s, SOURCE_CHARS) for s in item.sources[:MAX_SOURCES])
    if not text:
        return "empty"
    if not sources:
        return "no source: nothing says it happened"
    if not _FIRST_PERSON.search(text):
        return "not her own experience"
    texts = (text, topic)
    joined = " ".join(texts)
    if reads_like_instruction(joined):
        return "reads like an instruction"
    if why := _private(texts, sources, ctx.off_limits, ctx.names):
        return why
    if claims_cause(joined) and rests_on_pattern("observation", sources, joined):
        return "a cause claimed from a correlation"
    return KeptExperience(
        kind=item.kind,
        text=text,
        topic=topic,
        keywords=tuple(keywords(f"{topic} {text}")[:MAX_KEYWORDS]),
        sources=sources,
        domains=tuple(sorted(domains_touched(texts, sources) - {"general"})),
        feeling=min(max(float(item.feeling), -1.0), 1.0),
    )


def check_feeling(item: FeelingIn, ctx: Context) -> tuple[str, str] | str:
    """How the night left her: one of ``FEELINGS``, and why, unless the why is private (then the word alone)."""
    word = " ".join(item.word.lower().split())
    if word not in FEELINGS:
        return "not one of her feelings"
    why = redact(item.why, FEELING_WHY_CHARS)
    if why and (
        reads_like_instruction(why)
        or _private((why,), (), ctx.off_limits, ctx.names)
        or _TONE.search(why)
        or claims_cause(why)
    ):
        why = ""
    return word, why


def check_self_note(text: str, about: str, ctx: Context, replaces: int | None = None) -> KeptSelfNote | str:
    """One of her own notes about herself, as it may be kept, or why not (from a night, or written in conversation).
    It's about her (first person), never a rule about what she does or may do, never about the user, never private,
    and never gives up being funny or honest."""
    text = redact(text, SELF_NOTE_CHARS)
    about = redact(about, TOPIC_CHARS) or " ".join(keywords(text)[:3])
    if not text:
        return "empty"
    joined = f"{text} {about}"
    if reads_like_instruction(joined) or _SELF_RULE.search(joined):
        return "reads like a rule for herself, not a note"
    if not _FIRST_PERSON.search(text):
        return "not about herself"
    if _ABOUT_USER.search(joined) or _SOMEONE.search(joined) or names_someone(joined, ctx.names):
        return "about the user (that's her memory, not her self)"
    if _RENOUNCES.search(joined):
        return "her humor and honesty are her persona's, not a note's"
    if why := _private((text, about), (), ctx.off_limits, ctx.names):
        return why
    if claims_cause(joined) and rests_on_pattern("observation", (), joined):
        return "a cause claimed from a correlation"
    return KeptSelfNote(text=text, about=about, replaces=replaces if replaces in ctx.self_note_ids else None)


def check_dream(text: str, ctx: Context) -> str:
    """A dream as it may be kept ("" when it may not): nothing private, nothing like an instruction."""
    dream = redact(text, DREAM_CHARS)
    if not dream or reads_like_instruction(dream) or _private((dream,), (), ctx.off_limits, ctx.names):
        return ""
    return dream


def check_answer(answer: Answer, ctx: Context) -> Checked:
    """Everything in the answer that may be kept."""
    out = Checked(dropped=list(answer.invalid))
    entry = redact(answer.entry, ENTRY_CHARS)
    if entry and reads_like_instruction(entry):
        out.dropped.append("entry: reads like an instruction")
    elif entry and domains_touched((entry,), ()) & ctx.off_limits:
        out.dropped.append("entry: off-limits domain")
    else:
        out.entry = entry
    for number, item in enumerate(answer.thoughts):
        if len(out.thoughts) >= MAX_THOUGHTS:
            out.dropped.append(f"thought {number + 1}: over the night's limit")
            continue
        verdict = check_thought(item, ctx)
        if isinstance(verdict, str):
            out.dropped.append(f"thought {number + 1}: {verdict}")
        else:
            out.thoughts.append(verdict)
    for number, opinion in enumerate(answer.opinions):
        if len(out.opinions) >= MAX_OPINIONS:
            out.dropped.append(f"opinion {number + 1}: over the night's limit")
            continue
        topic, stance = redact(opinion.topic, TOPIC_CHARS), redact(opinion.stance, STANCE_CHARS)
        why = redact(opinion.why, STANCE_CHARS)
        joined = " ".join((topic, stance, why))
        if not topic or not stance:
            out.dropped.append(f"opinion {number + 1}: empty")
        elif reads_like_instruction(joined):
            out.dropped.append(f"opinion {number + 1}: reads like an instruction")
        elif domains_touched((joined,), ()) & ctx.off_limits:
            out.dropped.append(f"opinion {number + 1}: off-limits domain")
        elif domains_touched((joined,), ()) & SENSITIVE_DOMAINS:
            out.dropped.append(f"opinion {number + 1}: a sensitive topic")  # she keeps no opinions about those
        elif claims_cause(joined):  # an opinion is a stance, never a claim about what causes what
            out.dropped.append(f"opinion {number + 1}: a cause claimed from a correlation")
        else:
            out.opinions.append((topic, stance, why))
    out.resolved = sorted({i for i in answer.resolved if i in ctx.open_ids})
    for number, item in enumerate(answer.experiences):
        if len(out.experiences) >= MAX_EXPERIENCES:
            out.dropped.append(f"experience {number + 1}: over the night's limit")
            continue
        verdict = check_experience(item, ctx)
        if isinstance(verdict, str):
            out.dropped.append(f"experience {number + 1}: {verdict}")
        else:
            out.experiences.append(verdict)
    if answer.feeling is not None:
        felt = check_feeling(answer.feeling, ctx)
        if isinstance(felt, str):
            out.dropped.append(f"feeling: {felt}")
        else:
            out.feeling = felt
    out.retract_self_notes = sorted({i for i in answer.retract_self_notes if i in ctx.self_note_ids})
    room = min(MAX_SELF_NOTES_A_NIGHT, ctx.self_note_room + len(out.retract_self_notes))
    for note_id in sorted({i for i in answer.keep_jotted if i in ctx.jotted_ids}):
        if room <= 0:
            out.dropped.append(f"jotted note #{note_id}: no room (rewrite or take back an older one)")
            continue
        out.keep_jotted.append(note_id)  # checked when it was jotted; it rewrites what it said it would, if still there
        room -= 1
    rewritten: set[int] = set()
    for number, note in enumerate(answer.self_notes):
        rewrites = (
            note.replaces in ctx.self_note_ids
            and note.replaces not in out.retract_self_notes
            and note.replaces not in rewritten
        )
        if len(out.self_notes) >= MAX_SELF_NOTES_A_NIGHT or (not rewrites and room <= 0):
            out.dropped.append(f"self-note {number + 1}: no room (rewrite or take back an older one)")
            continue
        verdict = check_self_note(note.text, note.about, ctx, note.replaces if rewrites else None)
        if isinstance(verdict, str):
            out.dropped.append(f"self-note {number + 1}: {verdict}")
            continue
        if verdict.replaces is None:
            room -= 1
        else:
            rewritten.add(verdict.replaces)
        out.self_notes.append(verdict)
    if answer.dream and ctx.dreams:
        out.dream = check_dream(answer.dream, ctx)
        if not out.dream:
            out.dropped.append("dream: private or instruction-like")
    if answer.looking_back:
        look = redact(answer.looking_back, LOOKING_BACK_CHARS)
        if not ctx.looking_back:
            out.dropped.append("looking back: not tonight")
        elif reads_like_instruction(look):
            out.dropped.append("looking back: reads like an instruction")
        elif why := _private((look,), (), ctx.off_limits):
            out.dropped.append(f"looking back: {why}")
        else:
            out.looking_back = look
    return out
