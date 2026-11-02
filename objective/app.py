from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from PIL import Image, ImageOps, UnidentifiedImageError

import web_auth

from .omr import recognize_omr
from .recognition import recognize_handwritten_abcd, recognizer_version
from .templates import BUILTIN_TEMPLATES, make_grid_config


OPTIONS = {"A", "B", "C", "D"}
MAX_FILES = 100
MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_TOTAL_BYTES = 220 * 1024 * 1024
_JOB_THREADS: set[threading.Thread] = set()
_JOB_LOCK = threading.Lock()
SCHEMA_VERSION = 5
JOB_TERMINAL_STATES = {"completed", "needs_review", "recoverable", "failed"}
SUBMISSION_TERMINAL_STATES = {"completed", "needs_review", "recoverable", "failed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _connect(db_path: Path) -> sqlite3.Connection:
    class ClosingConnection(sqlite3.Connection):
        def __exit__(self, exc_type, exc_value, traceback):
            try:
                return super().__exit__(exc_type, exc_value, traceback)
            finally:
                self.close()

    conn = sqlite3.connect(str(db_path), timeout=10, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _rename_stage3_tables(conn: sqlite3.Connection) -> None:
    # Move pre-migration stage3 tables aside from legacy material-grade names.
    signatures = {
        "assessments": {"assessment_id", "answer_key_json", "answer_key_sha256"},
        "omr_templates": {"template_id", "config_json", "config_sha256"},
        "jobs": {"job_id", "assessment_id", "mode", "total_submissions"},
        "submissions": {"submission_id", "anonymous_code", "assessment_id", "max_score"},
        "submission_pages": {"page_id", "stored_path", "image_sha256"},
        "review_events": {"review_id", "question_no", "revised_option"},
        "audit_events": {"event_id", "event_type", "details_json"},
    }
    for old_name, required in signatures.items():
        new_name = f"objective_{old_name}"
        if not _table_exists(conn, old_name) or _table_exists(conn, new_name):
            continue
        if required <= _table_columns(conn, old_name):
            conn.execute(f'ALTER TABLE "{old_name}" RENAME TO "{new_name}"')


def _apply_objective_v1(conn: sqlite3.Connection) -> None:
    statements = [
        '''CREATE TABLE IF NOT EXISTS objective_assessments (
            assessment_id TEXT PRIMARY KEY, title TEXT NOT NULL, answer_version INTEGER NOT NULL,
            answer_key_json TEXT NOT NULL, answer_key_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS objective_omr_templates (
            template_id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
            config_json TEXT NOT NULL, config_sha256 TEXT NOT NULL, calibration_sha256 TEXT,
            confirmed INTEGER NOT NULL, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS objective_jobs (
            job_id TEXT PRIMARY KEY, assessment_id TEXT NOT NULL REFERENCES objective_assessments(assessment_id),
            mode TEXT NOT NULL, grouping TEXT NOT NULL, template_id TEXT, status TEXT NOT NULL,
            total_files INTEGER NOT NULL, total_submissions INTEGER NOT NULL,
            processed_submissions INTEGER NOT NULL DEFAULT 0, complete_submissions INTEGER NOT NULL DEFAULT 0,
            review_submissions INTEGER NOT NULL DEFAULT 0, error_submissions INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, finished_at TEXT)''',
        '''CREATE TABLE IF NOT EXISTS objective_submissions (
            submission_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES objective_jobs(job_id),
            assessment_id TEXT NOT NULL REFERENCES objective_assessments(assessment_id),
            anonymous_code TEXT NOT NULL UNIQUE, state TEXT NOT NULL, score REAL, max_score REAL NOT NULL,
            page_count INTEGER NOT NULL, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS objective_submission_pages (
            page_id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES objective_submissions(submission_id),
            page_no INTEGER NOT NULL, stored_path TEXT NOT NULL, image_sha256 TEXT NOT NULL,
            byte_count INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
            recognition_state TEXT NOT NULL DEFAULT 'queued', UNIQUE(submission_id, page_no))''',
        '''CREATE TABLE IF NOT EXISTS objective_answers (
            submission_id TEXT NOT NULL REFERENCES objective_submissions(submission_id), question_no INTEGER NOT NULL,
            candidate_option TEXT, final_option TEXT, max_points REAL NOT NULL, awarded_points REAL,
            state TEXT NOT NULL, source TEXT NOT NULL, confidence REAL, evidence_json TEXT, reason TEXT,
            PRIMARY KEY(submission_id, question_no))''',
        '''CREATE TABLE IF NOT EXISTS objective_review_events (
            review_id TEXT PRIMARY KEY, submission_id TEXT NOT NULL REFERENCES objective_submissions(submission_id),
            question_no INTEGER NOT NULL, previous_option TEXT, revised_option TEXT, reason TEXT NOT NULL,
            created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS objective_audit_events (
            event_id TEXT PRIMARY KEY, job_id TEXT, submission_id TEXT, page_id TEXT,
            event_type TEXT NOT NULL, image_sha256 TEXT, details_json TEXT NOT NULL, created_at TEXT NOT NULL)''',
        "CREATE INDEX IF NOT EXISTS idx_objective_submissions_assessment ON objective_submissions(assessment_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_objective_answers_state ON objective_answers(state)",
        "CREATE INDEX IF NOT EXISTS idx_objective_audit_job ON objective_audit_events(job_id, created_at)",
    ]
    for statement in statements:
        conn.execute(statement)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, name: str, declaration: str) -> None:
    if name not in _table_columns(conn, table):
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}')


def _apply_current_schema(conn: sqlite3.Connection) -> None:
    for table, name, declaration in (
        ("objective_jobs", "recovery_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("objective_jobs", "failure_code", "TEXT"),
        ("objective_jobs", "lease_updated_at", "TEXT"),
        ("objective_submissions", "recovery_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("objective_submissions", "failure_code", "TEXT"),
        ("objective_submission_pages", "recognition_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("objective_submission_pages", "failure_code", "TEXT"),
        ("objective_review_events", "page_id", "TEXT"),
    ):
        _add_column_if_missing(conn, table, name, declaration)
    conn.execute('''CREATE TABLE IF NOT EXISTS objective_review_items (
        review_item_id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
        job_id TEXT NOT NULL REFERENCES objective_jobs(job_id),
        submission_id TEXT NOT NULL REFERENCES objective_submissions(submission_id), page_id TEXT,
        question_no INTEGER, reason TEXT NOT NULL, error_type TEXT, state TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL, resolved_at TEXT, resolution TEXT)''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_objective_review_open ON objective_review_items(state,created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_objective_jobs_status ON objective_jobs(status,created_at)")
    conn.execute("UPDATE objective_jobs SET status='completed' WHERE status='complete'")
    conn.execute("UPDATE objective_submissions SET state='completed' WHERE state='complete'")
    # Backfill the durable queue so unresolved rows from the previous stage3 schema
    # become visible immediately after the migration.
    for row in conn.execute("SELECT a.submission_id,s.job_id,a.question_no,a.reason,a.state,p.page_id FROM objective_answers a JOIN objective_submissions s ON s.submission_id=a.submission_id LEFT JOIN objective_submission_pages p ON p.submission_id=s.submission_id WHERE a.state IN ('needs_review','processing_error')"):
        _queue_review_item(conn,job_id=row["job_id"],submission_id=row["submission_id"],question_no=int(row["question_no"]),page_id=row["page_id"],reason=row["reason"] or "answer_needs_review",error_type="MigrationBackfill" if row["state"]=="processing_error" else None)
    for row in conn.execute("SELECT submission_id,job_id,failure_code FROM objective_submissions WHERE state IN ('recoverable','failed') AND failure_code IS NOT NULL"):
        _queue_review_item(conn,job_id=row["job_id"],submission_id=row["submission_id"],reason=row["failure_code"],error_type="MigrationBackfill")


def _apply_objective_v3(conn: sqlite3.Connection) -> None:
    # Keep the local OCR workflow readable while adding private vision-run metadata.
    _add_column_if_missing(conn, "objective_submissions", "privacy_status", "TEXT NOT NULL DEFAULT 'unchecked'")
    _add_column_if_missing(conn, "objective_submissions", "vision_result_json", "TEXT NOT NULL DEFAULT '{}'")


def _init_db(
    db_path: Path,
    *,
    fail_after_migration: str | None = None,
    set_journal_mode: bool = True,
) -> None:
    # Migrations are atomic across objective v1 and the current schema.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        if set_journal_mode:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError("objective_schema_version_newer_than_application")
        if version < 1:
            _rename_stage3_tables(conn)
            _apply_objective_v1(conn)
            conn.execute("PRAGMA user_version=1")
            if fail_after_migration == "objective_v1":
                raise RuntimeError("injected_migration_failure")
            version = 1
        if version < 2:
            _apply_current_schema(conn)
            conn.execute("PRAGMA user_version=2")
            if fail_after_migration == "current":
                raise RuntimeError("injected_migration_failure")
            version = 2
        if version < 3:
            _apply_objective_v3(conn)
            conn.execute("PRAGMA user_version=3")
        if version < 4:
            _add_column_if_missing(conn, "objective_assessments", "source_exam_id", "TEXT")
            _add_column_if_missing(conn, "objective_assessments", "source_fingerprint", "TEXT")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_objective_source_snapshot ON objective_assessments(source_exam_id,source_fingerprint) WHERE source_exam_id IS NOT NULL")
            conn.execute("PRAGMA user_version=4")
        if version < 5:
            from answer_links import migrate
            migrate(conn)
            conn.execute('PRAGMA user_version=5')
        from exam_identity import migrate as migrate_identities
        migrate_identities(conn)
        for item in BUILTIN_TEMPLATES:
            config_json = json.dumps(item["config"], sort_keys=True)
            conn.execute(
                "INSERT OR IGNORE INTO objective_omr_templates(template_id,name,version,config_json,config_sha256,confirmed,created_at) VALUES(?,?,?,?,?,1,?)",
                (item["id"], item["name"], 1, config_json, _sha256(config_json.encode()), _utc_now()),
            )

def _insert_audit(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    job_id: str | None = None,
    submission_id: str | None = None,
    page_id: str | None = None,
    image_sha256: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    if job_id is None and submission_id:
        row = conn.execute("SELECT job_id FROM objective_submissions WHERE submission_id=?", (submission_id,)).fetchone()
        if row:
            job_id = str(row["job_id"])
    if page_id and submission_id is None:
        row = conn.execute("SELECT submission_id FROM objective_submission_pages WHERE page_id=?", (page_id,)).fetchone()
        if row:
            submission_id = str(row["submission_id"])
    if job_id is None and page_id:
        row = conn.execute("SELECT s.job_id,p.submission_id FROM objective_submission_pages p JOIN objective_submissions s ON s.submission_id=p.submission_id WHERE p.page_id=?", (page_id,)).fetchone()
        if row:
            job_id = str(row["job_id"])
            submission_id = submission_id or str(row["submission_id"])
    conn.execute(
        "INSERT INTO objective_audit_events(event_id,job_id,submission_id,page_id,event_type,image_sha256,details_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, job_id, submission_id, page_id, event_type, image_sha256, json.dumps(details or {}, sort_keys=True), _utc_now()),
    )


def _validate_answer_key(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise ValueError("答案键须包含1到100题")
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("答案键格式错误")
        try:
            question_no = int(row.get("question_no"))
            points = float(row.get("points"))
        except (TypeError, ValueError):
            raise ValueError("题号或分值格式错误") from None
        option = str(row.get("correct_option", "")).strip().upper()
        if question_no < 1 or question_no in seen or option not in OPTIONS or not (0 < points <= 100):
            raise ValueError("答案键含重复题号、非法选项或分值")
        seen.add(question_no)
        normalized.append({"question_no": question_no, "correct_option": option, "points": points})
    normalized.sort(key=lambda r: r["question_no"])
    if [r["question_no"] for r in normalized] != list(range(1, len(normalized) + 1)):
        raise ValueError("题号必须从1连续编号")
    return normalized


def _decode_image(raw: bytes) -> tuple[bytes, int, int, str]:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            width, height = image.size
            if width < 100 or height < 100 or width * height > 40_000_000:
                raise ValueError("图片尺寸不符合本地演示限制")
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue(), width, height, "image/png"
    except (UnidentifiedImageError, OSError):
        raise ValueError("上传文件不是可解码图片") from None


def _anonymous_code(conn: sqlite3.Connection) -> str:
    for _ in range(12):
        code = secrets.token_hex(5).upper()
        if conn.execute("SELECT 1 FROM objective_submissions WHERE anonymous_code=?", (code,)).fetchone() is None:
            return code
    raise RuntimeError("匿名码碰撞重试耗尽")


def _queue_review_item(
    conn: sqlite3.Connection, *, job_id: str, submission_id: str, reason: str,
    page_id: str | None = None, question_no: int | None = None, error_type: str | None = None,
) -> None:
    dedupe_key = f"{submission_id}:submission" if question_no is None else f"{submission_id}:q{question_no}"
    conn.execute(
        "INSERT INTO objective_review_items(review_item_id,dedupe_key,job_id,submission_id,page_id,question_no,reason,error_type,state,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,'open',?) ON CONFLICT(dedupe_key) DO UPDATE SET job_id=excluded.job_id,page_id=excluded.page_id,reason=excluded.reason,error_type=excluded.error_type,state='open',resolved_at=NULL,resolution=NULL",
        (uuid.uuid4().hex,dedupe_key,job_id,submission_id,page_id,question_no,reason,error_type,_utc_now()),
    )


def _update_submission_score(conn: sqlite3.Connection, submission_id: str) -> str:
    rows = conn.execute("SELECT state,awarded_points FROM objective_answers WHERE submission_id=?", (submission_id,)).fetchall()
    if not rows or any(row["state"] in ("needs_review", "processing_error") or row["awarded_points"] is None for row in rows):
        conn.execute("UPDATE objective_submissions SET state='needs_review', score=NULL WHERE submission_id=?", (submission_id,))
        return "needs_review"
    score = sum(float(row["awarded_points"]) for row in rows)
    conn.execute("UPDATE objective_submissions SET state='completed', score=?,failure_code=NULL WHERE submission_id=?", (score, submission_id))
    conn.execute("UPDATE objective_review_items SET state='resolved',resolved_at=?,resolution=COALESCE(resolution,'all_answers_resolved') WHERE submission_id=? AND state='open'", (_utc_now(),submission_id))
    return "completed"


def _finalize_submission(app: Flask, submission_id: str, *, recovered: bool = False) -> str:
    with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
        submission = conn.execute("SELECT job_id FROM objective_submissions WHERE submission_id=?", (submission_id,)).fetchone()
        if not submission:
            return "failed"
        state = _update_submission_score(conn, submission_id)
        answer_rows = conn.execute("SELECT question_no,state FROM objective_answers WHERE submission_id=? ORDER BY question_no", (submission_id,)).fetchall()
        page_by_question = {}
        for answer in answer_rows:
            item = conn.execute("SELECT page_id FROM objective_review_items WHERE submission_id=? AND question_no=? AND state='open' ORDER BY created_at DESC LIMIT 1", (submission_id,answer["question_no"])).fetchone()
            if answer["state"] in ("needs_review", "processing_error"):
                _queue_review_item(conn, job_id=submission["job_id"],submission_id=submission_id,question_no=int(answer["question_no"]),page_id=item["page_id"] if item else None,reason="answer_needs_review")
            else:
                conn.execute("UPDATE objective_review_items SET state='resolved',resolved_at=?,resolution='automatic_retry_completed' WHERE submission_id=? AND question_no=? AND state='open'", (_utc_now(),submission_id,answer["question_no"]))
        _insert_audit(conn, "submission_recovered" if recovered else ("submission_scored" if state == "completed" else "submission_needs_review"),job_id=submission["job_id"],submission_id=submission_id,details={"state":state,"answer_count":len(answer_rows),"scoring_engine":"deterministic"})
        return state


def _process_submission(app: Flask, submission_id: str, mode: str, template_config: dict[str, Any] | None) -> str:
    db_path: Path = app.config["OBJECTIVE_DB_PATH"]
    with _connect(db_path) as conn:
        submission = conn.execute("SELECT * FROM objective_submissions WHERE submission_id=?", (submission_id,)).fetchone()
        if not submission:
            raise ValueError("submission_missing")
        assessment = conn.execute("SELECT * FROM objective_assessments WHERE assessment_id=?", (submission["assessment_id"],)).fetchone()
        key_rows = json.loads(assessment["answer_key_json"])
        key_by_q = {row["question_no"]: row for row in key_rows}
        pages = conn.execute("SELECT * FROM objective_submission_pages WHERE submission_id=? ORDER BY page_no", (submission_id,)).fetchall()
        if not pages:
            raise ValueError("submission_pages_missing")
        conn.execute("UPDATE objective_submissions SET state='processing',score=NULL,failure_code=NULL WHERE submission_id=?", (submission_id,))
        conn.execute("UPDATE objective_submission_pages SET recognition_state='processing',recognition_attempts=recognition_attempts+1,failure_code=NULL WHERE submission_id=?", (submission_id,))
        for page in pages:
            _insert_audit(conn,"page_processing_started",job_id=submission["job_id"],submission_id=submission_id,page_id=page["page_id"],image_sha256=page["image_sha256"],details={"recognition_attempt":int(page["recognition_attempts"])+1})

    per_question: dict[int, list[dict[str, Any]]] = {}
    answer_page: dict[int,str] = {}
    omr_question_total = 0
    omr_layout_error = False
    auto_templates = bool((template_config or {}).get("automatic"))
    template_map = (template_config or {}).get("templates", {})
    if mode == "vision":
        image_paths: list[Path] = []
        for page in pages:
            path = Path(page["stored_path"])
            content = path.read_bytes()
            if not secrets.compare_digest(_sha256(content), page["image_sha256"]):
                raise ValueError("stored_image_hash_mismatch")
            image_paths.append(path)
        grader = app.config.get("OBJECTIVE_VISION_GRADER")
        if not callable(grader):
            raise ValueError("vision_grading_unavailable")
        vision_result = grader(image_paths, key_rows)
        model_rows = vision_result.get("models", {}) if isinstance(vision_result, dict) else {}
        if not isinstance(model_rows, dict):
            model_rows = {}
        safe_model_rows = {side: model_rows.get(side, {}) for side in ("A", "B")}
        with _connect(db_path) as conn:
            conn.execute("UPDATE objective_submissions SET vision_result_json=? WHERE submission_id=?", (json.dumps(safe_model_rows, ensure_ascii=False), submission_id))
            for page in pages:
                conn.execute("UPDATE objective_submission_pages SET recognition_state='auto',failure_code=NULL WHERE page_id=?", (page["page_id"],))
                _insert_audit(conn, "vision_page_sent", job_id=submission["job_id"], submission_id=submission_id, page_id=page["page_id"], image_sha256=page["image_sha256"], details={"page_no": int(page["page_no"]), "image_count": len(pages)})
        for key in key_rows:
            qno = int(key["question_no"])
            decisions: dict[str, dict[str, Any]] = {}
            for side in ("A", "B"):
                model_row = safe_model_rows.get(side) if isinstance(safe_model_rows.get(side), dict) else {}
                answer_map = model_row.get("answers") if isinstance(model_row.get("answers"), dict) else {}
                decision = answer_map.get(str(qno), {})
                decisions[side] = {"run_status": model_row.get("run_status", "failed"), **decision} if isinstance(decision, dict) else {"run_status": model_row.get("run_status", "failed"), "status": "question_unmatched", "option": None}
            a, b = decisions["A"], decisions["B"]
            a_status, b_status = a.get("status"), b.get("status")
            a_option = a.get("option") if a.get("option") in OPTIONS else None
            b_option = b.get("option") if b.get("option") in OPTIONS else None
            same_answer = a_status == b_status and a_status in ("answered", "blank") and a_option == b_option
            candidate_option = a_option if same_answer else None
            if same_answer:
                state, reason = "auto", None
                option = candidate_option
                blank = a_status == "blank"
            else:
                state, option, blank = "needs_review", None, False
                if a.get("run_status") != "ok" or b.get("run_status") != "ok":
                    reason = "model_run_failed"
                elif a_status == "question_unmatched" or b_status == "question_unmatched":
                    reason = "model_question_unmatched"
                elif a_status == "uncertain" or b_status == "uncertain":
                    reason = "model_uncertain"
                else:
                    reason = "model_disagreement"
            pages_used = [row.get("page_no") for row in decisions.values() if isinstance(row.get("page_no"), int) and 1 <= row["page_no"] <= len(pages)]
            page_no = pages_used[0] if pages_used else 1
            page_id = pages[page_no - 1]["page_id"]
            confidence_values = [row.get("confidence") for row in decisions.values() if isinstance(row.get("confidence"), (int, float))]
            evidence = {"models": decisions, "agreed": bool(same_answer)}
            per_question[qno] = [{"option": option, "candidate_option": candidate_option, "state": state, "reason": reason, "confidence": min(confidence_values) if confidence_values else None, "evidence": evidence, "blank": blank, "page_id": page_id, "page_no": page_no}]
            answer_page[qno] = page_id
    for page in pages if mode != "vision" else []:
        start = time.perf_counter()
        content = Path(page["stored_path"]).read_bytes()
        current_hash = _sha256(content)
        hash_matches = secrets.compare_digest(current_hash, page["image_sha256"])
        page_template_id = None
        question_offset = omr_question_total if mode == "omr" and submission["page_count"] > 1 else 0
        if not hash_matches:
            page_result = {}
            page_status = "needs_review"
            page_reason = "stored_image_hash_mismatch"
            omr_layout_error = mode == "omr"
        elif mode == "omr":
            matched = []
            for candidate_id, candidate_config in template_map.items():
                result = recognize_omr(content, candidate_config)
                if result.diagnostics.get("alignment") == "aligned" and result.diagnostics.get("signature_match") is True:
                    matched.append((candidate_id, candidate_config, result))
                if not auto_templates:
                    break
            if len(matched) == 1:
                page_template_id, page_config, result = matched[0]
                page_result = result.answers
                page_status = result.status
                page_reason = None
                omr_question_total += int(page_config["question_count"])
            else:
                page_result = {}
                page_status = "needs_review"
                page_reason = "layout_signature_mismatch" if not matched else "ambiguous_template_match"
                omr_layout_error = True
        elif mode == "handwriting":
            page_result = recognize_handwritten_abcd(content, len(key_by_q))
            if any(answer["state"] != "auto" for answer in page_result.values()):
                for answer in page_result.values():
                    if answer["state"] == "auto":
                        answer["state"] = "needs_review"
                        answer["reason"] = "same_page_review_required"
            page_status = "needs_review" if any(a["state"] != "auto" for a in page_result.values()) else "auto"
            page_reason = None
        else:
            page_result = {}
            page_status = "needs_review"
            page_reason = "recognizer_unavailable"

        if not hash_matches:
            with _connect(db_path) as audit_conn:
                _insert_audit(audit_conn,"stored_image_hash_mismatch",job_id=submission["job_id"],submission_id=submission_id,page_id=page["page_id"],image_sha256=current_hash,details={"source_hash_matches_stored":False})
        if mode == "omr" and page_status == "needs_review" and not page_result:
            qcount = len(key_by_q) if submission["page_count"] == 1 else int(next(iter(template_map.values()))["question_count"]) if template_map else len(key_by_q)
            page_result = {q: {"option":None,"state":"needs_review","reason":page_reason,"confidence":None,"evidence":None} for q in range(1,qcount+1)}

        for local_qno, answer in page_result.items():
            qno = int(local_qno) + question_offset
            if qno in key_by_q:
                per_question.setdefault(qno, []).append({**answer,"page_id":page["page_id"],"page_no":page["page_no"]})
                answer_page[qno] = page["page_id"]
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        with _connect(db_path) as update_conn:
            update_conn.execute("UPDATE objective_submission_pages SET recognition_state=?,failure_code=NULL WHERE page_id=?", (page_status,page["page_id"]))
            _insert_audit(update_conn,"recognition_completed",job_id=submission["job_id"],submission_id=submission_id,page_id=page["page_id"],image_sha256=current_hash,details={"source_hash_matches_stored":hash_matches,"recognizer":"local_omr_opencv" if mode=="omr" else f"rapidocr-onnxruntime/{recognizer_version()}","page_state":page_status,"template_id":page_template_id,"question_offset":question_offset,"elapsed_ms":elapsed_ms})

    omr_range_mismatch = mode == "omr" and (omr_layout_error or omr_question_total != len(key_by_q))
    # Answers commit before the summary transaction. A restart can safely finalize this complete set.
    with _connect(db_path) as conn:
        for qno,key in key_by_q.items():
            candidates=per_question.get(qno,[])
            if len(candidates)!=1:
                state="needs_review"
                reason="missing_answer" if not candidates else "duplicate_question_across_pages"
                candidate_option=option=confidence=evidence=None
            else:
                item=candidates[0]
                candidate_option=item.get("candidate_option") if item.get("candidate_option") in OPTIONS else (item.get("option") if item.get("option") in OPTIONS else None)
                is_valid_vision_blank = mode == "vision" and item["state"] == "auto" and bool(item.get("blank"))
                state="auto" if item["state"]=="auto" and (candidate_option in OPTIONS or is_valid_vision_blank) else "needs_review"
                reason=item.get("reason") if state=="needs_review" else None
                option=item.get("option") if state=="auto" else None
                confidence=item.get("confidence")
                evidence=item.get("evidence")
            if omr_range_mismatch:
                state="needs_review"; option=None; reason="omr_template_page_ranges_do_not_match_answer_key"
            awarded=float(key["points"]) if state=="auto" and option==key["correct_option"] else (0.0 if state=="auto" else None)
            conn.execute(
                "INSERT INTO objective_answers(submission_id,question_no,candidate_option,final_option,max_points,awarded_points,state,source,confidence,evidence_json,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(submission_id,question_no) DO UPDATE SET candidate_option=excluded.candidate_option,final_option=excluded.final_option,max_points=excluded.max_points,awarded_points=excluded.awarded_points,state=excluded.state,source=excluded.source,confidence=excluded.confidence,evidence_json=excluded.evidence_json,reason=excluded.reason",
                (submission_id,qno,candidate_option,option,float(key["points"]),awarded,state,mode,confidence,json.dumps(evidence) if evidence else None,reason),
            )
            if state=="needs_review":
                _queue_review_item(conn,job_id=submission["job_id"],submission_id=submission_id,page_id=answer_page.get(qno),question_no=qno,reason=reason or "answer_needs_review")
            else:
                conn.execute("UPDATE objective_review_items SET state='resolved',resolved_at=?,resolution='automatic_retry_completed' WHERE submission_id=? AND question_no=? AND state='open'",(_utc_now(),submission_id,qno))
        _insert_audit(conn,"answers_persisted",job_id=submission["job_id"],submission_id=submission_id,details={"answer_count":len(key_by_q),"answer_set_sha256":_sha256(json.dumps([{ "q":q,"state":per_question.get(q,[{}])[0].get("state") } for q in key_by_q],sort_keys=True).encode())})
    return _finalize_submission(app,submission_id)


def _load_job(app: Flask, job_id: str) -> tuple[dict[str, Any], list[list[dict[str, Any]]], dict[str, Any] | None]:
    with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
        job=conn.execute("SELECT * FROM objective_jobs WHERE job_id=?",(job_id,)).fetchone()
        if not job:
            raise ValueError("job_missing")
        groups=[]
        submissions=conn.execute("SELECT submission_id FROM objective_submissions WHERE job_id=? ORDER BY rowid",(job_id,)).fetchall()
        for submission in submissions:
            pages=conn.execute("SELECT page_id,page_no,image_sha256 FROM objective_submission_pages WHERE submission_id=? ORDER BY page_no",(submission["submission_id"],)).fetchall()
            groups.append([{"submission_id":submission["submission_id"],"page_id":row["page_id"],"page_no":row["page_no"],"sha256":row["image_sha256"]} for row in pages])
        template_id=job["template_id"]
        if job["mode"]=="omr":
            if template_id in (None,"","auto"):
                rows=conn.execute("SELECT template_id,config_json FROM objective_omr_templates WHERE confirmed=1 ORDER BY template_id").fetchall()
                config={"automatic":True,"templates":{row["template_id"]:json.loads(row["config_json"]) for row in rows}}
            else:
                row=conn.execute("SELECT template_id,config_json FROM objective_omr_templates WHERE template_id=? AND confirmed=1",(template_id,)).fetchone()
                config={"automatic":False,"templates":{row["template_id"]:json.loads(row["config_json"])}} if row else {"automatic":False,"templates":{}}
        else:
            config=None
        return dict(job),groups,config


def _refresh_job_summary(conn: sqlite3.Connection, job_id: str, *, event: str | None = None) -> str:
    states=[row["state"] for row in conn.execute("SELECT state FROM objective_submissions WHERE job_id=?",(job_id,))]
    done=sum(state in SUBMISSION_TERMINAL_STATES for state in states)
    complete=states.count("completed")
    review=states.count("needs_review")
    errors=conn.execute("SELECT COUNT(*) FROM objective_submissions WHERE job_id=? AND failure_code IS NOT NULL",(job_id,)).fetchone()[0]
    if any(state in ("queued","processing") for state in states):
        status="processing"
    elif "failed" in states:
        status="failed"
    elif "recoverable" in states:
        status="recoverable"
    elif review:
        status="needs_review"
    else:
        status="completed"
    finished=None if status=="processing" else _utc_now()
    conn.execute("UPDATE objective_jobs SET status=?,processed_submissions=?,complete_submissions=?,review_submissions=?,error_submissions=?,finished_at=? WHERE job_id=?",(status,done,complete,review,errors,finished,job_id))
    if event:
        _insert_audit(conn,event,job_id=job_id,details={"status":status,"processed":done,"completed":complete,"needs_review":review,"errors":errors})
    return status


def _mark_submission_for_review(conn: sqlite3.Connection, submission_id: str, *, error_type: str, reason: str, recoverable: bool, invalidate_existing: bool = False) -> None:
    submission=conn.execute("SELECT * FROM objective_submissions WHERE submission_id=?",(submission_id,)).fetchone()
    assessment=conn.execute("SELECT answer_key_json FROM objective_assessments WHERE assessment_id=?",(submission["assessment_id"],)).fetchone()
    keys=json.loads(assessment["answer_key_json"])
    pages=conn.execute("SELECT page_id,page_no FROM objective_submission_pages WHERE submission_id=? ORDER BY page_no",(submission_id,)).fetchall()
    job=conn.execute("SELECT mode FROM objective_jobs WHERE job_id=?",(submission["job_id"],)).fetchone()
    job_id=submission["job_id"]
    target="recoverable" if recoverable else "needs_review"
    conn.execute("UPDATE objective_submissions SET state=?,score=NULL,failure_code=? WHERE submission_id=?",(target,reason,submission_id))
    page_for_q=pages[0]["page_id"] if pages else None
    for key in keys:
        q=int(key["question_no"])
        existing=conn.execute("SELECT state FROM objective_answers WHERE submission_id=? AND question_no=?",(submission_id,q)).fetchone()
        if invalidate_existing or not existing or existing["state"] in ("processing_error","needs_review"):
            conn.execute("INSERT INTO objective_answers(submission_id,question_no,candidate_option,final_option,max_points,awarded_points,state,source,confidence,evidence_json,reason) VALUES(?,?,NULL,NULL,?,NULL,'processing_error',?,NULL,NULL,?) ON CONFLICT(submission_id,question_no) DO UPDATE SET final_option=NULL,awarded_points=NULL,state='processing_error',source=excluded.source,reason=excluded.reason",(submission_id,q,float(key["points"]),job["mode"],reason))
        _queue_review_item(conn,job_id=job_id,submission_id=submission_id,page_id=page_for_q,question_no=q,reason=reason,error_type=error_type)
    _queue_review_item(conn,job_id=job_id,submission_id=submission_id,page_id=page_for_q,reason=reason,error_type=error_type)
    _insert_audit(conn,"processing_error",job_id=job_id,submission_id=submission_id,page_id=page_for_q,details={"error_type":error_type,"failure_code":reason,"recoverable":recoverable})


def _process_job(app: Flask, job_id: str) -> None:
    db_path: Path = app.config["OBJECTIVE_DB_PATH"]
    try:
        job,groups,template_config=_load_job(app,job_id)
        mode=job["mode"]
        with _connect(db_path) as conn:
            conn.execute("UPDATE objective_jobs SET status='processing',lease_updated_at=? WHERE job_id=?",(_utc_now(),job_id))
            _insert_audit(conn,"job_started",job_id=job_id,details={"mode":mode,"submission_count":len(groups),"pages":sum(len(group) for group in groups)})
        for group in groups:
            if not group:
                continue
            submission_id=group[0]["submission_id"]
            with _connect(db_path) as conn:
                current=conn.execute("SELECT state FROM objective_submissions WHERE submission_id=?",(submission_id,)).fetchone()
            if not current or current["state"] in SUBMISSION_TERMINAL_STATES:
                continue
            try:
                result_state=_process_submission(app,submission_id,mode,template_config)
                if result_state in ("needs_review","completed"):
                    with _connect(db_path) as conn:
                        conn.execute("UPDATE objective_submissions SET failure_code=NULL WHERE submission_id=?",(submission_id,))
            except Exception as exc:
                is_recoverable=not isinstance(exc,(FileNotFoundError,PermissionError))
                reason = "stored_image_hash_mismatch" if str(exc) == "stored_image_hash_mismatch" else ("processing_failed" if is_recoverable else "source_image_unavailable")
                with _connect(db_path) as conn:
                    _mark_submission_for_review(conn,submission_id,error_type=type(exc).__name__,reason=reason,recoverable=is_recoverable)
                    _insert_audit(conn,"recognition_failed",job_id=job_id,submission_id=submission_id,details={"error_type":type(exc).__name__,"failure_code":reason})
        with _connect(db_path) as conn:
            _refresh_job_summary(conn,job_id,event="job_summary_written")
    except Exception as exc:
        # Covers startup/configuration/template failures before the per-submission loop.
        with _connect(db_path) as conn:
            affected=conn.execute("SELECT submission_id FROM objective_submissions WHERE job_id=? AND state NOT IN ('completed','needs_review','failed')",(job_id,)).fetchall()
            for row in affected:
                _mark_submission_for_review(conn,row["submission_id"],error_type=type(exc).__name__,reason="job_configuration_failed",recoverable=True)
            _refresh_job_summary(conn,job_id,event="job_failed_into_review_queue")
    finally:
        current=threading.current_thread()
        with _JOB_LOCK:
            _JOB_THREADS.discard(current)
            for key,value in list(_JOB_BY_ID.items()):
                if value is current:
                    _JOB_BY_ID.pop(key,None)


_JOB_BY_ID: dict[str, threading.Thread] = {}


def _launch_job(app: Flask, job_id: str) -> bool:
    with _JOB_LOCK:
        active=_JOB_BY_ID.get(job_id)
        if active and active.is_alive():
            return False
        worker=threading.Thread(target=_process_job,args=(app,job_id),name=f"objective-{job_id[:8]}",daemon=True)
        _JOB_THREADS.add(worker)
        _JOB_BY_ID[job_id]=worker
        worker.start()
        return True


def _recover_startup_jobs(app: Flask) -> None:
    db_path=app.config["OBJECTIVE_DB_PATH"]
    with _connect(db_path) as conn:
        rows=conn.execute("SELECT job_id,status FROM objective_jobs WHERE status IN ('queued','processing','recoverable') ORDER BY created_at").fetchall()
    to_launch=[]
    for row in rows:
        job_id=row["job_id"]
        with _JOB_LOCK:
            active=_JOB_BY_ID.get(job_id)
            if active and active.is_alive():
                continue
        with _connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            submissions=conn.execute("SELECT * FROM objective_submissions WHERE job_id=? ORDER BY rowid",(job_id,)).fetchall()
            for submission in submissions:
                state=submission["state"]
                if state not in ("processing","queued","recoverable"):
                    continue
                expected=conn.execute("SELECT COUNT(*) FROM json_each((SELECT answer_key_json FROM objective_assessments WHERE assessment_id=?))",(submission["assessment_id"],)).fetchone()[0]
                answer_count=conn.execute("SELECT COUNT(*) FROM objective_answers WHERE submission_id=?",(submission["submission_id"],)).fetchone()[0]
                job_mode=conn.execute("SELECT mode FROM objective_jobs WHERE job_id=?",(job_id,)).fetchone()["mode"]
                if job_mode == "vision" and state == "processing" and answer_count < expected:
                    # A restarted process cannot know whether one or both paid model calls
                    # completed. Stop here; a teacher must explicitly request another run.
                    _mark_submission_for_review(conn,submission["submission_id"],error_type="ProcessRestart",reason="vision_interrupted_manual_retry_required",recoverable=False)
                    _insert_audit(conn,"vision_interrupted_no_automatic_retry",job_id=job_id,submission_id=submission["submission_id"],details={"answer_count":answer_count,"expected":expected})
                    continue
                page_rows=conn.execute("SELECT page_id,stored_path,image_sha256,recognition_state FROM objective_submission_pages WHERE submission_id=? ORDER BY page_no",(submission["submission_id"],)).fetchall()
                intact=bool(page_rows)
                corrupt_page=None
                try:
                    for page in page_rows:
                        data=Path(page["stored_path"]).read_bytes()
                        if not secrets.compare_digest(_sha256(data),page["image_sha256"]):
                            intact=False; corrupt_page=page["page_id"]; break
                except OSError:
                    intact=False
                if intact and page_rows:
                    conn.execute("UPDATE objective_submissions SET state='queued',score=NULL,recovery_attempts=recovery_attempts+1,failure_code='interrupted_processing' WHERE submission_id=?",(submission["submission_id"],))
                    conn.execute("UPDATE objective_submission_pages SET recognition_state='queued',failure_code=NULL WHERE submission_id=?",(submission["submission_id"],))
                    _insert_audit(conn,"job_requeued_after_restart",job_id=job_id,submission_id=submission["submission_id"],details={"recovery_attempt":int(submission["recovery_attempts"])+1,"page_count":len(page_rows)})
                    _queue_review_item(conn,job_id=job_id,submission_id=submission["submission_id"],page_id=page_rows[0]["page_id"],reason="interrupted_processing",error_type="ProcessRestart")
                else:
                    _mark_submission_for_review(conn,submission["submission_id"],error_type="OSError",reason="stored_image_hash_mismatch" if corrupt_page else "source_image_unavailable",recoverable=False,invalidate_existing=True)
                    if corrupt_page:
                        _insert_audit(conn,"stored_image_hash_mismatch",job_id=job_id,submission_id=submission["submission_id"],page_id=corrupt_page,details={"source_hash_matches_stored":False,"detected_during_recovery":True})
                    continue
                # Only trust a complete persisted answer set after every source
                # page has passed the same integrity check used before recognition.
                if answer_count==expected and expected>0:
                    pages_finished=all(page["recognition_state"] in ("auto","needs_review","complete","completed") for page in page_rows)
                    if pages_finished:
                        _insert_audit(conn,"answers_reconciled_after_restart",job_id=job_id,submission_id=submission["submission_id"],details={"answer_count":answer_count,"expected":expected})
                        conn.execute("UPDATE objective_submissions SET state='processing',score=NULL WHERE submission_id=?",(submission["submission_id"],))
                        # finalize after leaving this transaction
                        continue
            conn.execute("UPDATE objective_jobs SET recovery_attempts=recovery_attempts+1,lease_updated_at=NULL WHERE job_id=?",(job_id,))
        # Complete answer-write-before-summary snapshots without rerunning recognition.
        with _connect(db_path) as conn:
            recoverable_ids=[r["submission_id"] for r in conn.execute("SELECT s.submission_id FROM objective_submissions s WHERE s.job_id=? AND s.state='processing' AND (SELECT COUNT(*) FROM objective_answers a WHERE a.submission_id=s.submission_id)=(SELECT json_array_length(answer_key_json) FROM objective_assessments x WHERE x.assessment_id=s.assessment_id)",(job_id,)).fetchall()]
            unfinished=conn.execute("SELECT COUNT(*) FROM objective_submissions WHERE job_id=? AND state IN ('queued','recoverable')",(job_id,)).fetchone()[0]
        for submission_id in recoverable_ids:
            _finalize_submission(app,submission_id,recovered=True)
        if unfinished or any(s["state"] in ("processing","queued","recoverable") for s in submissions):
            to_launch.append(job_id)
        else:
            with _connect(db_path) as conn:
                _refresh_job_summary(conn,job_id,event="job_recovered_after_restart")
    for job_id in to_launch:
        with _connect(db_path) as conn:
            conn.execute("UPDATE objective_jobs SET status='queued' WHERE job_id=? AND status IN ('processing','recoverable')",(job_id,))
        _launch_job(app,job_id)

def create_app(
    data_dir: str | Path | None = None,
    *,
    database_path: str | Path | None = None,
    upload_dir: str | Path | None = None,
    auth_enabled: bool = True,
    mount_prefix: str = "",
    initialize_schema: bool = True,
    set_journal_mode: bool = True,
    vision_grader=None,
    vision_status=None,
    source_exam_provider=None,
) -> Flask:
    sandbox_root = Path(__file__).resolve().parents[1]
    chosen = Path(data_dir) if data_dir else Path(os.environ.get("OBJECTIVE_DATA_DIR", sandbox_root / "runtime"))
    chosen = chosen.resolve()
    app = Flask(__name__, static_folder=None)
    app.config["OBJECTIVE_SANDBOX_ROOT"] = sandbox_root
    app.config["OBJECTIVE_DATA_DIR"] = chosen
    app.config["OBJECTIVE_DB_PATH"] = Path(database_path).resolve() if database_path else chosen / "objective.sqlite3"
    app.config["OBJECTIVE_UPLOAD_DIR"] = Path(upload_dir).resolve() if upload_dir else chosen / "uploads"
    app.config["OBJECTIVE_AUTH_ENABLED"] = bool(auth_enabled)
    app.config["OBJECTIVE_MOUNT_PREFIX"] = ("/" + mount_prefix.strip("/")) if mount_prefix.strip("/") else ""
    app.config["OBJECTIVE_VISION_GRADER"] = vision_grader
    app.config["OBJECTIVE_VISION_STATUS"] = vision_status
    app.config["MAX_CONTENT_LENGTH"] = MAX_TOTAL_BYTES
    if initialize_schema:
        _init_db(app.config["OBJECTIVE_DB_PATH"], set_journal_mode=set_journal_mode)
    app.config["OBJECTIVE_UPLOAD_DIR"].mkdir(parents=True, exist_ok=True)

    @app.before_request
    def require_teacher_session_and_csrf() -> Response | None:
        if not app.config["OBJECTIVE_AUTH_ENABLED"]:
            # The embedded main-service adapter has already applied the
            # existing web_auth.py session, CSRF, same-origin, and permission
            # middleware before forwarding this request to WSGI.
            return None
        # The same web_auth.py manager, cookie session and origin/CSRF checks used by
        # the material-grading routes protect every objective business route.
        class FlaskAuthAdapter:
            headers = request.headers
            client_address = (request.remote_addr or "127.0.0.1", 0)
            path = request.path

        adapter = FlaskAuthAdapter()
        path = request.path
        method = request.method.upper()
        public = path in {"/api/health", "/api/auth/session", "/api/auth/login", "/login"}
        public_login_asset = path in {"/static/auth.css", "/static/login.js"}

        if path == "/api/health" and method == "GET":
            return None

        if method == "POST" and not web_auth._same_origin(adapter):
            return jsonify({"error": "cross_origin_request_rejected"}), 403

        config = web_auth.auth_config(adapter)
        auth_is_configured = web_auth.AUTH_MANAGER.is_configured(config, adapter)
        if path == "/api/auth/login" and method == "POST":
            if not config.required or not auth_is_configured:
                return jsonify({"error": "teacher_auth_not_configured"}), 503
            payload = request.get_json(silent=True) or {}
            status_code, body, cookie = web_auth.AUTH_MANAGER.login(
                str(payload.get("username", "")), str(payload.get("password", "")),
                web_auth._client_key(adapter), str(payload.get("totp_code", "")), adapter,
                challenge_id=str(payload.get("challenge_id", "")),
                email_code=str(payload.get("email_code", "")),
            )
            response = jsonify(body)
            response.status_code = status_code
            response.headers["Cache-Control"] = "no-store, private"
            if cookie:
                response.headers.add("Set-Cookie", cookie)
            return response

        session = web_auth.AUTH_MANAGER.authenticate(request.headers.get("Cookie", ""), adapter)
        if path == "/api/auth/session" and method == "GET":
            if not config.required or not auth_is_configured or session is None:
                return jsonify({"required": True, "configured": bool(auth_is_configured), "authenticated": False, "totp_required": False}), 401
            return jsonify({"required": True, "configured": True, "authenticated": True, "cloud_mode": web_auth.cloud_mode_enabled(adapter), "totp_required": config.totp_required, "email_otp_required": config.email_otp_required, **web_auth.AUTH_MANAGER.public_session(session)})

        if path == "/api/auth/logout" and method == "POST":
            if session is None:
                return jsonify({"error": "authentication_required"}), 401
            supplied = request.headers.get("X-CSRF-Token", "").strip()
            if not supplied or not secrets.compare_digest(supplied, session.csrf_token):
                return jsonify({"error": "csrf_token_required"}), 403
            response = jsonify({"ok": True})
            response.headers.add("Set-Cookie", web_auth.AUTH_MANAGER.logout(request.headers.get("Cookie", ""), adapter))
            return response

        if not config.required or not auth_is_configured:
            if public or public_login_asset:
                if path == "/login" and method == "GET":
                    return None
                if public_login_asset and method == "GET":
                    return None
                if path == "/api/auth/session" and method == "GET":
                    return jsonify({"required": True, "configured": bool(auth_is_configured), "authenticated": False}), 401
                if path == "/api/auth/login":
                    return jsonify({"error": "teacher_auth_not_configured"}), 503
            return jsonify({"error": "teacher_auth_not_configured"}), 503

        if session is None:
            if path == "/login" and method == "GET":
                return None
            if public_login_asset and method == "GET":
                return None
            if path.startswith("/api/"):
                return jsonify({"error": "authentication_required", "authentication_required": True}), 401
            return Response("", status=303, headers={"Location": "/login", "Cache-Control": "no-store"})

        if method == "POST" and path != "/api/auth/login":
            supplied = request.headers.get("X-CSRF-Token", "").strip()
            if not supplied or not secrets.compare_digest(supplied, session.csrf_token):
                return jsonify({"error": "csrf_token_required"}), 403
        return None

    @app.after_request
    def secure_demo_headers(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.teardown_request
    def close_uploaded_file_storage(_error: BaseException | None) -> None:
        # Multipart streams may spool to Werkzeug TemporaryFile. Close every parsed
        # FileStorage explicitly, including early validation/error returns.
        try:
            for _key, values in request.files.lists():
                for file_storage in values:
                    file_storage.close()
        except (RuntimeError, OSError):
            pass

    @app.get("/")
    def index() -> Response:
        page = (sandbox_root / "objective_static" / "index.html").read_text(encoding="utf-8")
        page = page.replace("__OBJECTIVE_BASE__", app.config["OBJECTIVE_MOUNT_PREFIX"])
        return Response(page, mimetype="text/html; charset=utf-8")

    @app.get("/login")
    def login_page() -> Response:
        page = """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>阅卷工作台登录</title>
<link rel=\"stylesheet\" href=\"/static/auth.css\"><script src=\"/static/login.js\" defer></script></head>
<body><main class=\"auth-gate\"><form class=\"auth-card\" id=\"workbench-login\" autocomplete=\"on\">
<span class=\"eyebrow\">隔离客观题试用 · 安全登录</span><h2>阅卷工作台登录</h2>
<p id=\"auth-message\">正在检查服务器登录配置。</p>
<label id=\"auth-username-row\">用户名<input id=\"auth-username\" name=\"username\" autocomplete=\"username\" value=\"trial-\" maxlength=\"100\" required></label>
<label id=\"auth-password-row\">密码<input id=\"auth-password\" name=\"password\" type=\"password\" autocomplete=\"current-password\" maxlength=\"512\" required></label>
<label id=\"auth-totp-row\" hidden>双因素验证码<input id=\"auth-totp\" name=\"totp_code\" inputmode=\"numeric\" autocomplete=\"one-time-code\" pattern=\"[0-9]{6}\" maxlength=\"6\"></label>
<label id=\"auth-email-row\" hidden>邮箱动态码<input id=\"auth-email-code\" name=\"email_code\" inputmode=\"numeric\" autocomplete=\"one-time-code\" pattern=\"[0-9]{6}\" maxlength=\"6\" disabled></label>
<button class=\"button primary\" type=\"submit\">登录工作台</button><button class=\"button\" id=\"auth-back-button\" type=\"button\" hidden>返回账户输入</button>
<p class=\"auth-error\" id=\"auth-error\" role=\"alert\" aria-live=\"polite\"></p></form></main></body></html>"""
        return Response(page, mimetype="text/html; charset=utf-8")

    @app.get("/static/<path:filename>")
    def static_file(filename: str) -> Response:
        return send_from_directory(sandbox_root / "objective_static", filename)

    @app.get("/api/health")
    def health() -> Response:
        # Minimal public liveness response; it exposes no account, task, or data details.
        return jsonify({"ok": True, "service": "objective-isolated"})

    @app.get("/api/status")
    def status() -> Response:
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            templates = conn.execute("SELECT COUNT(*) AS n FROM objective_omr_templates WHERE confirmed=1").fetchone()["n"]
        vision_info = {"enabled": callable(app.config.get("OBJECTIVE_VISION_GRADER")), "available": False, "models": []}
        status_callback = app.config.get("OBJECTIVE_VISION_STATUS")
        if callable(status_callback):
            try:
                vision_info.update(status_callback())
            except Exception:
                vision_info["status_error"] = "model_configuration_unavailable"
        return jsonify({"status": "integrated-local" if app.config["OBJECTIVE_MOUNT_PREFIX"] else "isolated-demo", "external_model": "vision-dual" if vision_info["enabled"] else "disabled", "vision_grading": vision_info, "recognizer": f"RapidOCR ONNX Runtime {recognizer_version()}", "handwriting_mode": {"experimental": True, "default": False, "high_reliability": False}, "confirmed_templates": templates, "data_dir": str(app.config["OBJECTIVE_DATA_DIR"])})

    def source_exam(exam_id=None):
        if not callable(source_exam_provider):
            raise ValueError("当前独立服务未接入主线考试，请使用原独立答案键")
        return source_exam_provider(exam_id)

    def source_error(exc):
        return jsonify({"error": str(exc)}), 403 if isinstance(exc, PermissionError) else 404 if isinstance(exc, LookupError) else 400

    from answer_links import register
    register(app, source_exam_provider, _connect, _insert_audit, _utc_now)

    @app.get("/api/source-exams")
    def list_source_exams():
        try:
            return jsonify({"exams": source_exam()})
        except (ValueError, LookupError, PermissionError) as exc:
            return source_error(exc)

    @app.get("/api/source-exams/<exam_id>")
    def get_source_exam(exam_id):
        try:
            return jsonify(source_exam(exam_id))
        except (ValueError, LookupError, PermissionError) as exc:
            return source_error(exc)

    @app.post("/api/assessments/from-exam")
    def assessment_from_exam():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict) or set(payload) != {"exam_id"} or not isinstance(payload.get("exam_id"), str):
            return jsonify({"error": "仅接受正式考试编号，答案和分值由服务器读取"}), 400
        try:
            source = source_exam(payload["exam_id"])
            # Keep the existing engine's strict numbering/answer/score validation.
            key = _validate_answer_key(source["answer_key"])
        except (ValueError, LookupError, PermissionError) as exc:
            return source_error(exc)
        mapping = source["question_mapping"]
        if len(mapping) != len(key):
            return jsonify({"error": "题号映射不完整，未生成快照"}), 400
        for row, link in zip(key, mapping):
            if row["question_no"] != link["question_no"] or link["original_question_id"] != str(row["question_no"]):
                return jsonify({"error": "题号映射不兼容，未生成快照"}), 400
            row["original_question_id"] = link["original_question_id"]
        key_json = json.dumps(key, ensure_ascii=False, sort_keys=True)
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM objective_assessments WHERE source_exam_id=? AND source_fingerprint=?", (source["exam_id"], source["source_fingerprint"])).fetchone()
            if existing:
                assessment_id, version, created = existing["assessment_id"], existing["answer_version"], False
            else:
                assessment_id, created = uuid.uuid4().hex, True
                version = conn.execute("SELECT COALESCE(MAX(answer_version),0)+1 FROM objective_assessments WHERE source_exam_id=?", (source["exam_id"],)).fetchone()[0]
                conn.execute("INSERT INTO objective_assessments(assessment_id,title,answer_version,answer_key_json,answer_key_sha256,created_at,source_exam_id,source_fingerprint) VALUES(?,?,?,?,?,?,?,?)", (assessment_id,source["title"],version,key_json,_sha256(key_json.encode()),_utc_now(),source["exam_id"],source["source_fingerprint"]))
                _insert_audit(conn, "assessment_snapshot_created", details={"assessment_id":assessment_id,"source_exam_id":source["exam_id"],"source_fingerprint":source["source_fingerprint"],"answer_version":version})
        return jsonify({"assessment_id":assessment_id,"source_exam_id":source["exam_id"],"source_fingerprint":source["source_fingerprint"],"answer_version":version,"reused":not created,"question_mapping":mapping,"questions":source["questions"],"answer_key":key,"max_score":sum(row["points"] for row in key)}), 201 if created else 200

    @app.post("/api/assessments")
    def create_assessment() -> Response:
        payload = request.get_json(silent=True) or {}
        title = str(payload.get("title", "客观题快速匿名测试")).strip()[:80] or "客观题快速匿名测试"
        try:
            answer_key = _validate_answer_key(payload.get("answer_key"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        assessment_id = uuid.uuid4().hex
        key_json = json.dumps(answer_key, ensure_ascii=False, sort_keys=True)
        key_hash = _sha256(key_json.encode())
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            conn.execute("INSERT INTO objective_assessments(assessment_id,title,answer_version,answer_key_json,answer_key_sha256,created_at) VALUES(?,?,?,?,?,?)", (assessment_id, title, 1, key_json, key_hash, _utc_now()))
            _insert_audit(conn, "assessment_created", details={"assessment_id": assessment_id, "answer_version": 1, "question_count": len(answer_key), "answer_key_sha256": key_hash})
        return jsonify({"assessment_id": assessment_id, "title": title, "answer_version": 1, "question_count": len(answer_key), "max_score": sum(row["points"] for row in answer_key)}), 201

    @app.get("/api/assessments/<assessment_id>")
    def get_assessment(assessment_id: str) -> Response:
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            row = conn.execute("SELECT assessment_id,title,answer_version,answer_key_json,answer_key_sha256,created_at,source_exam_id,source_fingerprint FROM objective_assessments WHERE assessment_id=?", (assessment_id,)).fetchone()
        if not row:
            return jsonify({"error": "assessment_not_found"}), 404
        return jsonify({"assessment_id": row["assessment_id"], "title": row["title"], "answer_version": row["answer_version"], "answer_key": json.loads(row["answer_key_json"]), "answer_key_sha256": row["answer_key_sha256"], "created_at": row["created_at"], "source_exam_id":row["source_exam_id"], "source_fingerprint":row["source_fingerprint"], "question_mapping":[{"original_question_id":k["original_question_id"],"question_no":k["question_no"]} for k in json.loads(row["answer_key_json"]) if "original_question_id" in k]})

    @app.get("/api/templates")
    def list_templates() -> Response:
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            rows = conn.execute("SELECT template_id,name,version,config_json,config_sha256,calibration_sha256,created_at FROM objective_omr_templates WHERE confirmed=1 ORDER BY created_at,template_id").fetchall()
        return jsonify({"templates": [{"template_id": r["template_id"], "name": r["name"], "version": r["version"], "config": json.loads(r["config_json"]), "config_sha256": r["config_sha256"], "calibration_sha256": r["calibration_sha256"]} for r in rows]})

    @app.post("/api/templates/setup")
    def setup_template() -> Response:
        sample = request.files.get("sample")
        if not sample:
            return jsonify({"error": "calibration_sample_required"}), 400
        raw = sample.read(MAX_FILE_BYTES + 1)
        if not raw or len(raw) > MAX_FILE_BYTES:
            return jsonify({"error": "calibration_sample_size_invalid"}), 400
        try:
            normalized, _, _, _ = _decode_image(raw)
            question_count = int(request.form.get("question_count", "0"))
            columns = int(request.form.get("columns", "1"))
            signature = int(request.form.get("signature", "0"))
            width = int(request.form.get("page_width", "1200"))
            height = int(request.form.get("page_height", "1680"))
            config = make_grid_config(question_count=question_count, columns=columns, signature=signature, page_width=width, page_height=height)
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400
        proof = recognize_omr(normalized, config)
        if proof.diagnostics.get("alignment") != "aligned" or proof.diagnostics.get("signature_match") is not True:
            return jsonify({"error": "calibration_sample_not_aligned", "diagnostics": proof.diagnostics}), 422
        if request.form.get("confirm") != "true":
            return jsonify({"error": "teacher_confirmation_required_once_for_new_template"}), 400
        name = str(request.form.get("name", "新OMR版式")).strip()[:80] or "新OMR版式"
        config_json = json.dumps(config, sort_keys=True)
        config_hash = _sha256(config_json.encode())
        calibration_hash = _sha256(normalized)
        template_id = f"omr-custom-{uuid.uuid4().hex[:10]}"
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            duplicate_signature = conn.execute("SELECT template_id FROM objective_omr_templates WHERE confirmed=1 AND json_extract(config_json,'$.signature')=?", (signature,)).fetchone()
            if duplicate_signature:
                return jsonify({"error": "signature_already_in_use"}), 409
            conn.execute("INSERT INTO objective_omr_templates VALUES(?,?,?,?,?,?,1,?)", (template_id, name, 1, config_json, config_hash, calibration_hash, _utc_now()))
            _insert_audit(conn, "omr_template_confirmed", details={"template_id": template_id, "config_sha256": config_hash, "calibration_sha256": calibration_hash, "question_count": question_count, "columns": columns, "signature": signature})
        return jsonify({"template_id": template_id, "name": name, "version": 1, "config_sha256": config_hash, "calibration_sha256": calibration_hash, "confirmed": True}), 201

    def workspace_exam_matches(conn, assessment_id):
        # Optional additive binding for the unified workspace; standalone clients
        # keep the existing immutable assessment/submission ownership contract.
        if "X-Workspace-Exam-ID" not in request.headers:
            return True
        source = conn.execute("SELECT source_exam_id FROM objective_assessments WHERE assessment_id=?", (assessment_id,)).fetchone()
        return bool(source) and str(source["source_exam_id"] or "") == request.headers["X-Workspace-Exam-ID"]

    @app.post("/api/uploads")
    def upload() -> Response:
        assessment_id = str(request.form.get("assessment_id", ""))
        mode = str(request.form.get("mode", "")).strip()
        grouping = str(request.form.get("grouping", "one_per_image"))
        template_id = str(request.form.get("template_id", "")) or None
        automatic_template = mode == "omr" and template_id in (None, "", "auto")
        files = [f for f in request.files.getlist("files") if f and f.filename]
        if not 1 <= len(files) <= MAX_FILES:
            return jsonify({"error": f"select_1_to_{MAX_FILES}_images"}), 400
        if mode not in ("omr", "handwriting", "vision"):
            return jsonify({"error": "recognition_mode_required_or_unsupported"}), 400
        privacy_status = str(request.form.get("privacy_status", "unchecked")).strip()
        if mode == "vision":
            if not callable(app.config.get("OBJECTIVE_VISION_GRADER")):
                return jsonify({"error": "vision_grading_unavailable"}), 503
            if privacy_status not in {"pre_anonymized", "redacted", "no_identity_visible"}:
                return jsonify({"error": "privacy_confirmation_required_before_cloud_grading"}), 400
            status_callback = app.config.get("OBJECTIVE_VISION_STATUS")
            if callable(status_callback):
                try:
                    if not status_callback().get("available"):
                        return jsonify({"error": "vision_models_not_configured_on_computer"}), 503
                except Exception:
                    return jsonify({"error": "vision_model_configuration_unavailable"}), 503
        if grouping not in ("one_per_image", "multi_page_all"):
            return jsonify({"error": "unsupported_grouping_mode"}), 400
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            assessment = conn.execute("SELECT * FROM objective_assessments WHERE assessment_id=?", (assessment_id,)).fetchone()
            if assessment and not workspace_exam_matches(conn, assessment_id):
                return jsonify({"error": "workspace_exam_mismatch"}), 409
            if mode == "omr" and automatic_template:
                template_rows = conn.execute("SELECT template_id,config_json FROM objective_omr_templates WHERE confirmed=1 ORDER BY template_id").fetchall()
            elif mode == "omr":
                selected_row = conn.execute("SELECT template_id,config_json FROM objective_omr_templates WHERE template_id=? AND confirmed=1", (template_id,)).fetchone()
                template_rows = [selected_row] if selected_row else []
            else:
                template_rows = []
        if not assessment:
            return jsonify({"error": "assessment_not_found"}), 404
        if mode == "omr" and not template_rows:
            return jsonify({"error": "confirmed_omr_template_required"}), 400
        if mode in ("handwriting", "vision") and template_id:
            return jsonify({"error": "selected_mode_does_not_use_omr_template"}), 400

        staged: list[dict[str, Any]] = []
        total_bytes = 0
        normalized_total_bytes = 0
        try:
            for file_storage in files:
                raw = file_storage.read(MAX_FILE_BYTES + 1)
                if not raw or len(raw) > MAX_FILE_BYTES:
                    raise ValueError("each image must be between 1 byte and 12 MiB")
                total_bytes += len(raw)
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError("batch exceeds 220 MiB")
                normalized, width, height, _ = _decode_image(raw)
                normalized_total_bytes += len(normalized)
                if normalized_total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError("normalized batch exceeds 220 MiB")
                digest = _sha256(normalized)
                staged.append({"filename": file_storage.filename, "bytes": normalized, "sha256": digest, "width": width, "height": height, "raw_sha256": _sha256(raw)})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        groups = [[row] for row in staged] if grouping == "one_per_image" else [staged]
        source_sid = str(request.form.get('source_submission_id', ''))
        if source_sid:
            try:
                from shared_paper import approved_pages, METHOD
                if grouping != 'multi_page_all' or request.form.get('combined_confirmed') != 'true' or not assessment['source_exam_id']:
                    raise ValueError('请确认同份整卷来源、分组和两类评分授权')
                source_exam(assessment['source_exam_id'])
                with _connect(app.config['OBJECTIVE_DB_PATH']) as conn:
                    expected = approved_pages(conn, assessment['source_exam_id'], source_sid)
                    if [p['raw_sha256'] for p in staged] != expected:
                        raise ValueError('客观题图片与本份已授权外发图不一致')
                    prior = conn.execute('SELECT l.*,s.job_id,s.assessment_id FROM exam_answer_links l JOIN objective_submissions s ON s.submission_id=l.objective_submission_id WHERE l.exam_id=? AND l.subjective_submission_id=? ORDER BY l.confirmed_at DESC LIMIT 1', (assessment['source_exam_id'],source_sid)).fetchone()
                    if prior:
                        if prior['method'] != METHOD or prior['assessment_id'] != assessment_id or json.loads(prior['reason']).get('approved_pages') != expected:
                            raise ValueError('本份答卷已有其他关联或快照，请先核对原任务')
                        return jsonify(job_id=prior['job_id'],status='existing',reused=True,total_submissions=1),200
            except (ValueError, LookupError) as exc:
                return source_error(exc)
        template_configs = {row["template_id"]: json.loads(row["config_json"]) for row in template_rows}
        if mode == "omr" and not automatic_template:
            template_config = next(iter(template_configs.values()))
            key_count = len(json.loads(assessment["answer_key_json"]))
            if grouping == "multi_page_all":
                expected_pages = (key_count + int(template_config["question_count"]) - 1) // int(template_config["question_count"])
                expected_pages_exact = key_count % int(template_config["question_count"]) == 0 and expected_pages == len(staged)
            else:
                expected_pages = 1
                expected_pages_exact = key_count == int(template_config["question_count"])
            if not expected_pages_exact:
                return jsonify({"error": "omr_template_question_count_does_not_match_answer_key_and_grouping", "expected_pages": expected_pages, "selected_pages": len(staged) if grouping == "multi_page_all" else 1, "questions_per_page": template_config["question_count"], "answer_key_questions": key_count}), 400
        job_id = uuid.uuid4().hex
        upload_root: Path = app.config["OBJECTIVE_UPLOAD_DIR"]
        created_submissions: list[tuple[str, list[dict[str, Any]]]] = []
        try:
            with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if source_sid:
                    if approved_pages(conn, assessment['source_exam_id'], source_sid) != expected or conn.execute('SELECT 1 FROM exam_answer_links WHERE exam_id=? AND subjective_submission_id=?', (assessment['source_exam_id'],source_sid)).fetchone():
                        return jsonify(error='整卷来源或关联已变化，请刷新核实'),409
                conn.execute("INSERT INTO objective_jobs(job_id,assessment_id,mode,grouping,template_id,status,total_files,total_submissions,created_at) VALUES(?,?,?,?,?,'queued',?,?,?)", (job_id, assessment_id, mode, grouping, template_id, len(staged), len(groups), _utc_now()))
                for group_index,group in enumerate(groups):
                    submission_id = uuid.uuid4().hex
                    anon = _anonymous_code(conn)
                    max_score = sum(float(x["points"]) for x in json.loads(assessment["answer_key_json"]))
                    conn.execute("INSERT INTO objective_submissions(submission_id,job_id,assessment_id,anonymous_code,state,score,max_score,page_count,created_at,privacy_status) VALUES(?,?,?,?,?,?,?,?,?,?)", (submission_id, job_id, assessment_id, anon, "queued", None, max_score, len(group), _utc_now(), privacy_status if mode == "vision" else "unchecked"))
                    page_rows: list[dict[str, Any]] = []
                    for page_no, row in enumerate(group, start=1):
                        page_id = uuid.uuid4().hex
                        stored_path = upload_root / assessment_id / submission_id / f"page-{page_no:03d}-{row['sha256'][:16]}.png"
                        stored_path.parent.mkdir(parents=True, exist_ok=True)
                        stored_path.write_bytes(row["bytes"])
                        conn.execute("INSERT INTO objective_submission_pages(page_id,submission_id,page_no,stored_path,image_sha256,byte_count,width,height) VALUES(?,?,?,?,?,?,?,?)", (page_id, submission_id, page_no, str(stored_path), row["sha256"], len(row["bytes"]), row["width"], row["height"]))
                        page_rows.append({"page_id": page_id, "sha256": row["sha256"], "page_no": page_no})
                    if source_sid:
                        proof = json.dumps({'approved_pages':expected,'objective_pages':[p['sha256'] for p in group]})
                        conn.execute('INSERT INTO exam_answer_links(id,exam_id,objective_submission_id,subjective_submission_id,method,reason,actor,confirmed_at) VALUES(?,?,?,?,?,?,?,?)', (uuid.uuid4().hex,assessment['source_exam_id'],submission_id,source_sid,METHOD,proof,request.environ.get('history_grader.actor'),_utc_now()))
                        _insert_audit(conn,'same_upload_link_created',submission_id=submission_id,details={'subjective_submission_id':source_sid,'approved_pages':expected})
                    _insert_audit(conn, "submission_uploaded", job_id=job_id, submission_id=submission_id, details={"anonymous_code": anon, "page_count": len(group), "grouping": grouping, "mode": mode, "privacy_status": privacy_status if mode == "vision" else "unchecked"})
                    created_submissions.append((submission_id, page_rows))
                _insert_audit(conn, "upload_batch_created", job_id=job_id, details={"mode": mode, "grouping": grouping, "file_count": len(staged), "submission_count": len(groups), "template_id": template_id})
        except Exception:
            # Newly written files remain only in this isolated synthetic demo directory; record and leave them for inspection.
            raise

        _launch_job(app, job_id)
        return jsonify({"job_id": job_id, "status": "queued", "total_files": len(staged), "total_submissions": len(groups), "grouping": grouping}), 202

    @app.get("/api/jobs")
    def list_jobs() -> Response:
        assessment_id = request.args.get("assessment_id", "")
        exam_id = request.args.get("exam_id", "")
        if not assessment_id and not exam_id:
            return jsonify({"error": "请选择答案键或正式考试"}), 400
        if exam_id:
            try:
                if not any(e["exam_id"] == exam_id for e in source_exam()):
                    raise PermissionError("考试不存在或不可访问")
            except (ValueError, LookupError, PermissionError) as exc:
                return source_error(exc)
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            if assessment_id and not conn.execute("SELECT 1 FROM objective_assessments WHERE assessment_id=? AND (?='' OR source_exam_id=?)", (assessment_id, exam_id, exam_id)).fetchone():
                return jsonify({"error": "答案键不存在或不属于当前考试"}), 404
            rows = conn.execute("""SELECT j.job_id,j.assessment_id,j.mode,j.grouping,j.status,j.total_files,
                j.total_submissions,j.processed_submissions,j.complete_submissions,j.review_submissions,
                j.error_submissions,j.created_at,j.finished_at,a.answer_version,a.source_exam_id
                FROM objective_jobs j JOIN objective_assessments a ON a.assessment_id=j.assessment_id
                WHERE (?='' OR j.assessment_id=?) AND (?='' OR a.source_exam_id=?)
                ORDER BY j.created_at DESC,j.rowid DESC LIMIT 100""", (assessment_id,assessment_id,exam_id,exam_id)).fetchall()
            unfinished={r[0] for r in conn.execute("SELECT DISTINCT job_id FROM objective_submissions WHERE state NOT IN ('completed','needs_review','recoverable','failed')")}
        with _JOB_LOCK:
            live={job_id for job_id,worker in _JOB_BY_ID.items() if worker and worker.is_alive()}
        return jsonify({'jobs':[dict(row,deletable=row['status'] in JOB_TERMINAL_STATES and row['job_id'] not in unfinished and row['job_id'] not in live) for row in rows]})

    def deletion_access(conn,assessment_id,exam_id):
        if not isinstance(assessment_id,str) or not re.fullmatch('[a-f0-9]{32}',assessment_id) or not isinstance(exam_id,str) or (exam_id and not re.fullmatch('[a-f0-9]{32}',exam_id)):
            raise ValueError('答案键或考试编号无效')
        if not workspace_exam_matches(conn,assessment_id):raise ValueError('工作台考试与任务不一致')
        if exam_id and not any(e['exam_id']==exam_id for e in source_exam()):raise PermissionError('考试不存在或不可访问')

    @app.post('/api/jobs/batch-delete-preview')
    @app.post('/api/jobs/batch-delete')
    def jobs_batch_deletion():
        from .job_deletion import batch_items,batch_plan,batch_delete,cleanup
        body=request.get_json(silent=True)
        removing=request.path.endswith('/batch-delete')
        required={'items','confirmed','expected_fingerprint'} if removing else {'items'}
        if not isinstance(body,dict) or set(body)!=required:
            return jsonify(error='请先选择已结束任务并读取整批删除范围'),400
        try:
            items=batch_items(body['items'])
            with _JOB_LOCK:
                active={job_id for job_id,worker in _JOB_BY_ID.items() if worker and worker.is_alive()}
                with _connect(app.config['OBJECTIVE_DB_PATH']) as conn:
                    for item in items:deletion_access(conn,item['assessment_id'],item['exam_id'])
                    conn.execute('BEGIN IMMEDIATE' if removing else 'BEGIN')
                    if not removing:
                        return jsonify(batch_plan(conn,items,active_workers=active))
                    p,directories,root=batch_delete(conn,app.config['OBJECTIVE_UPLOAD_DIR'],body,audit=_insert_audit,actor=request.environ.get('history_grader.actor'),active_workers=active)
                warning=cleanup(directories,root)
                deleted=[i['job_id'] for i in p['items']]
                for job_id in deleted:_JOB_BY_ID.pop(job_id,None)
            return jsonify(ok=True,deleted_job_ids=deleted,jobs=p['jobs'],warning=warning)
        except (ValueError,LookupError,PermissionError) as exc:return source_error(exc)

    @app.get("/api/jobs/<job_id>/delete-preview")
    def job_delete_preview(job_id):
        from .job_deletion import plan
        key=request.args.get('assessment_id','');exam=request.args.get('exam_id','')
        try:
            with _JOB_LOCK:
                worker=_JOB_BY_ID.get(job_id)
                with _connect(app.config['OBJECTIVE_DB_PATH']) as conn:
                    deletion_access(conn,key,exam)
                    conn.execute('BEGIN')
                    p,_,_=plan(conn,job_id,key,exam,worker_active=bool(worker and worker.is_alive()))
            return jsonify(p)
        except (ValueError,LookupError,PermissionError) as exc:return source_error(exc)

    @app.post("/api/jobs/<job_id>/delete")
    def delete_job(job_id):
        from .job_deletion import delete,cleanup
        body=request.get_json(silent=True)
        if not isinstance(body,dict) or set(body)!={'assessment_id','exam_id','confirmed','expected_fingerprint'}:
            return jsonify({'error':'请先预览任务删除范围并明确确认'}),400
        try:
            with _JOB_LOCK:
                worker=_JOB_BY_ID.get(job_id)
                with _connect(app.config['OBJECTIVE_DB_PATH']) as conn:
                    deletion_access(conn,body['assessment_id'],body['exam_id'])
                    conn.execute('BEGIN IMMEDIATE')
                    p,directories,root=delete(conn,app.config['OBJECTIVE_UPLOAD_DIR'],body,job_id,audit=_insert_audit,
                        actor=request.environ.get('history_grader.actor'),worker_active=bool(worker and worker.is_alive()))
                # Only after the transaction commits do files get removed.
                warning=cleanup(directories,root)
                _JOB_BY_ID.pop(job_id,None)
            return jsonify({'ok':True,'job_id':p['job_id'],'assessment_id':p['assessment_id'],'warning':warning})
        except (ValueError,LookupError,PermissionError) as exc:return source_error(exc)

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str) -> Response:
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            job = conn.execute("SELECT * FROM objective_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not job:
                return jsonify({"error": "job_not_found"}), 404
            expected = request.args.get("assessment_id", "")
            if expected and job["assessment_id"] != expected:
                return jsonify({"error": "任务不属于当前答案键"}), 409
            submissions = conn.execute("SELECT submission_id,anonymous_code,state,score,max_score,page_count FROM objective_submissions WHERE job_id=? ORDER BY rowid", (job_id,)).fetchall()
        return jsonify({"job": dict(job), "submissions": [dict(row) for row in submissions]})

    @app.get("/api/submissions")
    def list_submissions() -> Response:
        assessment_id = request.args.get("assessment_id", "")
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            rows = conn.execute("SELECT s.submission_id,s.job_id,s.anonymous_code,s.state,s.score,s.max_score,s.page_count,s.created_at,j.mode FROM objective_submissions s JOIN objective_jobs j ON j.job_id=s.job_id WHERE s.assessment_id=? ORDER BY s.created_at, s.rowid", (assessment_id,)).fetchall()
        return jsonify({"submissions": [dict(row) for row in rows]})

    @app.get("/api/submissions/<submission_id>")
    def get_submission(submission_id: str) -> Response:
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            submission = conn.execute("SELECT submission_id,anonymous_code,state,score,max_score,page_count,created_at,privacy_status,vision_result_json FROM objective_submissions WHERE submission_id=?", (submission_id,)).fetchone()
            if not submission:
                return jsonify({"error": "submission_not_found"}), 404
            answers = conn.execute("SELECT question_no,candidate_option,final_option,max_points,awarded_points,state,source,confidence,evidence_json,reason FROM objective_answers WHERE submission_id=? ORDER BY question_no", (submission_id,)).fetchall()
        submission_data = dict(submission)
        model_results = json.loads(submission_data.pop("vision_result_json", "{}") or "{}")
        answer_data = []
        for row in answers:
            item = dict(row)
            evidence_payload = json.loads(item.pop("evidence_json") or "{}")
            item["model_decisions"] = evidence_payload.get("models", {}) if isinstance(evidence_payload, dict) else {}
            answer_data.append(item)
        return jsonify({"submission": submission_data, "model_results": model_results, "answers": answer_data})

    @app.get("/api/review-queue")
    def review_queue() -> Response:
        assessment_id = request.args.get("assessment_id", "")
        exam_id = request.args.get('exam_id','')
        if exam_id:
            try:
                if not any(e['exam_id']==exam_id for e in source_exam()):raise PermissionError('考试不存在或不可访问')
            except (ValueError,LookupError,PermissionError) as exc:return source_error(exc)
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            rows = conn.execute(
                """SELECT r.review_item_id,r.reason AS review_reason,r.error_type,r.question_no,
                          s.submission_id,s.job_id,s.assessment_id,k.source_exam_id,k.answer_version,s.anonymous_code,s.state AS submission_state,
                          s.score,s.max_score,s.page_count,a.candidate_option,a.final_option,
                          a.max_points,a.state,a.source,a.reason,a.evidence_json,p.page_no,COALESCE(r.page_id,p.page_id) AS page_id
                   FROM objective_review_items r
                   JOIN objective_submissions s ON s.submission_id=r.submission_id
                   JOIN objective_assessments k ON k.assessment_id=s.assessment_id
                   LEFT JOIN objective_answers a ON a.submission_id=r.submission_id AND a.question_no=r.question_no
                   LEFT JOIN objective_submission_pages p ON p.page_id=r.page_id
                   WHERE ((?<>'' AND (k.source_exam_id=? OR (?='all' AND k.source_exam_id IN (SELECT e.id FROM grading_exams e JOIN grading_exams chosen ON chosen.assignment_id=e.assignment_id WHERE chosen.id=?)))) OR (?='' AND s.assessment_id=?)) AND r.state='open'
                   ORDER BY s.rowid,r.question_no,r.created_at""",
                (exam_id,exam_id,request.args.get('records',''),exam_id,exam_id,assessment_id),
            ).fetchall()
        items = []
        for row in rows:
            evidence = json.loads(row["evidence_json"] or "{}") if row["evidence_json"] else {}
            items.append({
                "review_item_id": row["review_item_id"],
                "submission_id": row["submission_id"], "anonymous_code": row["anonymous_code"],
                "exam_id": row['source_exam_id'], "assessment_id": row['assessment_id'], "answer_version": row['answer_version'],
                "job_id": row["job_id"], "submission_state": row["submission_state"], "score": row["score"],
                "max_score": row["max_score"], "page_count": row["page_count"],
                "question_no": row["question_no"], "candidate_option": row["candidate_option"],
                "final_option": row["final_option"], "max_points": row["max_points"],
                "state": row["state"], "source": row["source"], "reason": row["reason"],
                "review_reason": row["review_reason"], "error_type": row["error_type"],
                "page_no": row["page_no"], "page_id": row["page_id"],
                "model_decisions": evidence.get("models", {}),
            })
        return jsonify({"items": items, "count": len(items)})

    @app.get("/api/submissions/<submission_id>/pages/<int:page_no>")
    def get_submission_page(submission_id: str, page_no: int) -> Response:
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            page = conn.execute(
                "SELECT p.page_id,p.stored_path,p.image_sha256,s.job_id FROM objective_submission_pages p JOIN objective_submissions s ON s.submission_id=p.submission_id WHERE p.submission_id=? AND p.page_no=?",
                (submission_id, page_no),
            ).fetchone()
        if not page:
            return jsonify({"error": "page_not_found"}), 404
        try:
            content = Path(page["stored_path"]).read_bytes()
        except OSError:
            with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
                _insert_audit(conn,"stored_page_unavailable",job_id=page["job_id"],submission_id=submission_id,page_id=page["page_id"],image_sha256=page["image_sha256"],details={"failure_code":"source_image_unavailable"})
            return jsonify({"error": "stored_page_unavailable"}), 404
        digest = _sha256(content)
        if not secrets.compare_digest(digest, page["image_sha256"]):
            with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
                _insert_audit(conn, "stored_image_hash_mismatch", job_id=page["job_id"], submission_id=submission_id, page_id=page["page_id"], image_sha256=digest, details={"source_hash_matches_stored":False})
            return jsonify({"error": "stored_image_hash_mismatch"}), 409
        response = Response(content, mimetype="image/png")
        response.headers["ETag"] = f'"{digest}"'
        return response

    @app.post("/api/submissions/<submission_id>/answers/<int:question_no>/review")
    def review_answer(submission_id: str, question_no: int) -> Response:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("option"), str):
            return jsonify({"error": "option_must_be_A_to_D_or_blank"}), 400
        selected = payload["option"].strip().upper()
        reason = payload.get("reason")
        if reason is None:
            reason = ""
        if not isinstance(reason, str) or len(reason.strip()) > 200:
            return jsonify({"error": "manual_change_reason_max_200_chars"}), 400
        reason = reason.strip()
        if (selected and selected not in OPTIONS) or (payload["option"] and not selected):
            return jsonify({"error": "option_must_be_A_to_D_or_blank"}), 400
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            conn.execute('BEGIN IMMEDIATE')
            answer = conn.execute("SELECT a.*,s.assessment_id,s.job_id FROM objective_answers a JOIN objective_submissions s ON s.submission_id=a.submission_id WHERE a.submission_id=? AND a.question_no=?", (submission_id, question_no)).fetchone()
            if not answer:
                return jsonify({"error": "answer_not_found"}), 404
            if not workspace_exam_matches(conn, answer["assessment_id"]):
                return jsonify({"error": "workspace_exam_mismatch"}), 409
            assessment = conn.execute("SELECT answer_key_json FROM objective_assessments WHERE assessment_id=?", (answer["assessment_id"],)).fetchone()
            key = next(row for row in json.loads(assessment["answer_key_json"]) if row["question_no"] == question_no)
            points = 0.0 if not selected else (float(answer["max_points"]) if selected == key["correct_option"] else 0.0)
            state = "manual_blank" if not selected else "manual_answer"
            conn.execute("UPDATE objective_answers SET final_option=?,awarded_points=?,state=?,reason=? WHERE submission_id=? AND question_no=?", (selected or None, points, state, reason, submission_id, question_no))
            previous_option = answer["final_option"] or answer["candidate_option"]
            review = conn.execute("SELECT page_id FROM objective_review_items WHERE submission_id=? AND question_no=? AND state='open' ORDER BY created_at DESC LIMIT 1", (submission_id,question_no)).fetchone()
            page_id = review["page_id"] if review else None
            conn.execute("INSERT INTO objective_review_events(review_id,submission_id,question_no,previous_option,revised_option,reason,created_at,page_id) VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, submission_id, question_no, previous_option, selected or None, reason, _utc_now(), page_id))
            conn.execute("UPDATE objective_review_items SET state='resolved',resolved_at=?,resolution='manual_answer_review' WHERE submission_id=? AND question_no=? AND state='open'", (_utc_now(),submission_id,question_no))
            _update_submission_score(conn, submission_id)
            _insert_audit(conn, "answer_manually_revised", job_id=answer["job_id"], submission_id=submission_id, page_id=page_id, details={"question_no": question_no, "previous_option": previous_option, "revised_option": selected or None, "reason_sha256": _sha256(reason.encode())})
            state_now = conn.execute("SELECT state FROM objective_submissions WHERE submission_id=?", (submission_id,)).fetchone()["state"]
            _refresh_job_summary(conn, answer["job_id"], event="job_summary_updated_after_manual_review")
        return jsonify({"submission_id": submission_id, "question_no": question_no, "option": selected or None, "submission_state": state_now, "awarded_points": points})

    @app.post("/api/submissions/<submission_id>/retry")
    def retry_submission(submission_id: str) -> Response:
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            conn.execute('BEGIN IMMEDIATE')
            submission=conn.execute("SELECT * FROM objective_submissions WHERE submission_id=?",(submission_id,)).fetchone()
            if not submission:
                return jsonify({"error":"submission_not_found"}),404
            if not workspace_exam_matches(conn, submission["assessment_id"]):
                return jsonify({"error":"workspace_exam_mismatch"}),409
            if submission["state"] not in ("recoverable","needs_review","failed") or not submission["failure_code"]:
                return jsonify({"error":"submission_not_retryable"}),409
            conn.execute("UPDATE objective_submissions SET state='queued',score=NULL,recovery_attempts=recovery_attempts+1 WHERE submission_id=?",(submission_id,))
            conn.execute("UPDATE objective_submission_pages SET recognition_state='queued',failure_code=NULL WHERE submission_id=?",(submission_id,))
            _insert_audit(conn,"submission_retry_requested",job_id=submission["job_id"],submission_id=submission_id,details={"recovery_attempt":int(submission["recovery_attempts"])+1})
            conn.execute("UPDATE objective_jobs SET status='queued',failure_code=NULL WHERE job_id=?",(submission["job_id"],))
            job_id=submission["job_id"]
        _launch_job(app,job_id)
        return jsonify({"submission_id":submission_id,"job_id":job_id,"status":"queued"}),202

    @app.get("/api/export.csv")
    def export_csv() -> Response:
        assessment_id = request.args.get("assessment_id", "")
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            assessment = conn.execute("SELECT answer_key_json FROM objective_assessments WHERE assessment_id=?", (assessment_id,)).fetchone()
            if not assessment:
                return jsonify({"error": "assessment_not_found"}), 404
            key = json.loads(assessment["answer_key_json"])
            job_id = request.args.get("job_id", "")
            if job_id and not conn.execute("SELECT 1 FROM objective_jobs WHERE job_id=? AND assessment_id=?", (job_id, assessment_id)).fetchone():
                return jsonify({"error": "任务不属于当前答案键"}), 409
            rows = conn.execute("SELECT * FROM objective_submissions WHERE assessment_id=? AND (?='' OR job_id=?) ORDER BY created_at,rowid", (assessment_id,job_id,job_id)).fetchall()
            answers_by_submission: dict[str, dict[int, sqlite3.Row]] = {}
            for answer in conn.execute("SELECT a.* FROM objective_answers a JOIN objective_submissions s ON s.submission_id=a.submission_id WHERE s.assessment_id=?", (assessment_id,)):
                answers_by_submission.setdefault(answer["submission_id"], {})[int(answer["question_no"])] = answer
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["anonymous_code", "state", "score", "max_score", *[f"Q{r['question_no']}" for r in key]])
        for row in rows:
            answers = answers_by_submission.get(row["submission_id"], {})
            answer_values = []
            for answer_key_item in key:
                answer_row = answers.get(int(answer_key_item["question_no"]))
                answer_values.append(answer_row["final_option"] or "" if answer_row else "")
            writer.writerow([row["anonymous_code"], row["state"], "" if row["score"] is None else row["score"], row["max_score"], *answer_values])
        response = Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8")
        response.headers["Content-Disposition"] = f"attachment; filename=objective-anonymous-{assessment_id[:8]}.csv"
        return response

    @app.get("/api/audit")
    def audit_events() -> Response:
        job_id = request.args.get("job_id", "")
        with _connect(app.config["OBJECTIVE_DB_PATH"]) as conn:
            rows = conn.execute("SELECT job_id,submission_id,page_id,event_type,image_sha256,details_json,created_at FROM objective_audit_events WHERE job_id=? ORDER BY rowid", (job_id,)).fetchall()
        return jsonify({"events": [{"job_id":row["job_id"],"submission_id":row["submission_id"],"page_id":row["page_id"],"event_type": row["event_type"], "image_sha256": row["image_sha256"], "details": json.loads(row["details_json"]), "created_at": row["created_at"]} for row in rows]})

    _recover_startup_jobs(app)
    return app
