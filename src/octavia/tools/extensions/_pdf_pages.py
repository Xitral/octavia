"""One page of a PDF as a picture for the model to look at: lecture slides that are images, and diagrams (a game
tree, a truth table) whose text alone doesn't make sense.

Windows renders it (``scripts/pdf_page.ps1``, Windows.Data.Pdf through the 64-bit Windows PowerShell), so nothing
needs installing. The PDF goes to a temporary file of its own and the picture comes back the same way; both are
deleted afterwards. The picture is sent as the same JPEG, at the same sizes, as a look at the screen.
"""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from octavia.tools.extensions.screen_capture import attachment, prepare_image

RENDER_WIDTH = 1600
TIMEOUT_S = 45.0
SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "pdf_page.ps1"


class PageError(RuntimeError):
    """The page couldn't be made into a picture; the message says why, in plain words."""


@dataclass(frozen=True)
class PagePicture:
    page: int
    pages: int  # in the whole PDF
    image: dict[str, str]  # a tool result's ``data["image"]``
    width: int
    height: int


def powershell() -> Path:
    """64-bit Windows PowerShell (Sysnative from a 32-bit process), which has the Windows Runtime's PDF renderer."""
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    native = root / "Sysnative" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return native if native.exists() else root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _run(pdf: Path, page: int, out: Path) -> tuple[int, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    done = subprocess.run(
        [str(powershell()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
         "-Pdf", str(pdf), "-Page", str(page), "-Out", str(out), "-Width", str(RENDER_WIDTH)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        creationflags=flags,
    )  # fmt: skip
    return done.returncode, done.stdout


def pages_in(stdout: str) -> int:
    for line in stdout.splitlines():
        if line.startswith("pages="):
            try:
                return int(line.removeprefix("pages="))
            except ValueError:
                return 0
    return 0


def render(data: bytes, page: int) -> PagePicture:
    """Page ``page`` (from 1) of the PDF in ``data``. Raises PageError."""
    if sys.platform != "win32":
        raise PageError("Pages can only be turned into pictures on Windows.")
    if not data.lstrip()[:5].startswith(b"%PDF-"):
        raise PageError("It isn't a PDF, so it has no pages to look at.")
    with tempfile.TemporaryDirectory(prefix="octavia-pdf-", ignore_cleanup_errors=True) as folder:
        pdf, out = Path(folder) / "document.pdf", Path(folder) / "page.png"
        pdf.write_bytes(data)
        try:
            code, stdout = _run(pdf, page, out)
        except subprocess.TimeoutExpired as exc:
            raise PageError("Windows took too long to draw that page.") from exc
        except OSError as exc:
            raise PageError(f"Windows' PDF renderer couldn't be started ({exc.strerror or exc}).") from exc
        pages = pages_in(stdout)
        if code == 3 or (pages and not 1 <= page <= pages):
            raise PageError(f"It has {pages} page{'s' if pages != 1 else ''}, so there's no page {page}.")
        if code != 0 or not out.exists():
            raise PageError("Windows couldn't draw that page (the PDF may be protected or damaged).")
        with Image.open(io.BytesIO(out.read_bytes())) as picture:
            pixels = np.asarray(picture.convert("RGB"))
    encoded, width, height = prepare_image(pixels)
    return PagePicture(page=page, pages=pages, image=attachment(encoded), width=width, height=height)


async def render_page(data: bytes, page: int) -> PagePicture:
    return await asyncio.to_thread(render, data, page)
