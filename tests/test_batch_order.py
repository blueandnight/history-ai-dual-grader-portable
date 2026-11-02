import base64
import csv
import io
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from PIL import Image

import app
try:
    from .schema_fixtures import rebuild_legacy_submissions_table
except ImportError:
    from schema_fixtures import rebuild_legacy_submissions_table


class OrderedBatchTests(unittest.TestCase):
    def setUp(self):
        self.previous_paths = app.APP_PATHS
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        app.configure_paths(root / "portable", root / "program")
        conn = app.get_db()
        conn.execute(
            "INSERT INTO assignments (id, name, question, standard_answer, criteria_json, sub_questions_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("aaaaaaaaaaaa", "测试题", "题目", "参考答案", json.dumps([{"id": "p1", "label": "要点", "score": 2, "keywords": []}]), "[]", app.utc_now()),
        )
        conn.commit()
        conn.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.AppHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        image = Image.new("RGB", (1000, 800), "white")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=85)
        self.image_url = "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)
        app.stop_grade_worker(timeout_seconds=1)
        with app.ENQUEUED_SUBMISSIONS_LOCK:
            app.ENQUEUED_SUBMISSIONS.clear()
        app._install_paths(self.previous_paths)
        self.temp.cleanup()

    def request_json(self, path, payload):
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def request_json_status(self, path, payload, headers=None):
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as response:
            with response:
                return response.code, json.loads(response.read().decode("utf-8"))

    def get_json(self, path):
        with urlopen(self.base_url + path, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def make_batch(self, labels, page_counts=None, request_id=None):
        page_counts = page_counts or [1] * len(labels)
        return self.request_json("/api/batches", {
            "assignment_id": "aaaaaaaaaaaa",
            "class_name": "高一一班",
            "batch_request_id": request_id or app.uuid.uuid4().hex,
            "items": [
                {"student_label": label, "page_count": page_count}
                for label, page_count in zip(labels, page_counts)
            ],
        })[1]

    def upload_item(self, batch, index, page_sequences=None):
        return self.request_json("/api/submissions", self.submission_payload(batch, index, page_sequences))[1]["submission"]

    def submission_payload(self, batch, index, page_sequences=None):
        item = batch["items"][index]
        sequence = index + 1
        page_count = item["page_count"]
        page_sequences = page_sequences or list(range(1, page_count + 1))
        photos = [
            {
                "name": f"page-{page_sequence}.jpg",
                "page_sequence": page_sequence,
                "data_url": self.image_url,
                "original_data_url": self.image_url,
            }
            for page_sequence in page_sequences
        ]
        return {
            "assignment_id": "aaaaaaaaaaaa",
            "class_name": "高一一班",
            # Uploads remain anonymous; identity binding happens after grading.
            "student_label": "",
            "answer_text": "",
            "batch_id": batch["batch_id"],
            "submission_id": item["submission_id"],
            "sequence": sequence,
            "idempotency_key": f"{batch['batch_id']}:{sequence}",
            "photos": photos,
        }

    def upload_batch(self, batch, reverse=False):
        indexes = list(range(len(batch["items"])))
        if reverse:
            indexes.reverse()
        with patch.object(app, "enqueue_submission", return_value=True):
            with ThreadPoolExecutor(max_workers=min(8, len(indexes))) as pool:
                list(pool.map(lambda index: self.upload_item(batch, index), indexes))

    @staticmethod
    def mock_result(mode="mock-success", retryable=False):
        result = {
            "mode": mode,
            "suggested_total": 2,
            "max_total": 2,
            "confidence": 0.9,
            "blocked": mode != "mock-success",
            "needs_review": mode != "mock-success",
            "summary": "模拟批改结果",
            "student_identity": {},
            "sub_questions": [],
        }
        if retryable:
            result["diagnostics"] = {"retryable": True, "message": "模拟瞬时失败"}
        return result

    def test_concurrent_upload_arrival_never_reorders_persisted_batch_sequences(self):
        batch = self.make_batch([f"学生{i:02d}" for i in range(1, 21)])
        self.assertEqual([item["sequence"] for item in batch["items"]], list(range(1, 21)))
        self.upload_batch(batch, reverse=True)
        conn = app.get_db()
        try:
            rows = conn.execute(
                "SELECT id, batch_sequence FROM submissions WHERE batch_id = ? ORDER BY batch_sequence",
                (batch["batch_id"],),
            ).fetchall()
            self.assertEqual([row["batch_sequence"] for row in rows], list(range(1, 21)))
            self.assertEqual([row["id"] for row in rows], [item["submission_id"] for item in batch["items"]])
            self.assertEqual(conn.execute("SELECT state FROM task_queue WHERE submission_id = ?", (rows[0]["id"],)).fetchone()[0], "queued")
            self.assertTrue(all(
                conn.execute("SELECT state FROM task_queue WHERE submission_id = ?", (row["id"],)).fetchone()[0] == "waiting_batch"
                for row in rows[1:]
            ))
            with self.assertRaises(app.sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO submissions (id, assignment_id, student_label, answer_text, photos_json, result_json, created_at, batch_id, batch_sequence) VALUES (?, ?, ?, '', '[]', '{}', ?, ?, 1)",
                    ("ffffffffffff", "aaaaaaaaaaaa", "重复序号", app.utc_now(), batch["batch_id"]),
                )
        finally:
            conn.close()

    def test_batch_cursor_advance_during_upload_enqueues_new_current_sequence(self):
        batch = self.make_batch(["合成答卷A", "合成答卷B"])
        with patch.object(app, "model_configured", return_value=False), patch.object(
            app, "enqueue_submission", return_value=True
        ):
            first = self.upload_item(batch, 0)

        conn = app.get_db()
        conn.execute(
            "UPDATE submissions SET state='completed', result_json=? WHERE id=?",
            (json.dumps(self.mock_result()), first["id"]),
        )
        conn.execute("UPDATE task_queue SET state='completed' WHERE submission_id=?", (first["id"],))
        conn.commit()
        conn.close()

        original_enqueue_current = app._enqueue_current_batch_submission

        def advance_cursor_then_enqueue(conn, batch_id):
            # Simulate sequence 1 finishing after sequence 2 read the old
            # cursor but before the post-upload recheck runs.
            conn.execute(
                "UPDATE grading_batches SET next_sequence=2, status='active' WHERE id=? AND next_sequence=1",
                (batch_id,),
            )
            return original_enqueue_current(conn, batch_id)

        with patch.object(app, "model_configured", return_value=False), patch.object(
            app, "_enqueue_current_batch_submission", side_effect=advance_cursor_then_enqueue
        ), patch.object(app, "enqueue_submission", return_value=True) as enqueue:
            second = self.upload_item(batch, 1)

        conn = app.get_db()
        try:
            queued = conn.execute(
                "SELECT state FROM task_queue WHERE submission_id=?", (second["id"],)
            ).fetchone()
            self.assertEqual(queued["state"], "queued")
            self.assertEqual(
                conn.execute("SELECT next_sequence FROM grading_batches WHERE id=?", (batch["batch_id"],)).fetchone()[0],
                2,
            )
        finally:
            conn.close()
        enqueue.assert_called_once_with(second["id"])

    def test_marking_codes_are_random_exam_scoped_and_retry_collisions(self):
        conn = app.get_db()
        exam_a = "a" * 32
        exam_b = "b" * 32
        conn.executemany(
            "INSERT INTO grading_exams (id, assignment_id, created_at) VALUES (?, ?, ?)",
            [(exam_a, "aaaaaaaaaaaa", app.utc_now()), (exam_b, "aaaaaaaaaaaa", app.utc_now())],
        )
        with patch.object(app.secrets, "token_hex", side_effect=["A" * 32]):
            first = app.generate_exam_marking_code(conn, exam_a)
        self.assertRegex(first, r"^[0-9A-F]{32}$")
        self.assertEqual(first, "A" * 32)
        conn.execute(
            "INSERT INTO exam_marking_codes (id, exam_id, code, created_at) VALUES (?, ?, ?, ?)",
            ("synthetic-code-row", exam_a, first, app.utc_now()),
        )
        with patch.object(app.secrets, "token_hex", side_effect=[first, "B" * 32]) as token_hex:
            after_collision = app.generate_exam_marking_code(conn, exam_a)
        self.assertEqual(after_collision, "B" * 32)
        self.assertEqual(token_hex.call_count, 2)
        with patch.object(app.secrets, "token_hex", side_effect=[first]):
            same_random_value_in_other_exam = app.generate_exam_marking_code(conn, exam_b)
        self.assertEqual(same_random_value_in_other_exam, first)
        conn.close()

    def test_single_upload_gets_anonymous_code_without_roster_matching(self):
        conn = app.get_db()
        conn.execute(
            "INSERT INTO roster_students (id, class_name, student_no, student_name, created_at) VALUES (?, ?, ?, ?, ?)",
            ("synthetic-single-student", "高一一班", "SYNTHETIC_NO_203", "SYNTHETIC_NAME_203", app.utc_now()),
        )
        conn.commit()
        conn.close()
        with patch.object(app, "model_configured", return_value=False), patch.object(
            app, "enqueue_submission", return_value=None
        ):
            status, response = self.request_json_status("/api/submissions", {
                "assignment_id": "aaaaaaaaaaaa",
                "class_name": "高一一班",
                "student_label": "SYNTHETIC_NAME_203",
                "answer_text": "",
                "photos": [{"name": "SYNTHETIC_NAME_203.jpg", "data_url": self.image_url}],
            })
        self.assertEqual(status, 202)
        submission = response["submission"]
        self.assertRegex(submission["marking_code"], r"^[0-9A-F]{32}$")
        self.assertEqual(submission["marking_status"], "unmatched")
        self.assertEqual(submission["student_name"], "")
        self.assertEqual(submission["class_name"], "")
        self.assertNotIn("SYNTHETIC_NAME_203", json.dumps(submission, ensure_ascii=False))
        conn = app.get_db()
        mapping = conn.execute(
            "SELECT binding_status, roster_student_id FROM exam_marking_codes WHERE submission_id=?",
            (submission["id"],),
        ).fetchone()
        self.assertEqual(tuple(mapping), ("unmatched", None))
        conn.close()

    def test_confirmed_quick_anonymous_record_joins_formal_exam_without_regrading(self):
        with patch.object(app, "model_configured", return_value=False), patch.object(
            app, "enqueue_submission", return_value=None
        ):
            status, response = self.request_json_status("/api/submissions", {
                "assignment_id": "aaaaaaaaaaaa",
                "class_name": "",
                "student_label": "",
                "answer_text": "",
                "idempotency_key": "synthetic-quick-record-for-formal-exam",
                "photos": [{"name": "synthetic-quick-page.jpg", "data_url": self.image_url}],
            })
        self.assertEqual(status, 202)
        submission_id = response["submission"]["id"]
        source_exam_id = response["submission"]["exam_id"]
        marking_code = response["submission"]["marking_code"]
        confirmed_at = app.utc_now()
        result_json = json.dumps({
            "mode": "vision-model",
            "suggested_total": 2,
            "max_total": 2,
            "summary": "合成的模拟模型结果",
            "sub_questions": [{"sub_question_id": "Q1", "suggested_total": 2}],
        }, ensure_ascii=False)
        evidence_json = json.dumps([{
            "criterion_id": "C1",
            "student_evidence": "合成证据",
            "evidence": {"photo_index": 1, "x": 0.2, "y": 0.2, "width": 0.3, "height": 0.1},
        }], ensure_ascii=False)
        conn = app.get_db()
        conn.execute(
            "UPDATE submissions SET state='completed', result_json=?, evidence_json=?, final_total=1.5, confirmed_at=? WHERE id=?",
            (result_json, evidence_json, confirmed_at, submission_id),
        )
        conn.commit()
        before = conn.execute(
            "SELECT exam_id, marking_code, marking_code_id, result_json, evidence_json, photos_json, "
            "answer_regions_json, final_total, confirmed_at, batch_id, batch_sequence "
            "FROM submissions WHERE id=?",
            (submission_id,),
        ).fetchone()
        task_state_before = conn.execute(
            "SELECT state FROM task_queue WHERE submission_id=?", (submission_id,)
        ).fetchone()["state"]
        conn.close()

        formal_batch = self.make_batch(["仅预留的正式批次序号"])
        target_exam_id = formal_batch["exam_id"]
        conn = app.get_db()
        batch_before = conn.execute(
            "SELECT next_sequence, status, pause_sequence FROM grading_batches WHERE id=?",
            (formal_batch["batch_id"],),
        ).fetchone()
        conn.close()

        with patch.object(app, "grade_submission", side_effect=AssertionError("must not grade again")) as grade, patch.object(
            app, "enqueue_submission"
        ) as enqueue:
            moved_status, moved = self.request_json_status("/api/attach-anonymous-submission", {
                "submission_id": submission_id,
                "target_exam_id": target_exam_id,
            })
        self.assertEqual(moved_status, 200)
        grade.assert_not_called()
        enqueue.assert_not_called()
        self.assertEqual(moved["submission"]["exam_id"], target_exam_id)
        self.assertEqual(moved["submission"]["marking_code"], marking_code)

        conn = app.get_db()
        after = conn.execute(
            "SELECT exam_id, marking_code, marking_code_id, result_json, evidence_json, photos_json, "
            "answer_regions_json, final_total, confirmed_at, batch_id, batch_sequence "
            "FROM submissions WHERE id=?",
            (submission_id,),
        ).fetchone()
        mapping = conn.execute(
            "SELECT exam_id, code, batch_id, batch_sequence, roster_student_id, binding_status "
            "FROM exam_marking_codes WHERE submission_id=?",
            (submission_id,),
        ).fetchone()
        task_state_after = conn.execute(
            "SELECT state FROM task_queue WHERE submission_id=?", (submission_id,)
        ).fetchone()["state"]
        batch_after = conn.execute(
            "SELECT next_sequence, status, pause_sequence FROM grading_batches WHERE id=?",
            (formal_batch["batch_id"],),
        ).fetchone()
        conn.close()

        self.assertEqual(before["exam_id"], source_exam_id)
        self.assertEqual(after["exam_id"], target_exam_id)
        for column in (
            "marking_code", "marking_code_id", "result_json", "evidence_json", "photos_json",
            "answer_regions_json", "final_total", "confirmed_at", "batch_id", "batch_sequence",
        ):
            self.assertEqual(after[column], before[column], column)
        self.assertEqual(tuple(mapping), (target_exam_id, marking_code, None, None, None, "unmatched"))
        self.assertEqual(task_state_after, task_state_before)
        self.assertEqual(tuple(batch_after), tuple(batch_before))
        ledger = self.get_json(f"/api/assignment-markings?assignment_id=aaaaaaaaaaaa")
        moved_record = next(item for item in ledger["records"] if item["submission_id"] == submission_id)
        self.assertEqual(moved_record["exam_id"], target_exam_id)
        self.assertIsNone(moved_record["batch_id"])

    def test_anonymous_upload_defers_identity_binding_and_rejects_duplicate_students(self):
        identity_name = "SYNTHETIC_DUPLICATE_NAME_SENTINEL"
        conn = app.get_db()
        conn.execute(
            "INSERT INTO roster_students (id, class_name, student_no, student_name, created_at) VALUES (?, ?, ?, ?, ?)",
            ("synthetic-duplicate-roster", "高一一班", "SYNTHETIC_DUPLICATE_NO", identity_name, app.utc_now()),
        )
        conn.commit()
        conn.close()

        batch = self.make_batch([identity_name, identity_name])
        mappings = self.get_json(f"/api/exam-mapping?exam_id={batch['exam_id']}")["mappings"]
        self.assertEqual([item["binding_status"] for item in mappings], ["unmatched", "unmatched"])
        self.assertTrue(all(item["roster_student_id"] is None for item in mappings))
        conn = app.get_db()
        manifest = conn.execute("SELECT manifest_json FROM grading_batches WHERE id=?", (batch["batch_id"],)).fetchone()[0]
        self.assertNotIn(identity_name, manifest)
        self.assertNotIn("SYNTHETIC_DUPLICATE_NO", manifest)
        conn.close()

        payload = self.submission_payload(batch, 0)
        with patch.object(app, "enqueue_submission", return_value=None):
            status, created = self.request_json_status("/api/submissions", payload)
        self.assertEqual(status, 202)
        submission = created["submission"]
        premature_bind, _ = self.request_json_status("/api/bind-student", {
            "submission_id": submission["id"],
            "roster_student_id": "synthetic-duplicate-roster",
        })
        self.assertEqual(premature_bind, 400)
        conn = app.get_db()
        conn.execute("UPDATE submissions SET state='completed' WHERE id=?", (submission["id"],))
        conn.commit()
        conn.close()
        bind_status, _ = self.request_json_status("/api/bind-student", {
            "submission_id": submission["id"],
            "roster_student_id": "synthetic-duplicate-roster",
        })
        self.assertEqual(bind_status, 200)
        second_id = batch["items"][1]["submission_id"]
        with patch.object(app, "enqueue_submission", return_value=None):
            second_status, second_created = self.request_json_status(
                "/api/submissions", self.submission_payload(batch, 1)
            )
        self.assertEqual(second_status, 202)
        conn = app.get_db()
        conn.execute("UPDATE submissions SET state='completed' WHERE id=?", (second_id,))
        conn.commit()
        conn.close()
        duplicate_bind, _ = self.request_json_status("/api/bind-student", {
            "submission_id": second_id,
            "roster_student_id": "synthetic-duplicate-roster",
        })
        self.assertEqual(duplicate_bind, 400)
        conn = app.get_db()
        stored = conn.execute("SELECT student_label, class_name, student_no, student_name FROM submissions WHERE id=?", (submission["id"],)).fetchone()
        mapping = conn.execute("SELECT binding_status, roster_student_id FROM exam_marking_codes WHERE submission_id=?", (submission["id"],)).fetchone()
        self.assertEqual(tuple(stored), (submission["marking_code"], "", "", ""))
        self.assertEqual(tuple(mapping), ("matched", "synthetic-duplicate-roster"))
        conn.close()

    def test_delayed_model_result_gates_next_sequence_until_durable_commit(self):
        batch = self.make_batch(["甲", "乙"])
        self.upload_batch(batch)
        started = threading.Event()
        release = threading.Event()
        guard = threading.Lock()
        active = 0
        peak = 0
        called = []

        def delayed_grade(row, *_args):
            nonlocal active, peak
            sequence = int(row["batch_sequence"])
            with guard:
                active += 1
                peak = max(peak, active)
                called.append(sequence)
            if sequence == 1:
                started.set()
                release.wait(5)
            with guard:
                active -= 1
            return self.mock_result()

        with patch.object(app, "grade_submission", side_effect=delayed_grade), patch.object(app, "enqueue_submission", return_value=True):
            first = threading.Thread(target=app.process_submission, args=(batch["items"][0]["submission_id"],))
            first.start()
            self.assertTrue(started.wait(3))
            app.process_submission(batch["items"][1]["submission_id"])
            self.assertEqual(called, [1], "later sequence reached the mocked model before the first result committed")
            release.set()
            first.join(timeout=5)
            self.assertFalse(first.is_alive())
            app.process_submission(batch["items"][1]["submission_id"])
        self.assertEqual(called, [1, 2])
        self.assertEqual(peak, 1)
        conn = app.get_db()
        try:
            batch_row = conn.execute("SELECT next_sequence, status FROM grading_batches WHERE id = ?", (batch["batch_id"],)).fetchone()
            self.assertEqual(batch_row["next_sequence"], 3)
            self.assertEqual(batch_row["status"], "completed")
        finally:
            conn.close()

    def test_model_failure_pauses_batch_and_manual_retry_reuses_same_sequence(self):
        batch = self.make_batch(["甲", "乙"])
        self.upload_batch(batch)
        calls = []

        def fail_then_succeed(row, *_args):
            calls.append(int(row["batch_sequence"]))
            return self.mock_result("vision-model-error", retryable=True) if len(calls) == 1 else self.mock_result()

        with patch.object(app, "grade_submission", side_effect=fail_then_succeed), patch.object(app, "enqueue_submission", return_value=True):
            app.process_submission(batch["items"][0]["submission_id"])
            app.process_submission(batch["items"][1]["submission_id"])
            self.assertEqual(calls, [1])
            conn = app.get_db()
            try:
                paused = conn.execute("SELECT status, next_sequence, pause_sequence FROM grading_batches WHERE id = ?", (batch["batch_id"],)).fetchone()
                self.assertEqual(tuple(paused), ("paused", 1, 1))
            finally:
                conn.close()
            self.request_json("/api/retry", {"submission_id": batch["items"][0]["submission_id"]})
            app.process_submission(batch["items"][0]["submission_id"])
            app.process_submission(batch["items"][1]["submission_id"])
        self.assertEqual(calls, [1, 1, 2])

    def test_restart_marks_inflight_batch_uncertain_and_never_auto_replays(self):
        batch = self.make_batch(["甲", "乙"])
        self.upload_batch(batch)
        conn = app.get_db()
        try:
            conn.execute("UPDATE submissions SET state='grading' WHERE id=?", (batch["items"][0]["submission_id"],))
            conn.execute("UPDATE task_queue SET state='running' WHERE submission_id=?", (batch["items"][0]["submission_id"],))
            app.recover_interrupted_grade_tasks(conn)
            conn.commit()
            row = conn.execute("SELECT state FROM submissions WHERE id=?", (batch["items"][0]["submission_id"],)).fetchone()
            batch_row = conn.execute("SELECT status, pause_sequence FROM grading_batches WHERE id=?", (batch["batch_id"],)).fetchone()
            self.assertEqual(row["state"], "recovery_pending")
            self.assertEqual(tuple(batch_row), ("paused", 1))
        finally:
            conn.close()
        called = []
        with patch.dict(app.os.environ, {"HISTORY_GRADER_WORKERS": "1"}), patch.object(
            app, "grade_submission", side_effect=lambda *_args: called.append("model") or self.mock_result()
        ), patch.object(app, "enqueue_submission", return_value=True):
            app.start_grade_worker()
            time.sleep(0.35)
            app.process_submission(batch["items"][0]["submission_id"])
            app.process_submission(batch["items"][1]["submission_id"])
        self.assertEqual(called, [], "startup recovery must not automatically repeat an uncertain provider call")

    def test_teacher_confirmed_skip_advances_cursor_and_writes_audit_event(self):
        batch = self.make_batch(["甲", "乙"])
        with patch.object(app, "enqueue_submission", return_value=True):
            self.upload_item(batch, 1)
        self.request_json("/api/batches/upload-failed", {
            "batch_id": batch["batch_id"], "sequence": 1, "error": "模拟上传损坏",
        })
        with patch.object(app, "enqueue_submission", return_value=True):
            self.request_json("/api/batches/skip", {
                "batch_id": batch["batch_id"],
                "submission_id": batch["items"][0]["submission_id"],
                "sequence": 1,
                "confirm_skip": True,
                "reason": "测试中教师确认跳过",
            })
        conn = app.get_db()
        try:
            state = conn.execute("SELECT next_sequence, status FROM grading_batches WHERE id=?", (batch["batch_id"],)).fetchone()
            self.assertEqual(tuple(state), (2, "active"))
            self.assertTrue(conn.execute("SELECT 1 FROM audit_events WHERE entity_id=? AND event_type='grading_batch_sequence_skipped'", (batch["batch_id"],)).fetchone())
            self.assertEqual(conn.execute("SELECT state FROM task_queue WHERE submission_id=?", (batch["items"][1]["submission_id"],)).fetchone()[0], "queued")
        finally:
            conn.close()

    def test_failed_upload_can_be_retried_at_its_reserved_sequence(self):
        batch = self.make_batch(["甲", "乙"])
        self.request_json("/api/batches/upload-failed", {
            "batch_id": batch["batch_id"], "sequence": 1, "error": "模拟网络中断",
        })
        with patch.object(app, "enqueue_submission", return_value=True):
            uploaded = self.upload_item(batch, 0)
        self.assertEqual(uploaded["sequence"], 1)
        conn = app.get_db()
        try:
            current = conn.execute("SELECT status, next_sequence, pause_sequence FROM grading_batches WHERE id=?", (batch["batch_id"],)).fetchone()
            self.assertEqual(tuple(current), ("active", 1, None))
            self.assertEqual(conn.execute("SELECT state FROM task_queue WHERE submission_id=?", (batch["items"][0]["submission_id"],)).fetchone()[0], "queued")
        finally:
            conn.close()

    def test_restart_pauses_partial_upload_and_allows_same_sequence_reupload(self):
        batch = self.make_batch(["甲"])
        conn = app.get_db()
        try:
            app.recover_interrupted_grade_tasks(conn)
            conn.commit()
            row = conn.execute("SELECT status, pause_sequence, manifest_json FROM grading_batches WHERE id=?", (batch["batch_id"],)).fetchone()
            manifest = json.loads(row["manifest_json"])
            self.assertEqual((row["status"], row["pause_sequence"]), ("paused", 1))
            self.assertEqual(manifest["items"][0]["upload_status"], "failed")
        finally:
            conn.close()
        with patch.object(app, "enqueue_submission", return_value=True):
            submission = self.upload_item(batch, 0)
        self.assertEqual(submission["sequence"], 1)
        conn = app.get_db()
        try:
            row = conn.execute("SELECT status, next_sequence FROM grading_batches WHERE id=?", (batch["batch_id"],)).fetchone()
            self.assertEqual(tuple(row), ("active", 1))
        finally:
            conn.close()

    def test_history_and_formal_csv_keep_batch_sequence_not_upload_arrival_order(self):
        conn = app.get_db()
        for name in ("甲", "乙", "丙"):
            conn.execute(
                "INSERT INTO roster_students (id, class_name, student_no, student_name, created_at) VALUES (?, ?, ?, ?, ?)",
                (f"synthetic-{name}", "高一一班", f"SYN-{name}", name, app.utc_now()),
            )
        conn.commit()
        conn.close()
        batch = self.make_batch(["甲", "乙", "丙"])
        self.upload_batch(batch, reverse=True)
        conn = app.get_db()
        try:
            for sequence, item in enumerate(batch["items"], start=1):
                result = {
                    "mode": "vision-model",
                    "suggested_total": 1,
                    "max_total": 2,
                    "blocked": False,
                    "summary": "模拟已确认记录",
                    "sub_questions": [],
                }
                conn.execute(
                    "UPDATE submissions SET state='completed', result_json=?, final_total=?, confirmed_at=?, created_at=? WHERE id=?",
                    (json.dumps(result), sequence / 2, app.utc_now(), f"2026-09-2{4-sequence}T00:00:00+00:00", item["submission_id"]),
                )
            conn.commit()
        finally:
            conn.close()
        for item, student_id in zip(batch["items"], ("synthetic-甲", "synthetic-乙", "synthetic-丙")):
            status, _ = self.request_json_status("/api/bind-student", {
                "submission_id": item["submission_id"],
                "roster_student_id": student_id,
            })
            self.assertEqual(status, 200)
        history = self.get_json("/api/assignment-submissions?assignment_id=aaaaaaaaaaaa")
        batch_history = [item for item in history["submissions"] if item["batch_id"] == batch["batch_id"]]
        self.assertEqual([item["sequence"] for item in batch_history], [1, 2, 3])
        with urlopen(self.base_url + "/api/export.csv?assignment_id=aaaaaaaaaaaa", timeout=10) as response:
            content = response.read().decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(content)))
        sequence_column = rows[0].index("批次原始序号")
        self.assertEqual([int(row[sequence_column]) for row in rows[1:]], [1, 2, 3])

    def test_multipage_upload_persists_stable_page_sequence(self):
        batch = self.make_batch(["多页学生"], [3])
        with patch.object(app, "enqueue_submission", return_value=True):
            submission = self.upload_item(batch, 0, [3, 1, 2])
        self.assertEqual([photo["page_sequence"] for photo in submission["photos"]], [1, 2, 3])
        self.assertEqual([Path(photo["path"]).name for photo in submission["photos"]], ["page_01.jpg", "page_02.jpg", "page_03.jpg"])

    def test_batch_request_replay_returns_same_reserved_sequences(self):
        request_id = "stable-camera-session-20"
        first = self.make_batch(["甲", "乙"], request_id=request_id)
        second = self.make_batch(["甲", "乙"], request_id=request_id)
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(first["items"], second["items"])

    def test_mobile_skip_route_is_available_but_admin_paths_remain_local(self):
        self.assertIn("/api/batches", app.AppHandler.MOBILE_WRITE_PATHS)
        self.assertIn("/api/batches/upload-failed", app.AppHandler.MOBILE_WRITE_PATHS)
        self.assertIn("/api/batches/skip", app.AppHandler.MOBILE_WRITE_PATHS)
        self.assertIn("/api/ai-payload-preview", app.AppHandler.MOBILE_PREVIEW_PATHS)
        self.assertNotIn("/api/backups", app.AppHandler.MOBILE_PREVIEW_PATHS)
        self.assertNotIn("/api/settings", app.AppHandler.MOBILE_PREVIEW_PATHS)

    def test_same_submission_cannot_be_claimed_by_two_workers(self):
        batch = self.make_batch(["同一答卷"])
        self.upload_batch(batch)
        model_started = threading.Event()
        release_model = threading.Event()
        calls = []

        def delayed_grade(*_args):
            calls.append("called")
            model_started.set()
            release_model.wait(5)
            return self.mock_result()

        with patch.object(app, "grade_submission", side_effect=delayed_grade), patch.object(
            app, "enqueue_submission", return_value=True
        ):
            first = threading.Thread(target=app.process_submission, args=(batch["items"][0]["submission_id"],))
            first.start()
            self.assertTrue(model_started.wait(3))
            second = threading.Thread(target=app.process_submission, args=(batch["items"][0]["submission_id"],))
            second.start()
            second.join(timeout=2)
            self.assertFalse(second.is_alive())
            release_model.set()
            first.join(timeout=5)
            self.assertFalse(first.is_alive())

        self.assertEqual(calls, ["called"])
        conn = app.get_db()
        try:
            submission = conn.execute("SELECT state FROM submissions WHERE id=?", (batch["items"][0]["submission_id"],)).fetchone()
            cursor = conn.execute("SELECT next_sequence, status FROM grading_batches WHERE id=?", (batch["batch_id"],)).fetchone()
            self.assertEqual(submission["state"], "completed")
            self.assertEqual(tuple(cursor), (2, "completed"))
        finally:
            conn.close()

    def test_result_commit_failure_rolls_back_cursor_and_pauses_batch(self):
        batch = self.make_batch(["甲", "乙"])
        self.upload_batch(batch)
        real_append_audit_event = app.append_audit_event
        model_calls = []

        def fail_grade_audit(conn, event_type, *args, **kwargs):
            if event_type == "submission_graded":
                raise app.sqlite3.OperationalError("simulated audit storage failure")
            return real_append_audit_event(conn, event_type, *args, **kwargs)

        with patch.object(app, "grade_submission", side_effect=lambda row, *_: model_calls.append(row["id"]) or self.mock_result()), patch.object(
            app, "append_audit_event", side_effect=fail_grade_audit
        ), patch.object(app, "enqueue_submission", return_value=True):
            app.process_submission(batch["items"][0]["submission_id"])
            app.process_submission(batch["items"][1]["submission_id"])

        self.assertEqual(model_calls, [batch["items"][0]["submission_id"]])
        conn = app.get_db()
        try:
            cursor = conn.execute("SELECT next_sequence, status, pause_sequence FROM grading_batches WHERE id=?", (batch["batch_id"],)).fetchone()
            failed = conn.execute("SELECT state, result_json FROM submissions WHERE id=?", (batch["items"][0]["submission_id"],)).fetchone()
            later_queue = conn.execute("SELECT state FROM task_queue WHERE submission_id=?", (batch["items"][1]["submission_id"],)).fetchone()
            self.assertEqual(tuple(cursor), (1, "paused", 1))
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(json.loads(failed["result_json"])["mode"], "processing-error")
            self.assertEqual(later_queue["state"], "waiting_batch")
            self.assertFalse(conn.execute(
                "SELECT 1 FROM audit_events WHERE entity_id=? AND event_type='submission_graded'",
                (batch["items"][0]["submission_id"],),
            ).fetchone())
        finally:
            conn.close()

    def test_failure_in_one_batch_does_not_pause_another_batch(self):
        first = self.make_batch(["甲"], request_id="cross-batch-first")
        second = self.make_batch(["乙"], request_id="cross-batch-second")
        with patch.object(app, "enqueue_submission", return_value=True):
            self.upload_item(first, 0)
            self.upload_item(second, 0)

        def fail_first_batch(row, *_args):
            if row["batch_id"] == first["batch_id"]:
                return self.mock_result("vision-model-error", retryable=True)
            return self.mock_result()

        with patch.object(app, "grade_submission", side_effect=fail_first_batch), patch.object(
            app, "enqueue_submission", return_value=True
        ):
            app.process_submission(first["items"][0]["submission_id"])
            app.process_submission(second["items"][0]["submission_id"])

        conn = app.get_db()
        try:
            first_state = conn.execute("SELECT next_sequence, status FROM grading_batches WHERE id=?", (first["batch_id"],)).fetchone()
            second_state = conn.execute("SELECT next_sequence, status FROM grading_batches WHERE id=?", (second["batch_id"],)).fetchone()
            self.assertEqual(tuple(first_state), (1, "paused"))
            self.assertEqual(tuple(second_state), (2, "completed"))
        finally:
            conn.close()

    def test_concurrent_retry_requests_are_idempotent(self):
        batch = self.make_batch(["甲", "乙"])
        self.upload_batch(batch)
        calls = []

        def fail_then_succeed(row, *_args):
            calls.append(row["id"])
            return self.mock_result("vision-model-error", retryable=True) if len(calls) == 1 else self.mock_result()

        with patch.object(app, "grade_submission", side_effect=fail_then_succeed), patch.object(
            app, "enqueue_submission", return_value=True
        ):
            app.process_submission(batch["items"][0]["submission_id"])
            barrier = threading.Barrier(2)

            def retry_at_once():
                barrier.wait(timeout=3)
                return self.request_json_status("/api/retry", {"submission_id": batch["items"][0]["submission_id"]})

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _index: retry_at_once(), range(2)))
            self.assertEqual(sorted(status for status, _body in results), [200, 400])
            app.process_submission(batch["items"][0]["submission_id"])

        self.assertEqual(calls, [batch["items"][0]["submission_id"]] * 2)
        conn = app.get_db()
        try:
            attempts = conn.execute("SELECT attempt_count FROM task_queue WHERE submission_id=?", (batch["items"][0]["submission_id"],)).fetchone()[0]
            retry_events = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=? AND event_type='submission_retry_requested'",
                (batch["items"][0]["submission_id"],),
            ).fetchone()[0]
            cursor = conn.execute("SELECT next_sequence, status FROM grading_batches WHERE id=?", (batch["batch_id"],)).fetchone()
            self.assertEqual(attempts, 1)
            self.assertEqual(retry_events, 1)
            self.assertEqual(tuple(cursor), (2, "active"))
        finally:
            conn.close()

    def test_multipage_upload_is_bound_to_reserved_student_and_page_manifest(self):
        batch = self.make_batch(["多页学生", "另一学生"], [2, 1])
        payload = self.submission_payload(batch, 0, [1, 2])

        wrong_submission = dict(payload, submission_id=batch["items"][1]["submission_id"])
        status, _ = self.request_json_status("/api/submissions", wrong_submission)
        self.assertEqual(status, 400)

        wrong_page_count = dict(payload, photos=payload["photos"][:1])
        status, _ = self.request_json_status("/api/submissions", wrong_page_count)
        self.assertEqual(status, 400)

        duplicate_page = dict(payload, photos=[dict(payload["photos"][0], page_sequence=1), dict(payload["photos"][1], page_sequence=1)])
        status, _ = self.request_json_status("/api/submissions", duplicate_page)
        self.assertEqual(status, 400)

        conn = app.get_db()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM submissions WHERE batch_id=?", (batch["batch_id"],)).fetchone()[0], 0)
        finally:
            conn.close()
        valid = self.upload_item(batch, 0, [2, 1])
        self.assertEqual(valid["marking_code"], batch["items"][0]["marking_code"])
        self.assertNotEqual(valid["student_label"], "多页学生")
        self.assertEqual(valid["student_name"], "")
        self.assertEqual([photo["page_sequence"] for photo in valid["photos"]], [1, 2])
        # A retry with a changed client label cannot change its pre-reserved
        # identity mapping or regenerate the code.
        replay_status, replay = self.request_json_status(
            "/api/submissions", dict(payload, student_label="另一学生")
        )
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["submission"]["marking_code"], valid["marking_code"])

    def test_mobile_token_cannot_skip_computer_created_batch(self):
        batch = self.make_batch(["甲"])
        self.request_json("/api/batches/upload-failed", {
            "batch_id": batch["batch_id"], "sequence": 1, "error": "模拟上传失败",
        })
        previous_hash = app.MOBILE_TOKEN_HASH
        previous_expiry = app.MOBILE_TOKEN_EXPIRES_AT
        token, _expiry = app.issue_mobile_token()
        try:
            with patch.object(app.AppHandler, "is_local_client", return_value=False):
                status, _body = self.request_json_status(
                    "/api/batches/skip",
                    {"batch_id": batch["batch_id"], "submission_id": batch["items"][0]["submission_id"], "sequence": 1, "confirm_skip": True},
                    {"Authorization": f"Bearer {token}"},
                )
            self.assertEqual(status, 403)
        finally:
            app.MOBILE_TOKEN_HASH = previous_hash
            app.MOBILE_TOKEN_EXPIRES_AT = previous_expiry

        conn = app.get_db()
        try:
            cursor = conn.execute("SELECT next_sequence, status, pause_sequence FROM grading_batches WHERE id=?", (batch["batch_id"],)).fetchone()
            self.assertEqual(tuple(cursor), (1, "paused", 1))
        finally:
            conn.close()

    def test_legacy_unbatched_history_and_csv_keep_created_at_order(self):
        labels = ["旧作业甲", "旧作业乙", "旧作业丙"]
        created = ["2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00", "2025-01-03T00:00:00+00:00"]
        conn = app.get_db()
        try:
            for index, (label, created_at) in enumerate(zip(labels, created), start=1):
                legacy_id = f"{index:012x}"
                result = {"mode": "vision-model", "suggested_total": 1, "max_total": 2, "blocked": False, "summary": "旧记录", "sub_questions": []}
                conn.execute(
                    "INSERT INTO submissions (id, assignment_id, student_label, answer_text, photos_json, result_json, created_at, final_total, confirmed_at, idempotency_key, request_hash, state) VALUES (?, ?, ?, '', '[]', ?, ?, 1, ?, ?, ?, 'completed')",
                    (legacy_id, "aaaaaaaaaaaa", label, json.dumps(result), created_at, created_at, f"legacy-{index}", f"hash-{index}"),
                )
            conn.commit()
        finally:
            conn.close()

        history = self.get_json("/api/assignment-submissions?assignment_id=aaaaaaaaaaaa")["submissions"]
        legacy_history = [item for item in history if item["student_label"].startswith("旧作业")]
        self.assertEqual([item["student_label"] for item in legacy_history], list(reversed(labels)))
        self.assertTrue(all(item["batch_id"] is None and item["sequence"] is None for item in legacy_history))

        with urlopen(self.base_url + "/api/export.csv?assignment_id=aaaaaaaaaaaa", timeout=10) as response:
            rows = list(csv.reader(io.StringIO(response.read().decode("utf-8-sig"))))
        student_column = rows[0].index("学生")
        exported = [row[student_column] for row in rows[1:] if row[student_column].startswith("旧作业")]
        self.assertEqual(exported, labels)

    def test_schema_v4_migration_keeps_existing_submission_unsequenced(self):
        previous_paths = app.APP_PATHS
        migration_root = Path(self.temp.name) / "schema-v4-migration"
        app.configure_paths(migration_root / "portable", migration_root / "program")
        try:
            conn = app.get_db()
            try:
                conn.execute(
                    "INSERT INTO assignments (id, name, question, standard_answer, criteria_json, sub_questions_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("aaaaaaaaaaaa", "旧题", "题目", "参考答案", "[]", "[]", app.utc_now()),
                )
                legacy_result = json.dumps({"mode": "legacy", "suggested_total": 1}, ensure_ascii=False)
                legacy_created = "2024-05-06T07:08:09+00:00"
                conn.execute(
                    "INSERT INTO submissions (id, assignment_id, student_label, answer_text, photos_json, result_json, created_at) VALUES (?, ?, ?, '', '[]', ?, ?)",
                    ("abcdef123456", "aaaaaaaaaaaa", "旧作业记录", legacy_result, legacy_created),
                )
                conn.commit()
                conn.execute("DROP TABLE IF EXISTS grading_batches")
                rebuild_legacy_submissions_table(
                    conn,
                    {
                        "batch_id",
                        "batch_sequence",
                        "exam_id",
                        "marking_code_id",
                        "marking_code",
                        "answer_regions_json",
                        "marking_status",
                        "ai_privacy_confirmed",
                        "ai_privacy_reviewed_at",
                        "ai_privacy_fingerprint",
                    },
                )
                conn.execute("UPDATE schema_meta SET schema_version=4 WHERE id=1")
                conn.commit()
            finally:
                conn.close()

            migrated = app.get_db()
            try:
                row = migrated.execute(
                    "SELECT result_json, created_at, batch_id, batch_sequence FROM submissions WHERE id=?",
                    ("abcdef123456",),
                ).fetchone()
                self.assertEqual(row["result_json"], legacy_result)
                self.assertEqual(row["created_at"], legacy_created)
                self.assertIsNone(row["batch_id"])
                self.assertIsNone(row["batch_sequence"])
                index = migrated.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_submissions_batch_sequence'"
                ).fetchone()
                self.assertIsNotNone(index)
                self.assertIn("UNIQUE INDEX", index["sql"].upper())
                self.assertEqual(migrated.execute("SELECT schema_version FROM schema_meta WHERE id=1").fetchone()[0], app.DB_SCHEMA_VERSION)
            finally:
                migrated.close()
        finally:
            app._install_paths(previous_paths)
