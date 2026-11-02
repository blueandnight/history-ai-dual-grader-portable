import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app
from auto_crop_double_review_poc import AutoCropReviewGateError, prepare_accepted_answer_crops


def questions():
    return app.clean_sub_questions([
        {
            "id": "Q17.1", "label": "17.1", "question": "指出秦唐明三代丞相制度变化。",
            "standard_answer": "秦设丞相；唐三省六部；明废丞相。", "max_score": 6,
            "criteria": [
                {"id": "C1", "label": "秦设丞相", "score": 2},
                {"id": "C2", "label": "唐三省六部", "score": 2},
                {"id": "C3", "label": "明废丞相", "score": 2},
            ],
        },
        {
            "id": "Q17.2", "label": "17.2", "question": "概括雅典民主政治的基本特征。",
            "standard_answer": "主权在民；轮流执政。", "max_score": 6,
            "criteria": [
                {"id": "C1", "label": "主权在民", "score": 3},
                {"id": "C2", "label": "轮流执政", "score": 3},
            ],
        },
    ])


def accepted_manifest(root: Path) -> dict:
    (root / "padding_validation").mkdir(parents=True)
    crops = []
    padding_crops = []
    for index, question_id in enumerate(("17.1", "17.2"), start=1):
        filename = f"padding_validation/{question_id.replace('.', '_')}_crop_padded.png"
        Image.new("RGB", (120 + index, 60 + index), "white").save(root / filename)
        bbox = {"x1": 0.1, "y1": 0.2, "x2": 0.9, "y2": 0.4}
        crops.append({
            "question_id": question_id, "page_number": 3, "reference_page_hash": "r" * 64,
            "alignment_run_id": "a" * 32, "layout_profile_id": "p" * 32,
            "source_student_image_hash": "s" * 64, "configured_bbox": bbox,
            "padding": {"top": 0.01, "bottom": 0.005, "left": 0, "right": 0},
            "effective_crop_bbox": bbox, "pixel_bbox": {"x1": 1, "y1": 2, "x2": 3, "y2": 4},
        })
        padding_crops.append({"question_id": question_id, "padded_file_name": filename, "visual_status": "passed"})
    return {
        "diagnostic_run": True, "exam_id": "e" * 32, "layout_profile_id": "p" * 32,
        "source_pdf_sha256": "f" * 64, "status": "cropped", "human_review": "passed",
        "accepted_pages": [{"page_number": 3, "reference_page_sha256": "r" * 64,
                             "alignment_metrics": {"quality_gate": "passed"}}],
        "crops": crops, "padding_validation": {"status": "passed", "crops": padding_crops},
    }


class AutoCropDoubleReviewPocTests(unittest.TestCase):
    def call_prepare(self, manifest, root, **overrides):
        args = {
            "exam_id": "e" * 32, "layout_profile_id": "p" * 32,
            "source_pdf_sha256": "f" * 64, "privacy_status": "redacted",
            "sub_questions": questions(), "teacher_confirmation_status": "confirmed_by_teacher",
            "teacher_confirmed_at": "2026-10-03T16:46:04+00:00",
            "required_question_ids": ["Q17.1", "Q17.2"],
        }
        args.update(overrides)
        return prepare_accepted_answer_crops(manifest, root, **args)

    def test_only_teacher_accepted_answer_crops_are_passed_to_grading(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = accepted_manifest(root)
            prepared = self.call_prepare(manifest, root)
            self.assertEqual([Path(p).name for p in prepared["photo_paths"]], [
                "17_1_crop_padded.png", "17_2_crop_padded.png",
            ])
            self.assertEqual([r["invocation_scope"] for r in prepared["answer_regions"]], ["answer_region"] * 2)
            self.assertEqual([r["sub_question_ids"] for r in prepared["answer_regions"]], [["Q17.1"], ["Q17.2"]])
            self.assertTrue(all(len(item["crop_sha256"]) == 64 for item in prepared["crop_provenance"]))

    def test_crop_review_failure_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = accepted_manifest(root)
            manifest["human_review"] = "pending_teacher_review"
            with self.assertRaisesRegex(AutoCropReviewGateError, "auto_crop_validation_not_passed"):
                self.call_prepare(manifest, root)

    def test_unchecked_privacy_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(AutoCropReviewGateError, "privacy_status_unchecked"):
                self.call_prepare(accepted_manifest(root), root, privacy_status="unchecked")

    def test_stale_pdf_hash_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(AutoCropReviewGateError, "layout_profile_stale"):
                self.call_prepare(accepted_manifest(root), root, source_pdf_sha256="0" * 64)

    def test_unconfirmed_or_incomplete_rubric_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subs = questions()
            subs[0]["max_score"] = None
            with self.assertRaisesRegex(AutoCropReviewGateError, "rubric_not_ready"):
                self.call_prepare(accepted_manifest(root), root, sub_questions=subs)

    def test_missing_roi_crop_blocks_without_fallback_to_page_image(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = accepted_manifest(root)
            manifest["padding_validation"]["crops"][0]["visual_status"] = "failed"
            with self.assertRaisesRegex(AutoCropReviewGateError, "crop_not_accepted"):
                self.call_prepare(manifest, root)

    def test_same_score_different_point_decisions_remains_a_conflict(self):
        model_a = {"suggested_total": 2, "sub_questions": [{"sub_question_id": "Q1", "criteria": [
            {"criterion_id": "C1", "decision": "full", "suggested_score": 2, "student_evidence": "甲"},
            {"criterion_id": "C2", "decision": "none", "suggested_score": 0, "student_evidence": ""},
        ]}]}
        model_b = {"suggested_total": 2, "sub_questions": [{"sub_question_id": "Q1", "criteria": [
            {"criterion_id": "C1", "decision": "none", "suggested_score": 0, "student_evidence": ""},
            {"criterion_id": "C2", "decision": "full", "suggested_score": 2, "student_evidence": "乙"},
        ]}]}
        comparison = app.compare_dual_review_results(model_a, model_b)
        self.assertEqual(comparison["risk_level"], "HIGH")
        self.assertIn("same_score_different_rubric", comparison["review_reasons"])

    def test_dual_grade_diagnostic_flag_is_forwarded_and_never_sets_teacher_final(self):
        questions_ = questions()
        region = {"invocation_scope": "answer_region", "sub_question_ids": ["Q17.1"]}
        seen = []

        def side_result(assignment, subs, photos, meta, config, regions, diagnostics, *, diagnostic_run=False, run_id):
            seen.append(diagnostic_run)
            result = {
                "mode": "vision-model", "suggested_total": 0, "max_total": 6,
                "confidence": 0.3, "needs_review": True, "blocked": False,
                "sub_questions": [{"sub_question_id": subs[0]["id"], "answer_status": "answered", "suggested_total": 0,
                                   "criteria": [{"criterion_id": "C1", "decision": "none", "suggested_score": 0,
                                                 "student_evidence": "", "evidence": None, "reason": "", "needs_review": True}]}],
            }
            diagnostics.update({"provider": config["provider"], "model_id": config["model"], "input_hash": "same"})
            return result

        configs = {
            "A": {"provider": "deepseek", "model": "deepseek-flash"},
            "B": {"provider": "mimo", "model": "mimo-v2.6-flash"},
        }
        with patch.object(app, "load_dual_review_model_configs", return_value=configs), patch.object(
            app, "_grade_dual_answer_regions_for_side", side_effect=side_result
        ):
            result = app.grade_submission_dual(
                {"id": "a" * 12, "question": "q"}, questions_[:1], "", [Path("crop.png")], [{"ai_redactions": []}],
                True, [region], True, None, diagnostic_run=True,
            )
        self.assertEqual(sorted(seen), [True, True])
        self.assertTrue(result["needs_review"])
        self.assertIsNone(result["dual_review"]["teacher_final"])
        self.assertEqual(result["dual_review"]["invocation_scope"], "answer_region")

    def test_unconfirmed_dual_review_is_marked_for_history_resume(self):
        row = {
            "id": "a" * 12, "assignment_id": "b" * 12, "exam_id": "e" * 32,
            "marking_code_id": None, "marking_code": "", "marking_status": "",
            "batch_id": None, "batch_sequence": None, "state": "completed",
            "source_channel": "computer", "class_name": "", "student_label": "诊断样本",
            "student_no": "", "student_name": "", "identity_status": "anonymous",
            "identity_method": "", "photos_json": "[]",
            "result_json": json.dumps({"mode": "vision-model", "suggested_total": 3,
                                        "max_total": 12, "dual_review": {"status": "complete"}}),
            "privacy_status": "redacted", "final_total": None, "confirmed_at": None,
            "manual_reviewed": 0, "review_note": "", "uncertain_model_call": 0,
        }
        listed = app.submission_list_public(row)
        self.assertTrue(listed["has_dual_review"])
        self.assertIsNone(listed["final_total"])


if __name__ == "__main__":
    unittest.main()
