"""Small adapter for binding an existing layout profile to an imported exam.

The persisted object remains the shared ``layout_profile`` contract. This module
adds exam/PDF provenance and page numbers to both subjective answer regions and
objective answer-row regions (handwritten A/B/C/D table cells), so one exam can
configure the whole answer sheet on the same page set.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any


def _canonical_objective_id(value: Any) -> str:
    """Return ``Q<n>`` for an objective question id, or '' if it is not numeric."""
    match = re.fullmatch(r"Q?(\d+)", str(value).strip(), re.IGNORECASE)
    return f"Q{int(match.group(1))}" if match else ""


def _objective_display(value: Any) -> str:
    match = re.fullmatch(r"Q(\d+)", str(value).strip(), re.IGNORECASE)
    return match.group(1) if match else str(value).strip()


def _objective_sort_key(value: str) -> int:
    match = re.fullmatch(r"Q(\d+)", value, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _region_geometry(
    incoming: dict[str, Any],
    page_map: dict[int, dict[str, Any]],
    label: str,
) -> tuple[int, dict[str, float]]:
    try:
        page_number = int(incoming.get("page_number"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须选择 PDF 页码") from exc
    if page_number not in page_map:
        raise ValueError(f"{label} 只能绑定到 PDF 试卷页")
    bbox = incoming.get("bbox")
    if not isinstance(bbox, dict):
        raise ValueError(f"{label} 的答题框无效")
    try:
        values = {key: float(bbox[key]) for key in ("x1", "y1", "x2", "y2")}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} 的答题框缺少归一化坐标") from exc
    if (
        not all(math.isfinite(value) for value in values.values())
        or values["x1"] < 0 or values["y1"] < 0
        or values["x2"] > 1 or values["y2"] > 1
        or values["x2"] <= values["x1"] or values["y2"] <= values["y1"]
    ):
        raise ValueError(f"{label} 的答题框必须是 0～1 范围内的有效矩形")
    return page_number, values


def layout_profile_status(
    required_question_ids: list[str],
    profile: dict[str, Any] | None,
    objective_question_ids: list[str] | None = None,
) -> dict[str, Any]:
    required = list(dict.fromkeys(str(item).strip() for item in required_question_ids if str(item).strip()))
    configured = {
        str(region.get("question_id") or "").strip()
        for region in (profile or {}).get("regions", [])
        if isinstance(region, dict) and region.get("region_type") == "subjective_answer"
    }
    configured.intersection_update(required)
    count, total = len(configured), len(required)
    status = "complete" if count == total else "missing" if count == 0 else "partial"

    objective_keys = list(dict.fromkeys(
        key for key in (_canonical_objective_id(value) for value in (objective_question_ids or [])) if key
    ))
    objective_configured: set[str] = set()
    for region in (profile or {}).get("regions", []):
        if not isinstance(region, dict) or region.get("region_type") == "subjective_answer":
            continue
        mapping = region.get("question_mapping")
        if isinstance(mapping, list):
            objective_configured.update(
                key for key in (_canonical_objective_id(value) for value in mapping) if key
            )
    objective_configured.intersection_update(objective_keys)
    objective_total = len(objective_keys)
    objective_count = len(objective_configured)
    objective_status = (
        "complete" if objective_count == objective_total
        else "missing" if objective_count == 0
        else "partial"
    ) if objective_total else "not_available"
    return {
        "status": status,
        "configured_count": count,
        "required_count": total,
        "configured_question_ids": sorted(configured),
        "objective_status": objective_status,
        "objective_configured_count": objective_count,
        "objective_required_count": objective_total,
        "objective_configured_question_ids": [
            _objective_display(key) for key in sorted(objective_configured, key=_objective_sort_key)
        ],
    }


def build_exam_layout_profile(
    *,
    exam_id: str,
    assignment_id: str,
    source_pdf_sha256: str,
    references: list[dict[str, Any]],
    required_question_ids: list[str],
    regions: list[dict[str, Any]],
    layout_profile_id: str,
    created_at: str,
    updated_at: str,
    objective_question_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build and validate the shared layout-profile shape with exam provenance.

    ``regions`` may mix:
    - ``subjective_answer``: one box per subjective sub-question;
    - ``handwritten_abcd``: one row box covering consecutive objective questions
      (``question_mapping`` in left-to-right order, ``rows`` x ``cols`` cells).
    """
    if not references:
        raise ValueError("当前考试没有可用的 PDF 试卷页 reference")
    if len(source_pdf_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in source_pdf_sha256.lower()):
        raise ValueError("正式考试没有有效的 PDF 来源 SHA-256")

    page_map: dict[int, dict[str, Any]] = {}
    dimensions: set[tuple[int, int]] = set()
    for reference in references:
        try:
            page_number = int(reference["page_number"])
            width = int(reference["reference_width"])
            height = int(reference["reference_height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("PDF reference 页缺少页码或尺寸信息") from exc
        if page_number < 1 or width < 1 or height < 1 or page_number in page_map:
            raise ValueError("PDF reference 页信息无效或重复")
        page_map[page_number] = copy.deepcopy(reference)
        dimensions.add((width, height))
    if len(dimensions) != 1:
        raise ValueError("当前 PDF 各试卷页尺寸不同，无法使用同一版式坐标系")
    reference_width, reference_height = next(iter(dimensions))

    expected_subjective = {str(value).strip() for value in required_question_ids if str(value).strip()}
    expected_objective = {
        key: value for value in (objective_question_ids or [])
        if (key := _canonical_objective_id(value))
    }
    if not isinstance(regions, list):
        raise ValueError("答题区列表格式无效")
    normalized_regions: list[dict[str, Any]] = []
    seen_subjective: set[str] = set()
    seen_objective: set[str] = set()
    seen_region_ids: set[str] = set()
    for index, incoming in enumerate(regions, 1):
        if not isinstance(incoming, dict):
            raise ValueError(f"第 {index} 个答题区格式无效")
        region_type = str(incoming.get("region_type") or "subjective_answer").strip()

        if region_type == "subjective_answer":
            question_id = str(incoming.get("question_id") or "").strip()
            if question_id not in expected_subjective:
                raise ValueError(f"小题 {question_id or index} 不属于本场考试的主观题")
            if question_id in seen_subjective:
                raise ValueError(f"小题 {question_id} 已有重复答题区")
            seen_subjective.add(question_id)
            page_number, values = _region_geometry(incoming, page_map, f"小题 {question_id}")
            region = {
                "region_id": f"subjective-{question_id}",
                "region_type": "subjective_answer",
                "question_id": question_id,
                "page_number": page_number,
                "bbox": values,
            }
        elif region_type == "handwritten_abcd":
            raw_mapping = incoming.get("question_mapping")
            if not isinstance(raw_mapping, list) or not raw_mapping:
                raise ValueError(f"第 {index} 个客观题区域缺少题号映射（question_mapping）")
            mapping: list[str] = []
            for value in raw_mapping:
                key = _canonical_objective_id(value)
                if not key or key not in expected_objective:
                    raise ValueError(f"题号 {str(value).strip() or index} 不属于本场考试的客观题")
                if key in mapping:
                    raise ValueError(f"客观题题号 {_objective_display(key)} 在同一个区域内重复")
                mapping.append(key)
            overlap = seen_objective.intersection(mapping)
            if overlap:
                raise ValueError("客观题题号重复出现在多个区域：" + "、".join(
                    _objective_display(key) for key in sorted(overlap, key=_objective_sort_key)
                ))
            seen_objective.update(mapping)
            try:
                rows = int(incoming.get("rows") or 1)
                cols = int(incoming.get("cols") or len(mapping))
            except (TypeError, ValueError) as exc:
                raise ValueError("客观题区域的行列数无效") from exc
            if rows < 1 or cols < 1 or rows * cols != len(mapping):
                raise ValueError(f"客观题区域行列数必须与题号数量一致（rows*cols = {len(mapping)}）")
            first, last = _objective_display(mapping[0]), _objective_display(mapping[-1])
            page_number, values = _region_geometry(incoming, page_map, f"客观题区域 {first}–{last}")
            region_id = f"objective-{page_number}-{first}-{last}"
            if region_id in seen_region_ids:
                raise ValueError(f"客观题区域 {first}–{last} 重复")
            region = {
                "region_id": region_id,
                "region_type": "handwritten_abcd",
                "page_number": page_number,
                "bbox": values,
                "rows": rows,
                "cols": cols,
                "question_mapping": mapping,
            }
        else:
            raise ValueError("当前仅支持主观题答题区和手写ABCD客观题答案行区域")

        seen_region_ids.add(region["region_id"])
        normalized_regions.append(region)

    if not normalized_regions:
        raise ValueError("没有已设置的答题区")

    profile = {
        "schema_version": "layout-profile-v1",
        "layout_profile_id": layout_profile_id,
        "name": f"exam-{exam_id}-answer-regions",
        "exam_id": exam_id,
        "assignment_id": assignment_id,
        "source_pdf_sha256": source_pdf_sha256.lower(),
        "reference_width": reference_width,
        "reference_height": reference_height,
        "reference_pages": [page_map[number] for number in sorted(page_map)],
        "alignment_config": {"method": "orb_ransac"},
        "regions": normalized_regions,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    # Import lazily: the shared layout-profile module currently also imports CV
    # handlers. Saving metadata should not make those imports run at app startup.
    from layout_profile import validate_layout_profile

    return validate_layout_profile(profile)
