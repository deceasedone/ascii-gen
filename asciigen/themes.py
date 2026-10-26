from __future__ import annotations

from dataclasses import dataclass

import numpy as np

LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)


@dataclass(frozen=True)
class Theme:
    label: str
    kind: str = "original"
    colors: tuple[str, ...] = ()
    background: str | None = None
    invert: bool = False
    accent: tuple[str, ...] = ()
    accent_hues: tuple[float, float] = (165, 265)


THEMES = {
    "original": Theme("Original"),
    "grayscale": Theme("Grayscale", "gradient", ("#2e2e2e", "#8f8f8f", "#ffffff"), "#000000"),
    "matrix": Theme("Matrix", "gradient", ("#003b0a", "#00c832", "#7dff9a", "#e6ffe9"), "#000000"),
    "amber": Theme("Amber", "gradient", ("#4a2600", "#ff9f00", "#ffd27a", "#fff5dc"), "#0b0700"),
    "glitch": Theme("Glitch", "gradient", ("#2a0b45", "#7b2fbf", "#c38bff", "#f4e6ff"), "#000000"),
    "synthwave": Theme(
        "Synthwave",
        "gradient",
        ("#3a1052", "#9c1f63", "#e8457f", "#ffa3c8", "#fff4f8"),
        "#14081f",
        accent=("#0c3a52", "#1aa9c4", "#7eeaff", "#e6fdff"),
    ),
    "crypt": Theme("Crypt", "gradient", ("#4a4a4a", "#9c9c9c", "#ececec"), "#4a0b0b"),
    "gameboy": Theme("Game Boy", "palette", ("#306230", "#8bac0f", "#9bbc0f", "#e0f8d0"), "#0f380f"),
    "paper": Theme("Paper", "gradient", ("#a39e94", "#4a4740", "#111111"), "#f3efe6", invert=True),
}


def hex_rgb(value: str) -> np.ndarray:
    value = value.lstrip("#")
    return np.array([int(value[i : i + 2], 16) for i in (0, 2, 4)], np.float32)


def saturate(rgb: np.ndarray, amount: float) -> np.ndarray:
    if amount == 1:
        return rgb
    y = (rgb @ LUMA)[..., None]
    return np.clip(y + (rgb - y) * amount, 0, 255)


def gradient(level: np.ndarray, stops: tuple[str, ...]) -> np.ndarray:
    colors = np.stack([hex_rgb(c) for c in stops])
    pos = np.linspace(0, 1, len(stops))
    return np.stack([np.interp(level, pos, colors[:, c]) for c in range(3)], -1).astype(np.float32)


def hue_mask(rgb: np.ndarray, hues: tuple[float, float], min_saturation: float = 0.22) -> np.ndarray:
    hi, lo = rgb.max(-1), rgb.min(-1)
    chroma = hi - lo
    safe = np.maximum(chroma, 1e-6)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    hue = np.where(hi == r, ((g - b) / safe) % 6, np.where(hi == g, (b - r) / safe + 2, (r - g) / safe + 4)) * 60
    return (chroma / np.maximum(hi, 1) > min_saturation) & (hue >= hues[0]) & (hue <= hues[1])


def _oklab(rgb: np.ndarray) -> np.ndarray:
    c = rgb / 255
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    lms = lin @ np.array(
        [[0.4122214708, 0.2119034982, 0.0883024619],
         [0.5363325363, 0.6806995451, 0.2817188376],
         [0.0514459929, 0.1073969566, 0.6299787005]],
        np.float32,
    )
    lms = np.cbrt(lms)
    return lms @ np.array(
        [[0.2104542553, 1.9779984951, 0.0259040371],
         [0.7936177850, -2.4285922050, 0.7827717662],
         [-0.0040720468, 0.4505937099, -0.8086757660]],
        np.float32,
    )


def nearest(rgb: np.ndarray, palette: tuple[str, ...]) -> np.ndarray:
    colors = np.stack([hex_rgb(c) for c in palette])
    dist = ((_oklab(rgb)[:, None, :] - _oklab(colors)[None, :, :]) ** 2).sum(-1)
    return colors[dist.argmin(1)]
