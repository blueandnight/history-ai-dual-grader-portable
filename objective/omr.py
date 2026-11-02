from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from page_alignment import align_page


OPTIONS = "ABCD"


@dataclass
class OMRPageResult:
    status: str
    answers: dict[int, dict[str, Any]]
    diagnostics: dict[str, Any]


def _read_signature(gray: np.ndarray, config: dict[str, Any]) -> str:
    height, width = gray.shape
    bits: list[str] = []
    box = max(3, round(width * 0.006))
    for x_norm, y_norm in config["signature_centers"]:
        cx, cy = round(x_norm * width), round(y_norm * height)
        patch = gray[max(cy - box, 0):cy + box + 1, max(cx - box, 0):cx + box + 1]
        dark_ratio = float(np.mean(patch < int(config["dark_threshold"]))) if patch.size else 0.0
        if 0.12 < dark_ratio < 0.55:
            return "ambiguous"
        bits.append("1" if dark_ratio >= 0.55 else "0")
    return "".join(bits)


def recognize_omr(image_bytes: bytes, config: dict[str, Any]) -> OMRPageResult:
    aligned = align_page(image_bytes, {"method": "fiducial_anchors", "template_config": config})
    if aligned.status != "aligned" or aligned.image is None:
        return OMRPageResult("needs_review", {}, aligned.diagnostics)
    return recognize_omr_aligned(aligned.image, config, aligned.diagnostics)


def recognize_omr_aligned(
    aligned_image: np.ndarray,
    config: dict[str, Any],
    alignment_diagnostics: dict[str, Any] | None = None,
    *,
    signature_image: np.ndarray | None = None,
) -> OMRPageResult:
    """Run existing OMR signature/mark detection on a page aligned upstream."""
    if aligned_image is None or not isinstance(aligned_image, np.ndarray) or aligned_image.ndim != 3:
        return OMRPageResult("needs_review", {}, {"alignment": "decode_failed"})
    diagnostics = dict(alignment_diagnostics or {"alignment": "aligned", "method": "shared_page_alignment"})
    gray = cv2.cvtColor(aligned_image, cv2.COLOR_BGR2GRAY)
    signature_source = signature_image if signature_image is not None else aligned_image
    signature_gray = cv2.cvtColor(signature_source, cv2.COLOR_BGR2GRAY) if signature_source is not aligned_image else gray
    observed_signature = _read_signature(signature_gray, config)
    if observed_signature != config["signature_bits"]:
        return OMRPageResult("needs_review", {}, {**diagnostics, "alignment": "layout_signature_mismatch", "signature_match": False})

    height, width = gray.shape
    radius = max(4, round(width * float(config["bubble_radius_norm"])))
    yy, xx = np.ogrid[-radius:radius + 1, -radius:radius + 1]
    inner_mask = xx * xx + yy * yy <= (radius * 0.55) ** 2
    answers: dict[int, dict[str, Any]] = {}
    blank_max = float(config["blank_ratio_max"])
    mark_min = float(config["mark_ratio_min"])
    for cell in config["question_cells"]:
        qno = int(cell["question"])
        ratios: list[float] = []
        for x_norm in cell["option_x"]:
            cx, cy = round(float(x_norm) * width), round(float(cell["y"]) * height)
            patch = gray[cy - radius:cy + radius + 1, cx - radius:cx + radius + 1]
            if patch.shape != inner_mask.shape:
                ratios.append(-1.0)
                continue
            ratios.append(float(np.mean(patch[inner_mask] < int(config["dark_threshold"]))))
        if any(r < 0 for r in ratios):
            answers[qno] = {"option": None, "state": "needs_review", "reason": "bubble_outside_image", "ratios": ratios}
            continue
        marked = [index for index, ratio in enumerate(ratios) if ratio >= mark_min]
        ambiguous = [index for index, ratio in enumerate(ratios) if blank_max < ratio < mark_min]
        if len(marked) > 1:
            answers[qno] = {"option": None, "state": "needs_review", "reason": "multiple_marks", "ratios": ratios}
        elif ambiguous:
            answers[qno] = {"option": None, "state": "needs_review", "reason": "ambiguous_or_erasure", "ratios": ratios}
        elif not marked:
            answers[qno] = {"option": None, "state": "needs_review", "reason": "unanswered", "ratios": ratios}
        else:
            index = marked[0]
            answers[qno] = {"option": OPTIONS[index], "state": "auto", "reason": None, "ratios": ratios}
    return OMRPageResult("auto" if all(a["state"] == "auto" for a in answers.values()) else "needs_review", answers, {**diagnostics, "alignment": "aligned", "signature_match": True, "observed_signature": observed_signature})
