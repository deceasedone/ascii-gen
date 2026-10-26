from __future__ import annotations

import hashlib
import os
import threading
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx"
MODEL_SHA256 = "309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8"
MODEL_DIR = Path(os.environ.get("ASCII_MODEL_DIR") or Path.home() / ".cache" / "asciigen")
INPUT_SIZE = 320
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)

_session = None
_lock = threading.Lock()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_model() -> Path:
    path = MODEL_DIR / "u2netp.onnx"
    if path.exists():
        return path
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    urllib.request.urlretrieve(MODEL_URL, part)
    if _sha256(part) != MODEL_SHA256:
        part.unlink(missing_ok=True)
        raise RuntimeError("Downloaded focus model failed its checksum")
    part.replace(path)
    return path


def _get_session():
    global _session
    with _lock:
        if _session is None:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            _session = ort.InferenceSession(str(ensure_model()), options, providers=["CPUExecutionProvider"])
    return _session


def subject_mask(image: Image.Image, size: tuple[int, int]) -> np.ndarray:
    session = _get_session()
    x = np.asarray(image.convert("RGB").resize((INPUT_SIZE, INPUT_SIZE), Image.LANCZOS), np.float32)
    x = (x / max(x.max(), 1) - MEAN) / STD
    pred = session.run(None, {session.get_inputs()[0].name: x.transpose(2, 0, 1)[None]})[0][0, 0]
    pred = (pred - pred.min()) / max(pred.max() - pred.min(), 1e-6)
    return np.asarray(Image.fromarray(pred.astype(np.float32)).resize(size, Image.BILINEAR), np.float32)
