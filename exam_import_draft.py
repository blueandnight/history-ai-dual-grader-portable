"""Join DOCX content drafts and PDF print-layout metadata without a second schema."""
from __future__ import annotations

from io import BytesIO
import hashlib
import math
import re
from typing import Any

from docx_question_import import parse_docx, question_kinds
from pdf_question_import import (
    PART_START_RE,
    QUESTION_START_RE,
    _join_pages,
    _numbered_groups,
    _parse_objective_answer_key,
    _parse_objective_question,
    resolve_page_roles,
    parse_pdf_question_import,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_key(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", str(text)).lower()


def layout_reference_page_numbers(layout_source: dict[str, Any] | None) -> list[int]:
    """Pages eligible as layout references: exam pages + the blank answer sheet.

    Teachers draw formal answer regions on the blank answer sheet, so those pages
    are required references. Scoring-reference/answer pages (which contain the
    answers and analysis) must never be exposed as layout references. Drafts
    created before ``reference_pages`` existed keep their exam-page-only behavior.
    """
    source = layout_source if isinstance(layout_source, dict) else {}
    raw = source.get("reference_pages")
    if not raw:
        raw = list(source.get("exam_pages") or []) + list(source.get("answer_sheet_pages") or [])
    numbers: list[int] = []
    for value in raw:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in numbers:
            numbers.append(number)
    return sorted(numbers)


def _read_pdf_layout_source(data: bytes, file_name: str) -> dict[str, Any]:
    if not isinstance(data, bytes) or not data or not data.startswith(b"%PDF-"):
        raise ValueError("pdf_format_invalid")
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ValueError("pdf_encrypted")
        raw_pages = [(number, page.extract_text() or "") for number, page in enumerate(reader.pages, 1)]
    except ImportError as exc:
        raise RuntimeError("pdf_parser_dependency_missing") from exc
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("pdf_read_failed") from exc

    roles = resolve_page_roles(raw_pages)
    text_available = sum(len(re.sub(r"\s", "", text)) for _, text in raw_pages) >= 80
    if not text_available:
        roles = [{"page_number": number, "page_role": "unknown"} for number, _ in raw_pages]
    page_roles = {item["page_number"]: item["page_role"] for item in roles}
    exam_pages = [(number, text) for number, text in raw_pages if text_available and page_roles[number] == "exam"]
    answer_pages = [(number, text) for number, text in raw_pages if text_available and page_roles[number] == "answer"]
    answer_sheet_pages = [number for number, _ in raw_pages if text_available and page_roles[number] == "answer_sheet"]
    exam_text, _ = _join_pages(exam_pages)
    answer_text, _ = _join_pages(answer_pages)
    groups = _numbered_groups(exam_text)
    kinds = question_kinds(exam_text.splitlines())
    objective_ids: list[str] = []
    objective_questions: dict[str, dict[str, Any]] = {}
    duplicate_objective_ids: list[str] = []
    for number, body, _ in groups:
        if kinds.get(number) != "objective":
            continue
        if number in objective_questions:
            # A repeated question number inside the exam region must not replace
            # the first, real question text and silently poison the comparison.
            duplicate_objective_ids.append(number)
            continue
        objective_ids.append(number)
        objective_questions[number] = _parse_objective_question(number, body, None)
    subjective_ids: list[str] = []
    for number, body, _ in groups:
        if kinds.get(number) != "subjective":
            continue
        parts = list(PART_START_RE.finditer(body))
        if parts:
            subjective_ids.extend(f"{number}.{part.group(1)}" for part in parts)
        else:
            subjective_ids.append(f"{number}.1")
    answer_key, answer_warnings = _parse_objective_answer_key(answer_text)
    if duplicate_objective_ids:
        answer_warnings.append(f"PDF试卷页重复识别到客观题号（保留首次出现，未覆盖）：{duplicate_objective_ids}。")
    if answer_sheet_pages:
        answer_warnings.append(f"answer_sheet_isolated：已识别答题卡页 {answer_sheet_pages}，其空白小题未参与同源比较。")
    return {
        "source": {
            "type": "pdf",
            "source_pdf_file_name": str(file_name),
            "source_pdf_sha256": _sha256(data),
            "page_count": len(raw_pages),
            "exam_pages": [number for number, _ in exam_pages],
            "answer_pages": [number for number, _ in answer_pages],
            "answer_sheet_pages": answer_sheet_pages,
            "reference_pages": layout_reference_page_numbers({
                "exam_pages": [number for number, _ in exam_pages],
                "answer_sheet_pages": answer_sheet_pages,
            }),
        },
        "page_roles": roles,
        "text_available": text_available,
        "objective_ids": objective_ids,
        "objective_questions": objective_questions,
        "subjective_ids": subjective_ids,
        "objective_answer_key": answer_key,
        "answer_sheet_pages": answer_sheet_pages,
        "warnings": answer_warnings,
    }


def _compare_question_sources(docx_draft: dict[str, Any], pdf_probe: dict[str, Any]) -> dict[str, Any]:
    docx_objective = [str(item.get("question_id")) for item in docx_draft.get("objective_questions", [])]
    pdf_objective = [str(item) for item in pdf_probe.get("objective_ids", [])]
    docx_subjective = [
        str(sub.get("question_id"))
        for major in docx_draft.get("material_questions", [])
        for sub in major.get("sub_questions", [])
    ]
    pdf_subjective = [str(item) for item in pdf_probe.get("subjective_ids", [])]
    warnings: list[str] = []
    warning_codes: list[str] = []
    conflicts: list[dict[str, Any]] = []
    if not pdf_probe.get("text_available"):
        warnings.append("source_pair_uncertain：PDF 没有可用于文字一致性核对的文本层；仍保留 PDF 版式来源，需教师确认同源关系。")
        warning_codes.extend(["pdf_text_unavailable", "source_pair_uncertain"])
        return {
            "status": "needs_teacher_confirmation", "objective_text_matches": 0,
            "objective_text_compared": 0, "objective_text_matching_ids": [], "docx_objective_ids": docx_objective,
            "pdf_objective_ids": pdf_objective, "docx_subjective_ids": docx_subjective,
            "pdf_subjective_ids": pdf_subjective, "warnings": warnings,
            "warning_codes": warning_codes, "conflicts": conflicts,
        }

    pdf_source = pdf_probe.get("source") or {}
    if "exam_pages" in pdf_source and not pdf_source["exam_pages"]:
        warnings.append("source_pair_uncertain：PDF 未识别到可比较的试题页（仅有答题卡或答案页）；题干同源关系需教师确认。")
        warning_codes.extend(["source_pair_uncertain"])
        return {
            "status": "needs_teacher_confirmation", "objective_text_matches": 0,
            "objective_text_compared": 0, "objective_text_matching_ids": [], "docx_objective_ids": docx_objective,
            "pdf_objective_ids": pdf_objective, "docx_subjective_ids": docx_subjective,
            "pdf_subjective_ids": pdf_subjective, "warnings": warnings,
            "warning_codes": warning_codes, "conflicts": conflicts,
        }

    id_differences = []
    count_mismatch = False
    section_labels = {"objective_questions": "客观题", "subjective_subquestions": "主观题"}
    for kind, left, right in (
        ("objective_questions", set(docx_objective), set(pdf_objective)),
        ("subjective_subquestions", set(docx_subjective), set(pdf_subjective)),
    ):
        if left != right:
            id_differences.append({"section": kind, "docx_only": sorted(left - right), "pdf_only": sorted(right - left)})
        if len(left) != len(right):
            count_mismatch = True
    if id_differences:
        warning_codes.append("question_id_mismatch")
        if count_mismatch:
            warning_codes.append("question_count_mismatch")
        warnings.append(f"source_pair_mismatch：DOCX 与 PDF 题号集合不一致：{id_differences}。")
        if count_mismatch:
            warnings.append("question_count_mismatch：DOCX 与 PDF 识别到的题目数量不同。")
        conflicts.extend(
            {
                "type": "question_id_mismatch",
                "section": item["section"],
                "label": section_labels.get(item["section"], item["section"]),
                "docx_only": item["docx_only"],
                "pdf_only": item["pdf_only"],
            }
            for item in id_differences
        )

    matches = 0
    matching_ids: list[str] = []
    compared = 0
    compared_ids: list[str] = []
    unmatched_ids: list[str] = []
    pdf_questions = pdf_probe.get("objective_questions", {})
    docx_questions = {str(item.get("question_id")): item for item in docx_draft.get("objective_questions", [])}
    for question_id in sorted(set(docx_questions) & set(pdf_questions), key=lambda value: int(value) if value.isdigit() else 999):
        docx_stem = _text_key(docx_questions[question_id].get("stem", ""))
        pdf_stem = _text_key(pdf_questions[question_id].get("stem", ""))
        if len(docx_stem) < 12 or len(pdf_stem) < 12:
            continue
        compared += 1
        compared_ids.append(question_id)
        prefix_length = min(16, len(docx_stem), len(pdf_stem))
        if docx_stem[:prefix_length] in pdf_stem or pdf_stem[:prefix_length] in docx_stem:
            matches += 1
            matching_ids.append(question_id)
        else:
            unmatched_ids.append(question_id)

    if id_differences:
        status = "mismatch"
    elif compared >= 3 and matches == 0:
        status = "mismatch"
        warnings.append("source_pair_mismatch：题号相同，但 PDF 与 DOCX 的题干抽样未发现可确认的文本对应。")
        warning_codes.append("source_pair_mismatch")
        conflicts.append({
            "type": "question_text_mismatch",
            "compared": compared,
            "matched": matches,
            "compared_ids": compared_ids,
            "unmatched_ids": unmatched_ids,
        })
    elif compared >= 3 and matches >= max(3, math.ceil(compared * 0.75)):
        status = "matched"
    else:
        status = "needs_teacher_confirmation"
        warnings.append("source_pair_uncertain：题号相似，但题干文本证据不足以自动确认两份文件同源。")
        warning_codes.append("source_pair_uncertain")

    if status == "mismatch" and "source_pair_mismatch" not in warning_codes:
        warning_codes.append("source_pair_mismatch")
    return {
        "status": status,
        "objective_text_matches": matches,
        "objective_text_compared": compared,
        "objective_text_matching_ids": matching_ids,
        "docx_objective_ids": docx_objective,
        "pdf_objective_ids": pdf_objective,
        "docx_subjective_ids": docx_subjective,
        "pdf_subjective_ids": pdf_subjective,
        "warnings": warnings,
        "warning_codes": list(dict.fromkeys(warning_codes)),
        "conflicts": conflicts,
    }


def _merge_answer_keys(
    docx_key: dict[str, str], pdf_key: dict[str, str]
) -> tuple[dict[str, str], dict[str, Any], dict[str, str], list[dict[str, Any]], list[str]]:
    merged = dict(docx_key)
    key_sources = {question_id: "docx" for question_id in docx_key}
    conflicts = []
    warnings = []
    for question_id, pdf_answer in pdf_key.items():
        if question_id not in merged:
            merged[question_id] = pdf_answer
            key_sources[question_id] = "pdf_fallback"
        elif merged[question_id] != pdf_answer:
            conflicts.append({
                "type": "answer_key_conflict", "question_id": question_id,
                "docx_answer": merged[question_id], "pdf_answer": pdf_answer,
            })
            # Do not select either source for the effective draft key until a
            # teacher resolves the conflict. Both originals remain below.
            merged.pop(question_id, None)
            key_sources.pop(question_id, None)
    if conflicts:
        crosscheck = {"status": "conflict", "docx_answer_key": docx_key, "pdf_answer_key": pdf_key}
        warnings.append("answer_key_conflict：DOCX 与 PDF 答案键存在不同答案；冲突题暂不选用任一答案，双方原值均保留，需教师裁决。")
    elif docx_key and pdf_key:
        crosscheck = {"status": "passed", "docx_answer_key": docx_key, "pdf_answer_key": pdf_key}
    elif docx_key:
        crosscheck = {"status": "docx_only", "docx_answer_key": docx_key, "pdf_answer_key": pdf_key}
    elif pdf_key:
        crosscheck = {"status": "pdf_fallback", "docx_answer_key": docx_key, "pdf_answer_key": pdf_key}
    else:
        crosscheck = {"status": "not_available", "docx_answer_key": docx_key, "pdf_answer_key": pdf_key}
    return merged, crosscheck, key_sources, conflicts, warnings


def _source_ref(file_type: str, file_name: str, data: bytes, **extra: Any) -> dict[str, Any]:
    return {"type": file_type, "file_name": str(file_name), "sha256": _sha256(data), **extra}


def build_exam_import_draft(
    *,
    docx_bytes: bytes | None = None,
    docx_file_name: str | None = None,
    pdf_bytes: bytes | None = None,
    pdf_file_name: str | None = None,
    pdf_reference_pages: list[dict[str, Any]] | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Build one shared draft. DOCX owns semantics when available; PDF owns layout."""
    if docx_bytes is None and pdf_bytes is None:
        raise ValueError("import_source_missing")
    references = list(pdf_reference_pages or [])

    if docx_bytes is None:
        assert pdf_bytes is not None
        pdf_fallback = parse_pdf_question_import(pdf_bytes, pdf_file_name or "source.pdf", project_dir=project_dir)
        if pdf_fallback.get("status") != "draft":
            pdf_fallback["pdf_semantic_fallback"] = True
            if pdf_fallback.get("error_code") == "pdf_text_unavailable":
                pdf_fallback["warning_codes"] = ["pdf_text_unavailable"]
            pdf_fallback["layout_reference"] = {"pages": references, "page_roles": pdf_fallback.get("page_roles", [])}
            pdf_fallback["source"] = {
                **pdf_fallback.get("source", {}),
                "content_source": _source_ref("pdf_semantic_fallback", pdf_file_name or "source.pdf", pdf_bytes),
                "question_source": _source_ref("pdf_semantic_fallback", pdf_file_name or "source.pdf", pdf_bytes),
                "answer_source": _source_ref("pdf", pdf_file_name or "source.pdf", pdf_bytes, relation="same_as_content"),
                "layout_source": pdf_fallback.get("source", {}),
                "source_pair_status": "not_applicable",
            }
            return pdf_fallback
        pdf_probe = _read_pdf_layout_source(pdf_bytes, pdf_file_name or "source.pdf")
        source_meta = pdf_probe["source"]
        pdf_fallback["warnings"] = list(pdf_fallback.get("warnings", [])) + [
            "pdf_semantic_fallback：PDF-only 仅生成轻量题目/答案草稿，教师确认前不得进入正式评分。"
        ]
        pdf_fallback["warning_codes"] = list(dict.fromkeys(list(pdf_fallback.get("warning_codes", [])) + ["pdf_semantic_fallback"]))
        return {
            **pdf_fallback,
            "pdf_semantic_fallback": True,
            "source": {
                **pdf_fallback.get("source", {}),
                "content_source": _source_ref("pdf_semantic_fallback", pdf_file_name or "source.pdf", pdf_bytes),
                "question_source": _source_ref("pdf_semantic_fallback", pdf_file_name or "source.pdf", pdf_bytes),
                "answer_source": _source_ref("pdf", pdf_file_name or "source.pdf", pdf_bytes, relation="same_as_content"),
                "layout_source": source_meta,
                "source_pair_status": "not_applicable",
            },
            "layout_reference": {"pages": references, "page_roles": pdf_probe["page_roles"]},
        }

    docx_name = docx_file_name or "source.docx"
    docx_draft = parse_docx(docx_bytes)
    docx_meta = _source_ref("docx", docx_name, docx_bytes)
    warnings = list(docx_draft.get("warnings", []))
    warning_codes = list(docx_draft.get("warning_codes", []))
    source_conflicts: list[dict[str, Any]] = []

    if pdf_bytes is None:
        warnings.append("layout_source_missing：尚无最终打印 PDF reference，不能建立可靠的页面配准模板。")
        warning_codes.append("layout_source_missing")
        docx_draft.update({
            "source": {
                "content_source": docx_meta, "question_source": docx_meta,
                "answer_source": {**docx_meta, "relation": "same_as_content"} if docx_draft.get("objective_answer_key") or any(sub.get("answer_found") for q in docx_draft.get("material_questions", []) for sub in q.get("sub_questions", [])) else None,
                "layout_source": None, "source_pair_status": "not_applicable",
            },
            "layout_reference": {"pages": [], "page_roles": []},
            "pdf_semantic_fallback": False,
            "warning_codes": list(dict.fromkeys(warning_codes)),
            "warnings": warnings,
        })
        return docx_draft

    assert pdf_file_name is not None or pdf_bytes is not None
    pdf_name = pdf_file_name or "source.pdf"
    pdf_probe = _read_pdf_layout_source(pdf_bytes, pdf_name)
    pair = _compare_question_sources(docx_draft, pdf_probe)
    warnings.extend(pair["warnings"])
    warning_codes.extend(pair["warning_codes"])
    pdf_meta = pdf_probe["source"]

    docx_missing_answers = [
        sub for major in docx_draft.get("material_questions", []) for sub in major.get("sub_questions", [])
        if not sub.get("answer_found")
    ]
    if docx_missing_answers and pair["status"] == "matched":
        # PDF answer text is consulted only as a fallback for missing DOCX data.
        fallback = parse_pdf_question_import(pdf_bytes, pdf_name, project_dir=project_dir)
        pdf_subanswers = {
            sub.get("question_id"): sub
            for major in fallback.get("material_questions", []) for sub in major.get("sub_questions", [])
        } if fallback.get("status") == "draft" else {}
        supplemented = []
        for sub in docx_missing_answers:
            candidate = pdf_subanswers.get(sub.get("question_id"))
            if not candidate or not candidate.get("answer_found"):
                continue
            sub["reference_answer"] = candidate["reference_answer"]
            sub["standard_answer"] = candidate["standard_answer"]
            sub["answer_found"] = True
            sub["rubric_draft"] = candidate["rubric_draft"]
            sub["rubric_parse_status"] = candidate["rubric_parse_status"]
            supplemented.append(sub.get("question_id"))
        if supplemented:
            warnings.append(f"PDF answers used only to fill DOCX-missing reference answers: {supplemented}。")

    docx_key = docx_draft.get("objective_answer_key", {})
    pdf_key = pdf_probe.get("objective_answer_key", {})
    if pair["status"] == "matched":
        merged_key, crosscheck, key_sources, key_conflicts, key_warnings = _merge_answer_keys(docx_key, pdf_key)
    else:
        # Keep the PDF key visible for review, but never join it to an unpaired
        # DOCX exam draft based only on coincidentally similar question labels.
        merged_key, key_sources, key_conflicts, key_warnings = dict(docx_key), {
            question_id: "docx" for question_id in docx_key
        }, [], []
        crosscheck = {
            "status": "source_pair_unconfirmed" if pair["status"] == "needs_teacher_confirmation" else "source_pair_mismatch",
            "docx_answer_key": docx_key,
            "pdf_answer_key": pdf_key,
        }
        if pdf_key:
            warnings.append("source_pair_uncertain：DOCX 与 PDF 尚未确认同源，PDF 答案键仅作为独立来源展示，未并入题目草稿。")
    source_conflicts.extend(pair["conflicts"])
    source_conflicts.extend(key_conflicts)
    warnings.extend(key_warnings)
    if key_conflicts:
        warning_codes.append("answer_key_conflict")

    # DOCX fills first; PDF may fill an absent key. Conflicting keys are
    # withheld from the effective draft until teacher confirmation.
    for question in docx_draft.get("objective_questions", []):
        question_id = str(question.get("question_id"))
        question["correct_answer_draft"] = merged_key.get(question_id)
        question["answer_found"] = question_id in merged_key
        if question_id not in merged_key:
            question["needs_teacher_review"] = True
    if docx_draft.get("objective_questions") and any(not item.get("answer_found") for item in docx_draft["objective_questions"]):
        warnings.append("answer_missing：仍有客观题没有可用答案。")
        warning_codes.append("answer_missing")

    has_docx_reference = bool(docx_draft.get("objective_answer_key")) or any(
        sub.get("answer_found") for major in docx_draft.get("material_questions", []) for sub in major.get("sub_questions", [])
    )
    has_pdf_reference = bool(pdf_key) and pair["status"] == "matched"
    if has_docx_reference and has_pdf_reference:
        answer_source: dict[str, Any] | None = {**docx_meta, "relation": "same_as_content"}
        if any(value == "pdf_fallback" for value in key_sources.values()):
            answer_source = {"type": "mixed", "sources": [answer_source, {**pdf_meta, "role": "fill_missing_only"}]}
    elif has_docx_reference:
        answer_source = {**docx_meta, "relation": "same_as_content"}
    elif has_pdf_reference:
        answer_source = {**pdf_meta, "role": "answer_fallback"}
    else:
        answer_source = None

    docx_draft.update({
        "source": {
            "content_source": docx_meta,
            "question_source": docx_meta,
            "answer_source": answer_source,
            "layout_source": pdf_meta,
            "source_pair_status": pair["status"],
            "source_pair_evidence": pair,
            "answer_key_sources": key_sources,
            "source_conflicts": source_conflicts,
        },
        "objective_answer_key": merged_key,
        "objective_answer_key_status": "draft",
        "objective_answer_key_confirmation_status": "pending_teacher_confirmation",
        "teacher_confirmation_status": "pending_teacher_confirmation",
        "layout_reference": {"pages": references, "page_roles": pdf_probe["page_roles"]},
        "answer_key_crosscheck": crosscheck,
        "pdf_semantic_fallback": False,
        "warning_codes": list(dict.fromkeys(warning_codes)),
        "warnings": warnings,
    })
    return docx_draft
