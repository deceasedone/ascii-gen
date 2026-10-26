import argparse
import os
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image

from asciigen import AsciiConverter, Settings
from asciigen.converter import BACKGROUNDS, FOCUS, MAX_COLUMNS, MODES, chat_text
from asciigen.glyphs import RAMPS
from asciigen.themes import THEMES
from asciigen.video import convert_video, probe

ANIMATED = {".gif", ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
TERMINAL_ASPECT = 0.5


def enable_ansi() -> None:
    if os.name == "nt":
        os.system("")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def terminal_converter(settings: Settings, width: int, height: int) -> AsciiConverter:
    size = shutil.get_terminal_size()
    cols = size.columns
    if cols * height / width * TERMINAL_ASPECT > size.lines - 1:
        cols = int((size.lines - 1) / (height / width * TERMINAL_ASPECT))
    return AsciiConverter(replace(settings, columns=max(20, cols)), stable=True, cell_aspect=TERMINAL_ASPECT)


def play(path: str, settings: Settings) -> None:
    meta = probe(path)
    fps = meta["fps"] or 24
    w, h = meta["size"]
    conv = terminal_converter(settings, w, h)
    reader = imageio_ffmpeg.read_frames(path)
    next(reader)
    sys.stdout.write("\x1b[2J\x1b[?25l")
    start = time.perf_counter()
    try:
        for i, raw in enumerate(reader):
            due = start + i / fps
            if time.perf_counter() - due > 1 / fps:
                continue
            frame = Image.fromarray(np.frombuffer(raw, np.uint8).reshape(h, w, 3))
            sys.stdout.write("\x1b[H" + conv.to_ansi(*conv.compute_grid(frame)))
            sys.stdout.flush()
            wait = due + 1 / fps - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
    except KeyboardInterrupt:
        pass
    finally:
        reader.close()
        sys.stdout.write("\x1b[0m\x1b[?25h\n")


def main() -> None:
    p = argparse.ArgumentParser(description="Convert an image, GIF or video to coloured ASCII art.")
    p.add_argument("input")
    p.add_argument(
        "output",
        nargs="?",
        default="-",
        help="- prints to the terminal; .png .txt .html .ans for images; .gif .mp4 for GIFs and videos",
    )
    p.add_argument("--play", action="store_true", help="play a GIF or video in the terminal")
    p.add_argument("--mode", choices=MODES, default="ascii")
    p.add_argument("--ramp", choices=[*RAMPS, "custom"], default="simple")
    p.add_argument("--chars", default="", help="characters for --ramp custom")
    p.add_argument("--text", default="ASCII ", help="phrase for --mode text")
    p.add_argument("--theme", choices=list(THEMES), default="original")
    p.add_argument("--focus", choices=FOCUS, default="off")
    p.add_argument("--columns", type=int, default=MAX_COLUMNS)
    p.add_argument("--font-size", type=int, default=12)
    p.add_argument("--detail", type=float, default=0.6)
    p.add_argument("--boost", type=float, default=2.0)
    p.add_argument("--saturation", type=float, default=1.0)
    p.add_argument("--background", choices=BACKGROUNDS, default="black")
    p.add_argument("--tint", type=float, default=0.25)
    p.add_argument("--max-seconds", type=float, default=None)
    p.add_argument("--chat", choices=["ascii", "braille"], help="print chat-ready text instead")
    a = p.parse_args()

    settings = Settings(
        mode=a.mode,
        ramp=a.ramp,
        custom_chars=a.chars,
        text=a.text,
        theme=a.theme,
        focus=a.focus,
        columns=a.columns,
        font_size=a.font_size,
        detail=a.detail,
        color_boost=a.boost,
        saturation=a.saturation,
        background=a.background,
        bg_strength=a.tint,
    )
    animated = Path(a.input).suffix.lower() in ANIMATED
    out_ext = Path(a.output).suffix.lower()
    enable_ansi()

    if a.play:
        play(a.input, settings)
        return

    if animated and a.output != "-":

        def show(done: int, total: int) -> None:
            sys.stdout.write(f"\rframe {done}/{total}")
            sys.stdout.flush()

        fmt = "gif" if out_ext == ".gif" else "mp4"
        info = convert_video(a.input, a.output, settings, fmt=fmt, max_seconds=a.max_seconds, progress=show)
        print(f"\n{info}")
        return

    if animated:
        reader = imageio_ffmpeg.read_frames(a.input)
        w, h = next(reader)["size"]
        image = Image.fromarray(np.frombuffer(next(reader), np.uint8).reshape(h, w, 3))
        reader.close()
    else:
        image = Image.open(a.input)

    if a.chat:
        print(chat_text(image, settings, min(a.columns, 120), a.chat))
        return
    if a.output == "-":
        conv = terminal_converter(settings, *image.size)
        print(conv.to_ansi(*conv.compute_grid(image)))
        return

    conv = AsciiConverter(settings)
    idx, fg, bg = conv.compute_grid(image)
    if out_ext == ".txt":
        Path(a.output).write_text(conv.to_text(idx), encoding="utf-8")
    elif out_ext in (".html", ".htm"):
        Path(a.output).write_text(conv.to_html(idx, fg, bg), encoding="utf-8")
    elif out_ext == ".ans":
        Path(a.output).write_text(conv.to_ansi(idx, fg, bg), encoding="utf-8")
    else:
        Image.fromarray(conv.render(idx, fg, bg)).save(a.output)
    print(f"saved {a.output}")


if __name__ == "__main__":
    main()
