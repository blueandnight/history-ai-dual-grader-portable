"""AI-assisted exam structure parsing from Word/PDF text (no images sent).

The deterministic importers remain the source of the draft and layout. This
module only adds an optional, teacher-confirmed AI pass that extracts question
numbers, options, answers and subjective reference answers so a different paper
(including a 26-question paper) does not require importer code changes.

Naming rules:
- objective ids are plain integers ("1".."26");
- subjective ids keep the major.part contract ("17.1", "18.2").
"""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any

AI_PARSE_PROMPT_VERSION = "exam-import-ai-parse-v1"
MAX_SOURCE_CHARS = 100_000
MAX_OBJECTIVE_QUESTIONS = 200
MAX_SUBJECTIVE_QUESTIONS = 100
OPTION_KEYS = ("A", "B", "C", "D")


def build_exam_import_ai_source(docx_text: str, pdf_pages: list[str], title: str = "") -> dict[str, Any]:
    """Build the text-only source sent to the model (never page images)."""
    docx_text = str(docx_text or "").strip()
    pdf_pages = [str(page or "").strip() for page in (pdf_pages or [])]
    parts: list[str] = []
    if title:
        parts.append(f"【试卷标题】{str(title).strip()}")
    if docx_text:
        parts.append("【Word 文档文字】\n" + docx_text)
    pdf_pages = [(number, text) for number, text in enumerate(pdf_pages, 1) if text]
    if pdf_pages:
        parts.append("【PDF 分页文字】\n" + "\n\n".join(
            f"【PDF 第 {number} 页】\n{text}" for number, text in pdf_pages
        ))
    source_text = "\n\n".join(parts).strip()
    if not source_text:
        raise ValueError("Word/PDF 中没有可提取的文字；当前 AI 解析只发送文字，请提供 Word 或可复制文字的 PDF")
    if len(source_text) > MAX_SOURCE_CHARS:
        raise ValueError(f"试卷文字过长（{len(source_text)} 字符），超过 AI 解析上限 {MAX_SOURCE_CHARS}；请拆分或精简后重试")
    labels = []
    if docx_text:
        labels.append("Word")
    if pdf_pages:
        labels.append(f"PDF {len(pdf_pages)} 页")
    return {
        "text": source_text,
        "text_chars": len(source_text),
        "docx_chars": len(docx_text),
        "pdf_chars": sum(len(text) for _, text in pdf_pages),
        "pdf_page_count": len(pdf_pages),
        "source_label": " + ".join(labels) + " 提取文字",
    }


def build_exam_import_ai_payload(source_text: str, model: str, provider: str = "") -> dict[str, Any]:
    """Build the text-only chat payload; provider JSON options are added by app."""
    selected_model = str(model or "").strip()
    if not selected_model:
        raise ValueError("尚未选择用于 AI 解析的模型")
    prompt = (
        "你是中学历史教师的试卷结构抽取助手。下面是从同一份试卷的 Word 文档和/或 PDF 中提取的文字；"
        "【PDF 第 N 页】只是分页标记。\n"
        "只抽取结构，不编造、不改写原文；看不清或缺失的字段留空字符串或 null。\n"
        "任务：\n"
        "1. title：试卷标题；\n"
        "2. objective_questions：客观选择题数组。question_id 为纯数字题号；stem 为题干原文；"
        "options 为 A/B/C/D 四个选项原文的对象；answer 为正确选项字母（从评分参考/答案页核对；无法确定用 null）；\n"
        "3. subjective_questions：主观材料题小题数组。question_id 形如 17.1；major_label 为大题号如 17；"
        "question 为设问原文；reference_answer 为参考答案原文（无法确定用空字符串）；max_score 为该小题满分数字（无法确定用 null）。\n"
        "题号必须来自原文，同一题号只出现一次，按原文顺序。不要返回评分点、解析过程或学生信息。"
        "只返回 JSON 对象，不要 Markdown。格式："
        '{"title":"","objective_questions":[{"question_id":"1","stem":"","options":{"A":"","B":"","C":"","D":""},"answer":"A"}],'
        '"subjective_questions":[{"question_id":"17.1","major_label":"17","question":"","reference_answer":"","max_score":4}]}'
        f"\n试卷文字如下：\n{source_text}"
    )
    payload = {
        "model": selected_model,
        "temperature": 0,
        "max_tokens": 8000,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "你输出的是教师审核用的试卷结构草案，不是最终考试数据。"},
            {"role": "user", "content": prompt},
        ],
    }
    return payload


def _text(value: Any, limit: int = 8000) -> str:
    return str(value or "").strip()[:limit]


def _objective_id(value: Any) -> str:
    match = re.fullmatch(r"\s*(\d{1,3})(?:\.0)?\s*", str(value or ""))
    if not match:
        return ""
    number = int(match.group(1))
    return str(number) if 1 <= number <= 999 else ""


def _subjective_id(value: Any) -> tuple[str, str]:
    match = re.fullmatch(r"\s*(\d{1,3})\s*[.．]\s*(\d{1,2})\s*", str(value or ""))
    if not match:
        return "", ""
    major, part = int(match.group(1)), int(match.group(2))
    if not 1 <= major <= 999 or not 1 <= part <= 99:
        return "", ""
    return f"{major}.{part}", str(major)


def _max_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score) or score <= 0 or score > 200:
        return None
    return round(score, 2)


def normalize_exam_import_ai_result(raw: Any) -> dict[str, Any]:
    """Validate model output; return canonical draft fragments plus issues."""
    if not isinstance(raw, dict):
        raise ValueError("AI 解析结果不是 JSON 对象")
    issues: list[str] = []
    objectives: list[dict[str, Any]] = []
    seen_objective: set[str] = set()
    raw_objectives = raw.get("objective_questions", raw.get("objectives"))
    for index, item in enumerate(raw_objectives if isinstance(raw_objectives, list) else [], 1):
        if len(objectives) >= MAX_OBJECTIVE_QUESTIONS:
            issues.append(f"客观题数量超过上限 {MAX_OBJECTIVE_QUESTIONS}，其余已忽略")
            break
        if not isinstance(item, dict):
            continue
        question_id = _objective_id(item.get("question_id", item.get("id")))
        if not question_id:
            issues.append(f"第 {index} 道客观题题号无效，已忽略")
            continue
        if question_id in seen_objective:
            issues.append(f"客观题题号重复：{question_id}，已保留首次出现")
            continue
        seen_objective.add(question_id)
        options: dict[str, str] = {}
        raw_options = item.get("options")
        if isinstance(raw_options, dict):
            for key, value in raw_options.items():
                option = str(key or "").strip().upper()
                text = _text(value, 500)
                if option in OPTION_KEYS and text:
                    options[option] = text
        answer = _text(item.get("answer", item.get("correct_answer")), 2).upper()
        if answer not in OPTION_KEYS:
            answer = ""
        objectives.append({
            "question_id": question_id,
            "stem": _text(item.get("stem", item.get("question"))),
            "options": options,
            "options_complete": all(options.get(key) for key in OPTION_KEYS),
            "answer": answer,
        })

    subjective: list[dict[str, Any]] = []
    seen_subjective: set[str] = set()
    raw_subjective = raw.get("subjective_questions", raw.get("subjectives"))
    for index, item in enumerate(raw_subjective if isinstance(raw_subjective, list) else [], 1):
        if len(subjective) >= MAX_SUBJECTIVE_QUESTIONS:
            issues.append(f"主观题数量超过上限 {MAX_SUBJECTIVE_QUESTIONS}，其余已忽略")
            break
        if not isinstance(item, dict):
            continue
        question_id, major_label = _subjective_id(item.get("question_id", item.get("id")))
        if not question_id:
            issues.append(f"第 {index} 道主观题小题号无效，已忽略")
            continue
        if question_id in seen_subjective:
            issues.append(f"主观题小题号重复：{question_id}，已保留首次出现")
            continue
        seen_subjective.add(question_id)
        supplied_major = _text(item.get("major_label"), 8)
        if supplied_major and _objective_id(supplied_major) != major_label:
            issues.append(f"主观题 {question_id} 的大题号不一致，已按小题号归入第 {major_label} 题")
        reference_answer = _text(item.get("reference_answer", item.get("answer")))
        subjective.append({
            "question_id": question_id,
            "major_label": major_label,
            "question": _text(item.get("question", item.get("stem"))),
            "reference_answer": reference_answer,
            "max_score": _max_score(item.get("max_score")),
        })

    if not objectives and not subjective:
        raise ValueError("AI 未返回可用的题号/答案结构；请检查模型或改用 Word 导入")
    return {
        "title": _text(raw.get("title"), 200),
        "objective_questions": objectives,
        "subjective_questions": subjective,
        "issues": issues,
    }


def _normalized_answer_compare(left: str, right: str) -> bool:
    return re.sub(r"\s+", "", str(left or "")).lower() == re.sub(r"\s+", "", str(right or "")).lower()


def merge_exam_import_ai_result(draft: dict[str, Any], parsed: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge AI ids/answers into the verified draft without deleting source text."""
    merged = copy.deepcopy(draft)
    warnings = list(merged.get("warnings") or [])
    warning_codes = list(dict.fromkeys(merged.get("warning_codes") or []))

    def warn(message: str, code: str | None = None) -> None:
        warnings.append(message)
        if code and code not in warning_codes:
            warning_codes.append(code)

    existing_objectives = merged.setdefault("objective_questions", [])
    if not isinstance(existing_objectives, list):
        existing_objectives = []
        merged["objective_questions"] = existing_objectives
    objective_by_id = {
        str(item.get("question_id")): item for item in existing_objectives if isinstance(item, dict)
    }
    ai_objective_by_id = {item["question_id"]: item for item in parsed["objective_questions"]}
    added_objectives: list[str] = []
    updated_answers: list[str] = []
    answer_conflicts: list[str] = []
    for question_id, ai in ai_objective_by_id.items():
        target = objective_by_id.get(question_id)
        if target is None:
            target = {
                "question_id": question_id,
                "stem": ai["stem"],
                "options": ai["options"],
                "question_type": "single_choice",
                "answer_found": bool(ai["answer"]),
                "correct_answer_draft": ai["answer"] or None,
                "needs_teacher_review": not (ai["stem"] and ai["options_complete"]),
                "needs_teacher_confirmation": True,
                "ai_parse": True,
            }
            existing_objectives.append(target)
            objective_by_id[question_id] = target
            added_objectives.append(question_id)
            continue
        if not str(target.get("stem") or "").strip() and ai["stem"]:
            target["stem"] = ai["stem"]
        existing_options = target.get("options") if isinstance(target.get("options"), dict) else {}
        if any(not str(existing_options.get(key) or "").strip() for key in OPTION_KEYS) and ai["options_complete"]:
            target["options"] = ai["options"]
        current_answer = str(target.get("correct_answer_draft") or "").strip().upper()
        if ai["answer"]:
            if not current_answer:
                target["correct_answer_draft"] = ai["answer"]
                target["answer_found"] = True
                updated_answers.append(question_id)
            elif current_answer != ai["answer"]:
                answer_conflicts.append(question_id)
        target["needs_teacher_review"] = not (
            str(target.get("stem") or "").strip()
            and all(str((target.get("options") or {}).get(key) or "").strip() for key in OPTION_KEYS)
        )
        target["needs_teacher_confirmation"] = True

    objective_answer_key = dict(merged.get("objective_answer_key") or {})
    for question_id, ai in ai_objective_by_id.items():
        if ai["answer"] and not str(objective_answer_key.get(question_id) or "").strip():
            objective_answer_key[question_id] = ai["answer"]
    merged["objective_answer_key"] = objective_answer_key
    missing_objective_ids = sorted(
        (question_id for question_id in objective_by_id if question_id not in ai_objective_by_id),
        key=lambda value: int(value) if value.isdigit() else 9999,
    )
    if missing_objective_ids:
        warn(
            "ai_parse_missing：AI 未返回以下客观题，已保留原草稿，请人工核对："
            + "、".join(missing_objective_ids[:30])
            + ("……" if len(missing_objective_ids) > 30 else ""),
            "ai_parse_missing",
        )

    material_questions = merged.get("material_questions")
    if not isinstance(material_questions, list):
        material_questions = []
        merged["material_questions"] = material_questions
    majors = {str(major.get("label") or ""): major for major in material_questions if isinstance(major, dict)}
    sub_by_id: dict[str, dict[str, Any]] = {}
    for major in material_questions:
        if not isinstance(major, dict):
            continue
        for sub in major.get("sub_questions") or []:
            if isinstance(sub, dict) and str(sub.get("question_id") or "").strip():
                sub_by_id[str(sub["question_id"])] = sub
    added_subjectives: list[str] = []
    updated_subjectives: list[str] = []
    subjective_conflicts: list[str] = []
    for ai in parsed["subjective_questions"]:
        question_id = ai["question_id"]
        major_label = ai["major_label"]
        existing = sub_by_id.get(question_id)
        if existing is not None:
            if not str(existing.get("question") or "").strip() and ai["question"]:
                existing["question"] = ai["question"]
            current_reference = str(existing.get("reference_answer") or existing.get("standard_answer") or "").strip()
            if ai["reference_answer"]:
                if not current_reference:
                    existing["reference_answer"] = ai["reference_answer"]
                    existing["standard_answer"] = ai["reference_answer"]
                    updated_subjectives.append(question_id)
                elif not _normalized_answer_compare(current_reference, ai["reference_answer"]):
                    subjective_conflicts.append(question_id)
            if existing.get("max_score") in (None, "") and ai["max_score"] is not None:
                existing["max_score"] = ai["max_score"]
            existing["needs_teacher_confirmation"] = True
            continue
        major = majors.get(major_label)
        if major is None:
            major = {"label": major_label, "prompt": "", "sub_questions": []}
            material_questions.append(major)
            majors[major_label] = major
        new_sub = {
            "label": question_id.split(".", 1)[1],
            "question": ai["question"],
            "standard_answer": ai["reference_answer"],
            "max_score": ai["max_score"],
            "criteriaText": "",
            "scoring_mode": "weighted",
            "point_value": 2.0,
            "answer_found": bool(ai["reference_answer"]),
            "question_id": question_id,
            "parent_question_id": major_label,
            "question_type": "subjective",
            "reference_answer": ai["reference_answer"],
            "rubric_draft": [],
            "rubric_parse_status": "needs_teacher_review",
            "needs_teacher_confirmation": True,
            "ai_parse": True,
        }
        major.setdefault("sub_questions", []).append(new_sub)
        sub_by_id[question_id] = new_sub
        added_subjectives.append(question_id)

    summary = {
        "objective": {
            "ai_count": len(ai_objective_by_id),
            "added": added_objectives,
            "updated_answers": updated_answers,
            "answer_conflicts": answer_conflicts,
            "missing_from_ai": missing_objective_ids,
        },
        "subjective": {
            "ai_count": len(parsed["subjective_questions"]),
            "added": added_subjectives,
            "updated_answers": updated_subjectives,
            "answer_conflicts": subjective_conflicts,
        },
        "issues": parsed["issues"],
        "prompt_version": AI_PARSE_PROMPT_VERSION,
    }
    if answer_conflicts:
        warn(
            "ai_parse_answer_conflict：AI 答案与现有答案不一致，未自动覆盖，请核对："
            + "、".join(answer_conflicts[:30])
            + ("……" if len(answer_conflicts) > 30 else ""),
            "ai_parse_answer_conflict",
        )
    if subjective_conflicts:
        warn(
            "ai_parse_answer_conflict：AI 参考答案与现有参考答案不一致，未自动覆盖，请核对："
            + "、".join(subjective_conflicts[:30])
            + ("……" if len(subjective_conflicts) > 30 else ""),
            "ai_parse_answer_conflict",
        )
    if added_objectives or added_subjectives:
        warn(
            f"ai_parse_added：AI 补充了 {len(added_objectives)} 道客观题、{len(added_subjectives)} 道主观小题，请逐项核对题目、答案与评分点。",
            "ai_parse_added",
        )
    for issue in parsed["issues"]:
        warn(f"ai_parse_issue：{issue}", "ai_parse_issue")
    warn(
        "ai_parse_applied：已应用 AI 解析草稿（发送的是 Word/PDF 文字，不是页面图片）；题目、答案和评分点仍需教师逐项核对后才能创建考试。",
        "ai_parse_applied",
    )
    merged["ai_parse"] = {"status": "applied", "summary": summary, "ai_title": parsed["title"]}
    merged["warnings"] = warnings
    merged["warning_codes"] = warning_codes
    return merged, summary
