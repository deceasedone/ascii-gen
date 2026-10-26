from __future__ import annotations

import asyncio
import io
import os
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from asciigen import AsciiConverter, Settings
from asciigen.converter import chat_text
from asciigen.glyphs import RAMPS
from asciigen.themes import THEMES
from asciigen.video import FORMATS, convert_video

MAX_UPLOAD_MB = float(os.environ.get("MAX_UPLOAD_MB", 100))
MAX_VIDEO_SECONDS = float(os.environ.get("MAX_VIDEO_SECONDS", 0))
MAX_VIDEO_WIDTH = int(os.environ.get("MAX_VIDEO_WIDTH", 1920))
MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", 40_000_000))
VIDEO_WORKERS = int(os.environ.get("VIDEO_WORKERS", 1))
JOB_TTL_SECONDS = 3600
SWEEP_INTERVAL_SECONDS = 600
MEDIA_TYPES = {"mp4": "video/mp4", "gif": "image/gif"}

WORK_DIR = Path(tempfile.gettempdir()) / "asciigen-jobs"
WORK_DIR.mkdir(exist_ok=True)
STATIC_DIR = Path(__file__).parent / "static"

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

executor = ThreadPoolExecutor(max_workers=VIDEO_WORKERS)
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def _sweep_jobs() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with jobs_lock:
        for jid, job in list(jobs.items()):
            if job["status"] in ("done", "error") and job["created"] < cutoff:
                jobs.pop(jid)
        tracked = set(jobs)
    for path in WORK_DIR.iterdir():
        if path.name not in tracked:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)


async def _sweep_periodically() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        await run_in_threadpool(_sweep_jobs)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _sweep_jobs()
    sweeper = asyncio.create_task(_sweep_periodically())
    yield
    sweeper.cancel()


app = FastAPI(title="ASCII Gen", lifespan=lifespan)
D = Settings()


def settings_form(
    mode: str = Form(D.mode),
    columns: int = Form(D.columns),
    font_size: int = Form(D.font_size),
    detail: float = Form(D.detail),
    color_boost: float = Form(D.color_boost),
    background: str = Form(D.background),
    bg_strength: float = Form(D.bg_strength),
    ramp: str = Form(D.ramp),
    custom_chars: str = Form(D.custom_chars),
    text: str = Form(D.text),
    theme: str = Form(D.theme),
    saturation: float = Form(D.saturation),
    focus: str = Form(D.focus),
) -> Settings:
    try:
        return Settings(
            mode=mode,
            columns=columns,
            font_size=font_size,
            detail=detail,
            color_boost=color_boost,
            background=background,
            bg_strength=bg_strength,
            ramp=ramp,
            custom_chars=custom_chars,
            text=text,
            theme=theme,
            saturation=saturation,
            focus=focus,
        ).validate()
    except ValueError as e:
        raise HTTPException(400, str(e))


def _too_large() -> HTTPException:
    return HTTPException(413, f"File is larger than {MAX_UPLOAD_MB:g} MB")


async def _read_image(file: UploadFile) -> Image.Image:
    limit = int(MAX_UPLOAD_MB * 1024 * 1024)
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise _too_large()
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        return image
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(400, "Could not read that image")


async def _save_upload(file: UploadFile, dest: Path) -> None:
    limit = int(MAX_UPLOAD_MB * 1024 * 1024)
    size = 0
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise _too_large()
            f.write(chunk)


@app.post("/api/convert/image")
async def convert_image(file: UploadFile = File(...), settings: Settings = Depends(settings_form)):
    image = await _read_image(file)

    def work() -> bytes:
        buf = io.BytesIO()
        AsciiConverter(settings).convert(image).save(buf, "PNG", compress_level=6)
        return buf.getvalue()

    return Response(await run_in_threadpool(work), media_type="image/png")


@app.post("/api/convert/text")
async def convert_text(file: UploadFile = File(...), settings: Settings = Depends(settings_form)):
    image = await _read_image(file)

    def work() -> dict:
        conv = AsciiConverter(settings)
        idx, fg, bg = conv.compute_grid(image)
        return {"text": conv.to_text(idx), "html": conv.to_html(idx, fg, bg)}

    return await run_in_threadpool(work)


@app.post("/api/convert/chat")
async def convert_chat(
    file: UploadFile = File(...),
    chat_columns: int = Form(60),
    chat_style: str = Form("ascii"),
    chat_light: bool = Form(False),
    settings: Settings = Depends(settings_form),
):
    if chat_style not in ("ascii", "braille"):
        raise HTTPException(400, "chat_style must be ascii or braille")
    image = await _read_image(file)
    text = await run_in_threadpool(chat_text, image, settings, chat_columns, chat_style, chat_light)
    return {"text": text, "chars": len(text)}


@app.get("/api/options")
def options():
    return {
        "themes": {
            key: {"label": t.label, "kind": t.kind, "colors": list(t.colors + t.accent), "background": t.background}
            for key, t in THEMES.items()
        },
        "ramps": RAMPS,
    }


@app.post("/api/convert/video")
async def start_video_job(
    file: UploadFile = File(...),
    format: str = Form("mp4"),
    settings: Settings = Depends(settings_form),
):
    if format not in FORMATS:
        raise HTTPException(400, f"format must be one of {FORMATS}")

    job_id = uuid.uuid4().hex
    job_dir = WORK_DIR / job_id
    src = job_dir / f"input{Path(file.filename or '').suffix.lower()[:8] or '.bin'}"

    job = {"status": "uploading", "done": 0, "total": 0, "error": None, "created": time.time(), "info": None}
    with jobs_lock:
        jobs[job_id] = job
    try:
        job_dir.mkdir()
        await _save_upload(file, src)
    except BaseException:
        with jobs_lock:
            jobs.pop(job_id, None)
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    job["status"] = "queued"

    def on_progress(done: int, total: int) -> None:
        job["done"], job["total"] = done, total

    def run() -> None:
        job["status"] = "running"
        try:
            job["info"] = convert_video(
                str(src),
                str(job_dir / f"output.{format}"),
                settings,
                fmt=format,
                max_seconds=MAX_VIDEO_SECONDS or None,
                max_width=MAX_VIDEO_WIDTH,
                progress=on_progress,
            )
            job["status"] = "done"
        except Exception as e:
            job["status"] = "error"
            job["error"] = "Could not convert this file. Is it a valid video or GIF?"
            if "focus model" in str(e):
                job["error"] = "Could not load the focus model. Try again with Focus off."
            print(f"job {job_id} failed: {e!r}")
            shutil.rmtree(job_dir, ignore_errors=True)
        finally:
            src.unlink(missing_ok=True)

    executor.submit(run)
    return {"job_id": job_id}


def _job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return job


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = _job(job_id)
    return {k: job[k] for k in ("status", "done", "total", "error", "info")}


@app.get("/api/jobs/{job_id}/result")
def job_result(job_id: str):
    job = _job(job_id)
    if job["status"] != "done":
        raise HTTPException(409, "Job is not finished")
    fmt = job["info"]["format"]
    return FileResponse(WORK_DIR / job_id / f"output.{fmt}", media_type=MEDIA_TYPES[fmt], filename=f"ascii.{fmt}")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job = _job(job_id)
    if job["status"] not in ("done", "error"):
        raise HTTPException(409, "Job is still running")
    with jobs_lock:
        jobs.pop(job_id, None)
    shutil.rmtree(WORK_DIR / job_id, ignore_errors=True)
    return {"deleted": job_id}


@app.get("/api/limits")
def limits():
    return {"max_upload_mb": MAX_UPLOAD_MB, "max_video_seconds": MAX_VIDEO_SECONDS}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
