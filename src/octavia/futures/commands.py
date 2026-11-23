"""`octavia futures ...`: the developer's view of Octavia Futures (docs/futures.md).

Everything it shows comes straight from the database: events, the world model, episodes, outcomes, hypotheses with
their evidence, predictions, experiments, triggers, what she said on her own and why, her own performance, and the
audit trail of what changed which belief and what caused it. ``html`` writes all of it into one (plain, ugly,
complete) page. It works while the assistant runs: the database is in WAL mode, and this only reads, except for
``reset``.
"""

from __future__ import annotations

import argparse
import html
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from octavia.futures.store import loads

if TYPE_CHECKING:
    from octavia.config import AppConfig
    from octavia.futures.engine import FuturesService

_TABLES = (
    "events",
    "entities",
    "relations",
    "episodes",
    "outcomes",
    "hypotheses",
    "predictions",
    "experiments",
    "triggers",
    "recommendations",
    "objectives",
    "performance",
    "changes",
    "habits",
)


async def run_command(config: AppConfig, args: argparse.Namespace) -> int:
    from octavia.futures.engine import FuturesService

    service = FuturesService(config)
    try:
        command = args.futures_command
        if command == "reset":
            if not args.yes:
                print("This deletes everything Octavia Futures learned (your controls stay). Add --yes to do it.")
                return 1
            from octavia.futures.reports import reset

            print(f"Deleted {await service.call(reset, service)} records.")
            return 0
        if command == "html":
            out = Path(args.out) if args.out else config.resolve_path("data/futures/debug.html")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(await service.call(debug_page, service), encoding="utf-8")
            print(out)
            return 0
        print(await service.call(_TEXT[command], service, args))
        return 0
    finally:
        service.store.close()


# ---------------------------------------------------------------------------------------------- text views
def _stamp(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def _status(service: FuturesService, args: Any) -> str:
    store = service.store
    lines = [f"Database: {store.path} (schema version {store.schema_version})"]
    for table in _TABLES:
        row = store.one(f"SELECT COUNT(*) AS n FROM {table}")
        lines.append(f"  {table:<16} {row['n'] if row else 0}")
    last = service.log.recent(limit=1)
    if last:
        lines.append(f"Last event: {last[0].type} at {_stamp(last[0].ts)}")
    from octavia.futures.reports import report

    lines.append("")
    lines.append(report(service, "privacy"))
    return "\n".join(lines)


def _events(service: FuturesService, args: Any) -> str:
    types = [args.type] if args.type else None
    rows = service.log.recent(types=types, limit=args.limit)
    return (
        "\n".join(
            f"{_stamp(e.ts)}  {e.type:<28} {e.source:<9} {e.subject or '':<34} {e.outcome or ''} {dict(e.data)}"
            for e in reversed(rows)
        )
        or "No events."
    )


def _world(service: FuturesService, args: Any) -> str:
    lines = []
    for entity in service.world.find(args.kind, limit=100):
        lines.append(f"{entity.ref:<44} {entity.status or '':<10} {entity.name}  {dict(entity.attrs)}")
        for relation in service.world.related(entity.ref, direction="out"):
            lines.append(f"    └ {relation.predicate} → {relation.object} {dict(relation.attrs) or ''}")
    return "\n".join(lines) or "The world model is empty."


def _episodes(service: FuturesService, args: Any) -> str:
    lines = []
    for e in service.episodes.recent(limit=args.limit):
        lines.append(f"#{e.id} [{e.kind}/{e.status}] {_stamp(e.started_at)} {e.title} — {e.outcome or ''}")
        for label, values in (("problems", e.problems), ("resolutions", e.resolutions), ("learned", e.learned)):
            if values:
                lines.append(f"    {label}: {'; '.join(values)}")
    return "\n".join(lines) or "No episodes."


def _outcomes(service: FuturesService, args: Any) -> str:
    return (
        "\n".join(
            f"#{o.id} {_stamp(o.resolved_at)} {o.kind:<14} {'ok ' if o.success else 'no ' if o.success is False else '-  '}"
            f"{o.subject:<36} {dict(o.features)}"
            for o in service.outcomes.list(limit=args.limit)
        )
        or "No outcomes."
    )


def _hypotheses(service: FuturesService, args: Any) -> str:
    lines = []
    for h in service.hypotheses.list(limit=args.limit):
        lines.append(f"#{h.id} [{h.status} · {h.strength} · {h.claim} · {h.knowledge.value}] {h.statement}")
        for ev in service.hypotheses.evidence(h.id, limit=3):
            lines.append(f"    {_stamp(ev['ts'])} {ev['direction']:+d} {ev['note']}")
    return "\n".join(lines) or "No hypotheses."


def _predictions(service: FuturesService, args: Any) -> str:
    lines = []
    for p in service.predictions.list(limit=args.limit):
        verdict = "" if p.correct is None else (" ✓" if p.correct else " ✗")
        lines.append(f"#{p.id} [{p.status}{verdict}] {round(p.probability * 100)}% {p.statement} — {p.basis}")
    a = service.predictions.accuracy()
    lines.append(
        f"Settled: {a.resolved}, right: {a.correct}, Brier: {a.brier if a.brier is None else round(a.brier, 3)}"
    )
    return "\n".join(lines)


def _experiments(service: FuturesService, args: Any) -> str:
    from octavia.futures.reports import report

    return report(service, "experiments", 20)


def _triggers(service: FuturesService, args: Any) -> str:
    return (
        "\n".join(
            f"#{t.id} [{t.status}] {t.description}: {t.message} (fired {t.fire_count}×, until {_stamp(t.expires_at)})"
            for t in service.triggers.list(limit=args.limit)
        )
        or "No triggers."
    )


def _recommendations(service: FuturesService, args: Any) -> str:
    lines = []
    for r in service.gate.list(limit=args.limit):
        lines.append(
            f"#{r.id} {_stamp(r.created_at)} [{r.status}: {r.reason}] {r.kind} ({r.policy}) {r.text}"
            + (f" → {r.response}" if r.response else "")
        )
    return "\n".join(lines) or "Nothing said or held back yet."


def _performance(service: FuturesService, args: Any) -> str:
    return (
        "\n".join(
            f"{s.kind:<9} {s.name:<28} {s.tally.describe():<10} "
            f"{'' if s.median_latency_s is None else f'{s.median_latency_s:.2f}s'} corrected {s.corrected}"
            for s in service.performance.stats()[: args.limit]
        )
        or "No measurements."
    )


def _report(service: FuturesService, args: Any) -> str:
    from octavia.futures.reports import report

    return report(service, args.topic, 10)


def _habits(service: FuturesService, args: Any) -> str:
    from octavia.futures.reports import habit

    if args.action != "list" and args.number is None:
        return "Say which habit, by its number: octavia futures habits drop 2"
    return habit(service, args.action, number=args.number)


def _explain(service: FuturesService, args: Any) -> str:
    from octavia.futures.reports import explain_target

    return explain_target(service, " ".join(args.what))


def _trace(service: FuturesService, args: Any) -> str:
    return (
        "\n".join(
            f"{_stamp(c['ts'])} {c['object']:<22} {c['change']:<14} by {c['provider']:<13} cause {c['cause'] or '-'} "
            f"{c['detail'] or ''}"
            for c in service.store.changes(args.object, limit=args.limit)
        )
        or "Nothing in the audit trail."
    )


_TEXT = {
    "status": _status,
    "events": _events,
    "world": _world,
    "episodes": _episodes,
    "outcomes": _outcomes,
    "hypotheses": _hypotheses,
    "predictions": _predictions,
    "experiments": _experiments,
    "triggers": _triggers,
    "recommendations": _recommendations,
    "performance": _performance,
    "report": _report,
    "habits": _habits,
    "explain": _explain,
    "trace": _trace,
}


# ---------------------------------------------------------------------------------------------- the debug page
def debug_page(service: FuturesService) -> str:
    """Every table as an HTML table, newest first: for looking at what she believes and why."""
    store = service.store
    parts = [
        "<!doctype html><meta charset='utf-8'><title>Octavia Futures</title>",
        "<style>body{font:13px system-ui;margin:16px;background:#fff;color:#111}table{border-collapse:collapse;"
        "margin-bottom:24px}td,th{border:1px solid #ccc;padding:3px 6px;vertical-align:top;text-align:left}"
        "th{background:#eee}h2{margin-top:28px}code{white-space:pre-wrap}</style>",
        f"<h1>Octavia Futures</h1><p>{html.escape(store.path)} · schema v{store.schema_version} · "
        f"{html.escape(_stamp(service.clock()))}</p>",
    ]
    order = {
        "events": "ts",
        "entities": "last_seen",
        "relations": "updated_at",
        "episodes": "started_at",
        "outcomes": "resolved_at",
        "hypotheses": "updated_at",
        "predictions": "created_at",
        "experiments": "created_at",
        "triggers": "created_at",
        "recommendations": "created_at",
        "objectives": "updated_at",
        "performance": "ts",
        "changes": "ts",
    }
    for table, column in order.items():
        rows = store.execute(f"SELECT * FROM {table} ORDER BY {column} DESC LIMIT 300")
        parts.append(f"<h2>{table} ({len(rows)} newest)</h2>")
        if not rows:
            parts.append("<p>empty</p>")
            continue
        keys = rows[0].keys()
        parts.append("<table><tr>" + "".join(f"<th>{html.escape(k)}</th>" for k in keys) + "</tr>")
        for row in rows:
            cells = []
            for key in keys:
                value = row[key]
                if isinstance(value, float) and key.endswith(("_at", "ts", "_seen")) and value > 1e9:
                    value = _stamp(value)
                elif isinstance(value, str) and value[:1] in "[{":
                    value = str(loads(value, value))
                cells.append(f"<td><code>{html.escape('' if value is None else str(value))}</code></td>")
            parts.append("<tr>" + "".join(cells) + "</tr>")
        parts.append("</table>")
    return "\n".join(parts)
