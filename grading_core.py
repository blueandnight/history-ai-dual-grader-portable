"""Scoring arithmetic extracted from the accepted mainline; no application state."""
from __future__ import annotations
import hashlib
import json
import math
import re
from typing import Any


def rubric_fingerprint(sub_questions: list[dict[str, Any]]) -> str:
    """Hash the complete ordered scoring rubric without involving secrets."""
    canonical = json.dumps(
        sub_questions,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def subjective_version_evidence(assignment, source_questions, scoring_questions):
    """Freeze inputs BEFORE grading; never reconstruct historical evidence on reads."""
    content = {
        "version": 1,
        "assignment_question": assignment["question"],
        "assignment_standard_answer": assignment["standard_answer"],
        "source_sub_questions": source_questions,
        "scoring_sub_questions": scoring_questions,
        "rubric_hash": rubric_fingerprint(scoring_questions),
    }
    content = json.loads(json.dumps(content, ensure_ascii=False, allow_nan=False))
    return {**content, "content_hash": rubric_fingerprint([content])}


def _version_question_key(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 1 and text[0].upper() == "Q" and text[1].isdigit():
        return text[1:]
    return text


def subjective_version_state(
    evidence: Any,
    *,
    assignment_question: Any,
    assignment_standard_answer: Any,
    source_questions: Any,
    scoring_questions: Any,
    graded_question_ids: Any,
    scoped_max_score: Any = None,
    grading_context: Any = None,
    dual_review: Any = None,
) -> str | None:
    """Validate recorded version evidence against current authoritative content.

    The stored hashes are only accepted after they are re-derived from the
    actual content currently attached to the assignment.  A separate current
    grading-question projection (same clean/scope steps as grading) is required
    so the teacher-final path cannot compare two stale hash strings.
    """
    if not isinstance(evidence, dict) or evidence.get("version") != 1:
        return "subjective_version_unverifiable"
    try:
        content = {key: value for key, value in evidence.items() if key != "content_hash"}
        if evidence.get("content_hash") != rubric_fingerprint([content]):
            return "subjective_version_unverifiable"
        historical = evidence.get("scoring_sub_questions")
        source = evidence.get("source_sub_questions")
        if (
            not isinstance(source, list)
            or not isinstance(historical, list)
            or not historical
            or not isinstance(source_questions, list)
            or not isinstance(scoring_questions, list)
            or not scoring_questions
        ):
            return "subjective_version_unverifiable"
        digest = rubric_fingerprint(historical)
        if evidence.get("rubric_hash") != digest:
            return "subjective_version_unverifiable"

        # Recompute from the current content instead of trusting a second stored hash.
        current_digest = rubric_fingerprint(scoring_questions)
        if digest != current_digest or historical != scoring_questions:
            return "subjective_content_version_mismatch"
        if source != source_questions or rubric_fingerprint(source) != rubric_fingerprint(source_questions):
            return "subjective_content_version_mismatch"
        if (
            evidence.get("assignment_question") != assignment_question
            or evidence.get("assignment_standard_answer") != assignment_standard_answer
        ):
            return "subjective_content_version_mismatch"

        if not isinstance(graded_question_ids, list) or not graded_question_ids:
            return "subjective_version_unverifiable"
        requested_ids = [_version_question_key(value) for value in graded_question_ids]
        snapshot_ids = [_version_question_key(item.get("id")) for item in historical if isinstance(item, dict)]
        current_ids = [_version_question_key(item.get("id")) for item in scoring_questions if isinstance(item, dict)]
        if (
            len(set(requested_ids)) != len(requested_ids)
            or len(snapshot_ids) != len(historical)
            or len(current_ids) != len(scoring_questions)
            or len(set(snapshot_ids)) != len(snapshot_ids)
        ):
            return "subjective_version_unverifiable"
        if snapshot_ids != current_ids or set(snapshot_ids) != set(requested_ids):
            return "subjective_content_version_mismatch"

        if scoped_max_score is not None:
            current_max = total_sub_questions(scoring_questions)
            try:
                expected_max = float(scoped_max_score)
            except (TypeError, ValueError):
                return "subjective_version_unverifiable"
            if not math.isfinite(expected_max) or abs(current_max - expected_max) > 1e-8:
                return "subjective_content_version_mismatch"

        if not isinstance(grading_context, dict) or grading_context.get("rubric_sha256") != digest:
            return "subjective_version_unverifiable"
        if isinstance(dual_review, dict) and dual_review.get("rubric_hash") != digest:
            return "subjective_version_unverifiable"
        return None
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return "subjective_version_unverifiable"


def total_score(criteria: list[dict[str, Any]]) -> float:
    total = sum(float(x["score"]) for x in criteria)
    if not math.isfinite(total):
        raise ValueError("评分点总分必须是有限数字")
    return total


def total_sub_questions(sub_questions: list[dict[str, Any]]) -> float:
    total = 0.0
    for item in sub_questions:
        raw_max = item.get("max_score")
        if raw_max in (None, ""):
            explicit_max = total_score(item["criteria"])
        else:
            try:
                explicit_max = float(raw_max)
            except (TypeError, ValueError) as exc:
                raise ValueError("小题满分无效") from exc
            if not math.isfinite(explicit_max) or explicit_max <= 0:
                raise ValueError("小题满分必须是有限正数")
        total += explicit_max
    if not math.isfinite(total):
        raise ValueError("题目总分必须是有限数字")
    return total


def capped_point_credit(claimed: float, maximum: float, accumulated: float) -> float:
    """Shared, existing per-question remaining-credit rule."""
    return min(claimed, max(0.0, maximum - accumulated))


def teacher_deterministic_score(sub_questions, point_decisions):
    """Score explicit teacher judgments against the saved rubric, never AI scores."""
    if any(q.get('scoring_mode')=='holistic' for q in sub_questions):
        raise ValueError('分档题请使用快捷总分终评，不生成逐点教师成绩')
    from auto_crop_double_review_poc import require_consistent_point_bank_values
    for question in sub_questions:
        require_consistent_point_bank_values(question)
    expected = {(str(q['id']), str(c['id'])) for q in sub_questions for c in q['criteria']}
    decisions = {(str(d['question_id']), str(d['rubric_point_id'])): d for d in point_decisions}
    if len(decisions) != len(point_decisions) or set(decisions) - expected:
        raise ValueError('教师终评包含重复或未知评分点')
    pending = sum(decisions.get(key, {}).get('decision') not in {'full', 'none'} for key in expected)
    if not expected or pending:
        raise ValueError(f'teacher_review_incomplete：存在未确定评分点（{pending}个），请完成给分/不给分判断')
    subtotals = []
    for q in sub_questions:
        maximum = float(q['max_score'])
        if not math.isfinite(maximum) or maximum <= 0:
            raise ValueError('教师终评的题目满分无效')
        subtotal = raw = 0.0
        for c in q['criteria']:
            value = float(c['score'])
            if not math.isfinite(value) or value < 0:
                raise ValueError('教师终评的评分点分值无效')
            claimed = value if decisions[(str(q['id']), str(c['id']))]['decision'] == 'full' else 0.0
            raw += claimed
            subtotal += capped_point_credit(claimed, maximum, subtotal)
        subtotals.append({'question_id': q['id'], 'raw_total': raw, 'subtotal': subtotal, 'max_score': maximum})
    return {'question_subtotals': subtotals, 'final_total': sum(q['subtotal'] for q in subtotals),
            'score_source': 'teacher_points_deterministic_v1'}



def normalize_student_no(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip())
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    return text


def normalize_student_identity(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    try:
        confidence = float(raw.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "student_no": normalize_student_no(raw.get("student_no") or raw.get("学号")),
        "student_name": str(raw.get("student_name") or raw.get("name") or raw.get("姓名") or "").strip(),
        "confidence": max(0.0, min(1.0, confidence)) if math.isfinite(confidence) else 0.0,
        "evidence": str(raw.get("evidence") or "").strip(),
    }


def normalize_evidence_box(raw: Any) -> dict[str, Any] | None:
    """Accept only normalized [0,1] boxes tied to a source photo page."""
    if not isinstance(raw, dict):
        return None
    box = raw.get("box") if isinstance(raw.get("box"), (list, tuple)) else raw
    if isinstance(box, (list, tuple)) and len(box) == 4:
        values = box
        keys = ("x", "y", "width", "height")
    else:
        values = [raw.get(key) for key in ("x", "y", "width", "height")]
        keys = ("x", "y", "width", "height")
    try:
        x, y, width, height = (float(value) for value in values)
        photo_index = int(raw.get("photo_index", raw.get("page", raw.get("image_index", 1))))
    except (TypeError, ValueError):
        return None
    if photo_index < 1 or not all(math.isfinite(value) for value in (x, y, width, height)):
        return None
    if not all(0 <= value <= 1 for value in (x, y, width, height)):
        return None
    if width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        return None
    return {
        "photo_index": photo_index,
        "x": round(x, 6),
        "y": round(y, 6),
        "width": round(width, 6),
        "height": round(height, 6),
        "coordinate_system": "normalized",
        "source": "model",
    }


def normalize_model_result(
    result: dict[str, Any],
    criteria: list[dict[str, Any]],
    max_total: float | None = None,
    scoring_mode: str = "weighted",
    *,
    evidence_rubric_echo_suspected,
) -> dict[str, Any]:
    block_reasons: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    for item in criteria:
        criterion_id = str(item.get("id", "")).strip()
        if not criterion_id or criterion_id in by_id:
            block_reasons.add("保存的评分细则包含空缺或重复评分点 ID")
            continue
        try:
            maximum = float(item.get("score"))
        except (TypeError, ValueError):
            maximum = float("nan")
        if not math.isfinite(maximum) or maximum <= 0:
            block_reasons.add("保存的评分细则包含无效分值")
            continue
        by_id[criterion_id] = item
    incoming = result.get("criteria")
    if not isinstance(incoming, list):
        block_reasons.add("模型返回的评分点列表格式无效")
        incoming = []
    if max_total is None:
        max_total = total_score(criteria)
    try:
        max_total = float(max_total)
    except (TypeError, ValueError):
        max_total = total_score(criteria)
        block_reasons.add("本题满分无效")
    if not math.isfinite(max_total) or max_total < 0:
        max_total = total_score(criteria)
        block_reasons.add("本题满分无效")
    scoring_mode = str(scoring_mode or "weighted").casefold()
    by_incoming_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for source in incoming:
        if not isinstance(source, dict):
            block_reasons.add("模型返回了格式无效的评分点")
            continue
        criterion_id = str(source.get("criterion_id", "")).strip()
        if not criterion_id:
            block_reasons.add("模型返回的评分点缺少稳定 ID")
            continue
        if criterion_id not in by_id:
            block_reasons.add("模型返回了未知评分点 ID")
            continue
        if criterion_id in by_incoming_id:
            duplicate_ids.add(criterion_id)
            block_reasons.add("模型对同一评分点返回了重复结果")
            continue
        by_incoming_id[criterion_id] = source

    normalized: list[dict[str, Any]] = []
    total = 0.0
    for item in criteria:
        cid = str(item.get("id", "")).strip()
        if cid not in by_id:
            continue
        maximum = float(by_id[cid]["score"])
        source = by_incoming_id.get(cid)
        if source is None:
            block_reasons.add("模型未返回全部评分点")
            normalized.append(
                {
                    "criterion_id": cid,
                    "criterion": by_id[cid]["label"],
                    "max_score": maximum,
                    "claimed_score": 0.0,
                    "suggested_score": 0.0,
                    "credit_status": "not_credited",
                    "decision": "missing",
                    "student_evidence": "",
                    "reason": "模型未返回该评分点的有效证据。",
                    "needs_review": True,
                    "evidence_missing": True,
                    "evidence_box_missing": True,
                    "evidence": None,
                }
            )
            continue

        evidence = str(source.get("student_evidence", "")).strip()
        rubric_echo_suspected = evidence_rubric_echo_suspected(evidence, by_id[cid])
        evidence_box = normalize_evidence_box(source.get("evidence") or source.get("evidence_box"))
        decision_text = str(source.get("decision") or "").strip().casefold()
        if cid in duplicate_ids:
            block_reasons.add("模型对同一评分点返回了冲突结果")
            raw_score = 0.0
            decision_text = "conflict"
            source_reason = "模型对该评分点返回重复结果，已隔离分值并要求人工复核。"
        else:
            try:
                raw_score = float(source.get("suggested_score", 0))
            except (TypeError, ValueError):
                raw_score = float("nan")
            source_reason = str(source.get("reason", "")).strip()
        if not math.isfinite(raw_score):
            block_reasons.add("模型返回了非有限评分")
            raw_score = 0.0
        if raw_score < 0 or raw_score > maximum:
            block_reasons.add("模型返回的评分超出评分点范围")
        bounded_score = max(0.0, min(maximum, raw_score))
        remaining = max(0.0, max_total - total)
        decision_valid = decision_text in {"full", "partial", "none", "uncertain"}
        if not decision_valid:
            block_reasons.add("模型返回的评分判定类型无效")
        if decision_text == "uncertain":
            claimed_score = 0.0
            block_reasons.add("模型对评分点证据不确定，需教师复核")
            if not source_reason:
                source_reason = "当前答题区域无法明确支持该评分点，等待教师核对。"
        elif scoring_mode == "point_bank":
            # In a capped point bank, decision describes whether the whole
            # answer point was present; suggested_score may be only the final
            # remaining fraction (or zero after the cap was already reached).
            if decision_text == "full":
                claimed_score = maximum
            elif decision_text == "partial" and 0 < remaining < maximum and bounded_score == remaining:
                claimed_score = maximum
            else:
                claimed_score = 0.0
                if decision_text == "partial":
                    block_reasons.add("按点计分结果只能在满分封顶时计入剩余分值")
                elif decision_text == "none" and bounded_score != 0:
                    block_reasons.add("模型的判定类型与评分数值冲突")
        else:
            claimed_score = bounded_score
            if decision_text == "none" and bounded_score != 0:
                block_reasons.add("模型的判定类型与评分数值冲突")
            elif decision_text == "full" and bounded_score < maximum:
                block_reasons.add("模型的判定类型与评分数值冲突")
            elif decision_text == "partial" and not (0 < bounded_score < maximum):
                block_reasons.add("模型的判定类型与评分数值冲突")

        declared_review = bool(source.get("needs_review", False))
        if declared_review:
            block_reasons.add("模型标记了需要人工复核的评分点")
        evidence_missing = claimed_score > 0 and not evidence
        evidence_box_missing = claimed_score > 0 and evidence_box is None
        if evidence_missing:
            block_reasons.add("有得分评分点缺少学生原文证据")
        if evidence_box_missing:
            block_reasons.add("有得分评分点缺少有效证据框")

        awarded = capped_point_credit(claimed_score, max_total, total)
        capped = claimed_score > awarded
        total += awarded
        reason = source_reason
        if capped:
            reason = "该要点虽有作答，但本小题已达到满分上限，不再加分。"
        normalized.append(
            {
                "criterion_id": cid,
                "criterion": by_id[cid]["label"],
                "max_score": maximum,
                "claimed_score": claimed_score,
                "suggested_score": awarded,
                "credit_status": "capped" if capped else "credited" if awarded > 0 else "not_credited",
                "decision": decision_text or "invalid",
                "student_evidence": evidence,
                "reason": reason,
                "rubric_echo_suspected": rubric_echo_suspected,
                "review_reasons": ["rubric_echo_suspected"] if rubric_echo_suspected else [],
                "needs_review": declared_review or decision_text == "uncertain" or evidence_missing or evidence_box_missing or cid in duplicate_ids,
                "evidence_missing": evidence_missing,
                "evidence_box_missing": evidence_box_missing,
                "evidence": evidence_box,
            }
        )

    evidence_owners: dict[str, dict[str, Any]] = {}
    if scoring_mode == "point_bank":
        for item in normalized:
            evidence_key = re.sub(r"[\W_]+", "", str(item.get("student_evidence", "")).casefold())
            if item.get("claimed_score", 0) <= 0 or not evidence_key:
                continue
            if evidence_key in evidence_owners:
                item["suggested_score"] = 0.0
                item["claimed_score"] = 0.0
                item["credit_status"] = "duplicate_evidence"
                item["needs_review"] = True
                item["reason"] = "与另一得分点引用了相同学生原文，不能重复计分。"
                block_reasons.add("不同评分点重复引用相同学生原文，需核验是否为独立要点")
            else:
                evidence_owners[evidence_key] = item
        total = sum(float(item["suggested_score"]) for item in normalized)

    confidence = result.get("confidence", 0)
    try:
        confidence = float(confidence or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if not math.isfinite(confidence):
        confidence = 0.0
    return {
        "mode": "vision-model",
        "suggested_total": total,
        "max_total": max_total,
        "scoring_mode": scoring_mode,
        "confidence": max(0.0, min(1.0, confidence)),
        "needs_review": True,
        "blocked": bool(block_reasons),
        "block_reasons": sorted(block_reasons),
        "summary": str(result.get("summary", "请教师核对原图后确认。")),
        "criteria": normalized,
    }


def normalize_sub_question_result(
    result: dict[str, Any], sub_questions: list[dict[str, Any]], photo_count: int | None = None,
    *, normalize_model_result,
) -> dict[str, Any]:
    from auto_crop_double_review_poc import require_consistent_point_bank_values
    for question in sub_questions:
        require_consistent_point_bank_values(question)
    incoming = result.get("sub_questions")
    if not isinstance(incoming, list):
        incoming = []
        id_errors = {"模型返回的小题列表格式无效或缺少稳定小题 ID"}
    else:
        id_errors: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    expected_ids = {str(item["id"]) for item in sub_questions}
    for item in incoming:
        if not isinstance(item, dict):
            id_errors.add("模型返回了格式无效的小题")
            continue
        key = str(item.get("sub_question_id") or item.get("id") or "").strip()
        if not key:
            id_errors.add("模型返回的小题缺少稳定 ID")
            continue
        if key not in expected_ids:
            id_errors.add("模型返回了未知小题 ID")
            continue
        if key in by_id:
            duplicate_ids.add(key)
            id_errors.add("模型对同一小题返回了重复结果")
            continue
        by_id[key] = item
    missing_ids = expected_ids - set(by_id)
    sub_results = []
    total = 0.0
    for sub_question in sub_questions:
        source = by_id.get(sub_question["id"], {})
        if sub_question["id"] in duplicate_ids:
            source = {"criteria": []}
        raw_status = str(source.get("answer_status", "answered")).strip().casefold()
        status_aliases = {
            "blank": "unanswered_candidate",
            "unanswered": "unanswered_candidate",
            "unreadable": "image_unreadable",
            "unmatched": "question_unmatched",
        }
        answer_status = status_aliases.get(raw_status, raw_status)
        if sub_question["id"] in missing_ids:
            answer_status = "question_unmatched"
        if answer_status not in {"answered", "unanswered_candidate", "question_unmatched", "image_unreadable"}:
            id_errors.add("模型返回了未知的作答状态")
            answer_status = "question_unmatched"
        if answer_status == "answered":
            normalized = normalize_model_result(
                source,
                sub_question["criteria"],
                max_total=float(sub_question["max_score"]),
                scoring_mode=sub_question.get("scoring_mode", "weighted"),
            )
        else:
            reasons = {
                "unanswered_candidate": "模型判断此小题可能未作答，等待教师确认是否确为空白",
                "question_unmatched": "答案或题号无法可靠对应到此小题，等待教师核对题号",
                "image_unreadable": "对应答卷图像无法可靠读取，等待教师核对或重拍",
            }
            normalized = {
                "mode": "vision-model",
                "suggested_total": None,
                "max_total": float(sub_question["max_score"]),
                "scoring_mode": sub_question.get("scoring_mode", "weighted"),
                "confidence": 0.0,
                "needs_review": True,
                "blocked": True,
                "block_reasons": [reasons[answer_status]],
                "summary": reasons[answer_status],
                "criteria": [],
            }
        if answer_status == "unanswered_candidate":
            from blank_consensus import observation
            normalized["blank_observation"] = observation(source, result)
        if id_errors:
            normalized["block_reasons"] = sorted(set(normalized.get("block_reasons", [])) | id_errors)
            normalized["blocked"] = True
            normalized["needs_review"] = True
        normalized.update(
            {
                "sub_question_id": sub_question["id"],
                "label": sub_question["label"],
                "question": sub_question["question"],
                "point_value": float(sub_question.get("point_value", 2)),
                "answer_status": answer_status,
            }
        )
        if normalized.get("suggested_total") is not None:
            total += float(normalized["suggested_total"])
        sub_results.append(normalized)
    unmapped_answers = []
    raw_unmapped = result.get("unmapped_answers", [])
    unmapped_errors: set[str] = set()
    if not isinstance(raw_unmapped, list) or len(raw_unmapped) > 50:
        raw_unmapped = []
        unmapped_errors.add("模型返回的未匹配答案清单格式无效")
    seen_answer_ids: set[str] = set()
    for index, item in enumerate(raw_unmapped, start=1):
        if not isinstance(item, dict):
            unmapped_errors.add("模型返回了格式无效的未匹配答案")
            continue
        answer_id = str(item.get("answer_id") or f"A{index}").strip()
        evidence = normalize_evidence_box(item.get("evidence"))
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", answer_id)
            or answer_id in seen_answer_ids
            or evidence is None
            or (photo_count is not None and evidence["photo_index"] > photo_count)
        ):
            unmapped_errors.add("未匹配答案缺少唯一编号或有效图片位置")
            continue
        seen_answer_ids.add(answer_id)
        unmapped_answers.append({
            "answer_id": answer_id,
            "photo_index": evidence["photo_index"],
            "evidence": evidence,
            "reason": "答案片段尚未可靠关联到已设置的小题",
        })
    if unmapped_errors:
        id_errors.update(unmapped_errors)
        for normalized in sub_results:
            normalized["block_reasons"] = sorted(set(normalized.get("block_reasons", [])) | unmapped_errors)
            normalized["blocked"] = True
            normalized["needs_review"] = True
    confidence = result.get("confidence", 0)
    try:
        confidence = float(confidence or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if not math.isfinite(confidence):
        confidence = 0.0
    block_reasons = sorted(
        id_errors | {
            reason
            for item in sub_results
            for reason in item.get("block_reasons", [])
        }
    )
    if missing_ids:
        block_reasons = sorted(set(block_reasons) | {"有小题未能从答卷中可靠识别或对应，需人工核对题号"})
    if unmapped_answers:
        block_reasons = sorted(set(block_reasons) | {"存在未匹配答案片段，需教师指定题号后重新处理"})
    return {
        "mode": "vision-model",
        "suggested_total": total,
        "max_total": total_sub_questions(sub_questions),
        "confidence": max(0.0, min(1.0, confidence)),
        "needs_review": True,
        "blocked": bool(block_reasons),
        "block_reasons": block_reasons,
        "requires_question_mapping": bool(unmapped_answers) or any(
            item.get("answer_status") == "question_unmatched" for item in sub_results
        ),
        "unmapped_answers": unmapped_answers,
        "summary": str(result.get("summary", "请教师核对原图后确认。")),
        "student_identity": normalize_student_identity(result.get("student_identity")),
        "sub_questions": sub_results,
    }
