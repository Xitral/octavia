"""The orb crossing the screen, as the user sees it: her window at home, the streak across the screen (drawn by the
trail windows), and her window at the far end, laid out on one stage from `drive.mjs orb --hop 1 --fps 60` frames.
Each window's square is outlined faintly, so where she's cut off by it shows.

    python tools/orb-hop.py <frames dir> <out dir> [--zip-out 0.22] [--colors new|old] [--label text]

Writes <out dir>/h0000.png... at 60 fps. Timing as lib/choreography.js has it: the streak sets off once she has
zipped out, and her new window shows her once the streak's head is `lead` of the way along its time.
"""

import argparse
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

FPS = 60
SIZE = 320  # her window
STAGE = (1400, 480)
A = (60, 80)  # her window at home (top left)
B = (1020, 80)  # ...and where she's going
GROUND = (16, 20, 24)
LEAD = 0.55
OLD = ['#0b2f8a', '#1e6bff', '#7fd4ff', '#7fd4ff', '#1e6bff', '#0b2f8a']
NEW = ['#17305f', '#3f6fc4', '#a9cdee', '#a9cdee', '#3f6fc4', '#17305f']


def rgb(hex_):
    n = int(hex_[1:], 16)
    return np.array([n >> 16, (n >> 8) & 255, n & 255], np.float32)


def smoothstep(t):
    x = np.clip(t, 0, 1)
    return x * x * (3 - 2 * x)


def ease_out(t):
    return 1 - (1 - min(1.0, max(0.0, t))) ** 3


def streak(img, frm, to, elapsed, duration, life, thickness, colors):
    """trail.js's paint(), per pixel: six stripes tapering to the tail, fading in from it, and a white head."""
    head = ease_out(elapsed / duration)
    tail = float(smoothstep((elapsed - 0.1 * duration) / (life - 0.1 * duration)))
    alpha = 1 - float(smoothstep((elapsed - duration) / (life - duration)))
    if head - tail <= 0.001 or alpha <= 0:
        return
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    length = math.hypot(dx, dy)
    u = ((xx - frm[0]) * dx + (yy - frm[1]) * dy) / (length * length)
    v = ((xx - frm[0]) * -dy + (yy - frm[1]) * dx) / length
    along = (u - tail) / max(1e-6, head - tail)
    inside = (along >= 0) & (along <= 1)
    half = thickness / 2 * (0.3 + 0.7 * np.clip(along, 0, 1))
    k = np.clip(np.floor((v / np.maximum(half, 1e-3) + 1) / 2 * 6), 0, 5).astype(int)
    inside &= np.abs(v) < half
    a = np.interp(np.clip(along, 0, 1), [0, 0.6, 1], [0, 0.55, 0.95]) * alpha * inside
    cols = np.stack([rgb(c) for c in colors])[k]
    img[:] = img * (1 - a[..., None]) + cols * a[..., None]
    hx, hy = frm[0] + dx * head, frm[1] + dy * head
    d = np.hypot(xx - hx, yy - hy) / (thickness * 0.8)
    g = np.interp(d, [0, 0.45, 1], [0.95, 0.5, 0]) * alpha * (d < 1)
    img[:] = img * (1 - g[..., None]) + 255 * g[..., None]


def over(img, frame, at):
    """A capture of her window (straight alpha, as capturePage saves it) over the stage."""
    f = np.asarray(frame.convert('RGBA'), np.float32)
    a = f[..., 3:4] / 255
    x, y = at
    region = img[y : y + f.shape[0], x : x + f.shape[1]]
    region[:] = f[..., :3] * a + region * (1 - a)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('frames')
    ap.add_argument('out')
    ap.add_argument('--zip-out', type=float, default=0.22)
    ap.add_argument('--colors', default='new')
    args = ap.parse_args()
    frames = sorted(Path(args.frames).glob('f*.png'))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    first_out, first_in = 6, 30  # drive.mjs --hop: 0.1 s at rest, then 0.4 s of zip-out, then the zip-in
    ca = (A[0] + SIZE / 2, A[1] + SIZE / 2)
    cb = (B[0] + SIZE / 2, B[1] + SIZE / 2)
    dist = math.hypot(cb[0] - ca[0], cb[1] - ca[1])
    duration = min(420, 220 + dist * 0.12)
    life = duration * 1.4
    thickness = max(12, round(SIZE * 0.06))
    start = first_out + round(args.zip_out * FPS)  # the streak sets off as she's zipped out
    arrive = start + round(duration * LEAD / 1000 * FPS)  # her new window shows her
    total = arrive + (len(frames) - first_in) + 20
    colors = NEW if args.colors == 'new' else OLD
    for n in range(total):
        img = np.empty((STAGE[1], STAGE[0], 3), np.float32)
        img[:] = GROUND
        elapsed = (n - start) * 1000 / FPS
        if elapsed >= 0:
            streak(img, ca, cb, elapsed, duration, life, thickness, colors)
        if n < first_in:
            over(img, Image.open(frames[n]), A)
        if n >= arrive:
            over(img, Image.open(frames[min(len(frames) - 1, first_in + n - arrive)]), B)
        im = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
        draw = ImageDraw.Draw(im)
        for x, y in (A, B):  # the windows she lives in, invisible on the real desktop
            draw.rectangle([x, y, x + SIZE - 1, y + SIZE - 1], outline=(46, 54, 64))
        im.save(out / f'h{n:04d}.png')
    print(f'{total} frames in {out}')


if __name__ == '__main__':
    main()
