from __future__ import annotations

import html
from dataclasses import dataclass, replace

import numpy as np
from PIL import Image

from .glyphs import ASCII, BLOCK, BRAILLE, BRAILLE_DOTS, CLASSIC_RAMP, RAMPS, build_atlas
from .themes import THEMES, gradient, hex_rgb, hue_mask, nearest, saturate

SUB_X, SUB_Y = 6, 12
CHUNK = 8192
MAX_COLUMNS = 500
LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)
CHAT_ASPECT = 0.45

MODES = ("ascii", "hybrid", "braille", "ramp", "text")
BACKGROUNDS = ("black", "tinted")
FOCUS = ("off", "dim", "hide")
BAYER = (np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], np.float32) + 0.5) / 16

_v = np.arange(256, dtype=np.float64) / 255
SRGB_TO_LINEAR = np.where(_v <= 0.04045, _v / 12.92, ((_v + 0.055) / 1.055) ** 2.4).astype(np.float32)


def to_srgb(linear: np.ndarray) -> np.ndarray:
    linear = np.clip(linear, 0, 1)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055)


def linear_resize(image: Image.Image, size: tuple[int, int]) -> np.ndarray:
    arr = np.asarray(image)
    return np.stack(
        [np.asarray(Image.fromarray(SRGB_TO_LINEAR[arr[..., c]]).resize(size, Image.BOX)) for c in range(3)], -1
    )


def hex_codes(rgb: np.ndarray) -> np.ndarray:
    c = np.clip(np.rint(rgb), 0, 255).astype(np.int64)
    return (c[..., 0] << 16) | (c[..., 1] << 8) | c[..., 2]


def atkinson(img: np.ndarray) -> np.ndarray:
    h, w = img.shape
    buf = np.zeros((h + 2, w + 3), np.float32)
    buf[:h, 1 : w + 1] = img
    out = np.zeros((h, w), bool)
    for y in range(h):
        row, nxt, nxt2 = buf[y], buf[y + 1], buf[y + 2]
        for x in range(1, w + 1):
            old = row[x]
            on = old > 0.5
            err = (old - on) / 8
            out[y, x - 1] = on
            row[x + 1] += err
            row[x + 2] += err
            nxt[x - 1] += err
            nxt[x] += err
            nxt[x + 1] += err
            nxt2[x] += err
    return out


def smoothstep(lo: float, hi: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - lo) / (hi - lo), 0, 1)
    return t * t * (3 - 2 * t)


@dataclass
class Frame:
    rows: int
    cols: int
    cells: np.ndarray
    patches: np.ndarray
    level: np.ndarray
    idx: np.ndarray
    mask: np.ndarray | None


@dataclass
class Settings:
    mode: str = "ascii"
    columns: int = MAX_COLUMNS
    font_size: int = 12
    background: str = "black"
    bg_strength: float = 0.25
    color_boost: float = 2.0
    detail: float = 0.6
    contrast: bool = True
    ramp: str = "simple"
    custom_chars: str = ""
    text: str = "ASCII "
    theme: str = "original"
    saturation: float = 1.0
    focus: str = "off"

    def validate(self) -> "Settings":
        if self.mode == "classic":
            self.mode, self.ramp = "ramp", "simple"
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if self.background not in BACKGROUNDS:
            raise ValueError(f"background must be one of {BACKGROUNDS}")
        if self.theme not in THEMES:
            raise ValueError(f"theme must be one of {tuple(THEMES)}")
        if self.focus not in FOCUS:
            raise ValueError(f"focus must be one of {FOCUS}")
        if self.ramp not in (*RAMPS, "custom"):
            raise ValueError(f"ramp must be one of {(*RAMPS, 'custom')}")
        self.custom_chars = "".join(dict.fromkeys(c for c in self.custom_chars if c.isprintable()))[:64]
        if self.ramp == "custom" and len(self.custom_chars) < 2:
            self.ramp = "simple"
        self.text = "".join(c for c in self.text.replace("\n", " ") if c.isprintable())[:200] or "ASCII "
        self.columns = int(np.clip(self.columns, 20, MAX_COLUMNS))
        self.font_size = int(np.clip(self.font_size, 6, 32))
        self.bg_strength = float(np.clip(self.bg_strength, 0, 1))
        self.color_boost = float(np.clip(self.color_boost, 0.5, 3))
        self.saturation = float(np.clip(self.saturation, 0, 2))
        self.detail = float(np.clip(self.detail, 0, 1))
        return self


class AsciiConverter:
    FLAT_THRESHOLD = (0.18, 0.04)
    RAMP_GAMMA = 0.75
    BLOCK_PENALTY = (0.08, 0.02)
    HYSTERESIS = 0.006
    HIDE_CUTOFF = (0.3, 0.6)
    DIM_FLOOR = 0.18

    def __init__(
        self,
        settings: Settings | None = None,
        stable: bool = False,
        cell_aspect: float | None = None,
        diffuse: bool = False,
    ):
        self.s = s = (settings or Settings()).validate()
        self.diffuse = diffuse
        self.theme = THEMES[s.theme]
        extra = s.text if s.mode == "text" else s.custom_chars if s.ramp == "custom" else ""
        self.atlas = atlas = build_atlas(s.font_size, extra)
        self.cell_aspect = cell_aspect or atlas.cell_w / atlas.cell_h
        self.stable = stable and s.mode != "text"
        self.prev_idx: np.ndarray | None = None
        self.prev_mask: np.ndarray | None = None

        d = SUB_X * SUB_Y
        self.flat_threshold = np.interp(s.detail, (0, 1), self.FLAT_THRESHOLD)
        self.margin = self.HYSTERESIS * d
        self.feats = atlas.features(SUB_X, SUB_Y)
        self.feats_sq = (self.feats**2).sum(1)

        kinds = atlas.kinds
        matchable = (ASCII, BLOCK, BRAILLE) if s.mode == "hybrid" else (ASCII,)
        self.glyph_ids = np.flatnonzero(np.isin(kinds, matchable))
        penalty = np.where(kinds[self.glyph_ids] != ASCII, np.interp(s.detail, (0, 1), self.BLOCK_PENALTY) * d, 0)
        self.match_t = np.ascontiguousarray(self.feats[self.glyph_ids].T)
        self.match_bias = (self.feats_sq[self.glyph_ids] + penalty).astype(np.float32)

        coverage = atlas.masks.mean(axis=(1, 2))
        ramp_chars = s.custom_chars if s.mode == "ramp" and s.ramp == "custom" else RAMPS[s.ramp] if s.mode == "ramp" else CLASSIC_RAMP
        ramp = atlas.index_of(ramp_chars)
        self.ramp = ramp[np.argsort(coverage[ramp], kind="stable")]
        reference = coverage[atlas.index_of(CLASSIC_RAMP[-1])[0]]
        gain = np.minimum(1, reference / np.maximum(coverage, 1e-6)) ** 0.5
        self.ink_gain = (gain if s.mode == "hybrid" else np.ones_like(gain)).astype(np.float32)
        self.space = atlas.index_of(" ")[0]

        braille_base = atlas.index_of(chr(0x2801))[0] - 1
        self.braille_lut = np.arange(256, dtype=np.int64) + braille_base
        self.braille_lut[0] = self.space
        self.text_idx = atlas.index_of(s.text)

    def grid_size(self, width: int, height: int) -> tuple[int, int]:
        cols = self.s.columns
        return cols, max(1, round(cols * height / width * self.cell_aspect))

    def output_size(self, width: int, height: int) -> tuple[int, int]:
        cols, rows = self.grid_size(width, height)
        return cols * self.atlas.cell_w, rows * self.atlas.cell_h

    def convert(self, image: Image.Image) -> Image.Image:
        return Image.fromarray(self.render(*self.compute_grid(image)))

    def compute_grid(self, image: Image.Image):
        frame = self.analyse(image)
        return self.finish(frame, self.stabilise(frame) if self.stable else frame.idx)

    def analyse(self, image: Image.Image) -> Frame:
        image = image.convert("RGB")
        cols, rows = self.grid_size(*image.size)

        sub = linear_resize(image, (cols * SUB_X, rows * SUB_Y))
        cells = sub.reshape(rows, SUB_Y, cols, SUB_X, 3).transpose(0, 2, 1, 3, 4).reshape(rows * cols, -1, 3)
        patches = np.sqrt(np.maximum(cells @ LUMA, 0))
        level = patches.mean(1)

        mask = None
        if self.s.focus != "off":
            from .focus import subject_mask

            mask = subject_mask(image, (cols, rows)).ravel()

        if self.s.contrast:
            subject = level[mask > 0.5] if mask is not None and (mask > 0.5).sum() > 50 else level
            lo, hi = np.percentile(subject, (1, 99))
            if hi - lo > 1e-3:
                patches = np.clip((patches - lo) / (hi - lo), 0, 1)
                level = np.clip((level - lo) / (hi - lo), 0, 1)
        if self.theme.invert:
            patches, level = 1 - patches, 1 - level

        mode = self.s.mode
        if mode == "braille":
            idx = self._braille(patches, cols)
        elif mode == "text":
            pos = (np.arange(rows)[:, None] * (cols + 3) + np.arange(cols)[None, :]) % len(self.text_idx)
            idx = self.text_idx[pos.ravel()]
        else:
            pos = np.minimum((level**self.RAMP_GAMMA * len(self.ramp)).astype(int), len(self.ramp) - 1)
            idx = self.ramp[pos]
            if mode in ("ascii", "hybrid"):
                edges = patches.std(1) > self.flat_threshold
                if edges.any():
                    idx[edges] = self.glyph_ids[self._match(patches[edges])]
        return Frame(rows, cols, cells, patches, level, idx, mask)

    def _braille(self, patches: np.ndarray, cols: int) -> np.ndarray:
        dots = patches.reshape(-1, 4, SUB_Y // 4, 2, SUB_X // 2).mean(axis=(2, 4))
        if self.diffuse:
            rows = len(patches) // cols
            grid = dots.reshape(rows, cols, 4, 2).transpose(0, 2, 1, 3).reshape(rows * 4, cols * 2)
            on = atkinson(grid).reshape(rows, 4, cols, 2).transpose(0, 2, 1, 3).reshape(-1, 4, 2)
        else:
            odd = (np.arange(len(patches)) % cols) % 2
            on = dots > np.stack([BAYER[:, [0, 1]], BAYER[:, [2, 3]]])[odd]
        bits = np.zeros(len(patches), np.int64)
        for bit, (col, row) in enumerate(BRAILLE_DOTS):
            bits |= on[:, row, col].astype(np.int64) << bit
        return self.braille_lut[bits]

    def stabilise(self, frame: Frame) -> np.ndarray:
        if frame.mask is not None:
            if self.prev_mask is not None and len(self.prev_mask) == len(frame.mask):
                frame.mask = 0.6 * frame.mask + 0.4 * self.prev_mask
            self.prev_mask = frame.mask
        idx, prev = frame.idx, self.prev_idx
        if self.stable and prev is not None and len(prev) == len(idx):
            keep = self._error(prev, frame.patches) <= self._error(idx, frame.patches) + self.margin
            idx = np.where(keep, prev, idx)
        self.prev_idx = idx
        return idx

    def finish(self, frame: Frame, idx: np.ndarray):
        s, theme, mask = self.s, self.theme, frame.mask
        if mask is not None and s.focus == "hide":
            idx = np.where(mask < self.HIDE_CUTOFF[0], self.space, idx)

        ink = self.feats[idx]
        fg = to_srgb(self._weighted_mean(frame.cells, ink)) * 255
        if theme.kind == "gradient":
            level = np.clip(frame.level * s.color_boost / 2, 0, 1)
            colored = fg
            fg = gradient(level, theme.colors)
            if theme.accent:
                fg = np.where(hue_mask(colored, theme.accent_hues)[:, None], gradient(level, theme.accent), fg)
        elif theme.kind == "palette":
            fg = nearest(self._boost(saturate(fg, s.saturation), 1), theme.colors)
        elif s.mode == "text":
            fg = saturate(fg, s.saturation)
            target = np.clip(frame.level * s.color_boost / 2, 0, 1) * 255
            fg = self._boost(fg * (target / np.maximum(fg @ LUMA, 1))[:, None] / s.color_boost, 1)
        else:
            fg = self._boost(saturate(fg, s.saturation), self.ink_gain[idx][:, None])

        if theme.background:
            bg = np.broadcast_to(hex_rgb(theme.background), fg.shape).astype(np.float32)
        elif s.background == "tinted":
            bg = to_srgb(self._weighted_mean(frame.cells, 1 - ink)) * 255 * s.bg_strength
        else:
            bg = np.zeros_like(fg)

        if mask is not None:
            if s.focus == "dim":
                weight = self.DIM_FLOOR + (1 - self.DIM_FLOOR) * smoothstep(0.2, 0.7, mask)
            else:
                weight = smoothstep(*self.HIDE_CUTOFF, mask)
            fg = bg + (fg - bg) * weight[:, None]
            if s.background == "tinted" and not theme.background:
                bg = bg * weight[:, None]

        shape = (frame.rows, frame.cols)
        return idx.reshape(shape), fg.reshape(*shape, 3), np.ascontiguousarray(bg).reshape(*shape, 3)

    @staticmethod
    def _weighted_mean(cells: np.ndarray, weights: np.ndarray) -> np.ndarray:
        total = weights.sum(1, keepdims=True)
        mean = np.einsum("nd,ndc->nc", weights, cells) / np.maximum(total, 1e-6)
        return np.where(total > 0.05, mean, cells.mean(1))

    def _match(self, patches: np.ndarray) -> np.ndarray:
        best = np.empty(len(patches), np.int64)
        for start in range(0, len(patches), CHUNK):
            p = patches[start : start + CHUNK]
            best[start : start + CHUNK] = (self.match_bias - 2 * (p @ self.match_t)).argmin(1)
        return best

    def _error(self, idx: np.ndarray, patches: np.ndarray) -> np.ndarray:
        return self.feats_sq[idx] - 2 * np.einsum("nd,nd->n", patches, self.feats[idx])

    def _boost(self, colors: np.ndarray, ink_gain) -> np.ndarray:
        peak = colors.max(axis=-1, keepdims=True)
        gain = np.minimum(self.s.color_boost * ink_gain, 255 / np.maximum(peak, 1))
        return colors * gain

    def render(self, idx: np.ndarray, fg: np.ndarray, bg: np.ndarray) -> np.ndarray:
        rows, cols = idx.shape
        ch, cw = self.atlas.cell_h, self.atlas.cell_w
        masks = self.atlas.masks[idx][..., None]
        out = bg[:, :, None, None, :] + (fg - bg)[:, :, None, None, :] * masks
        out = out.transpose(0, 2, 1, 3, 4).reshape(rows * ch, cols * cw, 3)
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)

    def page_background(self) -> str:
        return self.theme.background or "#000000"

    def _uniform_bg(self) -> bool:
        return bool(self.theme.background) or self.s.background != "tinted"

    def _runs(self, idx: np.ndarray, fg: np.ndarray, bg: np.ndarray):
        uniform = self._uniform_bg()
        keys = hex_codes(fg) if uniform else (hex_codes(fg) << 24) | hex_codes(bg)
        if uniform:
            filled = np.where(idx != self.space, np.arange(idx.shape[1]), 0)
            keys = np.take_along_axis(keys, np.maximum.accumulate(filled, axis=1), 1)
        chars = np.array(self.atlas.chars)
        for r in range(idx.shape[0]):
            k = keys[r]
            cuts = [0, *(np.flatnonzero(k[1:] != k[:-1]) + 1), idx.shape[1]]
            yield [
                ("".join(chars[idx[r, a:b]]), int(k[a]) >> 24 if not uniform else int(k[a]), None if uniform else int(k[a]) & 0xFFFFFF)
                for a, b in zip(cuts[:-1], cuts[1:])
            ]

    def to_text(self, idx: np.ndarray) -> str:
        chars = np.array(self.atlas.chars)
        return "\n".join("".join(row).rstrip() for row in chars[idx])

    def to_html(self, idx: np.ndarray, fg: np.ndarray, bg: np.ndarray) -> str:
        lines = []
        for runs in self._runs(idx, fg, bg):
            spans = []
            for text, fg_code, bg_code in runs:
                style = f"color:#{fg_code:06x}" + (f";background:#{bg_code:06x}" if bg_code is not None else "")
                spans.append(f'<span style="{style}">{html.escape(text)}</span>')
            lines.append("".join(spans))
        page = self.page_background()
        return f'<pre style="background:{page};color:#fff;font-family:monospace;line-height:1.1">' + "\n".join(lines) + "</pre>"

    def to_ansi(self, idx: np.ndarray, fg: np.ndarray, bg: np.ndarray) -> str:
        page = hex_rgb(self.page_background()).astype(int)
        page_code = f"\x1b[48;2;{page[0]};{page[1]};{page[2]}m"
        lines = []
        for runs in self._runs(idx, fg, bg):
            parts = [page_code]
            for text, fg_code, bg_code in runs:
                parts.append(f"\x1b[38;2;{fg_code >> 16};{(fg_code >> 8) & 255};{fg_code & 255}m")
                if bg_code is not None:
                    parts.append(f"\x1b[48;2;{bg_code >> 16};{(bg_code >> 8) & 255};{bg_code & 255}m")
                parts.append(text)
            lines.append("".join(parts) + "\x1b[0m")
        return "\n".join(lines)


def chat_text(image: Image.Image, settings: Settings, columns: int, style: str = "ascii", light: bool = False) -> str:
    chat = replace(
        settings,
        mode="braille" if style == "braille" else "ascii",
        columns=int(np.clip(columns, 20, 200)),
        theme="paper" if light else "original",
        background="black",
    )
    conv = AsciiConverter(chat, cell_aspect=CHAT_ASPECT, diffuse=True)
    lines = conv.to_text(conv.compute_grid(image)[0]).split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if style == "braille":
        return "\n".join(line.replace(" ", "⠀") or "⠀" for line in lines)
    return "```\n" + "\n".join(lines) + "\n```"
