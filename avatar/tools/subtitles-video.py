"""Put tools/subtitles-clip.mjs captures together into a video: the orb and her subtitles at their real places on a
ground that's half a white page and half a dark desktop, so both backings show. Two or more capture folders are
stacked, each labelled (before, after).

    python tools/subtitles-video.py <out.mp4> <label>=<dir> [<label>=<dir> ...] [--audio=<wav>]

--audio puts the recording a scripted clip was made from (subtitles-clip.mjs --script) under the picture.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BOX = (1060, 470, 1840, 990)  # the part of the screen shown (both windows sit inside it)
PAGE_RIGHT = 1330  # the white page covers the ground left of this
FPS = 15


def ground() -> Image.Image:
    w, h = BOX[2] - BOX[0], BOX[3] - BOX[1]
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (14 + y // 40, 18 + y // 30, 30 + y // 22)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, PAGE_RIGHT - BOX[0], h), fill=(250, 250, 248))
    for i, y in enumerate(range(40, h - 30, 26)):  # a page of text
        draw.rectangle((26, y, PAGE_RIGHT - BOX[0] - 30 - (i * 37) % 90, y + 7), fill=(196, 199, 205))
    return img


def frames(folder: Path) -> list[dict]:
    return json.loads((folder / "frames.json").read_text())


def compose(base: Image.Image, folder: Path, frame: dict, her: dict) -> Image.Image:
    img = base.copy().convert("RGBA")
    orb = folder / f"o-{frame['i']:04d}.png"
    if orb.exists():
        img.alpha_composite(Image.open(orb).convert("RGBA"), (her["x"] - BOX[0], her["y"] - BOX[1]))
    subs = folder / f"s-{frame['i']:04d}.png"
    if subs.exists() and frame["subs"]:
        b = frame["subs"]
        img.alpha_composite(Image.open(subs).convert("RGBA"), (b["x"] - BOX[0], b["y"] - BOX[1]))
    return img.convert("RGB")


def main() -> None:
    out = Path(sys.argv[1])
    audio = next((arg.split("=", 1)[1] for arg in sys.argv[2:] if arg.startswith("--audio=")), None)
    runs = [(arg.split("=", 1)[0], Path(arg.split("=", 1)[1])) for arg in sys.argv[2:] if not arg.startswith("--")]
    base = ground()
    font = ImageFont.truetype("segoeui.ttf", 22)
    lists = [(label, folder, frames(folder)) for label, folder in runs]
    count = min(len(f) for _, _, f in lists)
    w, h = base.size
    with tempfile.TemporaryDirectory() as tmp:
        for n in range(count):
            sheet = Image.new("RGB", (w, h * len(lists)))
            for k, (label, folder, fl) in enumerate(lists):
                her = next((f["her"] for f in fl if f["her"]), {"x": 1500, "y": 640})
                tile = compose(base, folder, fl[n], her)
                ImageDraw.Draw(tile).text((w - 16, 12), label, font=font, fill=(150, 170, 200), anchor="ra")
                sheet.paste(tile, (0, h * k))
            sheet.save(Path(tmp) / f"{n:04d}.png")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(Path(tmp) / "%04d.png"),
             *(["-i", audio, "-c:a", "aac", "-shortest"] if audio else []),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16", str(out)],
            check=True,
        )
    print(out)


if __name__ == "__main__":
    main()
