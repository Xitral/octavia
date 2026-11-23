"""A browser of her own for web tasks, driven over the Chrome DevTools Protocol.

Web tasks (``_web_tasks``) work in Chrome or Edge started with a profile of its own (``data/web_browser``), so the
logins made there persist (the user signs in once, in the window ``open_web_task_browser`` shows) and their own
browser is never touched. While tasks run it's headless: no window, and every click and keystroke goes to the page
through the protocol, so the real mouse and keyboard stay with the user and nothing takes the focus. Playwright isn't
a dependency; the protocol is plain JSON over a WebSocket (``websockets``) plus a few HTTP endpoints (``httpx``).

One browser serves every task, each in a tab of its own (``WebBrowser.page``); it's closed when the last one is done.
A browser left running by an earlier run (the app crashed) is found through the ``DevToolsActivePort`` file it writes
in its profile, and used again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

PROFILE_DIR = "data/web_browser"
PORT_FILE = "DevToolsActivePort"
VIEWPORT = (1280, 800)  # a page size Claude sees screenshots of as they are
LAUNCH_TIMEOUT_S = 20.0
LOAD_TIMEOUT_S = 15.0
SETTLE_S = 0.6  # after a click or a key, for the page's scripts to react
MAX_TEXT_CHARS = 6000
MAX_ELEMENTS = 150
SCREENSHOT_QUALITY = 70

_NOT_THERE = "That isn't on the page anymore (it changed); read the page again."


class BrowserError(Exception):
    """Something that went wrong in the browser, in words the task's brain can act on."""


def find_browser(env: Mapping[str, str] = os.environ) -> str | None:
    """Chrome, else Edge (always there on Windows 11): both speak the DevTools protocol and run headless."""
    places = [
        (env.get("PROGRAMFILES"), r"Google\Chrome\Application\chrome.exe"),
        (env.get("PROGRAMFILES(X86)"), r"Google\Chrome\Application\chrome.exe"),
        (env.get("LOCALAPPDATA"), r"Google\Chrome\Application\chrome.exe"),
        (env.get("PROGRAMFILES(X86)"), r"Microsoft\Edge\Application\msedge.exe"),
        (env.get("PROGRAMFILES"), r"Microsoft\Edge\Application\msedge.exe"),
    ]
    for root, rest in places:
        if root and (path := Path(root) / rest).is_file():
            return str(path)
    return shutil.which("chrome") or shutil.which("msedge")


# --------------------------------------------------------------------------- the protocol


class Cdp:
    """One DevTools connection (a tab's): numbered requests answered by id, events handed to their handlers."""

    def __init__(self, socket: Any) -> None:
        self._socket = socket
        self._next = 0
        self._waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], None]] = {}
        self.closed = False
        self._reader = asyncio.get_running_loop().create_task(self._read(), name="cdp-reader")

    def on(self, event: str, handler: Callable[[dict[str, Any]], None]) -> None:
        self._handlers[event] = handler

    async def send(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        if self.closed:
            raise BrowserError("The browser tab closed.")
        self._next += 1
        number = self._next
        answer: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[number] = answer
        try:
            await self._socket.send(json.dumps({"id": number, "method": method, "params": params or {}}))
            async with asyncio.timeout(timeout):
                return await answer
        except TimeoutError:
            raise BrowserError(f"The page didn't answer in time ({method}).") from None
        finally:
            self._waiting.pop(number, None)

    async def _read(self) -> None:
        try:
            async for raw in self._socket:
                message = json.loads(raw)
                number = message.get("id")
                if number is not None:
                    answer = self._waiting.get(number)
                    if answer is None or answer.done():
                        continue
                    if "error" in message:
                        answer.set_exception(BrowserError(str(message["error"].get("message", "the browser refused"))))
                    else:
                        answer.set_result(message.get("result", {}))
                elif handler := self._handlers.get(message.get("method", "")):
                    handler(message.get("params", {}))
        except Exception:
            log.debug("The DevTools connection ended", exc_info=True)
        finally:
            self.closed = True
            for answer in self._waiting.values():
                if not answer.done():
                    answer.set_exception(BrowserError("The browser tab closed."))

    async def close(self) -> None:
        self.closed = True
        with contextlib.suppress(Exception):
            await self._socket.close()
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader


# --------------------------------------------------------------------------- one tab

# Numbers what can be clicked or typed into (``data-oc-ref``) and returns them with the page's text. A control inside
# one already numbered (a button's icon) isn't numbered again; inputs always are.
_SCAN_JS = """(() => {
  const selector = 'a[href], button, input:not([type=hidden]), textarea, select, summary, [role=button], [role=link],'
    + ' [role=checkbox], [role=radio], [role=tab], [role=menuitem], [role=option], [role=switch], [role=textbox],'
    + ' [contenteditable=""], [contenteditable=true], [onclick], [tabindex]:not([tabindex="-1"]),'
    + ' .monaco-editor, .cm-editor, .CodeMirror';
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  document.querySelectorAll('[data-oc-ref]').forEach((e) => e.removeAttribute('data-oc-ref'));
  const items = [];
  const height = window.innerHeight;
  for (const el of document.querySelectorAll(selector)) {
    if (items.length >= __LIMIT__) break;
    const tag = el.tagName;
    const field = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
    if (!field && el.parentElement && el.parentElement.closest('[data-oc-ref]')) continue;
    const box = el.getBoundingClientRect();
    if (box.width < 4 || box.height < 4) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const type = (el.getAttribute('type') || '').toLowerCase();
    let role = el.getAttribute('role') || '';
    if (el.matches('.monaco-editor, .cm-editor, .CodeMirror')) role = 'code editor';
    else if (!role) {
      if (tag === 'A') role = 'link';
      else if (tag === 'BUTTON' || tag === 'SUMMARY') role = 'button';
      else if (tag === 'SELECT') role = 'select';
      else if (tag === 'TEXTAREA' || el.isContentEditable) role = 'textbox';
      else if (tag === 'INPUT') role = ['checkbox', 'radio', 'button', 'submit', 'file', 'range'].includes(type)
        ? (type === 'submit' ? 'button' : type) : 'textbox';
      else role = 'clickable';
    }
    const labelled = el.labels && el.labels.length ? el.labels[0].innerText : '';
    const text = field || role === 'code editor' ? '' : el.innerText;
    const img = el.querySelector && el.querySelector('img[alt]');
    const name = clean(el.getAttribute('aria-label') || labelled || text || el.getAttribute('placeholder')
      || el.getAttribute('title') || el.getAttribute('alt') || (img ? img.alt : '')
      || (tag === 'INPUT' && type === 'submit' ? el.value : '')).slice(0, 80);
    let value = '';
    if (tag === 'SELECT') value = el.selectedOptions.length ? el.selectedOptions[0].text : '';
    else if (field && !['checkbox', 'radio', 'submit', 'button'].includes(type)) value = type === 'password'
      ? (el.value ? '(filled in)' : '') : el.value;
    items.push({
      ref: items.length + 1, role, name, value: clean(value).slice(0, 80),
      checked: el.checked === true || el.getAttribute('aria-checked') === 'true'
        || el.getAttribute('aria-selected') === 'true',
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      where: box.bottom < 0 ? 'above' : box.top > height ? 'below' : '',
    });
    el.setAttribute('data-oc-ref', String(items.length));
  }
  const body = document.body ? document.body.innerText : '';
  return {title: document.title, url: location.href, text: body.replace(/\\n\\s*\\n+/g, '\\n\\n').trim(), items};
})()""".replace("__LIMIT__", str(MAX_ELEMENTS))

# The middle of a numbered element, scrolled into view first; null when it's gone.
_CENTER_JS = """((ref) => {
  const el = document.querySelector(`[data-oc-ref="${ref}"]`);
  if (!el) return null;
  el.scrollIntoView({block: 'center', inline: 'center'});
  const box = el.getBoundingClientRect();
  return {x: box.left + box.width / 2, y: box.top + box.height / 2};
})"""

# What a numbered element (or, for null, whatever has the keyboard focus, inside open shadow roots and same-site
# frames) is, for judging what a click or a key there would do (``_web_tasks.risky_keys``): its kind ("frame" for
# another site's frame, which can't be looked into), whether it's a search box, its form's buttons and which of them
# send it, and the text of the pop-up it's in. Null when nothing is.
_AROUND_JS = """((ref) => {
  let el = ref === null ? document.activeElement : document.querySelector(`[data-oc-ref="${ref}"]`);
  for (let hops = 0; el && hops < 20; hops++) {
    if (el.shadowRoot && el.shadowRoot.activeElement) { el = el.shadowRoot.activeElement; continue; }
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
      let inner = null;
      try { inner = el.contentDocument && el.contentDocument.activeElement; } catch (e) { inner = null; }
      if (inner && inner !== el.contentDocument.body) { el = inner; continue; }
    }
    break;
  }
  if (!el || el === document.body || el === document.documentElement) return null;
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const attr = (e, a) => (e.getAttribute && e.getAttribute(a)) || '';
  const closest = (e, selector) => {  // across shadow roots, out to their hosts
    for (let n = e; n; ) {
      const hit = n.closest ? n.closest(selector) : null;
      if (hit) return hit;
      const root = n.getRootNode ? n.getRootNode() : null;
      n = root && root.host ? root.host : null;
    }
    return null;
  };
  const named = (e) => clean(attr(e, 'aria-label') || e.innerText || e.value || attr(e, 'title')).slice(0, 80);
  const tag = el.tagName || '';
  const type = attr(el, 'type').toLowerCase();
  const role = attr(el, 'role').toLowerCase();
  const label = clean(attr(el, 'aria-label') || attr(el, 'placeholder') || attr(el, 'title') || attr(el, 'name')
    || (el.labels && el.labels.length ? el.labels[0].innerText : ''));
  let kind = 'other';
  if (tag === 'IFRAME' || tag === 'FRAME') kind = 'frame';
  else if (closest(el, '.monaco-editor, .cm-editor, .CodeMirror')) kind = 'editor';
  else if (tag === 'TEXTAREA' || el.isContentEditable || (role === 'textbox' && tag !== 'INPUT')) kind = 'lines';
  else if (tag === 'INPUT' && !['checkbox', 'radio', 'button', 'submit', 'reset', 'file', 'range', 'color', 'image']
    .includes(type)) kind = 'field';
  else if (['BUTTON', 'A', 'SUMMARY'].includes(tag) || ['button', 'submit', 'reset', 'image'].includes(type)
    || ['button', 'link', 'menuitem', 'tab', 'option', 'switch'].includes(role)) kind = 'control';
  const search = type === 'search' || ['searchbox', 'combobox'].includes(role) || !!closest(el, '[role=search]')
    || /\\b(search|find|filter|query|look ?up)\\b/i.test(label);
  const form = el.form || closest(el, 'form');
  const buttons = [], submits = [];
  if (form) {
    const all = new Set([...form.elements].filter((e) => e.tagName === 'BUTTON'
      || (e.tagName === 'INPUT' && ['submit', 'button', 'image', 'reset'].includes(e.type))));
    form.querySelectorAll('[role=button]').forEach((e) => all.add(e));
    for (const e of all) {
      const name = named(e);
      if (buttons.length < 20 && name) buttons.push(name);
      if (['submit', 'image'].includes(e.type) && e.tagName !== 'DIV' && name) submits.push(name);
    }
  }
  const name = kind === 'control' ? named(el) : label.slice(0, 80);
  const popup = closest(el, 'dialog, [role=dialog], [role=alertdialog], [aria-modal=true]');
  return {kind, search, name, in_form: !!form, buttons, submits,
    popup: popup ? clean(popup.innerText).slice(0, 600) : ''};
})"""

# Picks a <select>'s option by its text (a closed list can't be typed into); its label, or null when none matches.
_SELECT_JS = """((ref, wanted) => {
  const el = document.querySelector(`[data-oc-ref="${ref}"]`);
  if (!el || el.tagName !== 'SELECT') return null;
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const options = [...el.options];
  const option = options.find((o) => norm(o.text) === norm(wanted)) || options.find((o) => norm(o.text).includes(norm(wanted)));
  if (!option) return null;
  el.value = option.value;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return option.text;
})"""

_MODIFIERS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "win": 4, "cmd": 4, "shift": 8}
# name -> (key, code, Windows virtual-key code, text it types)
_KEYS: dict[str, tuple[str, str, int, str]] = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, ""),
    "escape": ("Escape", "Escape", 27, ""),
    "backspace": ("Backspace", "Backspace", 8, ""),
    "delete": ("Delete", "Delete", 46, ""),
    "space": (" ", "Space", 32, " "),
    "arrowup": ("ArrowUp", "ArrowUp", 38, ""),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, ""),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, ""),
    "arrowright": ("ArrowRight", "ArrowRight", 39, ""),
    "pageup": ("PageUp", "PageUp", 33, ""),
    "pagedown": ("PageDown", "PageDown", 34, ""),
    "home": ("Home", "Home", 36, ""),
    "end": ("End", "End", 35, ""),
}
_ALIASES = {"esc": "escape", "return": "enter", "up": "arrowup", "down": "arrowdown", "left": "arrowleft"}
_ALIASES |= {"right": "arrowright", "del": "delete"}


def parse_keys(combo: str) -> tuple[int, tuple[str, str, int, str]]:
    """'Ctrl+Shift+Enter' -> (modifier bits, the key). A single character types itself."""
    parts = [part.strip() for part in combo.replace(" + ", "+").split("+")]
    if not parts or not parts[-1]:
        raise BrowserError(f"“{combo}” isn't a key.")
    modifiers = 0
    for part in parts[:-1]:
        if part.casefold() not in _MODIFIERS:
            raise BrowserError(f"“{part}” isn't a modifier key (Ctrl, Shift, Alt).")
        modifiers |= _MODIFIERS[part.casefold()]
    last = parts[-1]
    name = _ALIASES.get(last.casefold(), last.casefold())
    if name in _KEYS:
        return modifiers, _KEYS[name]
    if len(last) == 1:
        upper = last.upper()
        if upper.isalpha():  # "Ctrl+A" is the a key; with Shift it's a capital
            last = upper if modifiers & _MODIFIERS["shift"] else last.lower()
        code = f"Key{upper}" if upper.isalpha() else f"Digit{last}" if last.isdigit() else ""
        return modifiers, (last, code, ord(upper) if upper.isalnum() else 0, last)
    raise BrowserError(f"“{last}” isn't a key I know.")


def describe(item: Mapping[str, Any]) -> str:
    """One numbered element as the brain reads it: [3] textbox "Email" = "a@b.c" (checked, below)."""
    line = f"[{item['ref']}] {item['role']}"
    if item.get("name"):
        line += f' "{item["name"]}"'
    if item.get("value"):
        line += f' = "{item["value"]}"'
    states = [s for s in ("checked", "disabled") if item.get(s)]
    if item.get("where"):
        states.append(f"scrolled {item['where']}")
    return line + (f" ({', '.join(states)})" if states else "")


def match_item(items: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    """The element named ``wanted``: the same name first, then one whose name holds it (enabled ones first)."""
    target = " ".join(wanted.split()).casefold()
    if not target:
        return None
    ordered = sorted(items, key=lambda item: bool(item.get("disabled")))
    for test in (lambda name: name == target, lambda name: target in name):
        for item in ordered:
            if test(str(item.get("name", "")).casefold()):
                return item
    return None


class WebPage:
    """A task's tab: open a page, read it, click, type and press keys in it, all through the protocol."""

    def __init__(self, cdp: Cdp, target: str, *, settle_s: float = SETTLE_S, load_timeout_s: float = LOAD_TIMEOUT_S):
        self.cdp = cdp
        self.target = target
        self.title = ""
        self.url = ""
        self._settle_s = settle_s
        self._load_timeout_s = load_timeout_s
        self._items: list[dict[str, Any]] = []
        self._side: set[asyncio.Task[Any]] = set()
        # Why a page's own "Are you sure…?" box (a confirm or a prompt) gets Cancel, from its message, or None for OK.
        # Without it every box is answered OK. What was answered Cancel waits in ``declined`` (``take_declined``).
        self.dialog_check: Callable[[str], str | None] | None = None
        self.declined: list[str] = []

    @property
    def closed(self) -> bool:
        return self.cdp.closed

    async def prepare(self, user_agent: str = "") -> None:
        """Pages that answer as in a focused, ordinary browser; dialogs that never hold the page up."""
        self.cdp.on("Page.javascriptDialogOpening", self._dialog)
        await self.cdp.send("Page.enable")
        await self.cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        if "HeadlessChrome" in user_agent:  # some sites turn a headless browser away
            await self.cdp.send(
                "Emulation.setUserAgentOverride", {"userAgent": user_agent.replace("HeadlessChrome", "Chrome")}
            )

    def _dialog(self, params: dict[str, Any]) -> None:
        """An alert or a "leave this page?" would hold the page until answered: it's answered at once. OK, unless it's
        an "Are you sure…?" (a confirm or a prompt) that ``dialog_check`` refuses."""
        accept = True
        message = " ".join(str(params.get("message", "")).split())
        if params.get("type") in ("confirm", "prompt") and self.dialog_check is not None:
            try:
                refused = self.dialog_check(message)
            except Exception:
                log.exception("Couldn't judge the page's dialog")
                refused = "I couldn't check what it does"
            if refused:
                accept = False
                shown = message if len(message) <= 160 else message[:157] + "…"
                self.declined.append(f"The page then asked “{shown}”, and it was answered Cancel: {refused}.")
        answer = asyncio.get_running_loop().create_task(
            self.cdp.send("Page.handleJavaScriptDialog", {"accept": accept}), name="cdp-dialog"
        )
        self._side.add(answer)
        answer.add_done_callback(self._side_done)

    def _side_done(self, task: asyncio.Task[Any]) -> None:
        self._side.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.debug("Couldn't answer a page's dialog: %s", task.exception())

    async def evaluate(self, expression: str) -> Any:
        reply = await self.cdp.send(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}
        )
        if "exceptionDetails" in reply:
            details = reply["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text", "")
            raise BrowserError(f"The page's script failed: {text}")
        return reply.get("result", {}).get("value")

    async def wait_ready(self) -> None:
        """Until the page has loaded (or the time is up: some never stop loading), then a moment for its scripts."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._load_timeout_s
        while loop.time() < deadline:
            try:
                if await self.evaluate("document.readyState") == "complete":
                    break
            except BrowserError:  # between two pages there's no document to ask
                if self.closed:
                    raise
            await asyncio.sleep(0.2)
        await asyncio.sleep(self._settle_s)

    async def goto(self, url: str) -> None:
        reply = await self.cdp.send("Page.navigate", {"url": url})
        if error := reply.get("errorText"):
            raise BrowserError(f"Couldn't open {url}: {error}.")
        await self.wait_ready()

    async def back(self) -> None:
        await self.evaluate("history.back()")
        await asyncio.sleep(self._settle_s)
        await self.wait_ready()

    async def scan(self) -> dict[str, Any]:
        found = await self.evaluate(_SCAN_JS)
        if not isinstance(found, dict):
            raise BrowserError("The page couldn't be read.")
        self.title, self.url = str(found.get("title", "")), str(found.get("url", ""))
        self._items = [item for item in found.get("items", []) if isinstance(item, dict)]
        return found

    async def read(self, max_chars: int = MAX_TEXT_CHARS) -> str:
        """The page as the brain reads it: title, address, text, then the numbered controls."""
        found = await self.scan()
        text = str(found.get("text", ""))
        if len(text) > max_chars:
            text = text[:max_chars] + "\n[… more text further down: scroll to read it]"
        lines = [f"Page: {self.title or '(no title)'}", f"Address: {self.url}", "", text or "(no text)", ""]
        if self._items:
            lines.append("What you can click or type into (use the number):")
            lines += [describe(item) for item in self._items]
        else:
            lines.append("Nothing to click or type into was found (web_screenshot shows the page).")
        return "\n".join(lines)

    async def element(self, ref: int | None, text: str) -> dict[str, Any]:
        """A numbered element by number, or by its name (the page is read again to find it)."""
        if ref is not None:
            item = next((item for item in self._items if item.get("ref") == ref), None)
            if item is None:
                raise BrowserError(f"There's no [{ref}] on the page; read it again.")
            return item
        await self.scan()
        item = match_item(self._items, text)
        if item is None:
            raise BrowserError(f"Nothing called “{text}” can be clicked on the page; read it to see what can.")
        return item

    def take_declined(self) -> list[str]:
        """What the page asked that was answered Cancel since the last call (``dialog_check``), once."""
        declined, self.declined = self.declined, []
        return declined

    async def around(self, item: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """What ``item`` is (None: whatever has the keyboard focus), as ``_AROUND_JS`` tells; None when there's
        nothing there."""
        ref = "null" if item is None else str(int(item["ref"]))
        found = await self.evaluate(f"({_AROUND_JS})({ref})")
        return found if isinstance(found, dict) else None

    async def _center(self, item: Mapping[str, Any]) -> tuple[float, float]:
        point = await self.evaluate(f"({_CENTER_JS})({int(item['ref'])})")
        if not isinstance(point, dict):
            raise BrowserError(_NOT_THERE)
        await asyncio.sleep(0.1)  # scrolled into view: let it come to rest
        point = await self.evaluate(f"({_CENTER_JS})({int(item['ref'])})")
        if not isinstance(point, dict):
            raise BrowserError(_NOT_THERE)
        return float(point["x"]), float(point["y"])

    async def click(self, item: Mapping[str, Any]) -> None:
        """A real click in the page (what its scripts see from a mouse), never the user's cursor."""
        x, y = await self._center(item)
        await self.cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        for kind in ("mousePressed", "mouseReleased"):
            await self.cdp.send(
                "Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1}
            )
        await asyncio.sleep(self._settle_s)
        await self.wait_ready()

    async def type(self, item: Mapping[str, Any], text: str, *, replace: bool = True, enter: bool = False) -> str:
        """Type into a field or code editor (what was in it replaced, unless not ``replace``); a list's option is
        picked by its text. Returns what was done, in words."""
        if item.get("role") == "select":
            picked = await self.evaluate(f"({_SELECT_JS})({int(item['ref'])}, {json.dumps(text)})")
            if picked is None:
                raise BrowserError(f"The list {describe(item)} has no option “{text}”.")
            await asyncio.sleep(self._settle_s)
            return f"Picked “{picked}” in {describe(item)}."
        await self.click(item)
        if replace:
            await self.press("Ctrl+A")
        if text:
            await self.cdp.send("Input.insertText", {"text": text})
        if enter:
            await self.press("Enter")
        await asyncio.sleep(self._settle_s)
        return f"Typed into {describe(item)}" + (" and pressed Enter." if enter else ".")

    async def press(self, combo: str) -> None:
        modifiers, (key, code, vk, typed) = parse_keys(combo)
        typing = bool(typed) and not modifiers & 0b111  # Ctrl, Alt or Meta make it a shortcut, not text
        down: dict[str, Any] = {
            "type": "keyDown" if typing else "rawKeyDown",
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "modifiers": modifiers,
        }
        if typing:
            down["text"] = typed
        await self.cdp.send("Input.dispatchKeyEvent", down)
        up = {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk, "modifiers": modifiers}
        await self.cdp.send("Input.dispatchKeyEvent", up)
        await asyncio.sleep(self._settle_s / 3)

    async def scroll(self, down: bool, item: Mapping[str, Any] | None = None) -> None:
        """A turn of the mouse wheel over the page's middle (or over ``item``: a pane of its own scrolls)."""
        x, y = (VIEWPORT[0] / 2, VIEWPORT[1] / 2) if item is None else await self._center(item)
        step = VIEWPORT[1] * 0.8
        await self.cdp.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": step if down else -step},
        )
        await asyncio.sleep(self._settle_s)

    async def screenshot(self) -> str:
        """What the page looks like, as base64 JPEG."""
        reply = await self.cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": SCREENSHOT_QUALITY})
        data = reply.get("data")
        if not isinstance(data, str) or not data:
            raise BrowserError("The browser gave no picture of the page.")
        return data

    async def close(self) -> None:
        for task in list(self._side):
            task.cancel()
        await self.cdp.close()


# --------------------------------------------------------------------------- the browser

Launch = Callable[[list[str]], Any]  # starts the browser; returns something with poll() and terminate()
Connect = Callable[[str], Awaitable[Any]]  # opens a WebSocket


def _launch(args: list[str]) -> Any:
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def _connect(url: str) -> Any:
    from websockets.asyncio.client import connect

    return await connect(url, max_size=None, open_timeout=10, ping_interval=None)


class WebBrowser:
    """The browser web tasks share: started on demand, a tab per task, closed when no task needs it."""

    def __init__(
        self,
        profile: Path,
        *,
        executable: Callable[[], str | None] = find_browser,
        launch: Launch = _launch,
        connect: Connect = _connect,
        transport: httpx.AsyncBaseTransport | None = None,
        launch_timeout_s: float = LAUNCH_TIMEOUT_S,
        page_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.profile = profile
        self._executable = executable
        self._launch = launch
        self._connect = connect
        self._transport = transport
        self._launch_timeout_s = launch_timeout_s
        self._page_options = dict(page_options or {})
        self._lock = asyncio.Lock()
        self._port: int | None = None
        self._process: Any = None
        self._user_agent = ""
        self.headless = True
        self._pages: dict[int, WebPage] = {}

    @property
    def running(self) -> bool:
        return self._port is not None

    @property
    def available(self) -> bool:
        return self._executable() is not None

    def where(self, owner: int) -> str:
        """The page task ``owner`` is on, in words ("" before it opened one)."""
        page = self._pages.get(owner)
        if page is None or not (page.title or page.url):
            return ""
        return f"“{page.title}”" if page.title else page.url

    # ------------------------------------------------------------------ tabs

    async def page(self, owner: int) -> WebPage:
        """Task ``owner``'s tab, opened on first use (the browser started headless if it isn't running). A window
        left open for signing in is closed first: tasks work out of sight."""
        async with self._lock:
            page = self._pages.get(owner)
            if page is not None and not page.closed:
                return page
            await self._check()
            if self._port is not None and not self.headless and not self._pages:
                await self._shutdown()
            if self._port is None:
                await self._start(headless=True, url="about:blank")
            target = await self._new_target("about:blank")
            socket = await self._connect(str(target["webSocketDebuggerUrl"]))
            page = WebPage(Cdp(socket), str(target["id"]), **self._page_options)
            try:
                await page.prepare(self._user_agent)
            except BaseException:
                await page.close()
                raise
            self._pages[owner] = page
            return page

    async def close_page(self, owner: int) -> None:
        """Task ``owner`` is done: its tab closes, and a headless browser with no tab left with it."""
        async with self._lock:
            page = self._pages.pop(owner, None)
            if page is None:
                return
            await page.close()
            with contextlib.suppress(httpx.HTTPError):
                async with self._http() as client:
                    await client.get(f"/json/close/{page.target}")
            if not self._pages and self.headless:
                await self._shutdown()

    async def show(self, url: str) -> None:
        """The browser in a window the user sees, at ``url`` (to sign in once): a headless one is restarted with a
        window, which can't happen while a task works in it."""
        async with self._lock:
            await self._check()
            if self._port is not None and not self.headless:
                await self._new_target(url)
                return
            if self._pages:
                raise BrowserError("A web task is working in the browser right now; it can be shown once it's done.")
            if self._port is not None:
                await self._shutdown()
            await self._start(headless=False, url=url)

    async def close(self) -> None:
        async with self._lock:
            for page in list(self._pages.values()):
                with contextlib.suppress(Exception):
                    await page.close()
            self._pages.clear()
            await self._check()
            if self._port is not None:
                await self._shutdown()

    # ------------------------------------------------------------------ the process

    def _http(self, timeout: float = 10.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{self._port}", transport=self._transport, timeout=timeout)

    async def _version(self, port: int) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=3.0) as client:
                reply = await client.get(f"http://127.0.0.1:{port}/json/version")
                reply.raise_for_status()
                found = reply.json()
        except (httpx.HTTPError, ValueError):
            return None
        return found if isinstance(found, dict) else None

    def _port_in_file(self) -> int | None:
        try:
            return int((self.profile / PORT_FILE).read_text(encoding="utf-8").splitlines()[0])
        except (OSError, ValueError, IndexError):
            return None

    async def _check(self) -> None:
        """Whether the browser is (still) there: one left by an earlier run is taken over, one the user closed is
        forgotten."""
        port = self._port or self._port_in_file()
        version = await self._version(port) if port else None
        if version is None:
            self._port, self._process = None, None
            for page in self._pages.values():
                await page.close()
            self._pages.clear()
            return
        if self._port is None:
            log.info("Using the web task browser left running on port %s", port)
            self._user_agent = str(version.get("User-Agent", ""))
            self.headless = "Headless" in self._user_agent
        self._port = port

    async def _start(self, *, headless: bool, url: str) -> None:
        executable = self._executable()
        if executable is None:
            raise BrowserError("Web tasks need Chrome or Edge, and neither is installed.")
        self.profile.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            (self.profile / PORT_FILE).unlink()
        args = [
            executable,
            "--remote-debugging-port=0",  # any free port: the browser writes it in PORT_FILE
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}",
        ]
        if headless:
            args += ["--headless=new", "--mute-audio"]
        args.append(url)
        log.info("Starting the web task browser (%s)", "headless" if headless else "in a window")
        self._process = self._launch(args)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._launch_timeout_s
        while loop.time() < deadline:
            port = self._port_in_file()
            if port and (version := await self._version(port)) is not None:
                self._port, self.headless = port, headless
                self._user_agent = str(version.get("User-Agent", ""))
                return
            if self._process is not None and self._process.poll() is not None:
                break
            await asyncio.sleep(0.2)
        self._stop_process()
        raise BrowserError(
            "The web task browser didn't start. If a window of it is still open from before, close it and try again."
        )

    async def _new_target(self, url: str) -> dict[str, Any]:
        try:
            async with self._http() as client:
                reply = await client.put(f"/json/new?{quote(url, safe=':/?&=#%')}")
                reply.raise_for_status()
                target = reply.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BrowserError(f"The browser couldn't open a tab: {exc}") from exc
        if not isinstance(target, dict) or "webSocketDebuggerUrl" not in target:
            raise BrowserError("The browser couldn't open a tab.")
        return target

    async def _shutdown(self) -> None:
        """Ask the browser to close (it saves its profile, logins included); stop the process if it won't."""
        version = await self._version(self._port) if self._port else None
        url = (version or {}).get("webSocketDebuggerUrl")
        if url:
            try:
                cdp = Cdp(await self._connect(str(url)))
                with contextlib.suppress(BrowserError):
                    await cdp.send("Browser.close", timeout=5.0)
                await cdp.close()
            except Exception:
                log.debug("Couldn't ask the web task browser to close", exc_info=True)
        port, loop = self._port, asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while port and loop.time() < deadline and await self._version(port) is not None:
            await asyncio.sleep(0.2)
        self._stop_process()
        self._port = None
        log.info("Closed the web task browser")

    def _stop_process(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            with contextlib.suppress(Exception):
                process.terminate()
