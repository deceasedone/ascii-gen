from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ASCII = 0
BLOCK = 1
BRAILLE = 2
SHADE = 3
EXTRA = 4

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/CascadiaMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]

_UL, _UR = (0, 0, 0.5, 0.5), (0.5, 0, 1, 0.5)
_LL, _LR = (0, 0.5, 0.5, 1), (0.5, 0.5, 1, 1)
BLOCK_SHAPES = {
    "█": [(0, 0, 1, 1)],
    "▀": [(0, 0, 1, 0.5)],
    "▄": [(0, 0.5, 1, 1)],
    "▌": [(0, 0, 0.5, 1)],
    "▐": [(0.5, 0, 1, 1)],
    "▂": [(0, 0.75, 1, 1)],
    "▆": [(0, 0.25, 1, 1)],
    "▎": [(0, 0, 0.25, 1)],
    "▊": [(0, 0, 0.75, 1)],
    "▘": [_UL],
    "▝": [_UR],
    "▖": [_LL],
    "▗": [_LR],
    "▚": [_UL, _LR],
    "▞": [_UR, _LL],
    "▛": [_UL, _UR, _LL],
    "▜": [_UL, _UR, _LR],
    "▙": [_UL, _LL, _LR],
    "▟": [_UR, _LL, _LR],
}

SHADES = {
    "░": lambda x, y: (x % 2 == 0) & (y % 2 == 0),
    "▒": lambda x, y: (x + y) % 2 == 0,
    "▓": lambda x, y: ~((x % 2 == 1) & (y % 2 == 1)),
}

BRAILLE_DOTS = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (0, 3), (1, 3)]

CLASSIC_RAMP = " .:-=+*#%@"
RAMPS = {
    "simple": CLASSIC_RAMP,
    "detailed": " .'`^\",:;Il!i><~+_-?][}{1)(|/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$",
    "blocks": " ░▒▓█",
    "binary": " 01",
}

SUPERSAMPLE = 4


def find_font_path() -> str | None:
    env = os.environ.get("ASCII_FONT")
    if env and os.path.exists(env):
        return env
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


@dataclass
class GlyphAtlas:
    chars: list[str]
    kinds: np.ndarray
    masks: np.ndarray
    cell_w: int
    cell_h: int

    def features(self, sx: int, sy: int) -> np.ndarray:
        g, h, w = self.masks.shape
        out = np.empty((g, sy * sx), np.float32)
        for i, m in enumerate(self.masks):
            img = Image.fromarray((m * 255).astype(np.uint8))
            out[i] = np.asarray(img.resize((sx, sy), Image.BOX), np.float32).ravel() / 255
        return out

    def index_of(self, chars: str) -> np.ndarray:
        lookup = {c: i for i, c in enumerate(self.chars)}
        return np.array([lookup[c] for c in chars], dtype=np.int64)


def _supersampled(cell_w: int, cell_h: int, draw_fn) -> np.ndarray:
    s = SUPERSAMPLE
    img = Image.new("L", (cell_w * s, cell_h * s), 0)
    draw_fn(ImageDraw.Draw(img), cell_w * s, cell_h * s)
    arr = np.asarray(img, np.float32) / 255
    return arr.reshape(cell_h, s, cell_w, s).mean(axis=(1, 3))


def _block_mask(rects, cell_w, cell_h):
    def draw(d, w, h):
        for x0, y0, x1, y1 in rects:
            d.rectangle([round(x0 * w), round(y0 * h), round(x1 * w) - 1, round(y1 * h) - 1], fill=255)

    return _supersampled(cell_w, cell_h, draw)


def _braille_mask(bits, cell_w, cell_h):
    def draw(d, w, h):
        r = 0.42 * min(w / 2, h / 4)
        for bit, (col, row) in enumerate(BRAILLE_DOTS):
            if bits & (1 << bit):
                cx, cy = (col + 0.5) * w / 2, (row + 0.5) * h / 4
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)

    return _supersampled(cell_w, cell_h, draw)


def _shade_mask(pattern, cell_w, cell_h):
    y, x = np.mgrid[0:cell_h, 0:cell_w]
    return pattern(x, y).astype(np.float32)


@lru_cache(maxsize=32)
def build_atlas(font_size: int = 12, extra: str = "") -> GlyphAtlas:
    path = find_font_path()
    font = ImageFont.truetype(path, font_size) if path else ImageFont.load_default(font_size)
    ascent, descent = font.getmetrics()
    cell_w = max(2, round(font.getlength("M")))
    cell_h = max(4, ascent + descent)

    chars, kinds, masks = [], [], []

    def add_text(c: str, kind: int) -> None:
        img = Image.new("L", (cell_w, cell_h), 0)
        ImageDraw.Draw(img).text((0, 0), c, font=font, fill=255)
        chars.append(c)
        kinds.append(kind)
        masks.append(np.asarray(img, np.float32) / 255)

    for code in range(32, 127):
        add_text(chr(code), ASCII)

    for c, pattern in SHADES.items():
        chars.append(c)
        kinds.append(SHADE)
        masks.append(_shade_mask(pattern, cell_w, cell_h))

    for c, rects in BLOCK_SHAPES.items():
        chars.append(c)
        kinds.append(BLOCK)
        masks.append(_block_mask(rects, cell_w, cell_h))

    for bits in range(1, 256):
        chars.append(chr(0x2800 + bits))
        kinds.append(BRAILLE)
        masks.append(_braille_mask(bits, cell_w, cell_h))

    known = set(chars)
    for c in dict.fromkeys(extra):
        if c not in known and c.isprintable():
            add_text(c, EXTRA)

    return GlyphAtlas(chars, np.array(kinds), np.stack(masks).astype(np.float32), cell_w, cell_h)
