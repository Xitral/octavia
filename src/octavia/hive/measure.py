"""What a lesson does, measured before it's shared with everyone (the maintainer's side).

The shared cases pass almost whole, so a lesson can't show its worth on their total: one more rule in every prompt
either changes nothing there or breaks something. So a lesson is shared **with the case it fixes**. The case (one
of the shared cases, new or old) is asked several times without the lesson and several times with it, and the
lesson is only worth every install's prompt when the case mostly fails without it and mostly passes with it. Then
every shared case is run with the lesson in, and it's refused if that total drops. Both numbers go into the feed
with the item (``channel.Item.effect``), so what an install is given comes with what it was accepted on.

Runs on the maintainer's Claude subscription; nothing is sent anywhere.
"""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

TRIES = 5
MIN_TRIES = 3
MAX_CHANCE = 0.05


_TRIAL_ENV = "ETHER_TRIAL_LESSON"
# The eval in a process of its own, as the gate runs it (her Claude sign-in, no API key in the way), with the lesson
# on trial put in the prompt for that process alone.
_BOOT = (
    "import json, os, sys\n"
    "from octavia.hive import shared\n"
    f"shared.TRIAL.extend(json.loads(os.environ.get('{_TRIAL_ENV}') or '[]'))\n"
    "from octavia.evolve import evals\n"
    "sys.exit(evals.main(sys.argv[1:]))\n"
)
RUN_TIMEOUT_S = 1800.0


def _run_eval(root: Path | None, args: list[str], trial: list[dict[str, str]]) -> dict[str, Any]:
    import octavia
    from octavia.config import find_project_root
    from octavia.evolve import evals

    root = Path(root) if root is not None else find_project_root()
    src = Path(octavia.__file__).resolve().parents[1]
    env = evals._environment(src, root)
    env[_TRIAL_ENV] = json.dumps(trial)
    with tempfile.TemporaryDirectory(prefix="ether-measure-") as tmp:
        out = Path(tmp) / "out.json"
        command = [sys.executable, "-c", _BOOT, "run", "--shared", "--quiet", "--root", str(root), "--out", str(out)]
        done = subprocess.run(  # noqa: S603
            [*command, *args], env=env, cwd=root, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, check=False
        )
        if done.returncode != 0 or not out.exists():
            raise ValueError(f"the shared cases couldn't be run: {(done.stderr or done.stdout).strip()[-300:]}")
        return json.loads(out.read_text(encoding="utf-8"))


async def _passes(
    root: Path | None, case_ids: list[str] | None, tries: int, workers: int, trial: list[dict[str, str]] | None = None
) -> tuple[int, int, int]:
    """(passes, asks, cases) over the shared cases named (all of them for None), each asked ``tries`` times, with
    ``trial`` lessons in the prompt. Asks that couldn't reach Claude aren't counted; when none could, that's said."""
    args = ["--workers", str(workers), "--tries", str(tries), "--until", str(tries + 1)]
    if case_ids is not None:
        args += ["--cases", ",".join(case_ids)]
    run = await asyncio.to_thread(_run_eval, root, args, trial or [])
    passes = asks = 0
    unreachable = ""
    for case in run.get("cases") or []:
        for attempt in case.get("attempts") or [case]:
            if attempt.get("infra"):
                unreachable = unreachable or str(attempt.get("error") or "no reason given")
                continue
            asks += 1
            passes += bool(attempt.get("passed"))
    if not asks and unreachable:  # said as it is: never a measurement of nothing
        raise ValueError(f"the cases couldn't be asked: {' '.join(unreachable.split())[:300]}")
    return passes, asks, len(run.get("cases") or [])


def chance(without: tuple[int, int], with_it: tuple[int, int]) -> float:
    """How likely passes split at least this much in the lesson's favour when it does nothing (Fisher's exact test,
    one-sided): of all the ways the passes seen could have fallen across the asks, the share with as many or more on
    the lesson's side."""
    (base, base_asks), (now, now_asks) = without, with_it
    passes, asks = base + now, base_asks + now_asks
    if not base_asks or not now_asks:
        return 1.0
    ways = math.comb(asks, passes)
    most = min(passes, now_asks)
    return sum(math.comb(now_asks, k) * math.comb(base_asks, passes - k) for k in range(now, most + 1)) / ways


def verdict(without: tuple[int, int], with_it: tuple[int, int], tries: int = TRIES) -> str | None:
    """Why the lesson isn't worth sharing on these numbers (passes, asks), or None when it is. Every ask has to
    have been answered; the case has to pass at least two thirds of the time with the lesson; and the difference
    has to be one chance makes no more than once in twenty (``chance``): 1 of 5 against 5 of 5 is, 2 of 5 against
    5 of 5 isn't yet, and more asks settle it either way."""
    (base, base_asks), (now, now_asks) = without, with_it
    if tries < MIN_TRIES:
        return f"{tries} asks are too few to tell a fix from luck ({MIN_TRIES} at least)"
    if base_asks < tries or now_asks < tries:
        return f"the case couldn't be asked every time ({base_asks} and {now_asks} of {tries}): measure it again"
    if now <= base:
        return f"the case does no better with it ({base} of {base_asks} without, {now} of {now_asks} with)"
    if 3 * now < 2 * now_asks:
        return f"the case still fails with it ({now} of {now_asks})"
    odds = chance(without, with_it)
    if odds > MAX_CHANCE:
        return (
            f"{base} of {base_asks} without and {now} of {now_asks} with could be luck (about {odds:.0%} of the "
            "time): ask more times (--tries) to tell"
        )
    return None


async def lesson_effect(
    root: Path | None, text: str, about: str, case_id: str, *, tries: int = TRIES, workers: int = 6, total: bool = True
) -> dict[str, Any]:
    """The lesson's measured effect: its case without and with it, and (``total``) every shared case with it in and
    without. ``refused`` says why it isn't to be shared, when it isn't."""
    trial = [{"text": text, "about": about, "id": "trial"}]
    without = (await _passes(root, [case_id], tries, workers))[:2]
    with_it = (await _passes(root, [case_id], tries, workers, trial))[:2]
    effect: dict[str, Any] = {
        "case": case_id,
        "without": f"{without[0]}/{without[1]}",
        "with": f"{with_it[0]}/{with_it[1]}",
        "refused": verdict(without, with_it, tries),
    }
    if total and effect["refused"] is None:
        in_passed, in_asks, cases = await _passes(root, None, 1, workers, trial)
        out_passed, out_asks, _ = await _passes(root, None, 1, workers)
        effect["shared_with"], effect["shared_without"] = f"{in_passed}/{in_asks}", f"{out_passed}/{out_asks}"
        if in_asks < cases or out_asks < cases:  # counts are only compared over the same, whole set
            effect["refused"] = f"not every shared case could be asked ({in_asks} and {out_asks} of {cases})"
        elif in_passed < out_passed - 1:  # one case either way is the set's own flake (58 or 59 of 59, run to run)
            effect["refused"] = (
                f"the shared cases drop with it in ({effect['shared_with']} from {effect['shared_without']})"
            )
    return effect


async def shared_total(root: Path | None, workers: int = 6) -> tuple[int, int, int]:
    """(passed, asked, cases): every shared case on this code, a case counted passed when it passes one of two asks
    (as the gate counts a flaky one). A release's measure, computed here and never typed; ``asked`` below ``cases``
    means Claude couldn't be reached for some, and the number isn't one to publish."""
    run = await asyncio.to_thread(_run_eval, root, ["--workers", str(workers), "--tries", "2"], [])
    every = run.get("cases") or []
    counted = [c for c in every if not c.get("infra")]
    passed = sum(1 for c in counted if c.get("passed") or any(a.get("passed") for a in c.get("attempts") or []))
    return passed, len(counted), len(every)


def publishable(measured: tuple[int, int, int], last: dict[str, Any] | None, allow_drop: bool = False) -> str | None:
    """Why a release with these shared-case numbers (``shared_total``) isn't to be published, or None: every case
    has to have been asked, and it may not pass fewer than the release before it (``last``: that one's
    ``shared_eval``) unless the maintainer says so."""
    passed, asked, cases = measured
    if not cases or asked < cases:
        return f"only {asked} of {cases} shared cases could be asked (Claude wasn't reachable for the rest)"
    before = last or {}
    if not allow_drop and before.get("total") and passed * before["total"] < before["passed"] * cases:
        return (
            f"it passes {passed}/{cases} shared cases, fewer than the last release "
            f"({before['passed']}/{before['total']}): --allow-drop to publish anyway"
        )
    return None
