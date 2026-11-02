"""Small layout-profile contract and shared aligned-page region dispatcher.

Layout profiles say where answers live. Rubrics and answer keys remain separate
grading data. All registered region handlers receive the same aligned image.
"""

from __future__ import annotations

import copy
import io
import math
import re
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from objective.omr import OMRPageResult, recognize_omr_aligned
from objective.recognition import recognize_handwritten_abcd
from page_alignment import PageAlignmentResult, align_page

REGION_TYPES = frozenset({"subjective_answer", "handwritten_abcd", "bubble_omr"})
ANSWER_STATUSES = frozenset({"recognized", "uncertain", "blank", "multiple", "failed"})


def _canonical_question_id(value: Any) -> str:
    raw = str(value).strip()
    if not raw:
        raise ValueError("question_mapping contains an empty id")
    if isinstance(value, int) or re.fullmatch(r"\d+", raw):
        return f"Q{int(raw)}"
    numeric_qid = re.fullmatch(r"Q(\d+)", raw, re.IGNORECASE)
    if numeric_qid:
        return f"Q{int(numeric_qid.group(1))}"
    return raw


def _bbox(raw: Any, label: str) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label}.bbox must be an object")
    try:
        values = {key: float(raw[key]) for key in ("x1", "y1", "x2", "y2")}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label}.bbox needs x1/y1/x2/y2") from exc
    if (
        not all(math.isfinite(value) for value in values.values())
        or values["x1"] < 0 or values["y1"] < 0
        or values["x2"] > 1 or values["y2"] > 1
        or values["x2"] <= values["x1"] or values["y2"] <= values["y1"]
    ):
        raise ValueError(f"{label}.bbox must be a non-empty normalized rectangle")
    return values


def validate_layout_profile(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("layout_profile must be an object")
    profile_id = str(raw.get("layout_profile_id") or "").strip()
    if not profile_id:
        raise ValueError("layout_profile_id is required")
    try:
        reference_width = int(raw.get("reference_width", 0))
        reference_height = int(raw.get("reference_height", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("reference dimensions must be integers") from exc
    if reference_width < 1 or reference_height < 1:
        raise ValueError("reference_width/reference_height are required")
    alignment_config = raw.get("alignment_config")
    if not isinstance(alignment_config, dict) or alignment_config.get("method") not in {"orb_ransac", "fiducial_anchors"}:
        raise ValueError("alignment_config.method must be orb_ransac or fiducial_anchors")
    regions = raw.get("regions")
    if not isinstance(regions, list) or not regions:
        raise ValueError("regions must be a non-empty array")

    seen_region_ids: set[str] = set()
    seen_question_ids: set[str] = set()
    normalized_regions: list[dict[str, Any]] = []
    for index, item in enumerate(regions, 1):
        if not isinstance(item, dict):
            raise ValueError(f"region {index} must be an object")
        region_id = str(item.get("region_id") or "").strip()
        region_type = str(item.get("region_type") or "").strip()
        if not region_id or region_id in seen_region_ids:
            raise ValueError("region_id must be present and unique")
        if region_type not in REGION_TYPES:
            raise ValueError(f"unsupported region_type: {region_type}")
        seen_region_ids.add(region_id)
        normalized = {**item, "region_id": region_id, "region_type": region_type, "bbox": _bbox(item.get("bbox"), region_id)}

        if region_type == "subjective_answer":
            if not str(item.get("question_id") or "").strip():
                raise ValueError(f"{region_id}.question_id is required")
            question_ids = [_canonical_question_id(item["question_id"])]
            normalized["question_id"] = question_ids[0]
        else:
            question_mapping = item.get("question_mapping")
            if not isinstance(question_mapping, list) or not question_mapping:
                raise ValueError(f"{region_id}.question_mapping must be a non-empty array")
            question_ids = [_canonical_question_id(value) for value in question_mapping]
            if len(set(question_ids)) != len(question_ids):
                raise ValueError(f"{region_id}.question_mapping contains duplicates")
            if region_type == "handwritten_abcd":
                try:
                    rows, cols = int(item["rows"]), int(item["cols"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"{region_id} needs positive rows and cols") from exc
                if rows < 1 or cols < 1:
                    raise ValueError(f"{region_id} needs positive rows and cols")
                if len(question_ids) != rows * cols:
                    raise ValueError(f"{region_id}.question_mapping count must equal rows*cols ({rows * cols})")
                normalized["rows"], normalized["cols"] = rows, cols
            else:
                omr_config = item.get("omr_config")
                if not isinstance(omr_config, dict) or not isinstance(omr_config.get("question_cells"), list):
                    raise ValueError(f"{region_id}.omr_config must use the existing OMR template config")
                if len(question_ids) != len(omr_config["question_cells"]):
                    raise ValueError(f"{region_id}.question_mapping count must equal the existing OMR question-cell count")
                if int(omr_config.get("page_width", 0)) != reference_width or int(omr_config.get("page_height", 0)) != reference_height:
                    raise ValueError(f"{region_id}.omr_config page dimensions must match layout profile reference dimensions")
                normalized["omr_config"] = copy.deepcopy(omr_config)

        overlap = seen_question_ids.intersection(question_ids)
        if overlap:
            raise ValueError(f"question ids occur in more than one region: {', '.join(sorted(overlap))}")
        seen_question_ids.update(question_ids)
        normalized_regions.append(normalized)

    return {
        **raw,
        "schema_version": str(raw.get("schema_version") or "layout-profile-v1"),
        "layout_profile_id": profile_id,
        "reference_width": reference_width,
        "reference_height": reference_height,
        "alignment_config": copy.deepcopy(alignment_config),
        "regions": normalized_regions,
    }


def _encode_png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("region crop encoding failed")
    return encoded.tobytes()


def _region_pixels(image: np.ndarray, bbox: dict[str, float]) -> tuple[int, int, int, int]:
    height, width = image.shape[:2]
    x1 = max(0, math.floor(bbox["x1"] * width))
    y1 = max(0, math.floor(bbox["y1"] * height))
    x2 = min(width, math.ceil(bbox["x2"] * width))
    y2 = min(height, math.ceil(bbox["y2"] * height))
    if x2 <= x1 or y2 <= y1:
        raise ValueError("region crop is empty")
    return x1, y1, x2, y2


def crop_subjective_regions_from_aligned_page(
    image: np.ndarray,
    regions: list[dict[str, Any]],
    *,
    safety_padding: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Crop already-aligned subjective regions using the shared profile geometry.

    This deliberately does not realign, resize, grade, or mutate ``image``.
    The pixel conversion is shared with the existing profile handlers.
    """
    if not isinstance(image, np.ndarray) or image.ndim not in {2, 3} or image.size == 0:
        raise ValueError("aligned page image is invalid")
    padding = {"top": 0.0, "bottom": 0.0, "left": 0.0, "right": 0.0}
    if safety_padding is not None:
        for edge in padding:
            value = float(safety_padding.get(edge, 0.0))
            if not math.isfinite(value) or value < 0:
                raise ValueError("safety padding values must be finite and non-negative")
            padding[edge] = value
    crops: list[dict[str, Any]] = []
    for region in regions:
        if not isinstance(region, dict) or region.get("region_type") != "subjective_answer":
            continue
        question_id = str(region.get("question_id") or "").strip()
        if not question_id:
            raise ValueError("subjective region question_id is required")
        configured_bbox = {key: float(region["bbox"][key]) for key in ("x1", "y1", "x2", "y2")}
        effective_crop_bbox = {
            "x1": max(0.0, configured_bbox["x1"] - padding["left"]),
            "y1": max(0.0, configured_bbox["y1"] - padding["top"]),
            "x2": min(1.0, configured_bbox["x2"] + padding["right"]),
            "y2": min(1.0, configured_bbox["y2"] + padding["bottom"]),
        }
        x1, y1, x2, y2 = _region_pixels(image, effective_crop_bbox)
        crop = image[y1:y2, x1:x2]
        if crop.size == 0:
            raise ValueError(f"empty crop for {question_id}")
        crops.append({
            "region_id": str(region.get("region_id") or f"subjective-{question_id}"),
            "question_id": question_id,
            "page_number": int(region["page_number"]),
            "region_type": "subjective_answer",
            # Keep the legacy field as the teacher-configured ROI for existing
            # readers; the effective runtime crop is recorded separately.
            "normalized_bbox": dict(configured_bbox),
            "configured_bbox": configured_bbox,
            "padding": dict(padding),
            "effective_crop_bbox": effective_crop_bbox,
            "pixel_bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
            "image_bytes": _encode_png(crop),
        })
    return crops


def crop_handwritten_cells(image: np.ndarray, region: dict[str, Any]) -> list[dict[str, Any]]:
    """Create row-major table cells from one normalized region; no per-cell setup."""
    x1, y1, x2, y2 = _region_pixels(image, region["bbox"])
    rows, cols = int(region["rows"]), int(region["cols"])
    map_ids = [_canonical_question_id(value) for value in region["question_mapping"]]
    if len(map_ids) != rows * cols:
        raise ValueError("question_mapping count must equal rows*cols")
    cell_width, cell_height = (x2 - x1) / cols, (y2 - y1) / rows
    pad_x = max(0, round(cell_width * float(region.get("cell_padding", 0.025))))
    pad_y = max(0, round(cell_height * float(region.get("cell_padding", 0.04))))
    cells: list[dict[str, Any]] = []
    for index, question_id in enumerate(map_ids):
        row, col = divmod(index, cols)
        left = max(x1, math.floor(x1 + col * cell_width) - pad_x)
        top = max(y1, math.floor(y1 + row * cell_height) - pad_y)
        right = min(x2, math.ceil(x1 + (col + 1) * cell_width) + pad_x)
        bottom = min(y2, math.ceil(y1 + (row + 1) * cell_height) + pad_y)
        crop = image[top:bottom, left:right]
        if crop.size == 0:
            raise ValueError(f"empty table cell for {question_id}")
        cells.append({
            "question_id": question_id,
            "row": row,
            "col": col,
            "cell_bbox": {"x1": left / image.shape[1], "y1": top / image.shape[0], "x2": right / image.shape[1], "y2": bottom / image.shape[0]},
            "image_bytes": _encode_png(crop),
        })
    return cells


def _subjective_handler(image: np.ndarray, region: dict[str, Any]) -> dict[str, Any]:
    # Reuse the existing answer-region crop/encoding implementation so that
    # profile crops preserve its established EXIF and request-image behavior.
    from app import crop_model_image_bytes

    encoded_page = _encode_png(image)
    bbox = region["bbox"]
    crop = crop_model_image_bytes(encoded_page, {
        "x": bbox["x1"], "y": bbox["y1"],
        "width": bbox["x2"] - bbox["x1"], "height": bbox["y2"] - bbox["y1"],
        "rotation_ccw": 0,
    })
    return {"region_id": region["region_id"], "region_type": region["region_type"], "question_id": str(region["question_id"]), "status": "recognized", "crop_image_bytes": crop}


def _handwritten_handler(image: np.ndarray, region: dict[str, Any], *, engine: Any | None = None) -> dict[str, Any]:
    answers: list[dict[str, Any]] = []
    for cell in crop_handwritten_cells(image, region):
        qmatch = re.fullmatch(r"Q(\d+)", cell["question_id"], re.IGNORECASE)
        if not qmatch:
            raise ValueError("handwritten_abcd question ids must be Q1..Q100")
        qno = int(qmatch.group(1))
        detected = recognize_handwritten_abcd(cell["image_bytes"], qno, engine=engine, expected_question_id=qno).get(qno, {})
        raw_state = str(detected.get("state") or "needs_review")
        state = "recognized" if raw_state == "auto" and detected.get("option") in {"A", "B", "C", "D"} else ("multiple" if detected.get("reason") == "duplicate_question_candidate" else "uncertain")
        answers.append({
            "question_id": cell["question_id"],
            "answer": detected.get("option") if state == "recognized" else None,
            "source_type": "handwritten_abcd",
            "status": state,
            "cell_bbox": cell["cell_bbox"],
            "recognition_result": detected,
            "confidence": detected.get("confidence"),
        })
    return {"region_id": region["region_id"], "region_type": region["region_type"], "status": "completed", "answers": answers}


def _omr_handler(image: np.ndarray, region: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(region["omr_config"])
    page_height, page_width = image.shape[:2]
    x1, y1, x2, y2 = _region_pixels(image, region["bbox"])
    crop_width, crop_height = x2 - x1, y2 - y1
    original_radius_norm = float(config["bubble_radius_norm"])
    cropped_cells = []
    for cell in config["question_cells"]:
        cell_copy = copy.deepcopy(cell)
        cy = float(cell["y"]) * page_height
        option_centers = [float(x) * page_width for x in cell["option_x"]]
        radius = page_width * original_radius_norm
        if cy - radius < y1 or cy + radius > y2 or any(x - radius < x1 or x + radius > x2 for x in option_centers):
            return {"region_id": region["region_id"], "region_type": region["region_type"], "status": "failed", "reason": "omr_bubble_outside_region"}
        cell_copy["y"] = (cy - y1) / crop_height
        cell_copy["option_x"] = [(x - x1) / crop_width for x in option_centers]
        cropped_cells.append(cell_copy)
    config["question_cells"] = cropped_cells
    config["page_width"], config["page_height"] = crop_width, crop_height
    config["bubble_radius_norm"] = original_radius_norm * page_width / crop_width
    omr_crop = image[y1:y2, x1:x2]
    result: OMRPageResult = recognize_omr_aligned(
        omr_crop, config,
        {"alignment": "aligned", "method": "shared_page_alignment", "region_id": region["region_id"]},
        signature_image=image,
    )
    local_ids = [int(cell["question"]) for cell in config["question_cells"]]
    mapping = dict(zip(local_ids, [_canonical_question_id(value) for value in region["question_mapping"]]))
    answers: list[dict[str, Any]] = []
    for question_no, recognized in result.answers.items():
        question_id = mapping.get(int(question_no))
        if question_id is None:
            continue
        raw_state = str(recognized.get("state") or "needs_review")
        reason = recognized.get("reason")
        state = "recognized" if raw_state == "auto" and recognized.get("option") in {"A", "B", "C", "D"} else ("multiple" if reason == "multiple_marks" else "blank" if reason == "unanswered" else "uncertain")
        answers.append({"question_id": question_id, "answer": recognized.get("option") if state == "recognized" else None, "source_type": "bubble_omr", "status": state, "recognition_result": recognized, "confidence": None})
    failed = result.diagnostics.get("signature_match") is False or not answers
    return {"region_id": region["region_id"], "region_type": region["region_type"], "status": "failed" if failed else "completed", "answers": answers, "diagnostics": result.diagnostics}


DEFAULT_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "subjective_answer": _subjective_handler,
    "handwritten_abcd": _handwritten_handler,
    "bubble_omr": _omr_handler,
}


def process_layout_profile(
    image: bytes | bytearray | memoryview | np.ndarray,
    raw_profile: Any,
    *,
    reference_image: bytes | bytearray | memoryview | np.ndarray | str | Path | None = None,
    profile_dir: str | Path | None = None,
    handlers: dict[str, Callable[..., dict[str, Any]]] | None = None,
    aligner: Callable[..., PageAlignmentResult] | None = None,
    ocr_engine: Any | None = None,
) -> dict[str, Any]:
    """Align once, then dispatch all profile regions against that same page."""
    profile = validate_layout_profile(raw_profile)
    alignment_config = copy.deepcopy(profile["alignment_config"])
    selected_reference = reference_image
    if selected_reference is None and profile.get("reference_image"):
        path = Path(str(profile["reference_image"]))
        if profile_dir is not None and not path.is_absolute():
            path = Path(profile_dir) / path
        selected_reference = path
    align = aligner or align_page
    aligned = align(image, alignment_config, reference_image=selected_reference)
    if aligned.status != "aligned" or aligned.image is None:
        return {
            "layout_profile_id": profile["layout_profile_id"],
            "status": "manual_review_required",
            "alignment_status": "failed",
            "alignment_diagnostics": aligned.diagnostics,
            "regions": [{"region_id": region["region_id"], "region_type": region["region_type"], "status": "failed", "reason": "page_alignment_failed"} for region in profile["regions"]],
            "answers": [],
        }
    aligned_height, aligned_width = aligned.image.shape[:2]
    if aligned_width != profile["reference_width"] or aligned_height != profile["reference_height"]:
        diagnostic = {**aligned.diagnostics, "alignment": "profile_reference_dimensions_mismatch", "aligned_width": aligned_width, "aligned_height": aligned_height, "expected_width": profile["reference_width"], "expected_height": profile["reference_height"]}
        return {
            "layout_profile_id": profile["layout_profile_id"],
            "status": "manual_review_required",
            "alignment_status": "failed",
            "alignment_diagnostics": diagnostic,
            "regions": [{"region_id": region["region_id"], "region_type": region["region_type"], "status": "failed", "reason": "page_alignment_dimensions_mismatch"} for region in profile["regions"]],
            "answers": [],
        }

    registry = {**DEFAULT_HANDLERS, **(handlers or {})}
    region_results: list[dict[str, Any]] = []
    objective_answers: list[dict[str, Any]] = []
    for region in profile["regions"]:
        try:
            handler = registry[region["region_type"]]
            if region["region_type"] == "handwritten_abcd":
                result = handler(aligned.image, region, engine=ocr_engine)
            else:
                result = handler(aligned.image, region)
            region_results.append(result)
            objective_answers.extend(result.get("answers", []))
        except Exception as exc:
            region_results.append({"region_id": region["region_id"], "region_type": region["region_type"], "status": "failed", "reason": "region_processing_failed", "error_type": type(exc).__name__})

    failed = any(result.get("status") == "failed" for result in region_results)
    return {
        "layout_profile_id": profile["layout_profile_id"],
        "status": "partial_processing" if failed else "completed",
        "alignment_status": "aligned",
        "alignment_diagnostics": aligned.diagnostics,
        "regions": region_results,
        "answers": objective_answers,
    }
