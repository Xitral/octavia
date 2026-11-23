"""The setup's guided connections: the pages it opens, the keys it checks as they're pasted, and help when stuck.

The steps themselves are scripted on the page (``avatar/renderer/welcome/guides.js``), so they can't wander off;
this side does what the page can't: open a page in the user's browser (only the addresses listed here), check a
pasted key against its service right away, say a line in the chosen voice, and answer an "I'm stuck" question with
the user's own model when one can already be reached (an API key they pasted, or a Claude account that's signed in).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

# The only pages the setup opens (a key from the page, never an address from it).
PAGES = {
    "discord": "https://discord.com/developers/applications",
    "ticktick": "https://ticktick.com/webapp/#settings/account",
    "gmail": "https://myaccount.google.com/apppasswords",
    "fish": "https://fish.audio/app/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "discord-dev-mode": "https://support.discord.com/hc/en-us/articles/206346498",
}
TIMEOUT_S = 12.0


def page(target: str, canvas_url: str = "") -> str | None:
    """The address to open for ``target`` ("canvas" needs the school's address), or None for anything unlisted."""
    if target == "canvas":
        url = canvas_url.strip().rstrip("/")
        if url and "://" not in url:
            url = "https://" + url
        return f"{url}/profile/settings" if re.match(r"^https://[\w.-]+$", url) else None
    return PAGES.get(target)


def open_page(target: str, canvas_url: str = "") -> bool:
    import webbrowser

    url = page(target, canvas_url)
    return bool(url) and webbrowser.open(url)


def _client() -> Any:
    import httpx

    from octavia.net import tls_context

    return httpx.Client(timeout=TIMEOUT_S, verify=tls_context(), follow_redirects=False)


def check(what: str, value: str, extra: str = "") -> tuple[bool, str]:
    """Whether a pasted key works, checked with its service: (ok, what to say). Never raises."""
    value = value.strip()
    if not value:
        return False, "Paste it in first."
    try:
        if what == "discord-token":
            with _client() as c:
                r = c.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {value}"})
            if r.status_code == 200:
                return True, f"Got it, that works. Your bot is {r.json().get('username', 'ready')}."
            return False, "Discord didn't accept that token. Reset it again and copy the new one."
        if what == "discord-owner":
            if re.fullmatch(r"\d{17,20}", value):
                return True, "That looks like a user ID."
            return False, "A user ID is a long number, 17 to 20 digits."
        if what == "ticktick":
            with _client() as c:
                r = c.get("https://api.ticktick.com/open/v1/project", headers={"Authorization": f"Bearer {value}"})
            return (True, "Got it, TickTick answered.") if r.status_code == 200 else (
                False, "TickTick didn't accept that token. Copy it again from Account & Security.")  # fmt: skip
        if what == "canvas-token":
            base = page("canvas", extra)
            if base is None:
                return False, "Fill in your school's Canvas address first."
            with _client() as c:
                r = c.get(base.replace("/profile/settings", "/api/v1/users/self"),
                          headers={"Authorization": f"Bearer {value}"})  # fmt: skip
            if r.status_code == 200:
                return True, f"Got it. Hi, {str(r.json().get('short_name') or 'there')}."
            return False, "Canvas didn't accept that token. Make a new one and paste it right away."
        if what == "gmail-password":
            import imaplib

            box = imaplib.IMAP4_SSL("imap.gmail.com", timeout=TIMEOUT_S)
            try:
                box.login(extra.strip(), value.replace(" ", ""))
            finally:
                try:
                    box.logout()
                except Exception:
                    pass
            return True, "Got it, Gmail let me in."
        if what == "anthropic":
            with _client() as c:
                r = c.get("https://api.anthropic.com/v1/models",
                          headers={"x-api-key": value, "anthropic-version": "2023-06-01"})  # fmt: skip
            return (True, "Got it, that key works.") if r.status_code == 200 else (
                False, "Anthropic didn't accept that key.")  # fmt: skip
        if what == "other-model":
            base = extra.strip().rstrip("/")
            if not re.match(r"^https?://", base):
                return False, "It needs the service's address first."
            with _client() as c:
                r = c.get(f"{base}/models", headers={"Authorization": f"Bearer {value}"} if value != "-" else {})
            return (True, "Got it, the service answered.") if r.status_code == 200 else (
                False, f"The service answered {r.status_code}. Check the key and the address.")  # fmt: skip
    except Exception as exc:  # offline, refused, a bad login: said plainly, never a crash
        text = str(exc)
        if "AUTHENTICATIONFAILED" in text or "Invalid credentials" in text:
            return False, "Gmail didn't accept that. Use an app password, not your normal password."
        return False, f"I couldn't check it just now ({type(exc).__name__}). It may still be right."
    return False, "I don't know how to check that one."


STUCK_PROMPT = """You're OCTAVIA, a voice assistant, helping someone through your own setup on Windows. They're on
this step of connecting {guide}: "{step}". They asked: "{question}". Answer in two or three short, plain sentences:
what to look for or try next. Don't make up page names or buttons you aren't sure of; if you can't tell, say what to
check. No lists, no markdown."""


def stuck(root: Path, guide: str, step: str, question: str, keys: dict[str, str]) -> str | None:
    """An answer from the user's own model when one can be reached now, else None (the page shows the step's tips)."""
    prompt = STUCK_PROMPT.format(guide=guide, step=step[:400], question=question[:400])
    key = keys.get("ANTHROPIC_API_KEY", "").strip()
    try:
        if key:
            with _client() as c:
                r = c.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    json={"model": "claude-haiku-5-5", "max_tokens": 200,
                          "messages": [{"role": "user", "content": prompt}]},
                )  # fmt: skip
            if r.status_code == 200:
                return "".join(b.get("text", "") for b in r.json().get("content", [])).strip() or None
        from octavia.brain.claude_auth import _cli, _env, auth_status
        from octavia.config import load_config

        config = load_config(root=root)
        status = auth_status(config) or {}
        cli = _cli(config)
        if status.get("loggedIn") and cli is not None:
            done = subprocess.run(
                [str(cli), "-p", prompt, "--model", "haiku"], env=_env(config), capture_output=True, text=True,
                timeout=60, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )  # fmt: skip
            answer = done.stdout.strip()
            return answer or None
    except Exception:
        return None
    return None
