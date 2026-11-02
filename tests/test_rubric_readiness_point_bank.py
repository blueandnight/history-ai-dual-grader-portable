import copy
import unittest

import app
from auto_crop_double_review_poc import rubric_readiness
from regression_rubric_fixture import confirm_frozen_rubric


def question(count=8, maximum=8, mode="point_bank"):
    return {"id": "Q2", "label": "Q2", "question": "合成设问", "standard_answer": "合成参考答案",
            "scoring_mode": mode, "point_value": 2, "max_score": maximum,
            "criteria": [{"id": f"C{i}", "label": f"合成要点{i}", "score": 2} for i in range(1, count+1)]}


def ready(q, confirmed=True):
    return rubric_readiness([q], teacher_confirmation_status="confirmed_by_teacher" if confirmed else "",
                            teacher_confirmed_at="2026-10-15T00:00:00Z" if confirmed else None,
                            required_question_ids=[q["id"]])


class PointBankReadinessTests(unittest.TestCase):
    def test_equal_sum_passes(self):
        self.assertEqual(ready(question(4)), (True, []))

    def test_larger_capped_bank_passes(self):
        self.assertEqual(ready(question(5, 6)), (True, []))

    def test_q2_eight_two_point_candidates_passes(self):
        self.assertEqual(ready(question()), (True, []))

    def test_every_hit_combination_is_still_capped_at_eight(self):
        q=question()
        for mask in range(256):
            incoming={"sub_questions": [{"sub_question_id": "Q2", "criteria": [
                {"criterion_id": p["id"], "decision": "full" if mask & (1 << i) else "none",
                 "suggested_score": 2 if mask & (1 << i) else 0,
                 "student_evidence": f"独立学生原文{i}" if mask & (1 << i) else "",
                 "evidence": {"photo_index": 1, "x": .1, "y": .1, "width": .1, "height": .1} if mask & (1 << i) else None}
                for i,p in enumerate(q["criteria"])]}]}
            result=app.normalize_sub_question_result(incoming, [q])
            self.assertEqual(result["suggested_total"], min(mask.bit_count()*2, 8))
            self.assertLessEqual(result["sub_questions"][0]["suggested_total"], 8)

    def test_undersized_bank_still_blocks(self):
        self.assertIn("rubric_sum_mismatch:Q2", ready(question(3))[1])

    def test_weighted_oversized_rubric_still_blocks(self):
        self.assertIn("rubric_sum_mismatch:Q2", ready(question(mode="weighted"))[1])

    def test_production_missing_confirmation_still_blocks(self):
        self.assertIn("teacher_confirmation_missing", ready(question(), False)[1])
        with self.assertRaisesRegex(ValueError, "teacher_confirmation_missing"):
            app._require_answer_region_rubric_ready({}, [question()], ["Q2"])

    def test_isolated_fixture_uses_existing_confirmation_and_keeps_rubric(self):
        q=question();old=copy.deepcopy(q)
        confirmed,_=confirm_frozen_rubric([q],title="合成回归",teacher_confirmed=True,confirmed_at="2026-10-15T00:00:00Z")
        self.assertEqual(q,old)
        app._require_answer_region_rubric_ready(confirmed,[q],["Q2"])
        self.assertTrue(confirmed["regression_fixture_only"])

    def test_fixture_requires_explicit_confirmation(self):
        with self.assertRaises(ValueError):
            confirm_frozen_rubric([question()],title="合成回归",teacher_confirmed=False,confirmed_at="2026-10-15T00:00:00Z")

    def test_invalid_point_values_still_block(self):
        for invalid in [0, -2, float("nan"), float("inf"), "bad"]:
            q=question();q["criteria"][0]["score"]=invalid
            self.assertFalse(ready(q)[0])

    def test_formal_missing_layout_still_blocks(self):
        with self.assertRaises(ValueError):
            app._require_formal_answer_regions({"answer_regions_required": True, "class_id": "class",
                "exam_id": "exam", "layout_profile": None}, [question()], [], {})


if __name__ == "__main__":
    unittest.main()
