from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import Callable

import imageio_ffmpeg
import numpy as np
from PIL import Image

from .converter import AsciiConverter, Settings

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
FORMATS = ("mp4", "gif")
WORKERS = max(1, min(8, int(os.environ.get("FRAME_WORKERS") or os.cpu_count() or 2)))
GIF_FILTER = "split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"


def probe(path: str) -> dict:
    reader = imageio_ffmpeg.read_frames(path)
    try:
        return next(reader)
    finally:
        reader.close()


def _fit_converter(settings: Settings, size: tuple[int, int], max_width: int) -> AsciiConverter:
    conv = AsciiConverter(settings, stable=True)
    while conv.output_size(*size)[0] > max_width and conv.s.font_size > 6:
        conv = AsciiConverter(replace(conv.s, font_size=conv.s.font_size - 1), stable=True)
    if conv.output_size(*size)[0] > max_width:
        conv = AsciiConverter(replace(conv.s, columns=max_width // conv.atlas.cell_w), stable=True)
    return conv


def _encoder(path: str, size: tuple[int, int], fps: float, fmt: str, log) -> subprocess.Popen:
    w, h = size
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24"]
    cmd += ["-s", f"{w}x{h}", "-r", f"{fps:g}", "-i", "-"]
    if fmt == "gif":
        cmd += ["-vf", GIF_FILTER, "-loop", "0"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    return subprocess.Popen([*cmd, path], stdin=subprocess.PIPE, stderr=log)


def convert_video(
    src: str,
    dst: str,
    settings: Settings,
    fmt: str = "mp4",
    max_seconds: float | None = None,
    max_fps: float = 30,
    max_width: int = 1920,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {FORMATS}")
    meta = probe(src)
    fps = min(meta["fps"] or max_fps, max_fps)
    duration = meta.get("duration") or 0
    if max_seconds:
        duration = min(duration, max_seconds) if duration else max_seconds
    total = max(1, round(duration * fps))

    input_params = ["-t", str(max_seconds)] if max_seconds else None
    reader = imageio_ffmpeg.read_frames(src, input_params=input_params, output_params=["-vf", f"fps={fps}"])
    w, h = next(reader)["size"]

    conv = _fit_converter(settings, (w, h), max_width)
    ow, oh = conv.output_size(w, h)
    pw, ph = (ow + ow % 2, oh + oh % 2) if fmt == "mp4" else (ow, oh)

    has_audio = fmt == "mp4" and bool(meta.get("audio_codec"))
    encoded = dst + f".frames.{fmt}" if has_audio else dst
    def analyse(raw: bytes):
        return conv.analyse(Image.fromarray(np.frombuffer(raw, np.uint8).reshape(h, w, 3)))

    def draw(frame, idx) -> bytes:
        out = conv.render(*conv.finish(frame, idx))
        if (pw, ph) != (ow, oh):
            out = np.pad(out, ((0, ph - oh), (0, pw - ow), (0, 0)))
        return out.tobytes()

    frames = 0
    window = WORKERS * 2
    analysed: deque[Future] = deque()
    drawn: deque[Future] = deque()

    def write_drawn(limit: int) -> None:
        nonlocal frames
        while len(drawn) > limit:
            encoder.stdin.write(drawn.popleft().result())
            frames += 1
            if progress:
                progress(frames, max(total, frames))

    def draw_analysed(limit: int) -> None:
        while len(analysed) > limit:
            frame = analysed.popleft().result()
            drawn.append(pool.submit(draw, frame, conv.stabilise(frame)))
            write_drawn(window)

    try:
        with tempfile.TemporaryFile() as log, ThreadPoolExecutor(WORKERS) as pool:
            encoder = _encoder(encoded, (pw, ph), fps, fmt, log)
            try:
                for raw in reader:
                    analysed.append(pool.submit(analyse, raw))
                    draw_analysed(window)
                draw_analysed(0)
                write_drawn(0)
            finally:
                for job in (*analysed, *drawn):
                    job.cancel()
                encoder.stdin.close()
                encoder.wait()
            if encoder.returncode:
                log.seek(0)
                raise RuntimeError(f"ffmpeg failed: {log.read().decode(errors='replace')[-500:]}")

        if has_audio:
            cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", encoded, "-i", src]
            cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest"]
            cmd += ["-movflags", "+faststart", dst]
            subprocess.run(cmd, check=True, capture_output=True)
            os.remove(encoded)
    except BaseException:
        for path in {encoded, dst} if frames else {encoded} - {dst}:
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)
        raise
    finally:
        reader.close()

    return {"frames": frames, "fps": fps, "width": pw, "height": ph, "audio": has_audio, "format": fmt}
