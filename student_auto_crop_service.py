"""Local diagnostic flow from a student photo to saved layout-profile crops."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from layout_profile import crop_subjective_regions_from_aligned_page, validate_layout_profile
from page_alignment import PageAlignmentResult, align_page


ReferenceLoader = Callable[[dict[str, Any]], bytes | np.ndarray | None]
PageAligner = Callable[..., PageAlignmentResult]
SUBJECTIVE_CROP_SAFETY_PADDING = {"top": 0.01, "bottom": 0.005, "left": 0.0, "right": 0.0}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _encode_png(image: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("诊断图片保存失败")
    return encoded.tobytes()


def _load_bgr(raw: bytes) -> np.ndarray | None:
    if not raw:
        return None
    return cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)


def _safe_status(status: str, **details: Any) -> dict[str, Any]:
    return {"status": status, "crops": [], "artifacts": [], **details}


def _normalized_metrics(result: PageAlignmentResult) -> dict[str, Any]:
    diagnostics = result.diagnostics or {}
    return {
        "quality_gate": "passed" if result.status == "aligned" and result.image is not None else "failed",
        "alignment_status": result.status,
        "failure_reason": None if result.status == "aligned" else diagnostics.get("alignment", "alignment_failed"),
        "keypoints_input": diagnostics.get("source_keypoints"),
        "keypoints_reference": diagnostics.get("reference_keypoints"),
        "good_matches": diagnostics.get("good_matches"),
        "inliers": diagnostics.get("inlier_count"),
        "inlier_ratio": diagnostics.get("inlier_ratio"),
        "reference_coverage": diagnostics.get("inlier_page_coverage"),
        "median_reprojection_error_norm": diagnostics.get("median_reprojection_error_norm"),
        "p95_reprojection_error_norm": diagnostics.get("p95_reprojection_error_norm"),
        "diagnostics": diagnostics,
    }


def _dominates(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Return whether one passed match is strictly better on all gate metrics."""
    first_metrics = first["metrics"]
    second_metrics = second["metrics"]
    greater = ("inliers", "inlier_ratio", "reference_coverage")
    lower = ("p95_reprojection_error_norm",)
    if any(first_metrics.get(key) is None or second_metrics.get(key) is None for key in (*greater, *lower)):
        return False
    no_worse = all(float(first_metrics[key]) >= float(second_metrics[key]) for key in greater)
    no_worse = no_worse and all(float(first_metrics[key]) <= float(second_metrics[key]) for key in lower)
    strictly_better = any(float(first_metrics[key]) > float(second_metrics[key]) for key in greater)
    strictly_better = strictly_better or any(float(first_metrics[key]) < float(second_metrics[key]) for key in lower)
    return no_worse and strictly_better


def _select_reference(matches: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    passed = [item for item in matches if item["metrics"]["quality_gate"] == "passed"]
    if not passed:
        return None, "alignment_failed"
    if len(passed) == 1:
        return passed[0], "page_matched"
    winners = [item for item in passed if all(item is other or _dominates(item, other) for other in passed)]
    if len(winners) == 1:
        return winners[0], "page_matched"
    return None, "page_match_ambiguous"


def _draw_profile_overlay(image: np.ndarray, regions: list[dict[str, Any]]) -> np.ndarray:
    overlay = image.copy()
    height, width = image.shape[:2]
    for region in regions:
        bbox = region["bbox"]
        x1, y1 = round(float(bbox["x1"]) * width), round(float(bbox["y1"]) * height)
        x2, y2 = round(float(bbox["x2"]) * width), round(float(bbox["y2"]) * height)
        cv2.rectangle(overlay, (x1, y1), (x2, y2), (32, 92, 220), 3)
        label = str(region.get("question_id") or "")
        if label:
            cv2.rectangle(overlay, (x1, max(0, y1 - 30)), (x1 + 94, y1), (32, 92, 220), -1)
            cv2.putText(overlay, label, (x1 + 5, max(19, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2, cv2.LINE_AA)
    return overlay


def _draw_bbox_overlay(image: np.ndarray, crop_items: list[dict[str, Any]], bbox_key: str, color: tuple[int, int, int]) -> np.ndarray:
    overlay = image.copy()
    height, width = image.shape[:2]
    for crop in crop_items:
        bbox = crop[bbox_key]
        x1, y1 = round(float(bbox["x1"]) * width), round(float(bbox["y1"]) * height)
        x2, y2 = round(float(bbox["x2"]) * width), round(float(bbox["y2"]) * height)
        cv2.rectangle(overlay, (x1, y1), (x2, y2), color, 3)
        label = str(crop.get("question_id") or "")
        if label:
            cv2.rectangle(overlay, (x1, max(0, y1 - 30)), (x1 + 94, y1), color, -1)
            cv2.putText(overlay, label, (x1 + 5, max(19, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2, cv2.LINE_AA)
    return overlay


def _padding_validation_text(
    crop_items: list[dict[str, Any]],
    aligned_shape: tuple[int, ...],
    *,
    exam_id: str,
    layout_profile_id: str,
    page_number: int,
    source_hash: str,
    reference_hash: str,
    run_id: str,
) -> str:
    lines = [
        "# Safety padding 验证",
        "",
        "本次只在运行时扩展 subjective_answer crop；教师保存的 layout profile ROI 未修改。",
        "页面匹配、配准和质量门沿用现有结果/实现，本文件不代表人工视觉通过。",
        "",
        f"- exam_id: `{exam_id}`",
        f"- layout_profile_id: `{layout_profile_id}`",
        f"- page_number: `{page_number}`",
        f"- source_student_image_hash: `{source_hash}`",
        f"- reference_page_hash: `{reference_hash}`",
        f"- alignment_run_id: `{run_id}`",
        f"- aligned page size: `{aligned_shape[1]}×{aligned_shape[0]}`",
        "- previous unpadded crop files: preserved in the earlier run artifact",
    ]
    prior_statuses = {"17.1": "failed_visual_review", "17.2": "passed_visual_review"}
    for crop in crop_items:
        question_id = str(crop["question_id"])
        previous_status = prior_statuses.get(question_id, "not_recorded")
        lines.append(f"- previous {question_id} crop: `{previous_status}`")
        lines.append(f"- new {question_id} crop: `pending_teacher_review`")
    lines.extend([
        "- auto_crop_validation: `pending_teacher_review`",
        "",
        "| Question | Configured bbox | Padding (top, bottom, left, right) | Effective crop bbox | Pixel bbox |",
        "|---|---|---|---|---|",
    ])
    for crop in crop_items:
        pad = crop["padding"]
        padding_text = f"top={pad['top']}, bottom={pad['bottom']}, left={pad['left']}, right={pad['right']}"
        lines.append(
            f"| {crop['question_id']} | `{crop['configured_bbox']}` | `{padding_text}` | "
            f"`{crop['effective_crop_bbox']}` | `{crop['pixel_bbox']}` |"
        )
    lines.extend(["", "人工查看所有 crop：确认学生手写内容完整且没有严重混入相邻小题答案。", "代码成功生成文件不等于 crop 通过；需要教师查看 padded crops 后再更新人工验收状态。", ""])
    return "\n".join(lines)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _report_text(run: dict[str, Any]) -> str:
    lines = [
        "# 学生答卷自动定位与裁切 POC",
        "",
        f"- run_id: `{run['alignment_run_id']}`",
        f"- exam_id: `{run['exam_id']}`",
        f"- layout_profile_id: `{run.get('layout_profile_id') or 'none'}`",
        f"- source PDF SHA-256: `{run.get('source_pdf_sha256') or 'none'}`",
        f"- student image SHA-256: `{run.get('source_student_image_hash') or 'none'}`",
        f"- input mode: `{run.get('input_mode') or 'unknown'}`",
        f"- status: `{run['status']}`",
        "- privacy: `local_diagnostic`; model API calls: 0",
        "",
        "## Page candidates and matching metrics",
        "",
        "| Candidate | PDF page | Good matches | Inliers | Inlier ratio | Reference coverage | Gate | Reason |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for candidate in run.get("page_candidates", []):
        for match in candidate.get("matches", []):
            metrics = match["metrics"]
            lines.append(
                f"| {candidate['candidate_id']} | {match['page_number']} | {metrics.get('good_matches', '—')} | "
                f"{metrics.get('inliers', '—')} | {metrics.get('inlier_ratio', '—')} | "
                f"{metrics.get('reference_coverage', '—')} | {metrics['quality_gate']} | {metrics.get('failure_reason') or '—'} |"
            )
    lines.extend(["", "## Accepted pages and crops", ""])
    if run.get("crops"):
        lines.append("| Question | Page | Configured bbox | Padding | Effective crop bbox | Pixel bbox | File |")
        lines.append("|---|---:|---|---|---|---|---|")
        for crop in run["crops"]:
            lines.append(
                f"| {crop['question_id']} | {crop['page_number']} | `{crop.get('configured_bbox', crop['normalized_bbox'])}` | "
                f"`{crop.get('padding', {})}` | `{crop.get('effective_crop_bbox', crop['normalized_bbox'])}` | "
                f"`{crop['pixel_bbox']}` | `{crop['file_name']}` |"
            )
    else:
        lines.append("没有生成 crop。")
    lines.extend([
        "",
        "## Review and limitations",
        "",
        f"- human_review: `{run.get('human_review', 'pending_teacher_review')}`",
        f"- candidate failures: `{run.get('warnings', [])}`",
        "- 页面匹配、配准和裁切均在本机完成；没有 OCR、评分或云端模型调用。",
        "- 原始照片按上传字节原样保存；aligned page 与 crops 是派生产物。",
    ])
    return "\n".join(lines) + "\n"


def run_student_auto_crop(
    *,
    exam_context: dict[str, Any],
    student_image_bytes: bytes,
    input_mode: str,
    reference_loader: ReferenceLoader,
    output_dir: str | Path,
    source_extension: str = ".jpg",
    aligner: PageAligner | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Match photo page candidates to exam references and apply saved ROIs.

    Every candidate/reference pair uses the existing ``page_alignment`` quality
    gate. A passed alignment image is retained and reused for cropping; it is
    never re-aligned and ROI coordinates are never changed.
    """
    exam_id = str(exam_context.get("exam_id") or "")
    profile_raw = exam_context.get("layout_profile")
    if not exam_context.get("layout_profile_id") or not isinstance(profile_raw, dict):
        return _safe_status("layout_profile_missing", exam_id=exam_id)
    if exam_context.get("pdf_hash_mismatch") or exam_context.get("profile_hash_mismatch"):
        return _safe_status("layout_profile_stale", exam_id=exam_id, reason="source_pdf_hash_mismatch")
    if str(profile_raw.get("source_pdf_sha256") or "").lower() != str(exam_context.get("source_pdf_sha256") or "").lower():
        return _safe_status("layout_profile_stale", exam_id=exam_id, reason="profile_pdf_hash_mismatch")
    if not exam_context.get("has_pdf_reference"):
        return _safe_status("reference_asset_missing", exam_id=exam_id, reason="pdf_reference_metadata_missing")
    reference_asset_status = str(exam_context.get("reference_asset_status") or "")
    if reference_asset_status in {
        "reference_asset_missing",
        "reference_asset_corrupt",
        "reference_rebuild_mismatch",
    }:
        return _safe_status(
            reference_asset_status,
            exam_id=exam_id,
            reason=exam_context.get("reference_asset_reason"),
            failures=exam_context.get("reference_asset_failures", []),
        )
    if input_mode not in {"single_page", "double_spread"}:
        return _safe_status("page_match_failed", exam_id=exam_id, reason="unsupported_input_mode")

    try:
        profile = validate_layout_profile(profile_raw)
    except (TypeError, ValueError, KeyError) as exc:
        return _safe_status("layout_profile_missing", exam_id=exam_id, reason=f"invalid_layout_profile:{type(exc).__name__}")
    references = [
        dict(item) for item in exam_context.get("references", [])
        if isinstance(item, dict) and item.get("page_role") in {"exam", "answer_sheet"}
    ]
    if not references:
        return _safe_status("reference_asset_missing", exam_id=exam_id, reason="exam_reference_metadata_missing")

    image = _load_bgr(student_image_bytes)
    if image is None or image.size == 0:
        return _safe_status("page_match_failed", exam_id=exam_id, reason="student_image_decode_failed")
    source_hash = _sha256(student_image_bytes)
    height, width = image.shape[:2]
    if input_mode == "single_page":
        candidates = [{"candidate_id": "single", "bounds": [0, 0, width, height], "image": image.copy()}]
    else:
        midpoint = width // 2
        if midpoint < 1:
            return _safe_status("page_match_failed", exam_id=exam_id, reason="spread_too_narrow")
        candidates = [
            {"candidate_id": "left", "bounds": [0, 0, midpoint, height], "image": image[:, :midpoint].copy()},
            {"candidate_id": "right", "bounds": [midpoint, 0, width, height], "image": image[:, midpoint:].copy()},
        ]

    run_id = run_id or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("alignment_run_id must be a 32-character hex id")
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    ext = source_extension.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".img"
    source_name = f"source_student_image{ext}"
    (out_dir / source_name).write_bytes(student_image_bytes)
    artifacts = [source_name]
    run: dict[str, Any] = {
        "diagnostic_run": True,
        "privacy_status": "local_diagnostic",
        "alignment_run_id": run_id,
        "exam_id": exam_id,
        "assignment_id": exam_context.get("assignment_id"),
        "layout_profile_id": profile["layout_profile_id"],
        "source_pdf_sha256": exam_context.get("source_pdf_sha256"),
        "reference_asset_status": exam_context.get("reference_asset_status"),
        "reference_rebuilt": bool(exam_context.get("reference_rebuilt")),
        "reference_rebuilt_pages": list(exam_context.get("reference_rebuilt_pages", [])),
        "reference_assets": [
            {
                "page_number": int(item["page_number"]),
                "relative_path": str(item.get("image_file") or ""),
                "reference_sha256": str(item.get("reference_sha256") or ""),
                "reference_width": item.get("reference_width"),
                "reference_height": item.get("reference_height"),
                "asset_status": item.get("resolved_asset_status") or item.get("asset_status"),
            }
            for item in exam_context.get("references", [])
            if isinstance(item, dict) and item.get("page_role") in {"exam", "answer_sheet"}
        ],
        "source_student_image_hash": source_hash,
        "source_student_image_sha256_after_save": _sha256((out_dir / source_name).read_bytes()),
        "input_mode": input_mode,
        "status": "uploaded",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "page_candidates": [],
        "accepted_pages": [],
        "crops": [],
        "warnings": [],
        "human_review": "pending_teacher_review",
        "model_api_calls": {"deepseek": 0, "mimo": 0, "qwen": 0},
        "artifacts": artifacts,
    }
    selected_align = aligner or align_page
    references.sort(key=lambda item: int(item["page_number"]))
    resolved_candidates: list[tuple[dict[str, Any], dict[str, Any], np.ndarray]] = []

    for candidate in candidates:
        candidate_name = f"candidate_{candidate['candidate_id']}.png"
        (out_dir / candidate_name).write_bytes(_encode_png(candidate["image"]))
        artifacts.append(candidate_name)
        candidate_result: dict[str, Any] = {
            "candidate_id": candidate["candidate_id"],
            "source_panel_bounds_xyxy": candidate["bounds"],
            "width": int(candidate["image"].shape[1]),
            "height": int(candidate["image"].shape[0]),
            "candidate_sha256": _sha256((out_dir / candidate_name).read_bytes()),
            "matches": [],
            "status": "uploaded",
            "matched_page_number": None,
        }
        private_matches: list[dict[str, Any]] = []
        for reference in references:
            page_number = int(reference["page_number"])
            reference_image = reference_loader(reference)
            if reference_image is None:
                metrics = {"quality_gate": "failed", "alignment_status": "failed", "failure_reason": "reference_image_missing"}
                candidate_result["matches"].append({"page_number": page_number, "reference_page_sha256": reference.get("reference_sha256"), "metrics": metrics})
                continue
            if isinstance(reference_image, bytes):
                reference_digest = _sha256(reference_image)
                decoded_reference = _load_bgr(reference_image)
            else:
                decoded_reference = reference_image
                reference_digest = _sha256(_encode_png(reference_image))
            expected_reference_hash = str(reference.get("reference_sha256") or "").lower()
            if expected_reference_hash and reference_digest.lower() != expected_reference_hash:
                metrics = {"quality_gate": "failed", "alignment_status": "failed", "failure_reason": "reference_page_hash_mismatch"}
                candidate_result["matches"].append({"page_number": page_number, "reference_page_sha256": expected_reference_hash, "metrics": metrics})
                continue
            result = selected_align(candidate["image"], profile["alignment_config"], reference_image=decoded_reference)
            metrics = _normalized_metrics(result)
            candidate_result["matches"].append({
                "page_number": page_number,
                "reference_page_sha256": expected_reference_hash or reference_digest,
                "metrics": metrics,
            })
            private_matches.append({"page_number": page_number, "metrics": metrics, "result": result, "reference": reference})

        chosen, candidate_status = _select_reference(private_matches)
        candidate_result["status"] = candidate_status
        if chosen is not None:
            candidate_result["matched_page_number"] = int(chosen["page_number"])
            resolved_candidates.append((candidate_result, chosen, candidate["image"]))
        run["page_candidates"].append(candidate_result)

    ambiguous = [item for item in run["page_candidates"] if item["status"] == "page_match_ambiguous"]
    if ambiguous:
        run["status"] = "page_match_ambiguous"
        run["warnings"].append("至少一个页面候选匹配到多个无法明确区分的 PDF 试卷页，已停止自动 crop。")
    elif not resolved_candidates:
        run["status"] = "alignment_failed"
        reasons = {
            match.get("metrics", {}).get("failure_reason")
            for candidate in run["page_candidates"] for match in candidate["matches"]
        }
        run["failure_code"] = (
            "page_layout_mismatch" if "page_layout_mismatch" in reasons
            else "insufficient_alignment_evidence"
        )
        run["warnings"].append("所有页面候选均未通过任一试卷页的现有配准质量门；未生成 crop。")
    else:
        pages = [int(candidate_result["matched_page_number"]) for candidate_result, _, _ in resolved_candidates]
        if len(pages) != len(set(pages)):
            run["status"] = "page_match_ambiguous"
            run["warnings"].append("多个照片候选同时匹配到同一 PDF 页，已停止自动 crop。")
        else:
            for candidate_result, chosen, _candidate_image in resolved_candidates:
                page_number = int(chosen["page_number"])
                aligned = chosen["result"].image
                if aligned is None:
                    candidate_result["status"] = "alignment_failed"
                    continue
                candidate_result["status"] = "alignment_passed"
                if chosen["result"].diagnostics.get("alignment_assisted"):
                    run["alignment_assisted"] = True
                    run["warnings"].append("已使用答题卡外框辅助对齐；印发版可能与参考页不同，请核对本批预览中的小题位置和手写内容，可批量验收，异常图片可单独取消。")
                run["accepted_pages"].append({
                    "candidate_id": candidate_result["candidate_id"],
                    "page_number": page_number,
                    "reference_page_sha256": str(chosen["reference"].get("reference_sha256") or ""),
                    "alignment_metrics": chosen["metrics"],
                })
                aligned_name = f"aligned_page_{page_number}.png"
                (out_dir / aligned_name).write_bytes(_encode_png(aligned))
                artifacts.append(aligned_name)
                page_regions = [
                    region for region in profile["regions"]
                    if region.get("region_type") == "subjective_answer"
                    and int(region.get("page_number", 0) or 0) == page_number
                ]
                page_regions.sort(key=lambda region: tuple(
                    int(part) if part.isdigit() else part
                    for part in str(region.get("question_id") or "").split(".")
                ))
                if not page_regions:
                    candidate_result["status"] = "roi_missing"
                    run["warnings"].append(f"PDF 第 {page_number} 页没有已配置的 subjective_answer ROI。")
                    continue
                crop_items = crop_subjective_regions_from_aligned_page(
                    aligned,
                    page_regions,
                    safety_padding=SUBJECTIVE_CROP_SAFETY_PADDING,
                )
                overlay_name = f"roi_overlay_page_{page_number}.png"
                (out_dir / overlay_name).write_bytes(_encode_png(_draw_profile_overlay(aligned, page_regions)))
                artifacts.append(overlay_name)
                padding_dir = out_dir / "padding_validation"
                padding_dir.mkdir(exist_ok=True)
                configured_overlay_name = "padding_validation/configured_roi_overlay.png"
                effective_overlay_name = "padding_validation/effective_crop_overlay.png"
                (out_dir / configured_overlay_name).write_bytes(
                    _encode_png(_draw_bbox_overlay(aligned, crop_items, "configured_bbox", (32, 92, 220)))
                )
                (out_dir / effective_overlay_name).write_bytes(
                    _encode_png(_draw_bbox_overlay(aligned, crop_items, "effective_crop_bbox", (40, 170, 70)))
                )
                artifacts.extend([configured_overlay_name, effective_overlay_name])
                padded_names: list[str] = []
                for crop in crop_items:
                    crop_stem = re.sub(r"[^0-9A-Za-z._-]+", "_", crop["question_id"]).replace(".", "_")
                    crop_name = f"{crop_stem}_crop.png"
                    (out_dir / crop_name).write_bytes(crop["image_bytes"])
                    artifacts.append(crop_name)
                    padded_name = f"padding_validation/{crop_stem}_crop_padded.png"
                    (out_dir / padded_name).write_bytes(crop["image_bytes"])
                    padded_names.append(padded_name)
                    artifacts.append(padded_name)
                    run["crops"].append({
                        "exam_id": exam_id,
                        "question_id": crop["question_id"],
                        "page_number": page_number,
                        "region_type": crop["region_type"],
                        "layout_profile_id": profile["layout_profile_id"],
                        "source_student_image_hash": source_hash,
                        "reference_page_hash": str(chosen["reference"].get("reference_sha256") or ""),
                        "normalized_bbox": crop["normalized_bbox"],
                        "configured_bbox": crop["configured_bbox"],
                        "padding": crop["padding"],
                        "effective_crop_bbox": crop["effective_crop_bbox"],
                        "pixel_bbox": crop["pixel_bbox"],
                        "alignment_run_id": run_id,
                        "file_name": crop_name,
                        "padding_validation_file_name": padded_name,
                    })
                validation_report = "padding_validation/PADDING_VALIDATION.md"
                (out_dir / validation_report).write_text(
                    _padding_validation_text(
                        crop_items,
                        aligned.shape,
                        exam_id=exam_id,
                        layout_profile_id=profile["layout_profile_id"],
                        page_number=page_number,
                        source_hash=source_hash,
                        reference_hash=str(chosen["reference"].get("reference_sha256") or ""),
                        run_id=run_id,
                    ),
                    encoding="utf-8",
                )
                artifacts.append(validation_report)
                run["padding_validation"] = {
                    "status": "pending_teacher_review",
                    "padding": dict(SUBJECTIVE_CROP_SAFETY_PADDING),
                    "configured_roi_unchanged": True,
                    "crops": [
                        {
                            "question_id": crop["question_id"],
                            "configured_bbox": crop["configured_bbox"],
                            "padding": crop["padding"],
                            "effective_crop_bbox": crop["effective_crop_bbox"],
                            "pixel_bbox": crop["pixel_bbox"],
                            "padded_file_name": name,
                            "visual_status": "pending_teacher_review",
                        }
                        for crop, name in zip(crop_items, padded_names)
                    ],
                    "artifacts": [configured_overlay_name, effective_overlay_name, validation_report],
                }
            if run["crops"]:
                run["status"] = "cropped"
                failed_candidates = [item["candidate_id"] for item in run["page_candidates"] if item["status"] in {"alignment_failed", "roi_missing"}]
                run["partial_processing"] = bool(failed_candidates)
                if failed_candidates:
                    run["warnings"].append("未通过配准或未配置 ROI 的页面候选已跳过；只输出通过质量门且有 ROI 的页面 crop。")
            elif run["status"] != "page_match_ambiguous":
                run["status"] = "roi_missing"

    run["artifacts"] = list(dict.fromkeys(artifacts))
    _write_json(out_dir / "run.json", run)
    (out_dir / "AUTO_CROP_REPORT.md").write_text(_report_text(run), encoding="utf-8")
    run["report_file"] = "AUTO_CROP_REPORT.md"
    return run


def record_manual_review(output_dir: str | Path, run_id: str, verdict: str) -> dict[str, Any]:
    """Record explicit teacher review of every displayed crop, not a score."""
    if verdict not in {"passed", "failed"}:
        raise ValueError("review verdict must be passed or failed")
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("alignment_run_id is invalid")
    root = Path(output_dir).resolve()
    folder = (root / run_id).resolve()
    if not folder.is_relative_to(root):
        raise ValueError("review artifact path is invalid")
    manifest_path = folder / "run.json"
    if not manifest_path.is_file():
        raise ValueError("crop run was not found")
    run = json.loads(manifest_path.read_text(encoding="utf-8"))
    if verdict == "passed" and (run.get("status") != "cropped" or not run.get("crops")):
        raise ValueError("只有已通过配准并生成的裁切图才能验收")
    run["human_review"] = verdict
    run["human_reviewed_at"] = datetime.now(timezone.utc).isoformat()
    padding = run.get("padding_validation")
    if isinstance(padding, dict):
        padding["status"] = verdict
        for crop in padding.get("crops", []):
            crop["visual_status"] = verdict
    _write_json(manifest_path, run)
    (folder / "AUTO_CROP_REPORT.md").write_text(_report_text(run), encoding="utf-8")
    return {"alignment_run_id": run_id, "human_review": verdict}
