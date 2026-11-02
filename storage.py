"""Database connection lifetime and read-only restore validation.

Paths and the existing migration callback are supplied explicitly per call.
Schema SQL remains in app until a separately reviewed extraction.
"""
from __future__ import annotations
import sqlite3
import json
from pathlib import Path
from typing import Callable, Any


def connect_database(data_dir: Path, db_path: Path, ensure_schema: Callable) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    database_existed = db_path.exists() and db_path.stat().st_size > 0
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        ensure_schema(conn, database_existed)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return conn


def validate_restored_database(path: Path, supported_schema: int) -> None:
    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        schema_version = None
        if "schema_meta" in tables:
            version_row = conn.execute("SELECT schema_version FROM schema_meta WHERE id = 1").fetchone()
            schema_version = int(version_row[0]) if version_row else None
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise ValueError("备份中的数据库无法打开") from exc
    finally:
        if conn is not None:
            conn.close()
    if quick_check != "ok" or not {"assignments", "submissions", "roster_students"}.issubset(tables):
        raise ValueError("备份中的数据库完整性校验失败")
    if schema_version is not None and schema_version > supported_schema:
        raise ValueError(f"备份数据库 schema 版本 {schema_version} 高于当前程序支持的 {supported_schema}，请使用更新版本恢复。")



def submission_public(row: sqlite3.Row, *, get_db) -> dict[str, Any]:
    result = json.loads(row["result_json"] or "{}")
    teacher_questions = []
    if row['state'] == 'completed':
        conn = get_db()
        try:
            assignment = conn.execute('SELECT sub_questions_json FROM assignments WHERE id = ?', (row['assignment_id'],)).fetchone()
            questions = json.loads(assignment['sub_questions_json'] or '[]') if assignment else []
            ids = (result.get('grading_scope') or {}).get('graded_question_ids')
            teacher_questions = [q for q in questions if not ids or q['id'] in ids]
        finally:
            conn.close()
    photos = json.loads(row["photos_json"] or "[]")
    anonymous = bool(row["marking_code_id"]) if "marking_code_id" in row.keys() else False
    if anonymous:
        result.pop("student_identity", None)
    return {
        "id": row["id"],
        "assignment_id": row["assignment_id"],
        "exam_id": row["exam_id"] if "exam_id" in row.keys() else None,
        "marking_code_id": row["marking_code_id"] if "marking_code_id" in row.keys() else None,
        "marking_code": row["marking_code"] if "marking_code" in row.keys() else "",
        "marking_status": row["marking_status"] if "marking_status" in row.keys() else "",
        "batch_id": row["batch_id"] if "batch_id" in row.keys() else None,
        "sequence": row["batch_sequence"] if "batch_sequence" in row.keys() else None,
        "state": row["state"] if "state" in row.keys() else "completed",
        "source_channel": row["source_channel"] if "source_channel" in row.keys() else "computer",
        "class_name": "" if anonymous else row["class_name"],
        "student_label": row["marking_code"] if anonymous else row["student_label"],
        "student_no": "" if anonymous else row["student_no"],
        "student_name": "" if anonymous else row["student_name"],
        "identity_status": "anonymous" if anonymous else row["identity_status"],
        "identity_method": "marking_code" if anonymous else row["identity_method"],
        "photos": photos,
        "result": result,
        "teacher_scoring_questions": teacher_questions,
        "privacy_status": row["privacy_status"] if "privacy_status" in row.keys() else "legacy_unknown",
        "final_total": row["final_total"],
        "confirmed_at": row["confirmed_at"],
        "confirmation_method": result.get('confirmation_method'),
        "teacher_reconfirmation_required": bool(result.get("teacher_reconfirmation_required")),
        "previous_teacher_final": result.get("previous_teacher_final"),
        "manual_reviewed": bool(row["manual_reviewed"]) if "manual_reviewed" in row.keys() else False,
        "review_note": row["review_note"] if "review_note" in row.keys() else "",
        "uncertain_model_call": bool(row["uncertain_model_call"]) if "uncertain_model_call" in row.keys() else False,
        "photos_deleted": bool(result.get("photos_deleted", False)),
    }


def submission_list_public(row: sqlite3.Row) -> dict[str, Any]:
    """Return only the local history fields needed for photo-cache cleanup."""
    result = json.loads(row["result_json"] or "{}")
    photos = json.loads(row["photos_json"] or "[]")
    anonymous = bool(row["marking_code_id"]) if "marking_code_id" in row.keys() else False
    if anonymous:
        result.pop("student_identity", None)
    return {
        "id": row["id"],
        "assignment_id": row["assignment_id"],
        "exam_id": row["exam_id"] if "exam_id" in row.keys() else None,
        "marking_code_id": row["marking_code_id"] if "marking_code_id" in row.keys() else None,
        "marking_code": row["marking_code"] if "marking_code" in row.keys() else "",
        "marking_status": row["marking_status"] if "marking_status" in row.keys() else "",
        "batch_id": row["batch_id"] if "batch_id" in row.keys() else None,
        "sequence": row["batch_sequence"] if "batch_sequence" in row.keys() else None,
        "state": row["state"],
        "student_label": row["marking_code"] if anonymous else row["student_label"],
        "student_no": "" if anonymous else row["student_no"],
        "student_name": "" if anonymous else row["student_name"],
        "final_total": row["final_total"],
        "confirmed_at": row["confirmed_at"],
        "confirmation_method": result.get('confirmation_method'),
        "suggested_total": result.get("suggested_total"),
        "max_total": result.get("max_total"),
        "grading_scope": result.get("grading_scope"),
        "teacher_reconfirmation_required": bool(result.get("teacher_reconfirmation_required")),
        "previous_teacher_final": result.get("previous_teacher_final"),
        "result_mode": result.get("mode", ""),
        "has_dual_review": isinstance(result.get("dual_review"), dict),
        "photos": photos,
        "photos_deleted": bool(result.get("photos_deleted", False)),
    }


def load_submission_public(submission_id: str, *, get_db, submission_public) -> dict[str, Any] | None:
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
        return submission_public(row) if row else None
    finally:
        conn.close()


def assignment_submission_rows(conn: sqlite3.Connection, assignment_id: str):
    return conn.execute(
                    "SELECT submissions.* FROM submissions LEFT JOIN grading_batches ON grading_batches.id = submissions.batch_id "
                    "WHERE submissions.assignment_id = ? "
                    "ORDER BY COALESCE(grading_batches.created_at, submissions.created_at) DESC, "
                    "CASE WHEN submissions.batch_id IS NULL THEN 1 ELSE 0 END, "
                    "CASE WHEN submissions.batch_id IS NOT NULL THEN submissions.batch_sequence END ASC, "
                    "submissions.created_at DESC LIMIT 500",
                    (assignment_id,),
                ).fetchall()


def roster_class_rows(conn: sqlite3.Connection):
    return conn.execute(
                "WITH names AS (SELECT id AS class_id,class_name FROM roster_classes UNION ALL SELECT NULL,class_name FROM roster_students WHERE class_id IS NULL AND class_name NOT IN (SELECT class_name FROM roster_classes) GROUP BY class_name) SELECT n.class_id,n.class_name,COUNT(s.id) AS student_count FROM names n LEFT JOIN roster_students s ON s.class_name=n.class_name AND s.enabled=1 GROUP BY n.class_id,n.class_name ORDER BY n.class_name"
            ).fetchall()


def roster_student_rows(conn: sqlite3.Connection, class_name: str):
    return conn.execute(
                "SELECT id,id AS student_id,class_id,student_no,student_name,student_name AS display_name,enabled FROM roster_students WHERE class_name = ? ORDER BY student_no, student_name",
                (class_name,),
            ).fetchall()
