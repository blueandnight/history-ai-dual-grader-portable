"""Detect answer pages photographed sideways before they reach a grader.

Answer sheets in this project are landscape pages. When a photo arrives with
portrait dimensions, the sheet content is usually rotated by 90 degrees. Each
vision model then has to guess the text direction on its own, and two graders
can read completely different text from the same page, which is far more
damaging than an ordinary score disagreement.

The detector below only needs local image processing: long printed rules give
the axis. When rules are vertical the page is sideways; a local OCR pass over
the two 90-degree candidates then picks the upright one. For that choice the
printed page footer ( "第2页 共3页" / the "请在...作答区域" instruction ) is the
strongest cue: in an upright page it sits at the bottom, and in the 180-degree
candidate it appears at the top. It never calls a model provider.
"""
from __future__ import annotations

import math
import re
import threading
from typing import Any

import cv2
import numpy as np
from PIL import Image

_LINE_TOLERANCE_DEGREES = 15.0
_HORIZONTAL_DOMINANCE = 1.5
_VERTICAL_DOMINANCE = 1.5
_OCR_CONFIDENCE_FLOOR = 0.3
_OCR_DECISIVE_RATIO = 1.05
_FOOTER_POSITION_GAP = 0.15
_FOOTER_PATTERN = re.compile(r"第\s*\d+\s*页|共\s*\d+\s*页|作答区域|超出矩形边框")
_OCR_ENGINE = None
_OCR_LOCK = threading.Lock()


def _line_lengths(image: Image.Image) -> tuple[float, float]:
    """Return total pixel length of horizontal and vertical long straight edges."""
    gray = np.array(image.convert("L"))
    scale = 1000.0 / max(gray.shape)
    if scale < 1:
        gray = cv2.resize(
            gray,
            (max(1, round(gray.shape[1] * scale)), max(1, round(gray.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=80,
        minLineLength=int(min(gray.shape) * 0.25),
        maxLineGap=8,
    )
    horizontal = vertical = 0.0
    for line in (lines if lines is not None else []):
        x1, y1, x2, y2 = (int(value) for value in np.asarray(line).reshape(-1)[:4])
        length = math.hypot(x2 - x1, y2 - y1)
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180
        if angle <= _LINE_TOLERANCE_DEGREES or angle >= 180 - _LINE_TOLERANCE_DEGREES:
            horizontal += length
        elif 90 - _LINE_TOLERANCE_DEGREES <= angle <= 90 + _LINE_TOLERANCE_DEGREES:
            vertical += length
    return horizontal, vertical


def _ocr_engine():
    global _OCR_ENGINE
    with _OCR_LOCK:
        if _OCR_ENGINE is None:
            from rapidocr_onnxruntime import RapidOCR

            _OCR_ENGINE = RapidOCR()
        return _OCR_ENGINE


def _text_signature(image: Image.Image) -> dict[str, Any] | None:
    """Local OCR yield plus the normalized vertical position of printed footer text."""
    try:
        engine = _ocr_engine()
    except Exception:
        return None
    array = np.array(image.convert("RGB"))
    scale = 900.0 / max(array.shape[:2])
    if scale < 1:
        array = cv2.resize(
            array,
            (max(1, round(array.shape[1] * scale)), max(1, round(array.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
    height = array.shape[0]
    try:
        result, _ = engine(array)
    except Exception:
        return None
    total = 0.0
    footer_position: float | None = None
    for box, text, score in (result or []):
        try:
            confidence = float(score)
        except (TypeError, ValueError):
            continue
        if confidence <= _OCR_CONFIDENCE_FLOOR:
            continue
        text = str(text)
        total += len(text) * confidence
        if _FOOTER_PATTERN.search(text):
            try:
                ys = [float(point[1]) for point in box]
            except (TypeError, ValueError, IndexError):
                continue
            position = (min(ys) + max(ys)) / 2 / max(height, 1)
            footer_position = position if footer_position is None else max(footer_position, position)
    return {"score": total, "footer_position": footer_position}


def _choose_direction(image: Image.Image) -> tuple[int, dict[str, Any]]:
    detail: dict[str, Any] = {}
    counter_clockwise = image.rotate(90, expand=True)
    clockwise = image.rotate(-90, expand=True)
    ccw = _text_signature(counter_clockwise)
    cw = _text_signature(clockwise)
    if ccw is None or cw is None:
        return 90, {**detail, "basis": "ocr_unavailable_default_ccw", "confidence": "low"}
    detail["ocr_ccw_score"] = round(ccw["score"], 1)
    detail["ocr_cw_score"] = round(cw["score"], 1)
    ccw_footer = ccw["footer_position"]
    cw_footer = cw["footer_position"]
    if ccw_footer is not None:
        detail["footer_ccw_position"] = round(ccw_footer, 3)
    if cw_footer is not None:
        detail["footer_cw_position"] = round(cw_footer, 3)
    if ccw_footer is not None and cw_footer is not None and abs(ccw_footer - cw_footer) >= _FOOTER_POSITION_GAP:
        if ccw_footer > cw_footer:
            return 90, {**detail, "basis": "footer_position", "confidence": "high"}
        return -90, {**detail, "basis": "footer_position", "confidence": "high"}
    if ccw["score"] > cw["score"] * _OCR_DECISIVE_RATIO:
        return 90, {**detail, "basis": "ocr", "confidence": "high"}
    if cw["score"] > ccw["score"] * _OCR_DECISIVE_RATIO:
        return -90, {**detail, "basis": "ocr", "confidence": "high"}
    return 90, {**detail, "basis": "tie_default_ccw", "confidence": "low"}


def detect_upright_rotation(image: Image.Image) -> tuple[int, dict[str, Any]]:
    """Return the rotation needed to make text upright.

    The returned angle is in degrees and follows ``Image.rotate`` semantics
    (positive = counter-clockwise): 0, 90 or -90. The detail mapping is
    diagnostic only and never contains image content.
    """
    horizontal, vertical = _line_lengths(image)
    detail: dict[str, Any] = {
        "horizontal_line_px": round(horizontal, 1),
        "vertical_line_px": round(vertical, 1),
    }
    if horizontal >= vertical * _HORIZONTAL_DOMINANCE:
        return 0, {**detail, "basis": "rule_lines", "confidence": "high"}
    if vertical < horizontal * _VERTICAL_DOMINANCE:
        return 0, {**detail, "basis": "ambiguous_lines", "confidence": "low"}
    rotation, choice = _choose_direction(image)
    return rotation, {**detail, **choice}


def apply_upright_rotation(image: Image.Image) -> tuple[Image.Image, dict[str, Any] | None]:
    """Apply a detected rotation; return the image and a diagnostic record."""
    try:
        rotation, detail = detect_upright_rotation(image)
    except Exception as exc:  # never block an upload because orientation is unknown
        return image, {"detected": "error", "error_type": type(exc).__name__}
    if not rotation:
        return image, {"rotation": 0, **detail}
    return image.rotate(rotation, expand=True), {"rotation": rotation, **detail}
