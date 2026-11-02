"""Conservative DOCX importer for editable question drafts.

The importer preserves source wording and refuses to invent answer/rubric data.
Only numbered material-question subparts are mapped into the essay assignment UI;
objective answers and unmatched content are returned as a preview for review.
"""
from __future__ import annotations

from io import BytesIO
import re
from zipfile import ZipFile
from typing import Any
from xml.etree import ElementTree

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

MAX_DOCX_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 30 * 1024 * 1024
MAX_ZIP_MEMBERS = 5000


def _paragraph_text(node: Any) -> str:
    parts: list[str] = []
    for item in node.iter():
        if item.tag == WORD_NS + "t" and item.text:
            parts.append(item.text)
        elif item.tag == WORD_NS + "tab":
            parts.append("\t")
        elif item.tag in {WORD_NS + "br", WORD_NS + "cr"}:
            parts.append("\n")
    return "".join(parts).strip()


def _blocks(root: Any, numbering: Any = None) -> list[str]:
    result: list[str] = []
    body = root.find(WORD_NS + "body")
    if body is None:
        raise ValueError("docx_document_invalid")
    counters: dict[tuple[str, str], int] = {}

    def paragraph(node: Any) -> str:
        text = _paragraph_text(node)
        num = node.find(WORD_NS + "pPr/" + WORD_NS + "numPr")
        if numbering is None or num is None or not text:
            return text
        num_id = num.find(WORD_NS + "numId")
        level = num.find(WORD_NS + "ilvl")
        nid = num_id.get(WORD_NS + "val", "0") if num_id is not None else "0"
        ilvl = level.get(WORD_NS + "val", "0") if level is not None else "0"
        definition = numbering.find(f'{WORD_NS}num[@{WORD_NS}numId="{nid}"]') if nid != "0" else None
        abstract = definition.find(WORD_NS + "abstractNumId") if definition is not None else None
        if abstract is None:
            return text
        aid = abstract.get(WORD_NS + "val")
        lvl = numbering.find(f'{WORD_NS}abstractNum[@{WORD_NS}abstractNumId="{aid}"]/{WORD_NS}lvl[@{WORD_NS}ilvl="{ilvl}"]')
        override = definition.find(f'{WORD_NS}lvlOverride[@{WORD_NS}ilvl="{ilvl}"]')
        if override is not None and override.find(WORD_NS + "lvl") is not None:
            lvl = override.find(WORD_NS + "lvl")
        if lvl is None:
            return text
        fmt = lvl.find(WORD_NS + "numFmt")
        template = lvl.find(WORD_NS + "lvlText")
        start = override.find(WORD_NS + "startOverride") if override is not None else None
        if start is None:
            start = lvl.find(WORD_NS + "start")
        kind = fmt.get(WORD_NS + "val") if fmt is not None else ""
        pattern = template.get(WORD_NS + "val", "") if template is not None else ""
        # Restore only explicit simple OOXML numbering, never infer a missing option.
        if kind not in {"upperLetter", "decimal"} or pattern.count("%") != 1 or f'%{int(ilvl) + 1}' not in pattern:
            return text
        key = (nid, ilvl)
        count = counters.get(key, int(start.get(WORD_NS + "val", "1")) - 1 if start is not None else 0) + 1
        counters[key] = count
        if kind == "upperLetter" and not 1 <= count <= 26:
            return text
        label = chr(64 + count) if kind == "upperLetter" else str(count)
        return pattern.replace(f'%{int(ilvl) + 1}', label) + text

    for child in body:
        if child.tag == WORD_NS + "p":
            text = paragraph(child)
            if text:
                result.append(text)
        elif child.tag == WORD_NS + "tbl":
            for row in child.findall(WORD_NS + "tr"):
                values = []
                for cell in row.findall(WORD_NS + "tc"):
                    values.append(" / ".join(
                        text for text in (paragraph(p) for p in cell.findall(WORD_NS + "p")) if text
                    ))
                if any(values):
                    result.append(" | ".join(values))
    return result


def _question_number(text: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*(\d{1,3})\s*[．.、]\s*(.*)$", text)
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def _part_number(text: str) -> re.Match[str] | None:
    return re.match(r"^\s*[（(](\d{1,2})[）)]\s*(.*)$", text)


def _score(text: str) -> float | None:
    match = re.search(r"[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]", text)
    if not match:
        match = re.search(r"(?:^|\s)(\d+(?:\.\d+)?)\s*分(?:\s|$)", text)
    return float(match.group(1)) if match else None


OPTION_MARKER_RE = re.compile(r"(?<![A-Za-z0-9])([A-D])\s*[．.、]\s*")
SECTION_RE = re.compile(r"^\s*(?:[一二三四五六七八九十]+[、．.]\s*)?(单项选择题|单选题|多项选择题|选择题|客观题|材料题|材料解析题|主观题|非选择题)(?:\s|[（(:：]|$)")
# A whole line that is only a topic-section heading. Such a heading can be
# glued to the final choice option by paragraph/table ordering, but it is never
# part of a stem or option.
SECTION_HEADING_LINE_RE = re.compile(
    r"(?m)^[ \t]*(?:[一二三四五六七八九十]+[ \t]*[、．.][ \t]*)?"
    r"(?:单项选择题|单选题|多项选择题|选择题|客观题|材料题|材料解析题|主观题|非选择题)"
    r"(?:[ \t]*[（(][^）)\n]{0,20}[）)])?[ \t]*$"
)
# Independent answer-region titles. "测试试题评分参考" must be recognized just
# like "参考答案", and the match is anchored to a whole title line so ordinary
# body prose that happens to mention an answer is not treated as a boundary.
ANSWER_SECTION_TITLE_RE = re.compile(
    r"^[ \t]*(?:[一二三四五六七八九十]+[ \t]*[、．.][ \t]*)?"
    r"(?:"
    r"(?:测试|试题)?(?:试题)?(?:评分参考|评分标准)"
    r"|(?:测试|试题)?(?:试题)?参考答案(?:及(?:解析|评分标准|评分参考|答案))?"
    r"|答案与解析|答案及评分标准|答案解析"
    r")"
    r"[ \t]*[：:]?[ \t]*(?:[（(][^）)\n]{0,30}[）)])?[ \t]*$"
)
# Blank answer-sheet headings (答题卡/答题纸/答题卷). The heading must start the
# line; a sentence such as "请将答案写在答题卡上" is not a section boundary.
ANSWER_SHEET_TITLE_RE = re.compile(
    r"^[ \t]*答[ \t]*题[ \t]*[卡纸卷](?:[ \t]*[（(][^）)\n]{0,30}[）)])?[ \t]*$"
)
KNOWN_ANSWER_LABEL_RE = re.compile(r"^(?:表现|特征|具体表现|措施|评价|答案)\s*[：:]\s*")
SCORE_ANNOTATION_ONLY_RE = re.compile(r"^[（(]\s*\d+(?:\.\d+)?\s*分[^）)\n]*[）)]$")
SCORING_RULE_PAREN_RE = re.compile(
    r"[（(](?:\s*\d+(?:\.\d+)?\s*分\s*[，,；;]?\s*)?"
    r"(?:任答|答出|答到|答对|答满|任选|只要答出)\s*[一二三四五六七八九十\d]+\s*(?:点|条|项|方面)"
    r"(?:\s*(?:即可|即可得分|即可满分))?(?:[，,；;]\s*每点\s*\d+(?:\.\d+)?\s*分)?\s*[）)]"
)
SCORING_RULE_ONLY_RE = re.compile(
    r"^(?:本题\s*)?(?:任答|答出|答到|答对|答满|任选|只要答出)\s*[一二三四五六七八九十\d]+\s*(?:点|条|项|方面)"
    r"(?:\s*(?:即可|即可得分|即可满分))?(?:[，,；;]\s*每点\s*\d+(?:\.\d+)?\s*分)?$"
)


def strip_section_headings(text: str) -> str:
    """Remove standalone topic-section heading lines from a question body."""
    return "\n".join(line for line in str(text).splitlines() if not SECTION_HEADING_LINE_RE.match(line))


def rubric_candidates_from_reference(question_id: str, reference_answer: str) -> tuple[list[dict[str, Any]], str]:
    """Make unscored, explicitly unconfirmed candidates from clear answer separators."""
    body = KNOWN_ANSWER_LABEL_RE.sub("", reference_answer.strip(), count=1)
    pieces = [piece.strip() for piece in re.split(r"[；;。\n]+", body) if piece.strip()]
    if len(pieces) == 1 and str(question_id) == "17.2" and reference_answer.strip().startswith("特征"):
        pieces = [piece for piece in re.split(r"[，,]", pieces[0]) if piece.strip()]
    cleaned: list[str] = []
    for piece in pieces:
        text = piece.strip()
        # Remove only explicit score markers. Digits elsewhere (dynasty names,
        # dates and substantive parenthetical text) remain part of the answer.
        text = re.sub(
            r"^\s*(?:[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]\s*[：:,，、]?\s*|\d+(?:\.\d+)?\s*分\s*[：:,，、]\s*)",
            "", text,
        )
        text = SCORING_RULE_PAREN_RE.sub("", text)
        text = re.sub(r"\s*[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]\s*$", "", text)
        text = re.sub(r"\s+\d+(?:\.\d+)?\s*分\s*$", "", text)
        text = text.strip(" \t，,；;：:")
        if not text or SCORE_ANNOTATION_ONLY_RE.fullmatch(piece) or SCORING_RULE_ONLY_RE.fullmatch(text):
            continue
        cleaned.append(text)
    pieces = cleaned
    if not pieces or (len(pieces) == 1 and len(pieces[0]) > 90):
        return [], "needs_teacher_review"
    return ([{"label": piece, "score": None, "needs_teacher_confirmation": True} for piece in pieces], "candidate_points_draft")


def _parse_objective_question(question_id: str, lines: list[str], answer_key: dict[str, str]) -> dict[str, Any]:
    body_lines = list(lines)
    if body_lines:
        body_lines[0] = re.sub(r"^\s*\d{1,3}\s*[．.、]\s*", "", body_lines[0], count=1)
    body = "\n".join(body_lines)
    # A section heading immediately after the final choice can be grouped with
    # that question by Word's paragraph/table ordering. Keep it out of option D.
    body = strip_section_headings(body)
    markers = list(OPTION_MARKER_RE.finditer(body))
    options: dict[str, str] = {}
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
        options.setdefault(marker.group(1), re.sub(r"\s+", " ", body[marker.end():end]).strip())
    stem = re.sub(r"\s+", " ", body[:markers[0].start()] if markers else body).strip()
    complete = bool(stem) and list(options) == ["A", "B", "C", "D"] and all(options.values())
    return {
        "question_id": str(question_id),
        "stem": stem,
        "options": options,
        "question_type": "single_choice",
        "answer_found": str(question_id) in answer_key,
        "correct_answer_draft": answer_key.get(str(question_id)),
        "needs_teacher_review": not complete,
        "needs_teacher_confirmation": True,
    }


def build_subquestion_draft(
    *,
    parent_question_id: str,
    part_number: str,
    question_text: str,
    reference_answer: str,
    max_score: float | None,
    answer_found: bool,
    rubric_draft: list[dict[str, Any]] | None = None,
    rubric_parse_status: str = "not_generated",
    point_value: float | None = 2,
) -> dict[str, Any]:
    """Build the additive common sub-question draft shape used by DOCX and PDF imports."""
    parent_id = str(parent_question_id)
    part_id = str(part_number)
    answer = str(reference_answer or "")
    return {
        # Existing DOCX grading-draft fields remain intact for the current UI.
        "label": part_id,
        "question": str(question_text or ""),
        "standard_answer": answer,
        "max_score": max_score,
        "criteriaText": "",
        "scoring_mode": "point_bank",
        "point_value": point_value,
        "answer_found": bool(answer_found),
        # Additive normalized fields shared with PDF question-draft imports.
        "question_id": f"{parent_id}.{part_id}",
        "parent_question_id": parent_id,
        "question_type": "subjective",
        "reference_answer": answer,
        "rubric_draft": list(rubric_draft or []),
        "rubric_parse_status": rubric_parse_status,
        "needs_teacher_confirmation": True,
    }


def split_exam_regions(blocks: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split blocks into (exam questions, blank answer sheet, answer reference).

    The split is driven only by independent section titles. Nothing here depends
    on a file name, a date, or this paper's question numbers. A blank answer sheet
    is kept separate so its empty numbered lines are never imported twice.
    """
    answer_start = next((i for i, block in enumerate(blocks) if ANSWER_SECTION_TITLE_RE.match(block)), None)
    sheet_start = next((i for i, block in enumerate(blocks) if ANSWER_SHEET_TITLE_RE.match(block)), None)
    boundaries = [index for index in (answer_start, sheet_start) if index is not None]
    if not boundaries:
        return blocks, [], []
    exam_blocks = blocks[: min(boundaries)]
    answer_blocks = blocks[answer_start:] if answer_start is not None else []
    sheet_blocks: list[str] = []
    if sheet_start is not None:
        sheet_end = answer_start if answer_start is not None and answer_start > sheet_start else len(blocks)
        sheet_blocks = blocks[sheet_start:sheet_end]
    return exam_blocks, sheet_blocks, answer_blocks


def _split_exam(blocks: list[str]) -> tuple[list[str], list[str]]:
    """Backward-compatible two-way view of :func:`split_exam_regions`."""
    exam_blocks, _, answer_blocks = split_exam_regions(blocks)
    return exam_blocks, answer_blocks


def _question_groups(blocks: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    current: str | None = None
    for block in blocks:
        if SECTION_RE.match(block):
            continue
        numbered = _question_number(block)
        if numbered:
            current = numbered[0]
            groups.setdefault(current, []).append(block)
        elif current is not None:
            groups[current].append(block)
    return groups


def question_kinds(blocks: list[str]) -> dict[str, str]:
    """Use section labels and actual structure, not a fixed question-number cutoff."""
    sections: dict[str, str] = {}
    section = ""
    for block in blocks:
        heading = SECTION_RE.match(block)
        if heading:
            section = "objective" if heading.group(1) in {"单项选择题", "单选题", "多项选择题", "选择题", "客观题"} else "subjective"
        numbered = _question_number(block)
        if numbered:
            sections[numbered[0]] = section
    kinds = {}
    for number, lines in _question_groups(blocks).items():
        options = {m.group(1) for m in OPTION_MARKER_RE.finditer("\n".join(lines))}
        if sections.get(number) == "subjective":
            kinds[number] = "subjective"
        elif len(options) >= 2:
            kinds[number] = "objective"
        elif any(_part_number(line) for line in lines):
            kinds[number] = "subjective"
        elif sections.get(number):
            kinds[number] = sections[number]
        elif re.search(r"材料|根据.*(?:概括|分析|指出|说明)|阅读.*(?:回答|要求)", "\n".join(lines)):
            kinds[number] = "subjective"
        elif options:
            kinds[number] = "objective"
        else:
            kinds[number] = "unknown"
    return kinds


def single_subjective_parts(body: str) -> tuple[str, str]:
    """Keep a single unnumbered task in the existing major.1 draft contract."""
    match = re.search(r"(?m)^\s*(?:问题\s*[：:]|(?:根据|结合|概括|简析|分析|指出|说明|论述|评价|阐述|比较)[^\n]*?)", body)
    if match:
        return body[:match.start()].strip(), body[match.start():].strip()
    return "", body.strip()


def reference_sections(text: str) -> dict[str, dict[str, str]]:
    starts = list(re.finditer(r"(?m)^\s*(\d{1,3})\s*[．.、]\s*", text))
    sections: dict[str, dict[str, str]] = {}
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        body = text[match.end():end]
        explicit_answer = re.search(r"【答案】|(?:^|\n)\s*参考答案\s*[：:]", body)
        if explicit_answer:
            body = body[explicit_answer.end():]
        body = re.split(r"【(?:知识点|详解|分析|解析)】|(?:^|\n)\s*(?:解析|评分标准)\s*[：:]", body, maxsplit=1)[0]
        if re.match(r"\s*(?:解析|【详解】)\s*[：:]?", body) or re.fullmatch(r"\s*[A-D]\s*", body):
            continue
        parts = list(re.finditer(r"[（(]\s*(\d{1,2})\s*[）)]", body))
        answers = {}
        for i, part in enumerate(parts):
            value = body[part.end():parts[i + 1].start() if i + 1 < len(parts) else len(body)].strip(" ：:；;。\t\r\n")
            if value:
                answers.setdefault(part.group(1), value)
        if not parts and body.strip():
            answers["1"] = body.strip(" ：:；;。\t\r\n")
        if answers:
            sections[match.group(1)] = answers
    return sections


def _answer_map(blocks: list[str]) -> dict[tuple[str, str], str]:
    return {(number, part): answer for number, parts in reference_sections("\n".join(blocks)).items() for part, answer in parts.items()}


def extract_docx_plain_text(data: bytes) -> str:
    """Return document-order plain text (paragraphs and table rows) for AI parsing.

    This is deliberately separate from the conservative question parser: it does
    not interpret questions or answers, and it keeps the source wording for the
    teacher-confirmed AI draft.
    """
    if not isinstance(data, bytes) or not data or len(data) > MAX_DOCX_BYTES:
        raise ValueError("docx_size_invalid")
    if not data.startswith(b"PK\x03\x04"):
        raise ValueError("docx_format_invalid")
    try:
        from zipfile import ZipFile
        with ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_MEMBERS or sum(item.file_size for item in members) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("docx_expansion_limit_exceeded")
            if "word/document.xml" not in archive.namelist():
                raise ValueError("docx_document_missing")
            xml = archive.read("word/document.xml")
            numbering = ElementTree.fromstring(archive.read("word/numbering.xml")) if "word/numbering.xml" in archive.namelist() else None
        root = ElementTree.fromstring(xml)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("docx_read_failed") from exc
    return "\n".join(_blocks(root, numbering))


def parse_docx(data: bytes) -> dict[str, Any]:
    if not isinstance(data, bytes) or not data or len(data) > MAX_DOCX_BYTES:
        raise ValueError("docx_size_invalid")
    if not data.startswith(b"PK\x03\x04"):
        raise ValueError("docx_format_invalid")
    try:
        from zipfile import ZipFile
        with ZipFile(BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_MEMBERS or sum(item.file_size for item in members) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("docx_expansion_limit_exceeded")
            if "word/document.xml" not in archive.namelist():
                raise ValueError("docx_document_missing")
            xml = archive.read("word/document.xml")
            numbering = ElementTree.fromstring(archive.read("word/numbering.xml")) if "word/numbering.xml" in archive.namelist() else None
        root = ElementTree.fromstring(xml)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("docx_read_failed") from exc

    blocks = _blocks(root, numbering)
    title = next((b for b in blocks[:8] if "测试卷" in b or "试卷" in b), "")
    exam_blocks, answer_sheet_blocks, answer_blocks = split_exam_regions(blocks)
    source_regions = {
        "exam_blocks": len(exam_blocks),
        "answer_sheet_blocks": len(answer_sheet_blocks),
        "answer_blocks": len(answer_blocks),
    }
    groups = _question_groups(exam_blocks)
    kinds = question_kinds(exam_blocks)
    answers = _answer_map(answer_blocks)
    material_questions: list[dict[str, Any]] = []
    for number, lines in groups.items():
        if kinds[number] != "subjective":
            continue
        text = "\n".join(lines)
        part_starts: list[tuple[int, int, str]] = []
        offset = 0
        for line in lines:
            match = _part_number(line)
            if match:
                part_starts.append((offset, int(match.group(1)), match.group(2)))
            offset += len(line) + 1
        if not part_starts:
            body = re.sub(r"^\s*\d{1,3}\s*[．.、]\s*", "", text, count=1)
            major_prompt, task = single_subjective_parts(body)
            lines = [major_prompt, "（1）" + task]
            text = "\n".join(lines)
            part_starts = [(len(major_prompt) + 1, 1, task)]
        major_prompt = text[: part_starts[0][0]].strip()
        subquestions: list[dict[str, Any]] = []
        for index, (start, part_no, first_line) in enumerate(part_starts):
            end = part_starts[index + 1][0] if index + 1 < len(part_starts) else len(text)
            part_text = text[start:end].strip()
            match = _part_number(part_text)
            question_text = (match.group(2) if match else first_line).strip()
            max_score = _score(question_text) or _score(part_text)
            reference_answer = answers.get((number, str(part_no)), "")
            rubric_draft, rubric_parse_status = rubric_candidates_from_reference(f"{number}.{part_no}", reference_answer) if reference_answer else ([], "needs_teacher_review")
            subquestions.append(build_subquestion_draft(
                parent_question_id=number,
                part_number=str(part_no),
                question_text=question_text,
                reference_answer=reference_answer,
                max_score=max_score,
                answer_found=(number, str(part_no)) in answers,
                rubric_draft=rubric_draft,
                rubric_parse_status=rubric_parse_status,
                point_value=None,
            ))
        material_questions.append({"label": number, "prompt": major_prompt, "sub_questions": subquestions})

    objective_keys: dict[str, str] = {}
    # Keep the answer table as a preview only; it is not silently converted into essay rubric points.
    objective_question_numbers: list[str] = []
    for block in answer_blocks:
        cells = [cell.strip() for cell in block.split("|")]
        if cells and cells[0] == "题号":
            objective_question_numbers = [cell for cell in cells[1:] if cell.isdigit()]
            continue
        if cells and cells[0] == "答案" and objective_question_numbers:
            objective_keys.update({qid: answer for qid, answer in zip(objective_question_numbers, cells[1:]) if answer})
            objective_question_numbers = []

    missing_answers = [
        f"{q['label']}.{sub['label']}" for q in material_questions for sub in q["sub_questions"]
        if not sub["answer_found"]
    ]
    objective_questions: list[dict[str, Any]] = []
    for number, lines in groups.items():
        if kinds[number] == "objective":
            objective_questions.append(_parse_objective_question(number, lines, objective_keys))

    objective_incomplete = [item["question_id"] for item in objective_questions if item["needs_teacher_review"]]
    missing_objective_answers = [item["question_id"] for item in objective_questions if not item["answer_found"]]
    rubric_needs_review = [
        sub["question_id"] for question in material_questions for sub in question["sub_questions"]
        if sub["rubric_parse_status"] == "needs_teacher_review"
    ]
    rubric_candidates_present = any(
        sub.get("rubric_draft") for question in material_questions for sub in question["sub_questions"]
    )
    # Missing answers remain pending confirmation, independently of structure.
    warnings = ([f"未识别到以下材料题参考答案：{', '.join(missing_answers)}"] if missing_answers else [])
    if answer_sheet_blocks:
        warnings.append(f"answer_sheet_isolated：已识别答题卡区域（{len(answer_sheet_blocks)} 个段落），其空白小题未导入。")
    unknown = [n for n, kind in kinds.items() if kind == "unknown"]
    if unknown:
        warnings.append(f"以下题目无法可靠区分题型，未自动归入客观题：{unknown}，请核对原文。")
    warning_codes: list[str] = []
    if objective_incomplete:
        warnings.append(f"docx_parse_incomplete：以下客观题题干或选项解析不完整：{objective_incomplete}。")
        warning_codes.append("docx_parse_incomplete")
    if missing_objective_answers:
        warnings.append(f"answer_missing：以下客观题答案未识别：{missing_objective_answers}。")
        warning_codes.append("answer_missing")
    if missing_answers:
        warning_codes.append("answer_missing")
    if rubric_needs_review:
        warnings.append(f"rubric_needs_teacher_review：以下小题未可靠拆分评分点：{rubric_needs_review}。")
        warning_codes.append("rubric_needs_teacher_review")
    elif rubric_candidates_present:
        warnings.append("rubric_needs_teacher_review：自动拆分的候选评分点尚未确认；分值与计分规则需教师决定。")
        warning_codes.append("rubric_needs_teacher_review")
    unknown_scores = [sub["question_id"] for question in material_questions for sub in question["sub_questions"] if sub["max_score"] is None]
    if unknown_scores:
        warnings.append(f"max_score_unknown：以下小题没有明确分值，保持 null：{unknown_scores}。")
        warning_codes.append("max_score_unknown")
    if not objective_questions and not material_questions:
        warnings.append("docx_parse_incomplete：没有识别到题目草稿，请教师核对 Word 段落/表格结构。")
        warning_codes.append("docx_parse_incomplete")
    return {
        "title": title,
        "status": "draft",
        "material_questions": material_questions,
        "objective_questions": objective_questions,
        "objective_answer_key": objective_keys,
        "objective_answer_key_status": "draft",
        "objective_answer_key_confirmation_status": "pending_teacher_confirmation",
        "teacher_confirmation_status": "pending_teacher_confirmation",
        "warning_codes": list(dict.fromkeys(warning_codes)),
        "warnings": warnings
        + (["导入内容是待核对草稿；评分点不会由解析器自动编造，候选 rubric 不含分值，未判定同义点、封顶或重复给分规则。"] if material_questions else ["未识别到可导入的编号材料题小题，请检查文档排版。"]),
        "source_block_count": len(blocks),
        "source_regions": source_regions,
    }
