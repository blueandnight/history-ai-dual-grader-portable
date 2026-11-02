from __future__ import annotations

import gc
import io
import json
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from objective.app import MAX_FILE_BYTES, _connect, _init_db, create_app
from objective.synthetic import photograph, render_omr_card
from objective.templates import BUILTIN_TEMPLATES
from objective_tests.auth_helpers import ObjectiveTestClient, login_client


class _ProcessStop(BaseException):
    pass


class Stage3BChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="objective-stage3b-")
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "runtime"
        self.app = create_app(self.data_dir)
        self.app.config.update(TESTING=True)
        self.app.test_client_class = ObjectiveTestClient
        self.client = login_client(self.app.test_client())
        self.assessment_id = self.create_assessment()

    def tearDown(self):
        self.temp.cleanup()

    def create_assessment(self, count=20):
        key = [{"question_no": q, "correct_option": "A", "points": 1} for q in range(1, count + 1)]
        response = self.client.post("/api/assessments", json={"title": "SYNTHETIC ONLY", "answer_key": key})
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["assessment_id"]

    def image_bytes(self):
        config = BUILTIN_TEMPLATES[0]["config"]
        page = render_omr_card(config, ["A"] * 20)
        return photograph(page, __import__("random").Random(100))

    def upload(self, payload=None):
        return self.client.post("/api/uploads", data={
            "assessment_id": self.assessment_id, "mode": "omr", "template_id": "auto",
            "grouping": "one_per_image", "files": (io.BytesIO(payload or self.image_bytes()), "synthetic.png"),
        }, content_type="multipart/form-data")

    def wait_job(self, client, job_id, terminal=("completed", "needs_review", "recoverable", "failed"), timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = client.get(f"/api/jobs/{job_id}")
            self.assertEqual(response.status_code, 200, response.get_json())
            data = response.get_json()
            if data["job"]["status"] in terminal:
                return data
            time.sleep(0.03)
        self.fail(f"job {job_id} did not reach a terminal/reviewable state")

    def wait_worker_exit(self, job_id, timeout=10):
        from objective.app import _JOB_BY_ID, _JOB_LOCK
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with _JOB_LOCK:
                worker = _JOB_BY_ID.get(job_id)
                if worker is None or not worker.is_alive():
                    return
            time.sleep(0.02)
        self.fail("simulated interrupted worker did not exit")

    def test_auth_session_protects_all_business_reads_and_writes(self):
        uploaded = self.upload()
        self.assertEqual(uploaded.status_code, 202, uploaded.get_json())
        result = self.wait_job(self.client, uploaded.get_json()["job_id"])
        submission_id = result["submissions"][0]["submission_id"]
        anonymous_code = result["submissions"][0]["anonymous_code"]
        anonymous = self.app.test_client()
        paths = (
            "/api/status",
            f"/api/jobs/{result['job']['job_id']}",
            "/api/submissions?assessment_id=" + self.assessment_id,
            f"/api/submissions/{submission_id}",
            f"/api/submissions/{submission_id}/pages/1",
            "/api/export.csv?assessment_id=" + self.assessment_id,
            "/api/review-queue?assessment_id=" + self.assessment_id,
            f"/api/audit?job_id={result['job']['job_id']}",
        )
        for path in paths:
            self.assertEqual(anonymous.get(path, headers={"Authorization": f"Bearer {anonymous_code}", "X-Anonymous-Code": anonymous_code}).status_code, 401, path)
        self.assertEqual(anonymous.post("/api/assessments", json={"answer_key": []}).status_code, 401)
        self.assertEqual(anonymous.post("/api/uploads", data={}).status_code, 401)
        self.assertEqual(anonymous.post(f"/api/submissions/{submission_id}/retry", headers={"Authorization": f"Bearer {anonymous_code}"}).status_code, 401)
        self.assertEqual(anonymous.get("/api/auth/session").status_code, 401)
        self.assertEqual(anonymous.get("/api/status", headers={"X-Anonymous-Code": "ANON-CODE-ONLY"}).status_code, 401)

    def test_csrf_required_and_cross_origin_posts_are_rejected(self):
        self.client.csrf_token = ""
        missing = self.client.post("/api/assessments", json={"title": "no token", "answer_key": [{"question_no": 1, "correct_option": "A", "points": 1}]})
        self.assertEqual(missing.status_code, 403)
        self.client.csrf_token = "wrong-token"
        wrong = self.client.post("/api/assessments", json={"title": "wrong token", "answer_key": [{"question_no": 1, "correct_option": "A", "points": 1}]})
        self.assertEqual(wrong.status_code, 403)
        self.client.csrf_token = "valid-again"
        evil = self.client.post("/api/assessments", json={"title": "evil", "answer_key": [{"question_no": 1, "correct_option": "A", "points": 1}]}, headers={"Origin": "https://attacker.example", "X-CSRF-Token": "valid-again"})
        self.assertEqual(evil.status_code, 403)
        self.client.csrf_token = ""
        login_client(self.client)
        accepted = self.client.post("/api/assessments", json={"title": "SYNTHETIC", "answer_key": [{"question_no": 1, "correct_option": "A", "points": 1}]})
        self.assertEqual(accepted.status_code, 201, accepted.get_json())

    def test_hash_failure_audit_is_job_submission_page_scoped(self):
        response = self.upload()
        self.assertEqual(response.status_code, 202, response.get_json())
        result = self.wait_job(self.client, response.get_json()["job_id"])
        submission_id = result["submissions"][0]["submission_id"]
        with _connect(self.app.config["OBJECTIVE_DB_PATH"]) as conn:
            page = conn.execute("SELECT stored_path FROM objective_submission_pages WHERE submission_id=?", (submission_id,)).fetchone()
        stored = Path(page["stored_path"])
        stored.write_bytes(stored.read_bytes() + b"synthetic-tamper")
        self.assertEqual(self.client.get(f"/api/submissions/{submission_id}/pages/1").status_code, 409)
        events = self.client.get(f"/api/audit?job_id={result['job']['job_id']}").get_json()["events"]
        mismatch = next(row for row in events if row["event_type"] == "stored_image_hash_mismatch")
        self.assertEqual(mismatch["job_id"], result["job"]["job_id"])
        self.assertEqual(mismatch["submission_id"], submission_id)
        self.assertIsNotNone(mismatch["page_id"])

    def test_worker_exception_creates_review_items_retry_and_preserves_one_submission(self):
        with patch("objective.app.recognize_omr", side_effect=RuntimeError("SYNTHETIC_PRIVATE_PATH_MARKER")):
            response = self.upload()
            self.assertEqual(response.status_code, 202, response.get_json())
            result = self.wait_job(self.client, response.get_json()["job_id"])
        self.assertEqual(result["job"]["status"], "recoverable")
        self.assertEqual(result["submissions"][0]["state"], "recoverable")
        submission_id = result["submissions"][0]["submission_id"]
        code = result["submissions"][0]["anonymous_code"]
        queue = self.client.get(f"/api/review-queue?assessment_id={self.assessment_id}").get_json()
        self.assertGreaterEqual(queue["count"], 21)
        self.assertTrue(any(item["question_no"] is None and item["error_type"] == "RuntimeError" for item in queue["items"]))
        audit = self.client.get(f"/api/audit?job_id={result['job']['job_id']}").get_json()["events"]
        failure = next(event for event in audit if event["event_type"] == "processing_error")
        self.assertEqual(failure["details"]["error_type"], "RuntimeError")
        self.assertNotIn("SYNTHETIC_PRIVATE_PATH_MARKER", json.dumps(audit))
        retry = self.client.post(f"/api/submissions/{submission_id}/retry")
        self.assertEqual(retry.status_code, 202, retry.get_json())
        completed = self.wait_job(self.client, retry.get_json()["job_id"])
        self.assertEqual(completed["job"]["status"], "completed")
        self.assertEqual(completed["submissions"][0]["anonymous_code"], code)
        with _connect(self.app.config["OBJECTIVE_DB_PATH"]) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM objective_submissions WHERE submission_id=?", (submission_id,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM objective_answers WHERE submission_id=?", (submission_id,)).fetchone()[0], 20)

    def test_template_configuration_failure_is_visible_and_actionable(self):
        with patch("objective.app._launch_job", return_value=False):
            response = self.upload()
        self.assertEqual(response.status_code, 202, response.get_json())
        job_id = response.get_json()["job_id"]
        with _connect(self.app.config["OBJECTIVE_DB_PATH"]) as conn:
            submission_id = conn.execute("SELECT submission_id FROM objective_submissions WHERE job_id=?", (job_id,)).fetchone()[0]
            template_id = conn.execute("SELECT template_id FROM objective_jobs WHERE job_id=?", (job_id,)).fetchone()[0]
            template_id = template_id if template_id != "auto" else conn.execute("SELECT template_id FROM objective_omr_templates WHERE confirmed=1 ORDER BY template_id LIMIT 1").fetchone()[0]
            conn.execute("UPDATE objective_omr_templates SET config_json='{' WHERE template_id=?", (template_id,))
        restarted = create_app(self.data_dir)
        restarted.config.update(TESTING=True)
        restarted.test_client_class = ObjectiveTestClient
        client = login_client(restarted.test_client())
        result = self.wait_job(client, job_id)
        self.assertEqual(result["job"]["status"], "recoverable")
        self.assertEqual(result["submissions"][0]["state"], "recoverable")
        queue = client.get(f"/api/review-queue?assessment_id={self.assessment_id}").get_json()
        visible = [item for item in queue["items"] if item["submission_id"] == submission_id]
        self.assertTrue(visible)
        self.assertTrue(any(item["error_type"] == "JSONDecodeError" for item in visible))
        audit = client.get(f"/api/audit?job_id={job_id}").get_json()["events"]
        self.assertTrue(any(event["event_type"] == "processing_error" and event["submission_id"] == submission_id for event in audit))

    def _make_crashed_job(self, stage):
        original_hook = threading.excepthook
        threading.excepthook = lambda _args: None
        try:
            target = "objective.app._process_submission" if stage == "before_recognition" else "objective.app.recognize_omr" if stage == "during_recognition" else "objective.app._finalize_submission" if stage == "after_answers_before_summary" else "objective.app._refresh_job_summary"
            with patch(target, side_effect=_ProcessStop()):
                response = self.upload()
                self.assertEqual(response.status_code, 202, response.get_json())
                job_id = response.get_json()["job_id"]
                self.wait_worker_exit(job_id)
            with _connect(self.app.config["OBJECTIVE_DB_PATH"]) as conn:
                sub = conn.execute("SELECT submission_id,anonymous_code,state FROM objective_submissions WHERE job_id=?", (job_id,)).fetchone()
                before = {"submission_id": sub["submission_id"], "anonymous_code": sub["anonymous_code"], "state": sub["state"]}
                answer_rows = [tuple(row) for row in conn.execute("SELECT * FROM objective_answers WHERE submission_id=? ORDER BY question_no", (sub["submission_id"],))]
                audit_rows = [tuple(row) for row in conn.execute("SELECT event_id,job_id,submission_id,page_id,event_type,image_sha256,details_json,created_at FROM objective_audit_events WHERE job_id=? ORDER BY rowid", (job_id,))]
                page = conn.execute("SELECT stored_path,image_sha256,recognition_state FROM objective_submission_pages WHERE submission_id=?", (sub["submission_id"],)).fetchone()
            return job_id, before, answer_rows, audit_rows, dict(page)
        finally:
            threading.excepthook = original_hook

    def test_interruption_boundaries_recover_and_are_idempotent(self):
        for stage in ("before_recognition", "during_recognition", "after_answers_before_summary", "before_job_summary"):
            with self.subTest(stage=stage):
                job_id, before, answers_before, audit_before, page = self._make_crashed_job(stage)
                if stage == "after_answers_before_summary":
                    self.assertEqual(len(answers_before), 20)
                if stage == "before_job_summary":
                    self.assertEqual(before["state"], "completed")
                restarted = create_app(self.data_dir)
                restarted.config.update(TESTING=True)
                restarted.test_client_class = ObjectiveTestClient
                client = login_client(restarted.test_client())
                result = self.wait_job(client, job_id)
                self.assertEqual(result["job"]["status"], "completed", result["job"])
                self.assertEqual(result["submissions"][0]["submission_id"], before["submission_id"])
                self.assertEqual(result["submissions"][0]["anonymous_code"], before["anonymous_code"])
                with _connect(restarted.config["OBJECTIVE_DB_PATH"]) as conn:
                    recovered_answers = [tuple(row) for row in conn.execute("SELECT * FROM objective_answers WHERE submission_id=? ORDER BY question_no", (before["submission_id"],))]
                    self.assertEqual(len(recovered_answers), 20)
                    if answers_before:
                        self.assertEqual(recovered_answers, answers_before)
                    recovered_audit = {row[0]: tuple(row) for row in conn.execute("SELECT event_id,job_id,submission_id,page_id,event_type,image_sha256,details_json,created_at FROM objective_audit_events WHERE job_id=?", (job_id,))}
                    for audit_row in audit_before:
                        self.assertEqual(recovered_audit[audit_row[0]], audit_row)
                    stored = Path(page["stored_path"]).read_bytes()
                    self.assertEqual(__import__("hashlib").sha256(stored).hexdigest(), page["image_sha256"])
                    self.assertGreater(conn.execute("SELECT COUNT(*) FROM objective_audit_events WHERE job_id=?", (job_id,)).fetchone()[0], 0)
                second = create_app(self.data_dir)
                second.config.update(TESTING=True)
                second.test_client_class = ObjectiveTestClient
                again = login_client(second.test_client())
                stable = self.wait_job(again, job_id)
                self.assertEqual(stable["submissions"][0]["anonymous_code"], before["anonymous_code"])
                with _connect(second.config["OBJECTIVE_DB_PATH"]) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM objective_answers WHERE submission_id=?", (before["submission_id"],)).fetchone()[0], 20)

    def test_unrecoverable_page_integrity_failure_enters_visible_review_queue(self):
        for failure in ("tampered", "missing"):
            with self.subTest(failure=failure):
                job_id, before, answers_before, _audit_before, page = self._make_crashed_job("after_answers_before_summary")
                stored = Path(page["stored_path"])
                if failure == "tampered":
                    stored.write_bytes(stored.read_bytes() + b"synthetic-tamper")
                else:
                    stored.unlink()
                restarted = create_app(self.data_dir)
                restarted.config.update(TESTING=True)
                restarted.test_client_class = ObjectiveTestClient
                client = login_client(restarted.test_client())
                result = self.wait_job(client, job_id)
                self.assertEqual(result["job"]["status"], "needs_review")
                row = next(item for item in result["submissions"] if item["submission_id"] == before["submission_id"])
                self.assertEqual(row["state"], "needs_review")
                self.assertIsNone(row["score"])
                queue = client.get(f"/api/review-queue?assessment_id={self.assessment_id}").get_json()
                submission_items = [item for item in queue["items"] if item["submission_id"] == before["submission_id"]]
                self.assertTrue(submission_items)
                self.assertTrue(any(item["error_type"] == "OSError" for item in submission_items))
                audit = client.get(f"/api/audit?job_id={job_id}").get_json()["events"]
                expected_event = "stored_image_hash_mismatch" if failure == "tampered" else "processing_error"
                self.assertTrue(any(event["event_type"] == expected_event and event["job_id"] == job_id for event in audit))
                with _connect(restarted.config["OBJECTIVE_DB_PATH"]) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM objective_review_items WHERE submission_id=? AND state='open'", (before["submission_id"],)).fetchone()[0], len(submission_items))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM objective_answers WHERE submission_id=?", (before["submission_id"],)).fetchone()[0], len(answers_before))

    def test_resourcewarning_upload_spool_is_closed_on_early_returns(self):
        many = [(io.BytesIO(b"x"), f"synthetic-{index}.png") for index in range(101)]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            response = self.client.post("/api/uploads", data={"assessment_id": self.assessment_id, "mode": "omr", "template_id": "auto", "files": many}, content_type="multipart/form-data")
            self.assertEqual(response.status_code, 400)
            del response
            large = self.upload(b"x" * (MAX_FILE_BYTES + 1))
            self.assertEqual(large.status_code, 400)
            del large
            gc.collect()
        leaks = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual(leaks, [])


def _make_synthetic_stage3_database(data_dir: Path, database_path: Path) -> None:
    """Build a portable pre-v1 objective DB using only synthetic test records."""
    app = create_app(data_dir)
    app.config.update(TESTING=True)
    app.test_client_class = ObjectiveTestClient
    client = login_client(app.test_client())
    answer_key = [{"question_no": q, "correct_option": "A", "points": 1} for q in range(1, 21)]
    assessment = client.post("/api/assessments", json={"title": "SYNTHETIC MIGRATION", "answer_key": answer_key})
    if assessment.status_code != 201:
        raise AssertionError(assessment.get_json())
    image = photograph(render_omr_card(BUILTIN_TEMPLATES[0]["config"], ["A"] * 20), __import__("random").Random(441))
    response = client.post("/api/uploads", data={
        "assessment_id": assessment.get_json()["assessment_id"],
        "mode": "omr", "template_id": "auto", "grouping": "one_per_image",
        "files": (io.BytesIO(image), "synthetic-migration.png"),
    }, content_type="multipart/form-data")
    if response.status_code != 202:
        raise AssertionError(response.get_json())
    job_id = response.get_json()["job_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = client.get(f"/api/jobs/{job_id}").get_json()["job"]["status"]
        if status in {"completed", "needs_review", "recoverable", "failed"}:
            break
        time.sleep(0.03)
    else:
        raise AssertionError("synthetic migration job did not finish")

    source = sqlite3.connect(app.config["OBJECTIVE_DB_PATH"])
    target = sqlite3.connect(database_path)
    source.backup(target)
    target.close()
    source.close()
    conn = sqlite3.connect(database_path)
    # Recreate the stage3 v0 naming boundary. The linked page file remains in
    # this test's temporary runtime so content and image hashes stay verifiable.
    for current_name, stage3_name in (
        ("objective_assessments", "assessments"),
        ("objective_omr_templates", "omr_templates"),
        ("objective_jobs", "jobs"),
        ("objective_submissions", "submissions"),
        ("objective_submission_pages", "submission_pages"),
        ("objective_review_events", "review_events"),
        ("objective_audit_events", "audit_events"),
    ):
        conn.execute(f'ALTER TABLE "{current_name}" RENAME TO "{stage3_name}"')
    conn.execute("DROP TABLE IF EXISTS objective_review_items")
    conn.execute("PRAGMA user_version=0")
    conn.commit()
    conn.close()


class MigrationChecks(unittest.TestCase):
    def test_fresh_stage3_upgrade_and_idempotent_current_schema(self):
        with tempfile.TemporaryDirectory(prefix="objective-migrations-") as temp:
            fresh = Path(temp) / "fresh.sqlite3"
            _init_db(fresh)
            with _connect(fresh) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)
                self.assertTrue(conn.execute("SELECT 1 FROM sqlite_master WHERE name='objective_review_items'").fetchone())
            stage3 = Path(temp) / "stage3-source.sqlite3"
            _make_synthetic_stage3_database(Path(temp) / "stage3-runtime", stage3)
            current = Path(temp) / "stage3-copy.sqlite3"
            source = sqlite3.connect(f"file:{stage3.as_posix()}?mode=ro", uri=True)
            target = sqlite3.connect(current)
            source.backup(target)
            target.close()
            source.close()
            original = sqlite3.connect(current)
            migration_pairs = (
                ("assessments", "objective_assessments"),
                ("omr_templates", "objective_omr_templates"),
                ("jobs", "objective_jobs"),
                ("submissions", "objective_submissions"),
                ("submission_pages", "objective_submission_pages"),
                ("objective_answers", "objective_answers"),
                ("review_events", "objective_review_events"),
                ("audit_events", "objective_audit_events"),
            )
            preserved = {}
            for old_name, new_name in migration_pairs:
                if not original.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (old_name,)).fetchone():
                    continue
                columns = [row[1] for row in original.execute(f'PRAGMA table_info("{old_name}")')]
                preserved[old_name] = (new_name, columns, [tuple(row) for row in original.execute(f'SELECT {",".join(columns)} FROM "{old_name}" ORDER BY rowid')])
            original_page_files = [row[0] for row in original.execute("SELECT stored_path FROM submission_pages ORDER BY rowid")]
            original.close()
            _init_db(current)
            first = sqlite3.connect(current)
            self.addCleanup(first.close)
            self.assertEqual(first.execute("PRAGMA user_version").fetchone()[0], 5)
            for old_name, (new_name, columns, rows) in preserved.items():
                actual = [tuple(row) for row in first.execute(f'SELECT {",".join(columns)} FROM "{new_name}" ORDER BY rowid')]
                expected_rows = rows
                status_column = "status" if old_name == "jobs" else "state" if old_name == "submissions" else None
                if status_column and status_column in columns:
                    status_index = columns.index(status_column)
                    expected_rows = [tuple((*row[:status_index], "completed", *row[status_index + 1:]) if row[status_index] == "complete" else row) for row in rows]
                self.assertEqual(actual, expected_rows, old_name)
            for stored_path in original_page_files:
                data = Path(stored_path).read_bytes()
                expected_hash = first.execute("SELECT image_sha256 FROM objective_submission_pages WHERE stored_path=?", (stored_path,)).fetchone()[0]
                self.assertEqual(__import__("hashlib").sha256(data).hexdigest(), expected_hash)
            schema1 = first.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            first.close()
            _init_db(current)
            second = sqlite3.connect(current)
            schema2 = second.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            self.assertEqual(schema1, schema2)
            self.assertEqual(second.execute("PRAGMA user_version").fetchone()[0], 5)
            second.close()

    def test_synthetic_legacy_material_schema_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="objective-legacy-main-") as temp:
            db = Path(temp) / "legacy.sqlite3"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE schema_meta (id INTEGER PRIMARY KEY CHECK (id=1),schema_version INTEGER NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE assignments (id TEXT PRIMARY KEY,name TEXT NOT NULL,question TEXT NOT NULL,standard_answer TEXT NOT NULL,criteria_json TEXT NOT NULL,sub_questions_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL);
                CREATE TABLE submissions (id TEXT PRIMARY KEY,assignment_id TEXT NOT NULL,student_label TEXT NOT NULL,answer_text TEXT NOT NULL,photos_json TEXT NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(assignment_id) REFERENCES assignments(id));
                INSERT INTO assignments VALUES('SYN-A','Synthetic','Synthetic prompt','A','{}','[]','synthetic-time');
                INSERT INTO submissions VALUES('SYN-S','SYN-A','SYNTHETIC','synthetic-answer','[]','{}','synthetic-time');
            """)
            before = {name: conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0] for name in ("schema_meta", "assignments", "submissions") if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()}
            row = conn.execute("SELECT * FROM submissions WHERE id='SYN-S'").fetchone()
            conn.close()
            _init_db(db)
            conn = sqlite3.connect(db)
            after = {name: conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0] for name in before}
            self.assertEqual(before, after)
            self.assertEqual(tuple(row), tuple(conn.execute("SELECT * FROM submissions WHERE id='SYN-S'").fetchone()))
            self.assertTrue(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='objective_submissions'").fetchone())
            conn.close()

    def test_migration_failure_rolls_back_renames_tables_and_version(self):
        with tempfile.TemporaryDirectory(prefix="objective-rollback-") as temp:
            stage3 = Path(temp) / "stage3-source.sqlite3"
            _make_synthetic_stage3_database(Path(temp) / "stage3-runtime", stage3)
            db = Path(temp) / "rollback.sqlite3"
            source = sqlite3.connect(f"file:{stage3.as_posix()}?mode=ro", uri=True)
            target = sqlite3.connect(db)
            source.backup(target)
            target.close()
            source.close()
            before = sqlite3.connect(db)
            before_tables = {r[0] for r in before.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            before_count = before.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
            before.close()
            with self.assertRaisesRegex(RuntimeError, "injected_migration_failure"):
                _init_db(db, fail_after_migration="objective_v1")
            after = sqlite3.connect(db)
            self.assertEqual(after.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual({r[0] for r in after.execute("SELECT name FROM sqlite_master WHERE type='table'")}, before_tables)
            self.assertEqual(after.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], before_count)
            self.assertFalse(after.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='objective_submissions'").fetchone())
            after.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
