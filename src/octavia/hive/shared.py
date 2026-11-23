"""The shared lessons every install's prompt gets (``shared_lessons.yaml``, filled by ``octavia hive accept``)."""

from __future__ import annotations

from pathlib import Path

LESSONS_FILE = Path(__file__).with_name("shared_lessons.yaml")
MAX_LESSONS = 40


def lessons(path: Path = LESSONS_FILE) -> list[dict[str, str]]:
    import yaml

    try:
        rows = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("lessons") or []
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict) and str(row.get("text", "")).strip():
            out.append({"text": " ".join(str(row["text"]).split()), "about": str(row.get("about", "")).strip()})
    return out[-MAX_LESSONS:]


def lessons_block(path: Path = LESSONS_FILE) -> str:
    """The prompt's block of shared lessons; "" when there are none (the prompt is then exactly as before)."""
    rows = lessons(path)
    if not rows:
        return ""
    lines = [f"- {r['text']}" + (f" ({r['about']})" if r["about"] else "") for r in rows]
    return (
        "Lessons learned across every Octavia (reviewed, general, about apps and how to work, never about a user):\n"
        + "\n".join(lines)
    )
