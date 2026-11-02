"""Teacher-final content-version gate regression tests.

These tests use the accepted three-stage synthetic fixture and only isolated
temporary databases.  They exercise the real main HTTP `/api/confirm` handler,
not the calculator directly.
"""
from __future__ import annotations

import copy
import json
import unittest
from contextlib import closing

import app
from tests import test_answer_links as links
from tests.test_unified_scores import seed_answers, seed_final


class ConfirmContentVersionTests(unittest.TestCase):
    def setUp(self):
        self.f = links.AnswerLinkTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.client = self.f.client
        with closing(app.get_db()) as conn, conn:
            for objective_id, subjective_id in zip(self.f.obj, self.f.sub):
                seed_answers(conn, objective_id)
                seed_final(conn, subjective_id)
        self.link = self.f.write().get_json()["link"]
        self.exam = self.f.exam
        self.assignment = self.f.assignment
        self.submission = self.f.sub[0]
        with closing(app.get_db()) as conn:
            row = conn.execute(
                "SELECT question, standard_answer, sub_questions_json FROM assignments WHERE id=?",
                (self.assignment,),
            ).fetchone()
        self.assignment_question = row["question"]
        self.assignment_answer = row["standard_answer"]
        self.original_sub_questions_json = row["sub_questions_json"]
        self.questions = json.loads(row["sub_questions_json"])

    def test_explicit_exam_binding_rejects_other_exam_without_mutation(self):
        before = self._db_state()
        status, body = self.f.fixture.f.request_json_status('/api/confirm', {
            'submission_id': self.submission, 'exam_id': 'f' * 32,
            'teacher_point_decisions': self._decisions(),
        })
        self.assertEqual(status, 400)
        self.assertEqual(before, self._db_state())

    def _decisions(self, full=True):
        return [
            {
                "question_id": question["id"],
                "rubric_point_id": point["id"],
                "choice": "teacher_full" if full else "teacher_none",
            }
            for question in self.questions
            for point in question["criteria"]
        ]

    def _confirm(self, decisions=None):
        return self.f.fixture.f.request_json_status(
            "/api/confirm",
            {
                "submission_id": self.submission,
                "teacher_point_decisions": decisions if decisions is not None else self._decisions(),
            },
        )

    def _reset_confirmation(self):
        with closing(app.get_db()) as conn, conn:
            conn.execute(
                "UPDATE submissions SET confirmed_at=NULL, final_total=NULL WHERE id=?",
                (self.submission,),
            )

    def _set_questions(self, questions):
        with closing(app.get_db()) as conn, conn:
            conn.execute(
                "UPDATE assignments SET sub_questions_json=? WHERE id=?",
                (json.dumps(questions, ensure_ascii=False), self.assignment),
            )

    def _set_assignment(self, question=None, answer=None):
        with closing(app.get_db()) as conn, conn:
            conn.execute(
                "UPDATE assignments SET question=?, standard_answer=? WHERE id=?",
                (
                    self.assignment_question if question is None else question,
                    self.assignment_answer if answer is None else answer,
                    self.assignment,
                ),
            )

    def _restore_content(self):
        with closing(app.get_db()) as conn, conn:
            conn.execute(
                "UPDATE assignments SET question=?, standard_answer=?, sub_questions_json=? WHERE id=?",
                (
                    self.assignment_question,
                    self.assignment_answer,
                    self.original_sub_questions_json,
                    self.assignment,
                ),
            )

    def _db_state(self):
        with closing(app.get_db()) as conn:
            submission = dict(
                conn.execute(
                    "SELECT final_total, confirmed_at, result_json FROM submissions WHERE id=?",
                    (self.submission,),
                ).fetchone()
            )
            links = [tuple(row) for row in conn.execute("SELECT * FROM exam_answer_links ORDER BY rowid")]
            objectives = [tuple(row) for row in conn.execute("SELECT * FROM objective_submissions ORDER BY rowid")]
            audits = [tuple(row) for row in conn.execute("SELECT * FROM audit_events ORDER BY id")]
        return {"submission": submission, "links": links, "objectives": objectives, "audits": audits}

    def _unified_row(self):
        response = self.client.get("/api/unified-scores?exam_id=" + self.exam)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return next(
            row
            for row in response.get_json()["rows"]
            if row.get("subjective") and row["subjective"].get("id") == self.submission
        )

    def _change(self, kind):
        if kind == "question":
            questions = copy.deepcopy(self.questions)
            questions[0]["question"] = str(questions[0].get("question") or "") + "（终评时不同题干）"
            self._set_questions(questions)
        elif kind == "material":
            self._set_assignment(question=str(self.assignment_question) + "\n（终评时不同材料上下文）")
        elif kind == "answer":
            questions = copy.deepcopy(self.questions)
            questions[0]["standard_answer"] = str(questions[0].get("standard_answer") or "") + "（终评时不同参考答案）"
            self._set_questions(questions)
            self._set_assignment(answer=str(self.assignment_answer) + "（终评时不同参考答案）")
        elif kind == "rubric_text":
            questions = copy.deepcopy(self.questions)
            questions[0]["criteria"][0]["label"] = str(questions[0]["criteria"][0].get("label") or "") + "（终评时不同评分点）"
            self._set_questions(questions)
        elif kind == "distribution":
            questions = copy.deepcopy(self.questions)
            criteria = questions[0]["criteria"]
            self.assertGreaterEqual(len(criteria), 2)
            total = sum(float(point["score"]) for point in criteria)
            first = float(criteria[0]["score"])
            criteria[0]["score"] = 1.0 if first != 1.0 else 2.0
            criteria[1]["score"] = total - criteria[0]["score"]
            self.assertAlmostEqual(sum(float(point["score"]) for point in criteria), total)
            self._set_questions(questions)
        else:
            raise AssertionError(kind)

    def test_unchanged_content_teacher_final_succeeds(self):
        self._reset_confirmation()
        status, body = self._confirm()
        self.assertEqual(status, 200, body)
        row = self._unified_row()
        self.assertTrue(row["complete"])
        self.assertEqual(row["total_score"], 68)
        self.assertEqual(row["subjective"]["score"], 8)
        with closing(app.get_db()) as conn:
            saved = conn.execute(
                "SELECT final_total, confirmed_at FROM submissions WHERE id=?", (self.submission,)
            ).fetchone()
        self.assertEqual(saved["final_total"], 8)
        self.assertTrue(saved["confirmed_at"])

    def test_each_content_change_is_rejected_and_restore_can_confirm(self):
        for kind in ("question", "material", "answer", "rubric_text", "distribution"):
            with self.subTest(kind=kind):
                self._reset_confirmation()
                before = self._db_state()
                self._change(kind)
                status, body = self._confirm()
                self.assertEqual(status, 400, body)
                self.assertIn("version", json.dumps(body, ensure_ascii=False))
                self.assertEqual(self._db_state(), before)
                self._restore_content()
        # A/A after restoring content must remain confirmable.
        status, body = self._confirm()
        self.assertEqual(status, 200, body)
        row = self._unified_row()
        self.assertTrue(row["complete"])
        self.assertEqual(row["total_score"], 68)

    def test_rejected_confirm_does_not_change_final_or_links(self):
        self._reset_confirmation()
        before = self._db_state()
        self._change("rubric_text")
        status, body = self._confirm()
        self.assertEqual(status, 400, body)
        after = self._db_state()
        self.assertEqual(after, before)
        self.assertIsNone(after["submission"]["final_total"])
        self.assertIsNone(after["submission"]["confirmed_at"])
        self.assertEqual(len([row for row in after["links"] if row[8] == 1]), 1)
        self._restore_content()

    def test_a_b_confirm_a_path_cannot_show_complete_total(self):
        self._reset_confirmation()
        # B: teacher final must be rejected.
        self._change("rubric_text")
        status, body = self._confirm()
        self.assertEqual(status, 400, body)
        # Recover A and confirm normally.
        self._restore_content()
        status, body = self._confirm()
        self.assertEqual(status, 200, body)
        self.assertEqual(self._unified_row()["total_score"], 68)
        # Change B again: unified-score version gate must still block.
        self._change("rubric_text")
        row = self._unified_row()
        self.assertFalse(row["complete"])
        self.assertIsNone(row["total_score"])
        self.assertEqual(row["subjective"]["version_status"], "subjective_content_version_mismatch")
        self._restore_content()
        self.assertEqual(self._unified_row()["total_score"], 68)

    def test_corrupt_present_evidence_is_rejected(self):
        self._reset_confirmation()
        with closing(app.get_db()) as conn, conn:
            result = json.loads(
                conn.execute("SELECT result_json FROM submissions WHERE id=?", (self.submission,)).fetchone()[0]
            )
            result["subjective_version_evidence"]["content_hash"] = "0" * 64
            conn.execute(
                "UPDATE submissions SET result_json=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), self.submission),
            )
        before = self._db_state()
        status, body = self._confirm()
        self.assertEqual(status, 400, body)
        self.assertEqual(self._db_state(), before)
        self.assertIsNone(self._db_state()["submission"]["final_total"])

    def test_changed_actual_scope_is_rejected(self):
        self._reset_confirmation()
        with closing(app.get_db()) as conn, conn:
            result = json.loads(
                conn.execute("SELECT result_json FROM submissions WHERE id=?", (self.submission,)).fetchone()[0]
            )
            original_result_json = json.dumps(result, ensure_ascii=False)
            result["grading_scope"] = {
                "graded_question_ids": [self.questions[0]["id"]],
                "scoped_max_score": self.questions[0]["max_score"],
                "source": "explicit",
            }
            conn.execute(
                "UPDATE submissions SET result_json=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), self.submission),
            )
        before = self._db_state()
        status, body = self._confirm()
        self.assertEqual(status, 400, body)
        self.assertEqual(self._db_state(), before)
        with closing(app.get_db()) as conn, conn:
            conn.execute(
                "UPDATE submissions SET result_json=? WHERE id=?",
                (original_result_json, self.submission),
            )
        status, body = self._confirm()
        self.assertEqual(status, 200, body)

    def test_missing_legacy_evidence_preserves_legacy_confirm_boundary(self):
        self._reset_confirmation()
        with closing(app.get_db()) as conn, conn:
            result = json.loads(
                conn.execute("SELECT result_json FROM submissions WHERE id=?", (self.submission,)).fetchone()[0]
            )
            result.pop("subjective_version_evidence", None)
            conn.execute(
                "UPDATE submissions SET result_json=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), self.submission),
            )
        status, body = self._confirm()
        self.assertEqual(status, 200, body)
        saved = self._unified_row()
        self.assertFalse(saved["complete"])
        self.assertIsNone(saved["total_score"])
        self.assertEqual(saved["subjective"]["version_status"], "subjective_version_unverifiable")
        with closing(app.get_db()) as conn:
            persisted = json.loads(
                conn.execute("SELECT result_json FROM submissions WHERE id=?", (self.submission,)).fetchone()[0]
            )
        # Historical evidence must not be synthesized by the confirmation path.
        self.assertNotIn("subjective_version_evidence", persisted)

    def test_zero_and_partial_teacher_finals_keep_existing_rules(self):
        self._reset_confirmation()
        status, body = self._confirm(self._decisions(full=False))
        self.assertEqual(status, 200, body)
        zero = self._unified_row()
        self.assertTrue(zero["complete"])
        self.assertEqual(zero["subjective"]["score"], 0)
        self.assertEqual(zero["total_score"], 60)

        self._reset_confirmation()
        first_question = self.questions[0]["id"]
        decisions = [
            {
                "question_id": question["id"],
                "rubric_point_id": point["id"],
                "choice": "teacher_full" if question["id"] == first_question else "teacher_none",
            }
            for question in self.questions
            for point in question["criteria"]
        ]
        status, body = self._confirm(decisions)
        self.assertEqual(status, 200, body)
        partial = self._unified_row()
        self.assertTrue(partial["complete"])
        self.assertEqual(partial["subjective"]["score"], 4)
        self.assertEqual(partial["total_score"], 64)


if __name__ == "__main__":
    unittest.main()
