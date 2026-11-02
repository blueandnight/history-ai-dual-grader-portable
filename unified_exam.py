"""Read-only adapter from confirmed main exams to objective engine input."""
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3


def confirmed_objectives(questions, answers, points):
    if not isinstance(questions, list) or len(questions) > 100:
        raise ValueError("客观题须为1—100道连续编号单选题")
    if not isinstance(answers, dict) or not isinstance(points, dict):
        raise ValueError("客观题答案或分值格式无效")
    rows = []
    seen = set()
    for q in questions:
        if not isinstance(q, dict):
            raise ValueError("客观题结构无效")
        qid = q.get("question_id")
        if not isinstance(qid, str) or not re.fullmatch(r"[1-9][0-9]*", qid) or qid in seen:
            raise ValueError("客观题题号重复或不兼容；须从1连续编号，不会自动重排")
        seen.add(qid)
        if q.get("question_type", "single_choice") != "single_choice":
            raise ValueError("本阶段仅支持A—D单选题")
        options = q.get("options")
        if q.get("needs_teacher_review") or not str(q.get("stem") or "").strip() or not isinstance(options, dict) or set(options) != set("ABCD") or any(not str(v or "").strip() for v in options.values()):
            raise ValueError(f"客观题{qid}题干或选项不完整")
        answer = answers.get(qid)
        if not isinstance(answer, str) or answer not in "ABCD" or len(answer) != 1:
            raise ValueError(f"客观题{qid}答案无效")
        raw = points.get(qid)
        try:
            score = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"客观题{qid}未确认有效分值") from None
        if isinstance(raw, bool) or not math.isfinite(score) or not 0 < score <= 100:
            raise ValueError(f"客观题{qid}分值须为0到100之间的正数")
        rows.append({"question_no": int(qid), "original_question_id": qid, "correct_option": answer, "points": score})
    rows.sort(key=lambda row: row["question_no"])
    if [row["question_no"] for row in rows] != list(range(1, len(rows)+1)):
        raise ValueError("客观题题号不兼容；须从1连续编号，不会自动重排")
    if set(points) != seen or set(answers) != seen:
        raise ValueError("客观题答案、分值与题号范围不一致")
    return rows



def objective_content(meta):
    if not isinstance(meta, dict) or meta.get("teacher_confirmation_status") != "confirmed_by_teacher" or meta.get("objective_answer_key_confirmation_status") != "confirmed_by_teacher":
        raise ValueError("考试内容尚未由教师确认")
    questions = meta.get("objective_questions", [])
    if not questions:
        raise ValueError("本考试没有客观题，无需生成客观题快照")
    key = confirmed_objectives(questions, meta.get("objective_answer_key"), meta.get("objective_points"))
    content = {"answer_key": key, "questions": sorted(questions, key=lambda q: int(q["question_id"])), "mapping": [{"original_question_id": k["original_question_id"], "question_no": k["question_no"]} for k in key]}
    fingerprint = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return dict(ready=True, answer_key=key, questions=content["questions"], question_mapping=content["mapping"], source_fingerprint=fingerprint, max_score=sum(k["points"] for k in key))


def read_source_exam(database_path, exam_id=None):
    """Access is granted by the existing authenticated teacher workspace middleware.

    Main exams currently have workspace-wide visibility, not per-teacher ownership.
    This adapter never grants a wider scope or opens a separate database.
    """
    if exam_id is not None and not re.fullmatch(r"[0-9a-f]{32}", exam_id):
        raise ValueError("正式考试编号无效")
    with closing(sqlite3.connect(Path(database_path).resolve().as_uri()+"?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        sql = "SELECT e.id exam_id,a.id assignment_id,a.name,a.import_metadata_json FROM grading_exams e JOIN assignments a ON a.id=e.assignment_id"
        rows = conn.execute(sql+(" WHERE e.id=?" if exam_id else " ORDER BY e.created_at DESC,e.id"), (exam_id,) if exam_id else ()).fetchall()
    if exam_id and not rows:
        raise LookupError("正式考试不存在或不可访问")
    exams = []
    for row in rows:
        item = {"exam_id": row["exam_id"], "assignment_id": row["assignment_id"], "title": row["name"]}
        try:
            meta = json.loads(row["import_metadata_json"] or "{}")
            item.update(objective_content(meta))
        except (ValueError, TypeError, KeyError) as exc:
            if exam_id:
                raise ValueError(str(exc)) from None
            item.update(ready=False, reason=str(exc))
        if not exam_id:
            item = {k: v for k, v in item.items() if k in {"exam_id", "assignment_id", "title", "ready", "reason", "max_score"}}
        exams.append(item)
    return exams[0] if exam_id else exams
