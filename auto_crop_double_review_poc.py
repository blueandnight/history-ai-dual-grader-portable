"""Fail-closed preparation helpers for the local auto-crop double-review POC."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any


APPROVED_PRIVACY_STATUSES = {"pre_anonymized", "redacted", "no_identity_visible"}


class AutoCropReviewGateError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def require_consistent_point_bank_values(question: dict[str, Any]) -> None:
    """A uniform point bank cannot silently replace teacher-saved point weights."""
    mode = str(question.get("scoring_mode", question.get("score_mode", "weighted"))).strip().casefold()
    if mode != "point_bank":
        return
    question_id = str(question.get("id") or "?")
    try:
        point_value = float(question.get("point_value", 2))
    except (TypeError, ValueError):
        point_value = math.nan
    if not math.isfinite(point_value) or point_value <= 0:
        raise AutoCropReviewGateError(f"rubric_point_value_unknown:{question_id}")
    for point in question.get("criteria") or []:
        try:
            value = float(point.get("score"))
        except (AttributeError, TypeError, ValueError):
            value = math.nan
        if not math.isfinite(value) or value != point_value:
            raise AutoCropReviewGateError(
                f"rubric_point_value_mismatch:{question_id}：按得分点封顶的各点分值必须等于每点分值；"
                "请教师核对，或改用按评分项分值累计，不能自动改写评分点"
            )


def rubric_readiness(
    sub_questions: list[dict[str, Any]],
    *,
    teacher_confirmation_status: str,
    teacher_confirmed_at: str | None,
    required_question_ids: list[str],
) -> tuple[bool, list[str]]:
    """Validate the saved grading config; never infer or fill rubric values."""
    reasons: list[str] = []
    if teacher_confirmation_status != "confirmed_by_teacher" or not teacher_confirmed_at:
        reasons.append("teacher_confirmation_missing")
    by_id = {str(item.get("id") or ""): item for item in sub_questions}
    for question_id in required_question_ids:
        question = by_id.get(question_id)
        if not question:
            reasons.append(f"question_missing:{question_id}")
            continue
        if question.get("scoring_mode") == "holistic":
            try:
                from holistic_scoring import ready
                ready(question)
            except (ValueError,TypeError):reasons.append(f"holistic_configuration_required:{question_id}")
            continue
        if question.get("needs_teacher_confirmation") is True:
            reasons.append(f"question_not_confirmed:{question_id}")
        if not str(question.get("question") or "").strip():
            reasons.append(f"question_text_missing:{question_id}")
        if not str(question.get("standard_answer") or "").strip():
            reasons.append(f"reference_answer_missing:{question_id}")
        try:
            require_consistent_point_bank_values(question)
        except AutoCropReviewGateError as exc:
            reasons.append(exc.reason)
        try:
            maximum = float(question.get("max_score"))
        except (TypeError, ValueError):
            maximum = math.nan
        if not math.isfinite(maximum) or maximum <= 0:
            reasons.append(f"max_score_unknown:{question_id}")
        criteria = question.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            reasons.append(f"rubric_missing:{question_id}")
            continue
        point_sum = 0.0
        for index, point in enumerate(criteria, start=1):
            if not isinstance(point, dict) or not str(point.get("id") or "").strip() or not str(point.get("label") or "").strip():
                reasons.append(f"rubric_point_incomplete:{question_id}:{index}")
                continue
            try:
                score = float(point.get("score"))
            except (TypeError, ValueError):
                score = math.nan
            if not math.isfinite(score) or score <= 0:
                reasons.append(f"rubric_point_score_unknown:{question_id}:{index}")
            else:
                point_sum += score
        # The existing point-bank scorer caps awarded points at question maximum.
        # Weighted rubrics still require equality; an undersized bank cannot reach
        # the maximum and remains blocked.
        capped_bank = str(question.get("scoring_mode") or "weighted").strip().casefold() == "point_bank"
        if math.isfinite(maximum) and not math.isclose(point_sum, maximum, rel_tol=0, abs_tol=1e-6) and not (capped_bank and point_sum > maximum):
            reasons.append(f"rubric_sum_mismatch:{question_id}")
    return not reasons, reasons


def prepare_accepted_answer_crops(
    run_manifest: dict[str, Any],
    run_directory: str | Path,
    *,
    exam_id: str,
    layout_profile_id: str,
    source_pdf_sha256: str,
    privacy_status: str,
    sub_questions: list[dict[str, Any]],
    teacher_confirmation_status: str,
    teacher_confirmed_at: str | None,
    required_question_ids: list[str],
) -> dict[str, Any]:
    """Resolve only teacher-accepted padded crops and make crop-only inputs."""
    if privacy_status not in APPROVED_PRIVACY_STATUSES:
        raise AutoCropReviewGateError("privacy_status_unchecked")
    if run_manifest.get("exam_id") != exam_id:
        raise AutoCropReviewGateError("exam_mismatch")
    if run_manifest.get("layout_profile_id") != layout_profile_id:
        raise AutoCropReviewGateError("layout_profile_mismatch")
    if not source_pdf_sha256 or run_manifest.get("source_pdf_sha256") != source_pdf_sha256:
        raise AutoCropReviewGateError("layout_profile_stale")
    if run_manifest.get("status") != "cropped":
        raise AutoCropReviewGateError("alignment_or_crop_not_passed")
    if run_manifest.get("human_review") != "passed":
        raise AutoCropReviewGateError("auto_crop_validation_not_passed")
    padding_validation = run_manifest.get("padding_validation") or {}
    if padding_validation.get("status") != "passed":
        raise AutoCropReviewGateError("crop_padding_not_passed")
    ready, rubric_reasons = rubric_readiness(
        sub_questions,
        teacher_confirmation_status=teacher_confirmation_status,
        teacher_confirmed_at=teacher_confirmed_at,
        required_question_ids=required_question_ids,
    )
    if not ready:
        raise AutoCropReviewGateError("rubric_not_ready:" + ",".join(rubric_reasons))

    requested = set(required_question_ids)
    questions_by_label = {
        str(item.get("label") or ""): str(item.get("id") or "")
        for item in sub_questions
    }
    questions_by_id = {str(item.get("id") or ""): item for item in sub_questions}
    accepted_pages = {
        int(item.get("page_number")): item
        for item in run_manifest.get("accepted_pages", [])
        if isinstance(item, dict) and item.get("page_number") is not None
    }
    manifest_crops = {
        str(item.get("question_id") or ""): item
        for item in run_manifest.get("crops", [])
        if isinstance(item, dict)
    }
    padding_crops = {
        str(item.get("question_id") or ""): item
        for item in padding_validation.get("crops", [])
        if isinstance(item, dict)
    }
    root = Path(run_directory).resolve()
    photo_paths: list[Path] = []
    photo_meta: list[dict[str, Any]] = []
    answer_regions: list[dict[str, Any]] = []
    crop_provenance: list[dict[str, Any]] = []
    for index, question_id in enumerate(required_question_ids, start=1):
        question = questions_by_id.get(question_id)
        if question is None:
            raise AutoCropReviewGateError(f"question_missing:{question_id}")
        crop = manifest_crops.get(str(question.get("label") or "")) or manifest_crops.get(question_id)
        padding_crop = padding_crops.get(str(question.get("label") or "")) or padding_crops.get(question_id)
        if not crop or not padding_crop:
            raise AutoCropReviewGateError(f"roi_missing:{question_id}")
        if padding_crop.get("visual_status") != "passed":
            raise AutoCropReviewGateError(f"crop_not_accepted:{question_id}")
        try:
            page_number = int(crop.get("page_number"))
        except (TypeError, ValueError):
            raise AutoCropReviewGateError(f"page_missing:{question_id}") from None
        accepted_page = accepted_pages.get(page_number)
        if not accepted_page:
            raise AutoCropReviewGateError(f"page_match_failed:{question_id}")
        metrics = accepted_page.get("alignment_metrics") or {}
        if metrics.get("quality_gate") != "passed":
            raise AutoCropReviewGateError(f"alignment_failed:{question_id}")
        if crop.get("reference_page_hash") != accepted_page.get("reference_page_sha256"):
            raise AutoCropReviewGateError(f"reference_hash_mismatch:{question_id}")
        if not crop.get("configured_bbox") or not crop.get("effective_crop_bbox") or not crop.get("pixel_bbox") or not crop.get("padding"):
            raise AutoCropReviewGateError(f"crop_provenance_incomplete:{question_id}")
        padded_name = str(padding_crop.get("padded_file_name") or crop.get("padding_validation_file_name") or "")
        if not padded_name:
            raise AutoCropReviewGateError(f"crop_missing:{question_id}")
        path = (root / padded_name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise AutoCropReviewGateError(f"crop_missing:{question_id}")
        try:
            from PIL import Image
            with Image.open(path) as image:
                width, height = image.size
                image.verify()
        except Exception as exc:
            raise AutoCropReviewGateError(f"crop_unreadable:{question_id}:{type(exc).__name__}") from exc
        image_bytes = path.read_bytes()
        image_sha256 = hashlib.sha256(image_bytes).hexdigest()
        photo_paths.append(path)
        photo_meta.append({
            "name": path.name,
            "sha256": image_sha256,
            "original_bytes": len(image_bytes),
            "width": width,
            "height": height,
            "ai_redactions": [],
        })
        answer_regions.append({
            "region_id": f"{layout_profile_id}:{question_id}",
            "profile_id": layout_profile_id,
            "photo_index": index,
            "x": 0.0,
            "y": 0.0,
            "width": 1.0,
            "height": 1.0,
            "sub_question_ids": [question_id],
            "coordinate_system": "perspective_corrected_page_normalized",
            "rotation_ccw": 0,
            "invocation_scope": "answer_region",
        })
        crop_provenance.append({
            "exam_id": exam_id,
            "question_id": question_id,
            "page_number": page_number,
            "layout_profile_id": layout_profile_id,
            "source_student_image_hash": run_manifest.get("source_student_image_hash"),
            "reference_page_hash": crop.get("reference_page_hash"),
            "alignment_run_id": crop.get("alignment_run_id"),
            "configured_bbox": crop.get("configured_bbox"),
            "padding": crop.get("padding"),
            "effective_crop_bbox": crop.get("effective_crop_bbox"),
            "pixel_bbox": crop.get("pixel_bbox"),
            "crop_file": padded_name,
            "crop_sha256": image_sha256,
            "crop_width": width,
            "crop_height": height,
            "crop_bytes": len(image_bytes),
            "source_type": "answer_region",
            "privacy_status": privacy_status,
        })
    if len(requested) != len(required_question_ids) or set(crop_provenance[i]["question_id"] for i in range(len(crop_provenance))) != requested:
        raise AutoCropReviewGateError("requested_question_set_mismatch")
    return {
        "photo_paths": photo_paths,
        "photo_meta": photo_meta,
        "answer_regions": answer_regions,
        "crop_provenance": crop_provenance,
        "privacy_status": privacy_status,
        "rubric_readiness": "passed",
        "rubric_question_ids": required_question_ids,
        "privacy_basis": "仅发送经教师确认的17.1/17.2答题区 crop；整页照片和PDF reference不进入模型请求。",
    }
