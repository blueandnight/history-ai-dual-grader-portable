"""Conservative text-layer PDF importer for the existing question draft shape.

This parser creates drafts only. It does not OCR, score, save assignments, or
call a model. Subjective items are emitted in the additive DOCX sub-question
draft schema from ``docx_question_import``.
"""
from __future__ import annotations

from io import BytesIO
import hashlib
import re
import struct
from typing import Any

from docx_question_import import (
    ANSWER_SECTION_TITLE_RE, ANSWER_SHEET_TITLE_RE,
    build_subquestion_draft, rubric_candidates_from_reference,
    question_kinds, reference_sections, single_subjective_parts, SECTION_RE,
    strip_section_headings, QUESTION_PREFIX,
)

MAX_PDF_BYTES = 50 * 1024 * 1024
PAGE_HEADER_RE = re.compile(
    r"历史(?:试卷|答案)第\s*\d+\s*页\s*[，,]\s*共\s*\d+\s*页"
    r"|(?m:^\s*第\s*\d+\s*页\s*$)"
)
QUESTION_START_RE = re.compile(QUESTION_PREFIX, re.M)
OPTION_MARKER_RE = re.compile(r"(?<![A-Za-z])([A-D])\s*[．.、]\s*")
PART_START_RE = re.compile(r"(?m)^[ \t]*[（(]\s*(\d{1,2})\s*[）)]\s*")
ANSWER_TABLE_RE = re.compile(
    r"题号\s*(?P<ids>\d{1,3}(?:\s+\d{1,3}){1,999})\s*答案\s*(?P<answers>[A-D](?:\s*[A-D]){1,999})"
)
EXPLICIT_SCORE_RE = re.compile(r"[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]")


def _normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    text = PAGE_HEADER_RE.sub("", text)
    return text


def _classify_page_role_evidence(text: str) -> tuple[str, bool]:
    """Return (page role, strong?) for one page.

    Strong evidence is an explicit page header or an independent section title.
    Weak evidence is only a topic-section heading line, which appears inside both
    exam pages and answer pages; it may bootstrap an unknown role but must never
    flip an already established answer/answer-sheet region back to exam.
    """
    normalized = _normalize_text(text)
    lines = normalized.splitlines()
    # Page headers carry the clearest role marker. Check them before
    # _normalize_text removes the repeating header from joined page content.
    has_answer_header = bool(re.search(r"历史答案第\s*\d+\s*页\s*[，,]\s*共\s*\d+\s*页", text))
    has_exam_header = bool(re.search(r"历史试卷第\s*\d+\s*页\s*[，,]\s*共\s*\d+\s*页", text))
    has_answer_sheet_marker = any(ANSWER_SHEET_TITLE_RE.match(line) for line in lines)
    has_answer_marker = has_answer_header or any(ANSWER_SECTION_TITLE_RE.match(line) for line in lines)
    has_exam_marker = has_exam_header or bool(re.search(r"单元检测.{0,40}试卷|测试卷", normalized))
    # Mixed pages are not clean references: scoring text always takes precedence.
    if has_answer_marker:
        return "answer", True
    if has_answer_sheet_marker and not has_exam_header:
        return "answer_sheet", True
    if has_exam_marker and not has_answer_marker:
        return "exam", True
    if not has_answer_marker and any(SECTION_RE.match(line) for line in lines):
        return "exam", False
    return "unknown", False


def classify_page_role(text: str) -> str:
    return _classify_page_role_evidence(text)[0]


def resolve_page_roles(pages: list[tuple[int, str]]) -> list[dict[str, Any]]:
    """Continue a section only after an explicit exam/answer page anchor."""
    current = "unknown"
    roles = []
    for number, text in pages:
        role, strong = _classify_page_role_evidence(text)
        if role != "unknown" and (strong or current == "unknown"):
            current = role
        roles.append({"page_number": number, "page_role": current})
    return roles


def _clean_piece(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" \t\r\n；;，,。 ")


def _join_pages(pages: list[tuple[int, str]]) -> tuple[str, list[tuple[int, int, int]]]:
    chunks: list[str] = []
    ranges: list[tuple[int, int, int]] = []
    offset = 0
    for page_number, text in pages:
        cleaned = _normalize_text(text)
        if chunks:
            chunks.append("\n")
            offset += 1
        start = offset
        chunks.append(cleaned)
        offset += len(cleaned)
        ranges.append((start, offset, page_number))
    return "".join(chunks), ranges


def _page_for_offset(offset: int, ranges: list[tuple[int, int, int]]) -> int | None:
    return next((page for start, end, page in ranges if start <= offset < end), None)


def _numbered_groups(text: str) -> list[tuple[str, str, int]]:
    matches = list(QUESTION_START_RE.finditer(text))
    groups = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        groups.append((match.group(1), text[match.end():end].strip(), match.start()))
    return groups


def _parse_objective_question(number: str, body: str, page_number: int | None) -> dict[str, Any]:
    # A standalone topic-section heading can share a text block with the final
    # choice when a PDF page break lands directly after that choice.
    body = strip_section_headings(body)
    markers = list(OPTION_MARKER_RE.finditer(body))
    options: dict[str, str] = {}
    labels = [match.group(1) for match in markers]
    if labels == ["A", "B", "C", "D"]:
        stem = _clean_piece(body[:markers[0].start()])
        for index, match in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
            options[match.group(1)] = _clean_piece(body[match.end():end])
    else:
        stem = _clean_piece(body[:markers[0].start()] if markers else body)
        for index, match in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
            options.setdefault(match.group(1), _clean_piece(body[match.end():end]))
    complete = bool(stem) and list(options) == ["A", "B", "C", "D"] and all(options.values())
    return {
        "question_id": number,
        "stem": stem,
        "options": options,
        "question_type": "single_choice",
        "source_page_number": page_number,
        "needs_teacher_review": not complete,
        "needs_teacher_confirmation": True,
    }


def _parse_objective_answer_key(answer_text: str) -> tuple[dict[str, str], list[str]]:
    answer_key: dict[str, str] = {}
    warnings: list[str] = []
    for match in ANSWER_TABLE_RE.finditer(answer_text):
        question_ids = re.findall(r"\d{1,3}", match.group("ids"))
        answers = re.findall(r"[A-D]", match.group("answers"))
        if len(question_ids) != len(answers):
            warnings.append("答案页客观题答案表的题号与答案数量不一致。")
            continue
        for question_id, answer in zip(question_ids, answers):
            if question_id in answer_key and answer_key[question_id] != answer:
                warnings.append(f"答案页重复出现客观题{question_id}且答案不一致。")
                continue
            answer_key[question_id] = answer
    return answer_key, warnings


def _answer_sections(answer_text: str) -> dict[str, dict[str, str]]:
    return {number: {part: _clean_piece(answer) for part, answer in parts.items()}
            for number, parts in reference_sections(answer_text).items()}


def _rubric_candidates(question_id: str, reference_answer: str) -> tuple[list[dict[str, Any]], str]:
    return rubric_candidates_from_reference(question_id, reference_answer)


def _extract_subjective_drafts(exam_text: str, answer_text: str) -> tuple[list[dict[str, Any]], list[str], set[str]]:
    kinds = question_kinds(exam_text.splitlines())
    exam_groups = {number: body for number, body, _ in _numbered_groups(exam_text) if kinds.get(number) == "subjective"}
    answer_groups = _answer_sections(answer_text)
    material_questions: list[dict[str, Any]] = []
    warnings: list[str] = []
    answer_major_ids = set(answer_groups) & set(exam_groups)

    for number, body in exam_groups.items():
        part_matches = list(PART_START_RE.finditer(body))
        if not part_matches:
            major_prompt, task = single_subjective_parts(body)
            body = major_prompt + "\n（1）" + task
            part_matches = list(PART_START_RE.finditer(body))
        major_prompt = _clean_piece(body[:part_matches[0].start()])
        answer_parts = answer_groups.get(number, {})
        actual_parts = [m.group(1) for m in part_matches]
        if not answer_parts:
            warnings.append(f"答案页未识别到主观题{number}的参考答案。")
        elif sorted(answer_parts, key=int) != sorted(actual_parts, key=int):
            warnings.append(f"主观题{number}试卷与答案页小题数量或编号不一致。")

        sub_questions = []
        for part_index, part_match in enumerate(part_matches):
            part_end = part_matches[part_index + 1].start() if part_index + 1 < len(part_matches) else len(body)
            prompt = _clean_piece(body[part_match.end():part_end])
            part_number = part_match.group(1)
            question_id = f"{number}.{part_number}"
            reference_answer = answer_parts.get(part_number, "")
            score_match = EXPLICIT_SCORE_RE.search(prompt)
            max_score = float(score_match.group(1)) if score_match else None
            rubric_draft, rubric_status = _rubric_candidates(question_id, reference_answer) if reference_answer else ([], "needs_teacher_review")
            if max_score is None:
                warnings.append(f"{question_id}未发现明确分值，max_score 保持 null。")
            if rubric_status == "needs_teacher_review":
                warnings.append(f"{question_id}参考答案未可靠拆成候选评分点，保留全文并标记教师复核。")
            sub_questions.append(build_subquestion_draft(
                parent_question_id=number,
                part_number=part_number,
                question_text=prompt,
                reference_answer=reference_answer,
                max_score=max_score,
                answer_found=bool(reference_answer),
                rubric_draft=rubric_draft,
                rubric_parse_status=rubric_status,
                point_value=None,
            ))
        material_questions.append({
            "label": number,
            "question_id": number,
            "question_type": "material_question",
            "prompt": major_prompt,
            "sub_questions": sub_questions,
            "needs_teacher_confirmation": True,
        })
    return material_questions, warnings, answer_major_ids


def _png_render_metadata(project_dir: str | None) -> list[dict[str, Any]]:
    if not project_dir:
        return []
    from pathlib import Path
    root = Path(project_dir) / "reports" / "artifacts" / "q9_pdf_reference_alignment"
    result = []
    for page_number in (3, 4):
        path = root / f"reference_q9_page{page_number}.png"
        if not path.is_file():
            continue
        data = path.read_bytes()
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
            continue
        width, height = struct.unpack(">II", data[16:24])
        result.append({
            "page_number": page_number,
            "reference_width": width,
            "reference_height": height,
            "reference_sha256": hashlib.sha256(data).hexdigest(),
            "render_dpi": 150,
        })
    return result


def parse_pdf_question_import(
    data: bytes,
    file_name: str,
    *,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Parse a text-based PDF into a draft compatible with the DOCX import shape."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_PDF_BYTES:
        raise ValueError("pdf_size_invalid")
    if not data.startswith(b"%PDF-"):
        raise ValueError("pdf_format_invalid")
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            return {
                "status": "error",
                "error_code": "pdf_encrypted",
                "warnings": ["PDF 已加密，未尝试绕过保护。"],
            }
        raw_pages = [(page_number, page.extract_text() or "") for page_number, page in enumerate(reader.pages, 1)]
    except ImportError as exc:
        raise RuntimeError("pdf_parser_dependency_missing") from exc
    except Exception as exc:
        raise ValueError("pdf_read_failed") from exc

    source = {
        "type": "pdf",
        "source_pdf_file_name": str(file_name),
        "source_pdf_sha256": hashlib.sha256(data).hexdigest(),
        "page_count": len(raw_pages),
    }
    char_count = sum(len(re.sub(r"\s", "", text)) for _, text in raw_pages)
    page_roles = resolve_page_roles(raw_pages)
    if char_count < 80:
        return {
            "status": "error",
            "error_code": "pdf_text_unavailable",
            "source": source,
            "page_roles": [{"page_number": n, "page_role": "unknown"} for n, _ in raw_pages],
            "warnings": ["PDF 没有足够的可解析文本层；本工具不会自动 OCR。"],
        }

    roles = {item["page_number"]: item["page_role"] for item in page_roles}
    exam_pages = [(number, text) for number, text in raw_pages if roles[number] == "exam"]
    answer_pages = [(number, text) for number, text in raw_pages if roles[number] == "answer"]
    answer_sheet_pages = [number for number, _ in raw_pages if roles[number] == "answer_sheet"]
    exam_text, exam_ranges = _join_pages(exam_pages)
    answer_text, _ = _join_pages(answer_pages)
    all_warnings: list[str] = []
    if answer_sheet_pages:
        all_warnings.append(f"answer_sheet_isolated：已识别答题卡页 {answer_sheet_pages}，其空白小题未作为试题或答案导入。")
    unknown_pages = [item["page_number"] for item in page_roles if item["page_role"] == "unknown"]
    if unknown_pages:
        all_warnings.append(f"以下页无法可靠判定为试卷或答案页：{unknown_pages}。")
    if not exam_pages:
        all_warnings.append("没有识别到试卷页；题目结构未自动归类。")
    if not answer_pages:
        all_warnings.append("没有识别到参考答案页。")

    numbered = _numbered_groups(exam_text)
    kinds = question_kinds(exam_text.splitlines())
    objective_questions: list[dict[str, Any]] = []
    exam_objective_ids: list[str] = []
    exam_major_ids: set[str] = set()
    for number, body, offset in numbered:
        if kinds.get(number) == "objective":
            exam_objective_ids.append(number)
            objective_questions.append(_parse_objective_question(number, body, _page_for_offset(offset, exam_ranges)))
        elif kinds.get(number) == "subjective":
            exam_major_ids.add(number)
        else:
            all_warnings.append(f"题目{number}题型无法可靠识别，未自动归入客观题，请教师核对。")

    duplicate_ids = sorted({qid for qid in exam_objective_ids if exam_objective_ids.count(qid) > 1}, key=int)
    if duplicate_ids:
        all_warnings.append(f"试卷页重复识别到客观题号：{duplicate_ids}。")
    incomplete_objectives = [q["question_id"] for q in objective_questions if q["needs_teacher_review"]]
    if incomplete_objectives:
        all_warnings.append(f"以下客观题题干或A/B/C/D选项解析不完整：{incomplete_objectives}。")

    objective_answer_key, key_warnings = _parse_objective_answer_key(answer_text)
    all_warnings.extend(key_warnings)
    missing_objective_answers = [qid for qid in exam_objective_ids if qid not in objective_answer_key]
    extra_objective_answers = sorted((set(objective_answer_key) - set(exam_objective_ids)), key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x))
    if missing_objective_answers:
        all_warnings.append(f"答案页缺失或未识别客观题答案：{missing_objective_answers}。")
    if extra_objective_answers:
        all_warnings.append(f"答案页存在试卷范围外的客观题答案：{extra_objective_answers}。")
    exam_objective_set = set(exam_objective_ids)
    answer_objective_set = set(objective_answer_key)
    if answer_objective_set - exam_objective_set:
        all_warnings.append(f"答案页有客观题号但试卷页没有对应题目：{sorted(answer_objective_set - exam_objective_set, key=int)}。")
    if exam_objective_set - answer_objective_set:
        all_warnings.append(f"试卷页有客观题但答案页未识别对应答案：{sorted(exam_objective_set - answer_objective_set, key=int)}。")

    material_questions, subjective_warnings, answer_subjective_ids = _extract_subjective_drafts(exam_text, answer_text)
    all_warnings.extend(subjective_warnings)
    if answer_subjective_ids - exam_major_ids:
        all_warnings.append(f"答案页出现试卷未识别的主观题：{sorted(answer_subjective_ids - exam_major_ids, key=int)}。")
    if exam_major_ids - answer_subjective_ids:
        all_warnings.append(f"试卷页出现答案页未识别的主观题：{sorted(exam_major_ids - answer_subjective_ids, key=int)}。")

    for question in objective_questions:
        question["answer_found"] = question["question_id"] in objective_answer_key
    title = next((line.strip() for line in exam_text.splitlines()[:10] if "测试卷" in line or "试卷" in line), "")
    warnings = all_warnings + [
        "客观答案键、参考答案与 rubric_draft 均为草稿，须教师确认后才可用于正式评分。",
        "Rubric 候选点仅按原答案标点作确定性拆分；未分配分值、未判定同义点、封顶或重复给分规则。",
    ]
    return {
        "status": "draft",
        "title": title,
        "source": {
            **source,
            "exam_pages": [number for number, _ in exam_pages],
            "answer_pages": [number for number, _ in answer_pages],
            "answer_sheet_pages": answer_sheet_pages,
            # Layout references need the blank answer sheet (where teachers draw
            # answer regions) but must never expose scoring-reference pages.
            "reference_pages": sorted(dict.fromkeys([number for number, _ in exam_pages] + answer_sheet_pages)),
        },
        "page_roles": page_roles,
        "objective_questions": objective_questions,
        "objective_answer_key": objective_answer_key,
        "objective_answer_key_status": "draft",
        "objective_answer_key_confirmation_status": "pending_teacher_confirmation",
        # Material questions use the same DOCX question/sub_questions contract.
        "material_questions": material_questions,
        "teacher_confirmation_status": "pending_teacher_confirmation",
        "reference_render_metadata": _png_render_metadata(project_dir),
        "warnings": warnings,
    }
