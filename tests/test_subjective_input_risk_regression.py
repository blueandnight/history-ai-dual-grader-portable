import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image

import app
from layout_profile import crop_subjective_regions_from_aligned_page
from student_auto_crop_service import run_student_auto_crop


def review(decision="full", score=2, evidence="学生原文", echo=False):
    return {"suggested_total": score, "sub_questions": [{"sub_question_id": "Q1", "criteria": [
        {"criterion_id": "C1", "decision": decision, "suggested_score": score,
         "student_evidence": evidence, "rubric_echo_suspected": echo},
    ]}]}


class SubjectiveInputRiskRegressionTests(unittest.TestCase):
    def context(self):
        bbox = {"x1": 0.1, "y1": 0.6, "x2": 0.9, "y2": 0.9}
        roi = {"region_id": "subjective-Q1", "region_type": "subjective_answer",
               "question_id": "Q1", "page_number": 1, "bbox": bbox}
        ctx = {"answer_regions_required": True, "exam_id": "e" * 32, "layout_profile_id": "p" * 32,
               "source_pdf_sha256": "f" * 64, "layout_profile": {"regions": [roi]},
               "references": [{"page_number": 1, "reference_sha256": "r" * 64}]}
        provenance = {"exam_id": ctx["exam_id"], "layout_profile_id": ctx["layout_profile_id"],
                      "question_id": "Q1", "page_number": 1, "configured_bbox": bbox,
                      "effective_crop_bbox": bbox, "source_pdf_sha256": ctx["source_pdf_sha256"],
                      "reference_page_sha256": "r" * 64, "source_student_image_sha256": "s" * 64,
                      "alignment_run_id": "a" * 32, "alignment_status": "aligned",
                      "quality_gate": "passed", "crop_status": "passed"}
        region = {"region_id": "accepted-Q1", "profile_id": ctx["layout_profile_id"], "photo_index": 1,
                  "x": 0, "y": 0, "width": 1, "height": 1, "rotation_ccw": 0,
                  "coordinate_system": "perspective_corrected_page_normalized",
                  "sub_question_ids": ["Q1"], "invocation_scope": "answer_region"}
        return ctx, roi, provenance, region

    def questions(self):
        return app.clean_sub_questions([{"id": "Q1", "label": "17.1", "question": "概括措施",
            "standard_answer": "合成措施", "criteria": [{"id": "C1", "label": "合成措施", "score": 2}]}])

    def test_formal_provider_receives_existing_roi_crop_not_whole_page(self):
        ctx, roi, provenance, region = self.context()
        questions = self.questions()
        app._require_formal_answer_regions(ctx, questions, [region], {1: provenance})
        # The existing shared crop function is used on an aligned synthetic page.
        page = np.full((400, 300, 3), 255, dtype=np.uint8)
        page[:150] = [255, 0, 0]  # Synthetic printed area outside the configured answer ROI.
        crop_bytes = crop_subjective_regions_from_aligned_page(page, [roi])[0]["image_bytes"]
        crop = np.array(Image.open(io.BytesIO(crop_bytes)))
        self.assertFalse((crop[:, :, 0] != crop[:, :, 1]).any())
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "crop.jpg"
            Image.fromarray(crop).save(path)
            prepared = app.prepare_model_image(path)
            region["approved_model_sha256"] = hashlib.sha256(app.crop_model_image_bytes(prepared, region)).hexdigest()
            region["approved_prompt_sha256"] = hashlib.sha256(app.build_vision_prompt_text(
                questions[0]["question"], questions, privacy_mode=True, answer_regions=[region]).encode()).hexdigest()
            photos = [{"image_role": "answer_region", "answer_region_provenance": provenance,
                       "approved_ai_page_sha256": hashlib.sha256(prepared).hexdigest(), "ai_redactions": []}]
            response = json.dumps({"sub_questions": [{"sub_question_id": "Q1", "answer_status": "answered",
                "criteria": [{"criterion_id": "C1", "decision": "none", "suggested_score": 0,
                              "student_evidence": "", "needs_review": False}]}]})
            captured = []
            cm = MagicMock(); cm.__enter__.return_value = object()
            def capture(req, timeout):
                captured.append(json.loads(req.data)); return cm
            diagnostics = {}
            with patch.object(app, "open_model_request", side_effect=capture), patch.object(
                app, "read_model_chat_content", return_value=response), patch.object(
                app, "preflight_private_model_diagnostic_dir", return_value=None), patch.object(
                app, "cloud_mode_enabled", return_value=False):
                app._grade_dual_answer_regions_for_side({"id": "synthetic", "question": "whole paper"},
                    questions, [path], photos,
                    {"provider": "deepseek", "model": "deepseek-flash", "base_url": "https://api.deepseek.com/v1",
                     "api_key": "SYNTHETIC_KEY"}, [region], diagnostics, run_id="synthetic")
            self.assertEqual(len(captured), 1)
            content = captured[0]["messages"][1]["content"]
            with Image.open(io.BytesIO(base64.b64decode(content[1]["image_url"]["url"].split(',')[1]))) as image:
                self.assertEqual(image.size, (240, 120))
            serialized = json.dumps(captured)
            for forbidden in ["student_id", "identity_code", "display_name", "roster", "whole paper"]:
                self.assertNotIn(forbidden, serialized)
            request = diagnostics["question_runs"][0]["request_metadata"]
            self.assertEqual(request["input_mode"], "answer_region")
            self.assertEqual(request["crop_provenance"][0]["alignment_run_id"], "a" * 32)

    def test_formal_missing_roi_page_alignment_or_provenance_fails_closed(self):
        ctx, roi, provenance, region = self.context()
        variants = []
        for field, value in [("layout_profile", None), ("profile_hash_mismatch", True)]:
            mutated = copy.deepcopy(ctx); mutated[field] = value; variants.append((mutated, [region], {1: provenance}))
        mutated = copy.deepcopy(ctx); mutated["layout_profile"]["regions"] = []; variants.append((mutated, [region], {1: provenance}))
        mutated = copy.deepcopy(ctx); mutated["references"] = []; variants.append((mutated, [region], {1: provenance}))
        variants += [(ctx, [], {}), (ctx, [{**region, "invocation_scope": "whole_paper"}], {})]
        for field, value in [("alignment_status", "failed"), ("quality_gate", "failed"),
                             ("crop_status", "failed"), ("layout_profile_id", "stale")]:
            variants.append((ctx, [region], {1: {**provenance, field: value}}))
        with patch.object(app, "open_model_request") as network:
            for context, regions, verified in variants:
                with self.subTest(context=context, verified=verified), self.assertRaisesRegex(ValueError, "answer_region_unavailable"):
                    app._require_formal_answer_regions(context, self.questions(), regions, verified)
            network.assert_not_called()

    def test_frozen_images_without_profile_cannot_be_auto_cropped(self):
        result = run_student_auto_crop(exam_context={"exam_id": "e" * 32, "layout_profile": None},
            student_image_bytes=b"not-sent", input_mode="single_page", reference_loader=lambda _: None,
            output_dir="unused")
        self.assertEqual(result["status"], "layout_profile_missing")
        self.assertEqual(result["crops"], [])

    def test_quick_anonymous_without_formal_layout_keeps_legacy_path(self):
        app._require_formal_answer_regions(None, self.questions(), [], {})
        app._require_formal_answer_regions({"answer_regions_required": False}, self.questions(), [], {})
        with patch.object(app, "grade_submission_dual", return_value={"diagnostics": {}}) as dual:
            app.grade_submission({}, self.questions(), "", [], answer_regions=[], dual_review_enabled=True)
        self.assertEqual(dual.call_args.args[6], [])

    def test_queue_blocks_missing_layout_and_tampered_crop_before_provider(self):
        ctx, _, provenance, region = self.context()
        for tampered in [False, True]:
            with self.subTest(tampered=tampered), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); data = root / "data"; data.mkdir()
                path = data / "crop.jpg"; Image.new("RGB", (240, 120), "white").save(path)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                bad = {**provenance, "crop_sha256": "0" * 64, "crop_bytes": path.stat().st_size}
                photo = {"path": "data/crop.jpg", "original_path": "data/crop.jpg", "page_sequence": 1,
                         "original_sha256": digest, "sha256": digest, "original_content_type": "image/jpeg",
                         "image_role": "answer_region" if tampered else "source_page", "answer_region_provenance": bad}
                with patch.multiple(app, PORTABLE_ROOT=root, DATA_DIR=data, DB_PATH=data / "test.sqlite3", BACKUP_DIR=root / "backups"), patch.object(
                    app, "_exam_layout_profile_context", return_value=ctx if tampered else {"answer_regions_required": True}), patch.object(
                    app, "grade_submission") as grade, patch.object(app, "open_model_request") as network:
                    conn = app.get_db()
                    conn.execute("INSERT INTO assignments(id,name,question,standard_answer,criteria_json,sub_questions_json,created_at) VALUES(?,?,?,?,?,?,?)",
                                 ("a" * 12, "synthetic", "合成", "合成", "[]", json.dumps(self.questions()), app.utc_now()))
                    conn.execute("INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state,answer_regions_json) VALUES(?,?,?,?,?,?,?,?,?)",
                                 ("s" * 12, "a" * 12, "synthetic", "", json.dumps([photo]), "{}", app.utc_now(), "queued", json.dumps([region] if tampered else [])))
                    conn.commit(); conn.close()
                    app.process_submission("s" * 12)
                    conn = app.get_db()
                    saved = json.loads(conn.execute("SELECT result_json FROM submissions WHERE id=?", ("s" * 12,)).fetchone()[0]); conn.close()
                    self.assertTrue(saved["blocked"])
                    self.assertEqual(saved["diagnostics"]["error_category"], "answer_region_unavailable")
                    grade.assert_not_called(); network.assert_not_called()

    def test_answer_region_does_not_bypass_privacy_gate(self):
        with patch.object(app, "open_model_request") as network, self.assertRaisesRegex(ValueError, "隐私"):
            app.call_vision_model({"question": "合成"}, self.questions(), "", [Path("not-read.jpg")],
                                  photo_meta=[{}], privacy_confirmed=False, anonymous_mode=True,
                                  answer_regions=[self.context()[3]], invocation_scope="answer_region")
        network.assert_not_called()

    def test_prompt_only_adds_source_and_no_completion_rules_schema_unchanged(self):
        prompt = app.build_vision_prompt_text("合成材料", self.questions())
        self.assertIn("来源归属约束", prompt)
        self.assertIn("禁止语义补足", prompt)
        self.assertIn("学生确实手写抄录", prompt)
        self.assertNotIn("东汉", prompt)
        self.assertNotIn("取消支郡", prompt)
        self.assertIn("decision 只能填写 full、partial、none 或 uncertain", prompt)
        self.assertIn("confidence、summary、sub_questions、unmapped_answers", prompt)

    def test_missing_evidence_only_for_credit_claims(self):
        for decision, score in [("full", 2), ("partial", 1), ("hit", 0), ("full", 0), ("none", 2)]:
            with self.subTest(decision=decision, score=score):
                comparison = app.compare_dual_review_results(review(decision, score, ""), review(decision, score, ""))
                self.assertIn("evidence_missing", comparison["review_reasons"])
        for decision in ["none", "not_hit"]:
            comparison = app.compare_dual_review_results(review(decision, 0, ""), review(decision, 0, ""))
            self.assertNotIn("evidence_missing", comparison["review_reasons"])
            self.assertEqual(comparison["risk_level"], "NO_CONFLICT")

    def test_echo_is_hint_real_conflict_still_high(self):
        a = review(echo=True)
        comparison = app.compare_dual_review_results(a, copy.deepcopy(a))
        self.assertEqual(comparison["review_reasons"], ["rubric_echo_suspected"])
        self.assertNotEqual(comparison["risk_level"], "HIGH")
        comparison = app.compare_dual_review_results(a, review("none", 0, ""))
        self.assertEqual(comparison["risk_level"], "HIGH")
        self.assertIn("score_disagreement", comparison["review_reasons"])


if __name__ == "__main__":
    unittest.main()
