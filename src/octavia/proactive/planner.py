"""Tonight's pick: the candidates not prepared lately, each with her guess at the user's answer, ranked (by one cheap
model call when it's on, else by the rules' own score), the best ``max_items``.

What's skipped and why:

* the same thing (``Candidate.key``) prepared in the last ``REPEAT_S``, or still waiting for an answer;
* the same thing the user turned down in the last ``REJECTED_S``;
* a failed try only waits ``RETRY_FAILED_S`` (it never got done);
* once her guesses have earned a say (``learn.steering``), what she guesses they wouldn't want.

The ranker only orders and drops what the rules found; it can't add anything, so everything that runs came from
plain code and passed ``rules.refused``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from octavia.proactive import learn
from octavia.proactive.rules import Candidate
from octavia.proactive.store import FAILED, REJECT, Item

log = logging.getLogger(__name__)

REPEAT_S = 6 * 86400.0
REJECTED_S = 30 * 86400.0
RETRY_FAILED_S = 20 * 3600.0
MAX_RANKED = 12  # candidates shown to the ranker

Ranker = Callable[[str, str], Awaitable[str]]  # (system prompt, prompt) -> its reply


@dataclass
class Pick:
    candidate: Candidate
    guess: learn.Guess
    why: str = ""  # the ranker's reason


def fresh(candidates: Sequence[Candidate], history: Sequence[Item], now: float) -> tuple[list[Candidate], list[str]]:
    """The candidates worth considering tonight, and a line for each one left out."""
    kept, skipped = [], []
    for candidate in candidates:
        past = [i for i in history if i.key == candidate.key]
        why = ""
        for item in past:
            age = now - (item.created or 0)
            if item.decision == REJECT and age < REJECTED_S:
                why = "you turned it down"
            elif not item.decided and item.status != FAILED:
                why = "still waiting for your answer"
            elif item.status == FAILED and age < RETRY_FAILED_S:
                why = "it failed earlier tonight"
            elif item.status != FAILED and age < REPEAT_S:
                why = "prepared lately"
            if why:
                break
        if why:
            skipped.append(f"{candidate.title}: {why}")
        else:
            kept.append(candidate)
    return kept, skipped


def guesses(candidates: Sequence[Candidate], history: Sequence[Item], *, steer: bool) -> list[Pick]:
    """Each candidate with her guess; once steering, the unlikely ones dropped and the rest weighted by it."""
    picks = [Pick(c, learn.predict(c.features, history)) for c in candidates]
    if not steer:
        return picks
    picks = [p for p in picks if p.guess.p >= learn.DROP_BELOW]
    return sorted(picks, key=lambda p: -(p.candidate.score * (0.5 + p.guess.p)))


RANK_SYSTEM = """\
You choose what {name} prepares tonight for {user}, from a list a program made of what's coming up. Prepared work \
only: {user} decides in the morning whether to use it. Pick what would help {user} most tomorrow and the next few \
days, judging by what you know of them below (deadlines, exams and how far along they are, what they've said matters, \
and how they answered this kind of work before). Leave out what they wouldn't want, what's too late to help, and \
anything their notes say not to do. Answer with JSON only: {{"order": [{{"key": "...", "why": "a few words"}}], \
"skip": [{{"key": "...", "why": "a few words"}}]}}, best first, using only the keys given."""


def rank_prompt(model_text: str, picks: Sequence[Pick], max_items: int, *, steer: bool = False) -> str:
    """The ranker's prompt. Her guesses at the user's answers are in it only once they've earned a say (``steer``)."""
    lines = [model_text, "", f"Candidates (choose at most {max_items}):"]
    for pick in picks[:MAX_RANKED]:
        c = pick.candidate
        guess = f" Your guess that they'd want it: {round(pick.guess.p * 100)}% ({pick.guess.basis})." if steer else ""
        lines.append(f'- key "{c.key}": {c.title} ({c.why}).{guess}')
    return "\n".join(lines)


def parse_rank(reply: str, picks: Sequence[Pick]) -> list[Pick] | None:
    """The ranker's order over ``picks`` (only keys it was given, each once), or None when it's unreadable."""
    match = re.search(r"\{.*\}", reply or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    order = data.get("order") if isinstance(data, dict) else None
    if not isinstance(order, list):
        return None
    by_key = {p.candidate.key: p for p in picks}
    out: list[Pick] = []
    for entry in order:
        key = entry.get("key") if isinstance(entry, dict) else entry
        pick = by_key.pop(str(key), None) if key is not None else None
        if pick is not None:
            why = entry.get("why") if isinstance(entry, dict) else ""
            pick.why = " ".join(str(why or "").split())[:160]
            out.append(pick)
    return out


async def choose(
    picks: list[Pick],
    *,
    max_items: int,
    model_text: str,
    ranker: Ranker | None = None,
    name: str = "Octavia",
    user: str = "the user",
    steer: bool = False,
) -> tuple[list[Pick], str]:
    """The best ``max_items`` of ``picks`` (best first) and how they were chosen: "ranked", or "rules" when the
    ranker is off or its answer wasn't usable."""
    if not picks:
        return [], "nothing to choose from"
    if ranker is not None:
        try:
            reply = await ranker(
                RANK_SYSTEM.format(name=name, user=user), rank_prompt(model_text, picks, max_items, steer=steer)
            )
        except Exception:
            log.warning("Proactive: the ranker failed", exc_info=True)
            reply = ""
        ranked = parse_rank(reply, picks)
        if ranked is not None:
            return ranked[:max_items], "ranked"
        log.info("Proactive: the ranker's answer wasn't usable; going by the rules' order")
    return picks[:max_items], "rules"  # already best first: the rules' score, weighted by her guesses once steering
