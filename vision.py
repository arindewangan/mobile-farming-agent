"""
No-LLM on-device vision toolkit (Tier-1 automation aid).

The accessibility tree (``recipeui.ui_state`` / uiautomator) is fast and cheap but
blind to anything drawn as pixels — video frames, thumbnails, ad graphics, icons,
text baked into images (view counts, "Skip Ad" countdowns, custom buttons). This
module reads those pixels with two SMALL, CPU-only, packable models — no LLM, no
GPU, nothing leaving the box:

  * OCR   — RapidOCR (PP-OCR ONNX, ~10 MB, bundled with the pip package). Finds any
            on-screen TEXT and its tappable center.
  * TEMPLATE — OpenCV normalized-cross-correlation. Finds an ICON/graphic by image.

Coordinates come out in the screenshot's pixel space (the device's CURRENT
orientation), so ``adb input tap`` lands on them in any rotation — the same reason
the flows tap via adb rather than scrcpy.

Everything runs in a thread executor so a ~1-3 s OCR pass never blocks the agent's
event loop (heartbeat, other devices). OCR is meant as a FALLBACK for when the a11y
tree comes up empty, and as a first-class primitive recipes can use to automate
apps that expose no useful accessibility nodes — all without any LLM.
"""
from __future__ import annotations

import asyncio
import base64
import re

import adb

_OCR = None            # lazy RapidOCR singleton (loading pulls the ONNX models)
_OCR_LOCK = asyncio.Lock()


async def _ocr_engine():
    global _OCR
    if _OCR is None:
        async with _OCR_LOCK:
            if _OCR is None:
                from rapidocr_onnxruntime import RapidOCR  # local import: heavy-ish
                _OCR = await asyncio.get_event_loop().run_in_executor(None, RapidOCR)
    return _OCR


def _decode(jpeg: bytes):
    import cv2
    import numpy as np
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


def _crop(img, region):
    """region = (x1,y1,x2,y2) as fractions (0-1) of the image; returns (crop, ox, oy)."""
    if not region:
        return img, 0, 0
    h, w = img.shape[:2]
    x1, y1, x2, y2 = region
    ox, oy = int(x1 * w), int(y1 * h)
    return img[oy:int(y2 * h), ox:int(x2 * w)], ox, oy


async def ocr_read(serial: str, region=None, jpeg: bytes | None = None) -> list[dict]:
    """Return every text region on screen: {text, x, y, conf} with (x,y) the
    tappable center in device pixels. `region` optionally crops to a fractional
    box (faster + fewer false hits). Pass an already-captured `jpeg` to skip
    the screencap — lets a caller chaining OCR with another screenshot-based
    check (e.g. detect.classify_screen) share one capture instead of two."""
    if jpeg is None:
        jpeg = await adb.screencap_full_jpeg(serial, quality=88)
    if not jpeg:
        return []
    img = _decode(jpeg)
    if img is None:
        return []
    crop, ox, oy = _crop(img, region)
    engine = await _ocr_engine()
    result, _ = await asyncio.get_event_loop().run_in_executor(None, lambda: engine(crop))
    out: list[dict] = []
    for box, text, conf in (result or []):
        cx = int(sum(p[0] for p in box) / 4) + ox
        cy = int(sum(p[1] for p in box) / 4) + oy
        out.append({"text": text, "x": cx, "y": cy, "conf": float(conf)})
    return out


async def ocr_find(serial: str, needle: str, region=None, min_conf: float = 0.5,
                    jpeg: bytes | None = None) -> dict | None:
    """Find on-screen text containing `needle` (case-insensitive substring) and
    return {text, x, y, conf}, or None. Prefers the topmost match. Pass an
    already-captured `jpeg` to skip the screencap (see ocr_read)."""
    n = needle.strip().lower()
    hits = [r for r in await ocr_read(serial, region, jpeg=jpeg)
            if r["conf"] >= min_conf and n in r["text"].lower()]
    if not hits:
        return None
    hits.sort(key=lambda r: r["y"])  # topmost first
    return hits[0]


async def ocr_tap(serial: str, needle: str, region=None) -> dict:
    """OCR-find `needle` and adb-tap its center. Returns {ok, x?, y?, text?}."""
    hit = await ocr_find(serial, needle, region)
    if not hit:
        return {"ok": False, "found": False, "query": needle}
    await adb.shell(serial, f"input tap {hit['x']} {hit['y']}")
    return {"ok": True, "found": True, "x": hit["x"], "y": hit["y"], "text": hit["text"]}


async def ocr_wait_for(serial: str, needle: str, timeout: float = 15.0, region=None) -> dict:
    """Poll OCR until `needle` appears on screen (or timeout)."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hit = await ocr_find(serial, needle, region)
        if hit:
            return {"ok": True, "present": True, **hit}
        await asyncio.sleep(1.0)
    return {"ok": False, "present": False, "query": needle}


async def template_find(serial: str, template_b64: str, threshold: float = 0.82) -> dict:
    """Find an icon/graphic on screen by OpenCV template match (NCC). Returns
    {ok, present, score, x?, y?}. Delegates to the existing matcher in recipeui."""
    import recipeui
    return await recipeui.match_template(serial, template_b64, threshold)


async def available() -> bool:
    """Is the OCR stack importable (models present)?"""
    try:
        await _ocr_engine()
        return True
    except Exception:  # noqa: BLE001
        return False


# convenience re-export so callers can build a region from pixel fractions
def region(x1: float, y1: float, x2: float, y2: float):
    return (x1, y1, x2, y2)
