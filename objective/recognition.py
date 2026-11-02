from __future__ import annotations

import importlib.metadata
import re
import threading
from typing import Any

import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR


_ENGINE: RapidOCR | None = None
_ENGINE_LOCK = threading.Lock()
_ANSWER_PATTERN = re.compile(r"(?<!\d)0?(\d{1,3})\s*[.．:：、)）\-]?\s*([ABCD])(?:\b|$)", re.IGNORECASE)
_QUESTION_ONLY_PATTERN = re.compile(r"^\s*0?(\d{1,3})\s*[.．:：、)）\-]?\s*$")
_OPTION_ONLY_PATTERN = re.compile(r"^\s*([ABCD])\s*[.．、]?\s*$", re.IGNORECASE)


def get_engine() -> RapidOCR:
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = RapidOCR()
    return _ENGINE


def recognizer_version() -> str:
    try:
        return importlib.metadata.version("rapidocr-onnxruntime")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def recognize_handwritten_abcd(
    image_bytes: bytes,
    question_count: int,
    *,
    engine: Any | None = None,
    question_ids: list[int] | None = None,
    expected_question_id: int | None = None,
) -> dict[int, dict[str, Any]]:
    """Run the real local RapidOCR detector/recognizer; never returns a score."""
    if expected_question_id is not None:
        if not isinstance(expected_question_id, int) or not 1 <= expected_question_id <= question_count:
            raise ValueError("expected_question_id must be in 1..question_count")
        question_ids = [expected_question_id]
    expected_questions = sorted(set(question_ids)) if question_ids is not None else list(range(1, question_count + 1))
    if any(not isinstance(q, int) or q < 1 or q > question_count for q in expected_questions):
        raise ValueError("question_ids must be in 1..question_count")
    encoded = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        return {q: {"option": None, "state": "needs_review", "reason": "image_decode_failed", "confidence": None, "evidence": None} for q in expected_questions}
    actual_engine = engine or get_engine()
    detections, _timings = actual_engine(image)
    by_question: dict[int, list[dict[str, Any]]] = {}
    labels: list[dict[str, Any]] = []
    options: list[dict[str, Any]] = []
    for detection in detections or []:
        if not isinstance(detection, (list, tuple)) or len(detection) < 3:
            continue
        box, text, confidence = detection[0], str(detection[1]).upper(), float(detection[2])
        safe_box = [[round(float(x), 1), round(float(y), 1)] for x, y in box]
        points = np.asarray(box, dtype=np.float32)
        left, top = float(points[:, 0].min()), float(points[:, 1].min())
        right, bottom = float(points[:, 0].max()), float(points[:, 1].max())
        center_x, center_y = (left + right) / 2, (top + bottom) / 2
        safe_detection = {"confidence": confidence, "evidence": safe_box, "left": left, "right": right, "center_x": center_x, "center_y": center_y, "height": max(1.0, bottom - top)}
        for match in _ANSWER_PATTERN.finditer(text):
            qno = int(match.group(1))
            option = match.group(2).upper()
            if qno not in expected_questions:
                continue
            by_question.setdefault(qno, []).append({"option": option, "confidence": confidence, "evidence": safe_box})
        question_only = _QUESTION_ONLY_PATTERN.fullmatch(text)
        option_only = _OPTION_ONLY_PATTERN.fullmatch(text)
        if question_only:
            qno = int(question_only.group(1))
            if qno in expected_questions:
                labels.append({**safe_detection, "question_no": qno})
        elif option_only:
            options.append({**safe_detection, "option": option_only.group(1).upper()})

    # RapidOCR often returns the printed question label and handwritten letter in separate boxes.
    # Pair only same-row, right-of-label detections; ambiguous/multiple candidates remain review items.
    for option in options:
        possible = []
        for label in labels:
            vertical_distance = abs(option["center_y"] - label["center_y"])
            row_tolerance = max(option["height"], label["height"]) * 0.72
            horizontal_distance = option["center_x"] - label["center_x"]
            if option["left"] < label["center_x"] or horizontal_distance > label["height"] * 3.2:
                continue
            if vertical_distance <= row_tolerance:
                possible.append((vertical_distance, label))
        if not possible:
            # In a table cell the question mapping comes from the layout, so
            # accept one isolated A/B/C/D detection for that known cell only.
            if expected_question_id is not None:
                by_question.setdefault(expected_question_id, []).append({
                    "option": option["option"],
                    "confidence": float(option["confidence"]),
                    "evidence": option["evidence"],
                })
            continue
        possible.sort(key=lambda item: item[0])
        closest_distance = possible[0][0]
        closest = [label for distance, label in possible if distance <= closest_distance + 2.0]
        for label in closest:
            by_question.setdefault(label["question_no"], []).append({
                "option": option["option"],
                "confidence": min(float(option["confidence"]), float(label["confidence"])),
                "evidence": [label["evidence"], option["evidence"]],
            })

    answers: dict[int, dict[str, Any]] = {}
    for qno in expected_questions:
        matches = by_question.get(qno, [])
        if len(matches) != 1:
            reason = "unrecognized" if not matches else "duplicate_question_candidate"
            answers[qno] = {"option": None, "state": "needs_review", "reason": reason, "confidence": None, "evidence": None}
            continue
        match = matches[0]
        if match["confidence"] < 0.65:
            answers[qno] = {"option": match["option"], "state": "needs_review", "reason": "low_recognition_confidence", "confidence": round(match["confidence"], 4), "evidence": match["evidence"]}
        else:
            answers[qno] = {"option": match["option"], "state": "auto", "reason": None, "confidence": round(match["confidence"], 4), "evidence": match["evidence"]}
    return answers
