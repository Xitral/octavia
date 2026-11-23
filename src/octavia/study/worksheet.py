"""Worksheets: a practice problem to work on the page, the way an exam is done on paper. The drawing comes with boxes
on it to write in (each node's value, α and β, each state's expansion number, the path) and branches to click to cross
them out; "Check my work" grades every box by code (the kind's ``mark``) and shows what's right, what's wrong and what
belongs there, with the first step that went wrong. It prints too: blank, for paper practice, or with the answers.

It's a page in the user's own browser, from a small server on this PC only (127.0.0.1), started on first use and
running while she does. Every address carries a random token made at start, so no other page or program can read a
problem or send a check without it. Nothing here knows any one kind of problem: a kind with a ``worksheet`` gets one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
from collections.abc import Callable
from html import escape
from typing import Any

from octavia.study.practice import Marked, PracticeKind, Problem, ProblemBook, get_kind

log = logging.getLogger(__name__)

MAX_BODY = 64_000
MAX_HEADERS = 60
MAX_CONNECTIONS = 8
Graded = Callable[[Problem, PracticeKind, Marked], None]


class WorksheetServer:
    """The worksheet pages, served on 127.0.0.1. Call on the event loop."""

    def __init__(self, book: ProblemBook, on_graded: Graded | None = None) -> None:
        self.book = book
        self.on_graded = on_graded
        self.token = secrets.token_urlsafe(18)
        self._server: asyncio.base_events.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._slots: asyncio.Semaphore | None = None
        self.port = 0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        if self._server is not None and self._loop is loop:
            return
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self._loop, self._slots = loop, asyncio.Semaphore(MAX_CONNECTIONS)
        self.port = self._server.sockets[0].getsockname()[1]

    def url(self, problem: int) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}/sheet/{problem}"

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    # ------------------------------------------------------------------------------------------------ http

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        slots = self._slots
        if slots is None or slots.locked():  # a few at a time: it's one person's page
            writer.close()
            return
        async with slots:
            await self._serve_one(reader, writer)

    async def _serve_one(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, kind, body = await asyncio.wait_for(self._answer(reader), 10)
        except Exception:
            log.debug("A worksheet request failed", exc_info=True)
            status, kind, body = 400, "text/plain; charset=utf-8", b"Bad request"
        head = (
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\nContent-Type: {kind}\r\n"
            f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
            "Referrer-Policy: no-referrer\r\nContent-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src data:; connect-src 'self'; form-action 'none'; base-uri 'none'; "
            "frame-ancestors 'none'\r\nConnection: close\r\n\r\n"
        )
        with contextlib.suppress(Exception):
            writer.write(head.encode("ascii") + body)
            await writer.drain()
            writer.close()

    async def _answer(self, reader: asyncio.StreamReader) -> tuple[int, str, bytes]:
        request = (await reader.readline()).decode("latin-1").split()
        headers: dict[str, str] = {}
        for _ in range(MAX_HEADERS + 1):
            line = (await reader.readline()).decode("latin-1").strip()
            if not line:
                break
            name, _, value = line.partition(":")
            headers[name.strip().casefold()] = value.strip()
        else:
            return 400, "text/plain; charset=utf-8", b"Bad request"
        if len(request) < 2:
            return 400, "text/plain; charset=utf-8", b"Bad request"
        # Only this PC's own address (a web page that renamed its site to 127.0.0.1 is refused), and only from it.
        mine = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if headers.get("host", "").casefold() not in mine:
            return 403, "text/plain; charset=utf-8", b"Forbidden"
        origin = headers.get("origin")
        if origin is not None and origin.casefold() not in {f"http://{m}" for m in mine}:
            return 403, "text/plain; charset=utf-8", b"Forbidden"
        method, path = request[0], request[1]
        found = re.fullmatch(r"/([\w-]+)/sheet/(\d+)(/mark)?", path)
        if found is None or not secrets.compare_digest(found.group(1), self.token):
            return 404, "text/plain; charset=utf-8", b"Not found"
        ident, marking = int(found.group(2)), bool(found.group(3))
        problem = self.book.get(ident)
        kind = get_kind(problem.kind) if problem is not None else None
        if problem is None or kind is None or kind.worksheet(problem.data) is None:
            return 404, "text/plain; charset=utf-8", b"No such worksheet"
        if method == "GET" and not marking:
            return 200, "text/html; charset=utf-8", page(problem, kind, f"/{self.token}/sheet/{ident}/mark").encode()
        if method == "POST" and marking:
            length = int(headers.get("content-length", "0") or 0)
            essence = headers.get("content-type", "").split(";")[0].strip().casefold()
            if not 0 < length <= MAX_BODY or essence != "application/json":  # a cross-site form can't send this
                return 400, "text/plain; charset=utf-8", b"Bad request"
            filled = json.loads(await reader.readexactly(length))
            if not isinstance(filled, dict):
                return 400, "text/plain; charset=utf-8", b"Bad request"
            marked = kind.mark(problem.data, {str(k)[:60]: str(v)[:600] for k, v in list(filled.items())[:500]})
            if self.on_graded is not None:
                try:
                    self.on_graded(problem, kind, marked)
                except Exception:
                    log.warning("Couldn't pass on a graded worksheet", exc_info=True)
            out = {
                "right": marked.grade.right,
                "lines": marked.grade.lines,
                "marks": marked.marks,
                "expected": marked.expected,
            }
            return 200, "application/json", json.dumps(out).encode()
        return 405, "text/plain; charset=utf-8", b"Not allowed"


# ---------------------------------------------------------------------------------------------------- the page

STYLE = """
body{margin:0;font-family:Segoe UI,sans-serif;background:#f4f5f7;color:#111}
main{max-width:1200px;margin:0 auto;padding:18px}
h1{font-size:20px;margin:0 0 4px} p.how{margin:4px 0 14px;color:#333;font-size:14px}
.sheet{position:relative;background:#fff;border:1px solid #d5d8de;border-radius:8px;width:100%}
.sheet svg{display:block;width:100%;height:auto}
.blank{position:absolute;transform:translate(-50%,-50%);font:600 14px Segoe UI,sans-serif;text-align:center;
 min-width:34px;box-sizing:border-box;
 border:1.5px solid #9aa3b2;border-radius:5px;background:rgba(255,255,255,.92);padding:2px 0;color:#0d47a1}
.blank::placeholder{color:#b8bec9;font-weight:400}
.cut{position:absolute;transform:translate(-50%,-50%);width:22px;height:22px;border-radius:50%;
 border:1.5px dashed #b8bec9;background:rgba(255,255,255,.85);cursor:pointer;color:#c62828;font:700 15px sans-serif;
 line-height:18px;padding:0}
.cut.on{border:1.5px solid #c62828;background:#fff}
.right{border-color:#2e7d32!important;background:#e8f5e9!important}
.wrong,.extra{border-color:#c62828!important;background:#ffebee!important}
.missed{border-color:#ef6c00!important;background:#fff3e0!important}
.want{position:absolute;transform:translate(-50%,0);font:600 11px Segoe UI,sans-serif;color:#c62828;
 white-space:nowrap;pointer-events:none}
.bar{display:flex;gap:8px;margin:12px 0;flex-wrap:wrap}
button.act{font:600 14px Segoe UI,sans-serif;padding:8px 14px;border-radius:6px;border:1px solid #9aa3b2;
 background:#fff;cursor:pointer} button.go{background:#1565c0;color:#fff;border-color:#1565c0}
#result{white-space:pre-wrap;font-size:15px;line-height:1.45;padding:10px 12px;border-radius:6px;display:none}
#result.ok{display:block;background:#e8f5e9} #result.no{display:block;background:#fff3e0}
#result.err{display:block;background:#ffebee}
.key{font-size:12px;color:#555}
@media print{body{background:#fff}.bar,.key,#result{display:none!important}.sheet{border:none}
 body.blank-print .blank{color:transparent}
 body.blank-print .blank,body.blank-print .cut{border-color:#9aa3b2!important;background:#fff!important}
 body.blank-print .cut{color:transparent;border-style:dashed!important}
 .want{display:none}}
@page{size:landscape;margin:10mm}
"""

SCRIPT = """
const BLANKS = %s, MARK_URL = %s, W = %s, H = %s;
const sheet = document.getElementById('sheet');
for (const b of BLANKS) {
  let el;
  if (b.kind === 'cut') {
    el = document.createElement('button'); el.className = 'cut'; el.type = 'button'; el.title = 'Cross out this branch';
    el.onclick = () => { el.classList.toggle('on'); el.textContent = el.classList.contains('on') ? '✕' : ''; };
  } else {
    el = document.createElement('input'); el.className = 'blank'; el.placeholder = b.label || '';
    el.style.width = (b.width / W * 100) + '%%'; el.autocomplete = 'off'; el.spellcheck = false;
    if (b.kind === 'number') el.inputMode = 'text';
  }
  el.dataset.id = b.id; el.style.left = (b.x / W * 100) + '%%'; el.style.top = (b.y / H * 100) + '%%';
  sheet.appendChild(el);
}
function values() {
  const out = {};
  for (const el of sheet.querySelectorAll('[data-id]'))
    out[el.dataset.id] = el.classList.contains('cut') ? (el.classList.contains('on') ? 'x' : '') : el.value;
  return out;
}
function clearMarks() {
  for (const el of sheet.querySelectorAll('[data-id]')) el.classList.remove('right', 'wrong', 'missed', 'extra');
  for (const el of sheet.querySelectorAll('.want')) el.remove();
  const r = document.getElementById('result'); r.className = ''; r.textContent = '';
}
let checking = false;
async function check() {
  if (checking) return;
  checking = true;
  clearMarks();
  const r = document.getElementById('result');
  let res;
  try {
    const reply = await fetch(MARK_URL, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify(values())});
    if (!reply.ok) throw new Error(reply.status);
    res = await reply.json();
  } catch (e) {
    r.textContent = "This sheet couldn't be checked: Octavia may have restarted since it opened. Ask her to open " +
      "the worksheet again (what you wrote here stays on this page).";
    r.className = 'err'; checking = false; return;
  } finally { checking = false; }
  for (const el of sheet.querySelectorAll('[data-id]')) {
    const mark = res.marks[el.dataset.id]; if (!mark) continue;
    el.classList.add(mark);
    const want = res.expected[el.dataset.id];
    if (want !== undefined && mark !== 'right') {
      const tip = document.createElement('div'); tip.className = 'want';
      tip.textContent = want === '' ? 'leave empty' : (want === 'x' ? 'cut' : want);
      tip.style.left = el.style.left;
      const up = el.dataset.id.startsWith('a:');  // under an α box is the β box: put it above
      tip.style.top = 'calc(' + el.style.top + (up ? ' - ' + (el.offsetHeight / 2 + 16) : ' + ' + (el.offsetHeight / 2 + 2)) + 'px)';
      sheet.appendChild(tip);
    }
  }
  r.textContent = res.lines.join('\\n'); r.className = res.right ? 'ok' : 'no';
}
function startOver() {
  clearMarks();
  for (const el of sheet.querySelectorAll('[data-id]')) {
    if (el.classList.contains('cut')) { el.classList.remove('on'); el.textContent = ''; } else el.value = '';
  }
}
function printBlank() { document.body.classList.add('blank-print'); window.print(); }
window.addEventListener('afterprint', () => document.body.classList.remove('blank-print'));
"""


def _script_json(value: Any) -> str:
    """JSON safe inside a <script> element."""
    return json.dumps(value).replace("</", "<\\/").replace("<!--", "<\\!--")


def page(problem: Problem, kind: PracticeKind, mark_url: str) -> str:
    sheet = kind.worksheet(problem.data)
    assert sheet is not None
    blanks = [
        {"id": b.id, "x": b.x, "y": b.y, "kind": b.kind, "label": b.label, "width": b.width} for b in sheet.blanks
    ]
    title = escape(kind.title(problem))
    script = SCRIPT % (_script_json(blanks), _script_json(mark_url), _script_json(sheet.width),
                       _script_json(sheet.height))  # fmt: skip
    return (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{title}</title>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'><style>{STYLE}</style></head>"
        f"<body><main><h1>{title}</h1><p class='how'>{escape(sheet.instructions)}</p>"
        f"<div class='sheet' id='sheet'>{sheet.svg}</div>"
        "<div class='bar'><button class='act go' type='button' onclick='check()'>Check my work</button>"
        "<button class='act' type='button' onclick='startOver()'>Start over</button>"
        "<button class='act' type='button' onclick='printBlank()'>Print blank</button>"
        "<button class='act' type='button' onclick='window.print()'>Print with my answers</button></div>"
        "<div id='result'></div><p class='key'>Green: right. Red: wrong (what belongs there is under it). "
        "Orange: left out.</p>"
        f"<script>{script}</script></main></body></html>"
    )
