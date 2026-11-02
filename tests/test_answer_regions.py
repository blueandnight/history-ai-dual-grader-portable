import base64
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

import app
from answer_region_profiles import Q9_FIXED_RUBRIC_SHA256, answer_region_profile


def one_question():
    return app.clean_sub_questions([{
        "id": "Q1",
        "label": "17.1",
        "question": "只说明秦唐明三代丞相制度变化。",
        "standard_answer": "秦设丞相；唐实行三省六部制；明废丞相。",
        "criteria": [{"id": "C1", "label": "秦朝设丞相，辅助皇帝处理全国政务", "score": 2}],
    }])


def region_for_question():
    return {
        "region_id": "test-q1",
        "profile_id": "test-profile",
        "photo_index": 1,
        "x": 0.2,
        "y": 0.3,
        "width": 0.5,
        "height": 0.4,
        "sub_question_ids": ["Q1"],
        "coordinate_system": "ccw_rotated_page_normalized",
        "rotation_ccw": 90,
        "invocation_scope": "answer_region",
    }


class AnswerRegionTests(unittest.TestCase):
    def test_fixed_profile_is_only_enabled_for_exact_rubric_and_one_page(self):
        profile = answer_region_profile(Q9_FIXED_RUBRIC_SHA256, 1)
        self.assertIsNotNone(profile)
        self.assertEqual(profile["page_count"], 1)
        self.assertTrue(all(item["invocation_scope"] == "answer_region" for item in profile["regions"]))
        self.assertIsNone(answer_region_profile("0" * 64, 1))
        self.assertIsNone(answer_region_profile(Q9_FIXED_RUBRIC_SHA256, 2))

    def test_crop_rotation_normalized_geometry_and_hash_are_stable_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Image.new("RGB", (300, 400), "white")
            draw = ImageDraw.Draw(source)
            draw.rectangle((80, 100, 220, 260), fill="red")
            path = Path(temp) / "page.jpg"
            source.save(path, format="JPEG", quality=94)
            original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            model_page = app.prepare_model_image(path)
            region = region_for_question()
            first = app.crop_model_image_bytes(model_page, region)
            second = app.crop_model_image_bytes(model_page, region)
            self.assertEqual(first, second)
            self.assertEqual(hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest())
            with Image.open(io.BytesIO(first)) as crop:
                # CCW rotation makes a 400x300 coordinate page before cropping.
                self.assertEqual(crop.size, (200, 120))
            self.assertEqual(original_hash, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_crop_evidence_maps_back_through_rotation(self):
        result = {"sub_questions": [{"criteria": [{
            "evidence": {"photo_index": 1, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}
        }]}], "unmapped_answers": []}
        region = {
            "region_id": "r1", "photo_index": 1, "x": 0.25, "y": 0.25,
            "width": 0.5, "height": 0.5, "rotation_ccw": 90,
        }
        app.map_crop_evidence_to_source(result, [region])
        evidence = result["sub_questions"][0]["criteria"][0]["evidence"]
        self.assertAlmostEqual(evidence["x"], 0.45)
        self.assertAlmostEqual(evidence["y"], 0.30)
        self.assertAlmostEqual(evidence["width"], 0.20)
        self.assertAlmostEqual(evidence["height"], 0.15)
        self.assertEqual(evidence["answer_region_id"], "r1")

    def test_rubric_echo_is_a_review_hint_not_an_automatic_score_change(self):
        criteria = [{"id": "C1", "label": "秦朝设丞相，辅助皇帝处理全国政务", "score": 2}]
        normalized = app.normalize_model_result({
            "criteria": [{
                "criterion_id": "C1", "decision": "full", "suggested_score": 2,
                "student_evidence": "秦朝设丞相，辅助皇帝处理全国政务",
                "evidence": {"photo_index": 1, "x": 0.1, "y": 0.1, "width": 0.3, "height": 0.1},
            }]
        }, criteria, max_total=2, scoring_mode="weighted")
        point = normalized["criteria"][0]
        self.assertTrue(point["rubric_echo_suspected"])
        self.assertEqual(point["review_reasons"], ["rubric_echo_suspected"])
        self.assertEqual(point["suggested_score"], 2)
        self.assertFalse(normalized["blocked"])
        self.assertFalse(app.evidence_rubric_echo_suspected("秦朝设丞相负责处理政务", criteria[0]))

    def test_deepseek_and_mimo_receive_same_question_crop_and_canonical_input(self):
        questions = one_question()
        region = region_for_question()
        with tempfile.TemporaryDirectory() as temp:
            image_path = Path(temp) / "page.jpg"
            Image.new("RGB", (300, 400), "white").save(image_path, format="JPEG")
            page_bytes = app.prepare_model_image(image_path)
            region["approved_model_sha256"] = hashlib.sha256(
                app.crop_model_image_bytes(page_bytes, region)
            ).hexdigest()
            region["approved_prompt_sha256"] = hashlib.sha256(app.build_vision_prompt_text(
                questions[0]["question"], questions, privacy_mode=True, answer_regions=[region]
            ).encode("utf-8")).hexdigest()
            photo_meta = [{
                "ai_redactions": [],
                "approved_ai_page_sha256": hashlib.sha256(page_bytes).hexdigest(),
            }]
            response = json.dumps({"sub_questions": [{
                "sub_question_id": "Q1",
                "answer_status": "answered",
                "criteria": [{
                    "criterion_id": "C1", "decision": "full", "suggested_score": 2,
                    "student_evidence": "秦朝设丞相",
                    "evidence": {"photo_index": 1, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2},
                }],
            }]})
            context = MagicMock()
            context.__enter__.return_value = object()
            seen_bodies = []

            def capture_request(request, timeout):
                seen_bodies.append(json.loads(request.data.decode("utf-8")))
                return context

            outputs = {}
            for side, provider, model, base_url in (
                ("A", "deepseek", "deepseek-flash", "https://api.deepseek.com/v1"),
                ("B", "mimo", "mimo-v2.6-flash", "https://api.xiaomimimo.com/v1"),
            ):
                diagnostics = {}
                with patch.object(app, "open_model_request", side_effect=capture_request), patch.object(
                    app, "read_model_chat_content", return_value=response
                ), patch.object(app, "preflight_private_model_diagnostic_dir", return_value=None), patch.object(
                    app, "cloud_mode_enabled", return_value=False
                ):
                    result = app._grade_dual_answer_regions_for_side(
                        {"id": "synthetic", "question": "whole paper"}, questions, [image_path],
                        photo_meta, {"provider": provider, "model": model, "base_url": base_url,
                                     "api_key": "SYNTHETIC_KEY"}, [region], diagnostics,
                        diagnostic_run=True, run_id=f"test-{side}",
                    )
                outputs[side] = (result, diagnostics)

            self.assertEqual(
                len(seen_bodies), 2,
                msg=f"mocked per-question calls failed: {[(side, item[1].get('question_errors'), item[0].get('block_reasons')) for side, item in outputs.items()]}",
            )
            self.assertEqual(len({
                body["messages"][1]["content"][1]["image_url"]["url"] for body in seen_bodies
            }), 1)
            self.assertEqual(
                outputs["A"][1]["question_runs"][0]["request_crop_hash"],
                outputs["B"][1]["question_runs"][0]["request_crop_hash"],
            )
            self.assertEqual(outputs["A"][1]["input_hash"], outputs["B"][1]["input_hash"])
            self.assertEqual(outputs["A"][1]["question_runs"][0]["request_metadata"]["invocation_scope"], "answer_region")
            self.assertTrue(outputs["A"][1]["diagnostic_run"])
            self.assertEqual(outputs["A"][0]["suggested_total"], outputs["B"][0]["suggested_total"])

    def test_teacher_point_resolution_and_completeness_support_legacy_total_only(self):
        sub_questions = [{"id": "Q1", "criteria": [{"id": "C1"}, {"id": "C2"}]}]
        dual_review = {"results": {
            "A": {"sub_questions": [{"sub_question_id": "Q1", "criteria": [
                {"criterion_id": "C1", "decision": "full"},
                {"criterion_id": "C2", "decision": "none"},
            ]}]},
            "B": {"sub_questions": [{"sub_question_id": "Q1", "criteria": [
                {"criterion_id": "C1", "decision": "none"},
                {"criterion_id": "C2", "decision": "full"},
            ]}]},
        }}
        decisions = app.normalize_teacher_dual_point_decisions(dual_review, [
            {"question_id": "Q1", "rubric_point_id": "C1", "choice": "support_A"},
            {"question_id": "Q1", "rubric_point_id": "C2", "choice": "both_wrong_custom_not_hit"},
        ])
        self.assertEqual(decisions[0]["teacher_decision"], "hit")
        self.assertEqual(decisions[0]["resolution"], "support_A")
        self.assertEqual(decisions[1]["teacher_decision"], "not_hit")
        self.assertEqual(decisions[1]["resolution"], "both_wrong_custom")
        self.assertEqual(app.teacher_review_completeness(sub_questions, decisions), "point_level_complete")
        self.assertEqual(app.teacher_review_completeness(sub_questions, []), "total_only")

    def test_answer_region_scope_sets_scoped_totals_and_point_completeness(self):
        def question(question_id, point_count, point_score):
            return {
                "id": question_id,
                "max_score": 6,
                "criteria": [{"id": f"C{index}", "score": point_score} for index in range(1, point_count + 1)],
            }

        full_exam = [
            question("Q17.1", 3, 2),
            question("Q17.2", 2, 3),
            question("Q17.3", 1, 6),
            question("Q18.1", 1, 6),
            question("Q18.2", 1, 6),
        ]
        regions = [
            {"invocation_scope": "answer_region", "sub_question_ids": ["Q17.1"]},
            {"invocation_scope": "answer_region", "sub_question_ids": ["Q17.2"]},
        ]
        scoped, metadata = app.resolve_grading_scope(full_exam, regions)
        self.assertEqual([item["id"] for item in scoped], ["Q17.1", "Q17.2"])
        self.assertEqual(metadata["graded_question_ids"], ["Q17.1", "Q17.2"])
        self.assertEqual(metadata["scoped_max_score"], 12)

        def scored_question(question_id, scores):
            return {
                "sub_question_id": question_id,
                "answer_status": "answered",
                "suggested_total": sum(scores),
                "max_total": 6,
                "criteria": [{
                    "criterion_id": f"C{index}", "decision": "full" if score else "none",
                    "suggested_score": score,
                } for index, score in enumerate(scores, start=1)],
            }

        result = {
            "mode": "vision-model", "suggested_total": None, "max_total": 30,
            "sub_questions": [scored_question("Q17.1", [2, 2, 2]), scored_question("Q17.2", [0, 0])],
            "dual_review": {
                "base_side": "A", "question_ids": [item["id"] for item in full_exam],
                "results": {
                    side: {
                        "suggested_total": None, "max_total": 30,
                        "sub_questions": [scored_question("Q17.1", [2, 2, 2]), scored_question("Q17.2", [0, 0])],
                    }
                    for side in ("A", "B")
                },
                "comparison": {"risk_level": "HIGH", "review_reasons": ["canonical_input_mismatch"]},
            },
        }
        app.apply_grading_scope_to_result(result, scoped, metadata)
        self.assertEqual(result["suggested_total"], 6)
        self.assertEqual(result["max_total"], 12)
        self.assertEqual(result["dual_review"]["results"]["A"]["suggested_total"], 6)
        self.assertEqual(result["dual_review"]["results"]["B"]["max_total"], 12)
        self.assertEqual(result["dual_review"]["question_ids"], ["Q17.1", "Q17.2"])
        self.assertEqual(result["dual_review"]["comparison"]["risk_level"], "HIGH")
        self.assertIn("canonical_input_mismatch", result["dual_review"]["comparison"]["review_reasons"])

        decisions = [
            {"question_id": question_id, "rubric_point_id": f"C{index}", "teacher_decision": "hit"}
            for question_id, point_count in (("Q17.1", 3), ("Q17.2", 2))
            for index in range(1, point_count + 1)
        ]
        self.assertEqual(app.teacher_review_completeness(scoped, decisions), "point_level_complete")
        self.assertEqual(app.teacher_review_completeness(full_exam, decisions), "total_only")

    def test_legacy_result_without_scope_keeps_full_exam_maximum(self):
        full_exam = [
            {"id": question_id, "max_score": 6, "criteria": [{"id": "C1", "score": 6}]}
            for question_id in ("Q17.1", "Q17.2", "Q17.3", "Q18.1", "Q18.2")
        ]
        scoped, metadata = app.resolve_grading_scope(full_exam)
        self.assertEqual([item["id"] for item in scoped], [item["id"] for item in full_exam])
        self.assertEqual(metadata["scoped_max_score"], 30)
        self.assertEqual(metadata["source"], "whole_exam")

    def test_scope_reconfirmation_keeps_old_final_and_point_choices_until_teacher_saves(self):
        full_exam = [
            {"id": "Q17.1", "max_score": 6, "criteria": [{"id": f"C{i}", "score": 2} for i in range(1, 4)]},
            {"id": "Q17.2", "max_score": 6, "criteria": [{"id": f"C{i}", "score": 3} for i in range(1, 3)]},
            *[
                {"id": question_id, "max_score": 6, "criteria": [{"id": "C1", "score": 6}]}
                for question_id in ("Q17.3", "Q18.1", "Q18.2")
            ],
        ]
        old_point_decisions = [
            {
                "question_id": question_id,
                "rubric_point_id": f"C{index}",
                "decision": "full",
                "teacher_decision": "hit",
                "resolution": "support_both",
                "source": "AI-A+AI-B",
            }
            for question_id, count in (("Q17.1", 3), ("Q17.2", 2))
            for index in range(1, count + 1)
        ]
        old_teacher_final = {
            "final_total": 12,
            "max_total": 30,
            "grading_scope": None,
            "point_decisions": old_point_decisions,
            "teacher_review_completeness": "total_only",
            "confirmed_at": "2026-10-04T12:00:00+08:00",
        }
        result = {
            "suggested_total": None,
            "max_total": 30,
            "dual_review": {"teacher_final": json.loads(json.dumps(old_teacher_final))},
        }

        scope = app.prepare_submission_scope_reconfirmation(
            result,
            full_exam,
            ["Q17.1", "Q17.2"],
            source="根据本次实际 answer-region / Beta 评分范围确认的 Q17.1 + Q17.2",
            previous_final_total=12,
            previous_max_total=30,
            previous_confirmed_at="2026-10-04T12:00:00+08:00",
        )

        self.assertEqual(scope["graded_question_ids"], ["Q17.1", "Q17.2"])
        self.assertEqual(scope["scoped_max_score"], 12)
        self.assertEqual(result["max_total"], 12)
        self.assertTrue(result["teacher_reconfirmation_required"])
        self.assertEqual(result["previous_teacher_final"], {
            "final_total": 12.0,
            "max_total": 30.0,
            "confirmed_at": "2026-10-04T12:00:00+08:00",
        })
        self.assertEqual(result["dual_review"]["teacher_final"], old_teacher_final)
        self.assertEqual(result["dual_review"]["teacher_final"]["final_total"], 12)
        self.assertEqual(result["dual_review"]["teacher_final"]["max_total"], 30)
        self.assertEqual(result["dual_review"]["teacher_final"]["point_decisions"], old_point_decisions)
        scoped, _ = app.resolve_grading_scope(full_exam, graded_question_ids=["Q17.1", "Q17.2"])
        self.assertEqual(app.teacher_review_completeness(scoped, old_point_decisions), "point_level_complete")
        self.assertEqual(app.total_sub_questions(full_exam), 30)


if __name__ == "__main__":
    unittest.main()
