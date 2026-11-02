from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from layout_profile import crop_handwritten_cells, process_layout_profile, validate_layout_profile
from objective.omr import recognize_omr, recognize_omr_aligned
from objective.synthetic import photograph, render_omr_card
from objective.templates import BUILTIN_TEMPLATES
from page_alignment import PageAlignmentResult, align_page


def _profile(regions, method="orb_ransac"):
    return {
        "layout_profile_id": "mixed-test-v1",
        "name": "mixed test",
        "reference_image": "blank.jpg",
        "reference_width": 200,
        "reference_height": 100,
        "alignment_config": {"method": method},
        "regions": regions,
    }


def _region(region_id, region_type, *, qid=None, mapping=None, bbox=None, **extra):
    item = {
        "region_id": region_id,
        "region_type": region_type,
        "bbox": bbox or {"x1": 0.05, "y1": 0.1, "x2": 0.95, "y2": 0.9},
        **extra,
    }
    if qid is not None:
        item["question_id"] = qid
    if mapping is not None:
        item["question_mapping"] = mapping
    return item


class LayoutProfileTests(unittest.TestCase):
    def test_one_profile_accepts_all_three_region_types(self):
        omr = dict(BUILTIN_TEMPLATES[0]["config"])
        omr["page_width"], omr["page_height"] = 200, 100
        profile = validate_layout_profile(_profile([
            _region("essay-17-1", "subjective_answer", qid="17.1"),
            _region("hand-1-2", "handwritten_abcd", mapping=[1, 2], rows=1, cols=2),
            _region("omr-3-22", "bubble_omr", mapping=list(range(3, 23)), omr_config=omr),
        ]))
        self.assertEqual([r["region_type"] for r in profile["regions"]], ["subjective_answer", "handwritten_abcd", "bubble_omr"])
        self.assertEqual(profile["schema_version"], "layout-profile-v1")

    def test_handwritten_grid_splits_row_major_without_teacher_cell_setup(self):
        page = np.zeros((100, 200, 3), dtype=np.uint8)
        region = _region("hand", "handwritten_abcd", mapping=[1, 2, 3, 4], rows=2, cols=2, bbox={"x1": 0.1, "y1": 0.1, "x2": 0.9, "y2": 0.9}, cell_padding=0)
        cells = crop_handwritten_cells(page, region)
        self.assertEqual([cell["question_id"] for cell in cells], ["Q1", "Q2", "Q3", "Q4"])
        self.assertEqual([cv2.imdecode(np.frombuffer(cell["image_bytes"], np.uint8), cv2.IMREAD_COLOR).shape[:2] for cell in cells], [(40, 80)] * 4)

    def test_handwritten_mapping_count_must_match_grid_size(self):
        with self.assertRaisesRegex(ValueError, r"rows\*cols"):
            validate_layout_profile(_profile([_region("hand", "handwritten_abcd", mapping=[1, 2, 3], rows=2, cols=2)]))

    def test_subjective_region_reuses_existing_answer_region_cropper(self):
        from layout_profile import _subjective_handler

        page = np.full((100, 200, 3), 255, dtype=np.uint8)
        region = _region("s", "subjective_answer", qid="17.1", bbox={"x1": 0.1, "y1": 0.2, "x2": 0.8, "y2": 0.9})
        with patch("app.crop_model_image_bytes", return_value=b"existing-answer-region-crop") as cropper:
            output = _subjective_handler(page, region)
        self.assertEqual(output["crop_image_bytes"], b"existing-answer-region-crop")
        cropper.assert_called_once()
        _, crop_region = cropper.call_args.args
        self.assertEqual(crop_region["x"], 0.1)
        self.assertEqual(crop_region["y"], 0.2)
        self.assertAlmostEqual(crop_region["width"], 0.7)
        self.assertAlmostEqual(crop_region["height"], 0.7)

    def test_handwriting_handler_maps_each_cell_and_preserves_confidence(self):
        from layout_profile import _handwritten_handler

        image = np.full((100, 200, 3), 255, dtype=np.uint8)
        region = _region("hand", "handwritten_abcd", mapping=[3, 7], rows=1, cols=2)

        def fake_recognizer(_crop, _count, *, engine=None, question_ids=None, expected_question_id=None):
            qno = expected_question_id
            return {qno: {"option": "C", "state": "auto", "reason": None, "confidence": 0.91, "evidence": None}}

        with patch("layout_profile.recognize_handwritten_abcd", side_effect=fake_recognizer) as recognizer:
            output = _handwritten_handler(image, region)
        self.assertEqual([row["question_id"] for row in output["answers"]], ["Q3", "Q7"])
        self.assertEqual([row["answer"] for row in output["answers"]], ["C", "C"])
        self.assertEqual([row["confidence"] for row in output["answers"]], [0.91, 0.91])
        self.assertEqual(recognizer.call_count, 2)

    def test_existing_recognizer_can_read_a_single_mapped_abcd_cell_without_printed_question_number(self):
        from objective.recognition import recognize_handwritten_abcd

        ok, encoded = cv2.imencode(".png", np.full((80, 80, 3), 255, dtype=np.uint8))
        self.assertTrue(ok)
        box = [[10, 10], [40, 10], [40, 50], [10, 50]]

        def one_option(_image):
            return ([[box, "C", 0.96]], None)

        result = recognize_handwritten_abcd(encoded.tobytes(), 3, engine=one_option, expected_question_id=3)
        self.assertEqual(result[3]["option"], "C")
        self.assertEqual(result[3]["state"], "auto")

        def ambiguous_options(_image):
            return ([[box, "C", 0.96], [box, "D", 0.93]], None)

        ambiguous = recognize_handwritten_abcd(encoded.tobytes(), 3, engine=ambiguous_options, expected_question_id=3)
        self.assertEqual(ambiguous[3]["state"], "needs_review")
        self.assertEqual(ambiguous[3]["reason"], "duplicate_question_candidate")

    def test_objective_answers_share_one_schema_and_source_type(self):
        page = np.full((100, 200, 3), 255, dtype=np.uint8)
        hand = _region("hand", "handwritten_abcd", mapping=[1], rows=1, cols=1)
        omr_config = dict(BUILTIN_TEMPLATES[0]["config"])
        omr_config["question_cells"] = omr_config["question_cells"][:1]
        omr_config["question_count"] = 1
        omr = _region("omr", "bubble_omr", mapping=[2], omr_config=omr_config)
        result_obj = type("OMRResult", (), {"status": "auto", "answers": {1: {"option": "B", "state": "auto", "reason": None}}, "diagnostics": {"alignment": "aligned", "signature_match": True}})()

        def hand_handler(_page, _region, engine=None):
            return {"status": "completed", "answers": [{"question_id": "Q1", "answer": "A", "source_type": "handwritten_abcd", "status": "recognized"}]}

        with patch("layout_profile.recognize_omr_aligned", return_value=result_obj):
            from layout_profile import _omr_handler
            omr_output = _omr_handler(page, omr)
        hand_output = hand_handler(page, hand)
        self.assertEqual(omr_output["answers"][0]["question_id"], "Q2")
        self.assertEqual(omr_output["answers"][0]["answer"], "B")
        self.assertEqual(omr_output["answers"][0]["source_type"], "bubble_omr")
        self.assertEqual({"question_id", "answer", "source_type", "status"}.issubset(hand_output["answers"][0]), True)

    def test_bubble_handler_reuses_omr_mark_detection_after_alignment(self):
        config = BUILTIN_TEMPLATES[0]["config"]
        aligned = render_omr_card(config, ["A"] * int(config["question_count"]))
        page = cv2.cvtColor(np.array(aligned), cv2.COLOR_RGB2BGR)
        outcome = recognize_omr_aligned(page, config, {"alignment": "aligned", "method": "test_shared"})
        self.assertEqual(outcome.status, "auto")
        self.assertTrue(all(answer["state"] == "auto" for answer in outcome.answers.values()))
        # Legacy Stage4 entry still uses the same shared aligner before OMR.
        photographed = photograph(aligned, __import__("random").Random(11))
        legacy = recognize_omr(photographed, config)
        shared_alignment = align_page(photographed, {"method": "fiducial_anchors", "template_config": config})
        self.assertEqual(shared_alignment.status, "aligned")
        through_shared_pipeline = recognize_omr_aligned(shared_alignment.image, config, shared_alignment.diagnostics)
        from layout_profile import _omr_handler
        region = {
            "region_id": "omr-whole-card",
            "region_type": "bubble_omr",
            "bbox": {"x1": 0.04, "y1": 0.04, "x2": 0.96, "y2": 0.96},
            "question_mapping": list(range(1, 21)),
            "omr_config": config,
        }
        cropped_region = _omr_handler(shared_alignment.image, region)
        self.assertEqual(legacy.status, "auto")
        self.assertEqual(through_shared_pipeline.answers, legacy.answers)
        self.assertEqual(cropped_region["status"], "completed")
        self.assertEqual([row["answer"] for row in cropped_region["answers"]], [row["option"] for row in legacy.answers.values()])
        self.assertEqual(sum(answer["state"] == "auto" for answer in legacy.answers.values()), 20)

    def test_all_handlers_receive_the_same_aligned_page_and_alignment_runs_once(self):
        omr_config = dict(BUILTIN_TEMPLATES[0]["config"])
        omr_config["page_width"], omr_config["page_height"] = 200, 100
        omr_config["question_cells"] = omr_config["question_cells"][:1]
        profile = _profile([
            _region("s", "subjective_answer", qid="17.1"),
            _region("h", "handwritten_abcd", mapping=[1], rows=1, cols=1),
            _region("o", "bubble_omr", mapping=[2], omr_config=omr_config),
        ])
        aligned_page = np.full((100, 200, 3), 255, dtype=np.uint8)
        calls = []

        def fake_aligner(_image, _config, *, reference_image=None):
            calls.append("align")
            return PageAlignmentResult("aligned", aligned_page, {"alignment": "aligned"})

        def handler(_page, region, **_kwargs):
            calls.append((region["region_type"], _page is aligned_page))
            return {"region_id": region["region_id"], "status": "completed", "answers": []}

        output = process_layout_profile(b"test", profile, aligner=fake_aligner, handlers={key: handler for key in ("subjective_answer", "handwritten_abcd", "bubble_omr")})
        self.assertEqual(calls[0], "align")
        self.assertEqual(calls.count("align"), 1)
        self.assertTrue(all(item[1] for item in calls[1:]))
        self.assertEqual(output["status"], "completed")

    def test_alignment_failure_blocks_every_region_handler(self):
        profile = _profile([_region("s", "subjective_answer", qid="17.1")])
        calls = []
        output = process_layout_profile(b"bad", profile, aligner=lambda *_a, **_kw: PageAlignmentResult("failed", None, {"alignment": "failed"}), handlers={"subjective_answer": lambda *_a: calls.append("called")})
        self.assertEqual(output["status"], "manual_review_required")
        self.assertEqual(output["regions"][0]["reason"], "page_alignment_failed")
        self.assertEqual(calls, [])

    def test_region_failure_keeps_other_region_outputs(self):
        profile = _profile([_region("s", "subjective_answer", qid="17.1"), _region("h", "handwritten_abcd", mapping=[1], rows=1, cols=1)])
        aligned_page = np.zeros((10, 10, 3), dtype=np.uint8)
        profile["reference_width"], profile["reference_height"] = 10, 10

        def handler(_page, region, **_kwargs):
            if region["region_id"] == "s":
                raise RuntimeError("local crop error")
            return {"region_id": "h", "status": "completed", "answers": [{"question_id": "Q1", "answer": "C", "source_type": "handwritten_abcd", "status": "recognized"}]}

        output = process_layout_profile(b"x", profile, aligner=lambda *_a, **_kw: PageAlignmentResult("aligned", aligned_page, {}), handlers={"subjective_answer": handler, "handwritten_abcd": handler})
        self.assertEqual(output["status"], "partial_processing")
        self.assertEqual(output["regions"][0]["status"], "failed")
        self.assertEqual(output["answers"][0]["answer"], "C")

    def test_orb_ransac_aligns_reference_page_and_applies_quality_gate(self):
        reference = np.full((600, 420, 3), 255, dtype=np.uint8)
        for y in range(45, 560, 42):
            cv2.line(reference, (30, y), (390, y), (0, 0, 0), 2)
            cv2.putText(reference, f"Q{y}", (38, y - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
        for x in range(50, 400, 30):
            cv2.line(reference, (x, 20), (x, 580), (50, 50, 50), 1)
        source = cv2.warpPerspective(reference, np.array([[0.9, 0.03, 35], [-0.02, 0.9, 50], [0.00005, -0.00003, 1]], dtype=np.float32), (500, 700), borderValue=(255, 255, 255))
        result = align_page(source, {"method": "orb_ransac", "min_matches": 8, "min_inliers": 6, "max_reprojection_error_norm": 0.01}, reference_image=reference)
        self.assertEqual(result.status, "aligned", result.diagnostics)
        self.assertEqual(result.image.shape[:2], reference.shape[:2])
        self.assertEqual(result.diagnostics["method"], "orb_ransac")
        failed = align_page(np.full((100, 100, 3), 255, dtype=np.uint8), {"method": "orb_ransac"}, reference_image=reference)
        self.assertEqual(failed.status, "failed")

    def test_orb_ransac_reads_reference_from_unicode_windows_path(self):
        reference = np.full((600, 420, 3), 255, dtype=np.uint8)
        for y in range(45, 560, 42):
            cv2.line(reference, (30, y), (390, y), (0, 0, 0), 2)
            cv2.putText(reference, f"Q{y}", (38, y - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
        for x in range(50, 400, 30):
            cv2.line(reference, (x, 20), (x, 580), (50, 50, 50), 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "标准答卷页.png"
            ok, encoded = cv2.imencode(".png", reference)
            self.assertTrue(ok)
            encoded.tofile(path)
            result = align_page(reference, {"method": "orb_ransac"}, reference_image=path)
        self.assertEqual(result.status, "aligned", result.diagnostics)
        self.assertEqual(result.image.shape[:2], reference.shape[:2])

    @staticmethod
    def _registration_sheet():
        reference = np.full((900, 640, 3), 255, dtype=np.uint8)
        for y in range(55, 320, 40):
            cv2.putText(reference, f"EXAM {y} ABCD 1234", (55, y), cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 0, 0), 2)
        cv2.rectangle(reference, (55, 360), (585, 830), (0, 0, 0), 2)
        for y in (400, 570, 740):
            cv2.putText(reference, f"Q{y}", (75, y), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 0), 2)
        return reference

    def test_phone_resolution_preserves_answer_area_geometry(self):
        reference = self._registration_sheet()
        source = cv2.resize(reference, (1920, 2700), interpolation=cv2.INTER_CUBIC)
        # Extra writing should not be mistaken for a different printed layout.
        cv2.putText(source, "student answer", (300, 1500), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 3)
        result = align_page(source, {"method": "orb_ransac"}, reference_image=reference)
        self.assertEqual(result.status, "aligned", result.diagnostics)
        self.assertEqual(result.diagnostics["detection_width"], 640)
        self.assertEqual(result.image.shape, reference.shape)
        # Test the lower border, beyond the dense header matching points.
        self.assertGreater(np.mean(result.image[828:833, 100:540, 0] < 160), .3)

    def test_matching_header_does_not_authorize_shifted_answer_layout(self):
        reference = self._registration_sheet()
        source = reference.copy()
        source[350:] = 255
        cv2.putText(source, "OTHER CONTENT", (125, 550), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
        result = align_page(source, {"method": "orb_ransac"}, reference_image=reference)
        self.assertEqual(result.status, "failed", result.diagnostics)
        self.assertIsNone(result.image)
        self.assertEqual(result.diagnostics["alignment"], "page_layout_mismatch")
        self.assertFalse(result.diagnostics["printed_layout_passed"])

    def test_edited_card_with_shared_frame_is_labelled_for_teacher_review(self):
        reference = self._registration_sheet()
        source = reference.copy()
        source[350:] = 255
        cv2.rectangle(source, (125, 430), (550, 870), (0, 0, 0), 2)
        result = align_page(source, {"method": "orb_ransac"}, reference_image=reference)
        self.assertEqual(result.status, "aligned", result.diagnostics)
        self.assertEqual(result.diagnostics["method"], "printed_frame_teacher_assisted")
        self.assertTrue(result.diagnostics["alignment_assisted"])
        self.assertTrue(result.diagnostics["printed_layout_passed"])
        self.assertFalse(result.diagnostics["feature_support"]["printed_layout_passed"])

    def test_unrelated_page_and_blank_photo_fail_closed(self):
        reference = self._registration_sheet()
        unrelated = np.full_like(reference, 255)
        for y in range(80, 850, 50):
            cv2.putText(unrelated, f"OTHER PAGE {y}", (140, y), cv2.FONT_HERSHEY_SIMPLEX, .6, (0, 0, 0), 1)
        for image in (unrelated, np.full_like(reference, 255)):
            result = align_page(image, {"method": "orb_ransac"}, reference_image=reference)
            self.assertEqual(result.status, "failed", result.diagnostics)
            self.assertIsNone(result.image)

    def test_low_feature_coverage_can_reach_validated_printed_frame(self):
        reference = self._registration_sheet()
        original_area = cv2.contourArea
        first = True
        def concentrated_matches(contour, *args, **kwargs):
            nonlocal first
            if first:
                first = False
                return reference.shape[0] * reference.shape[1] * .0156
            return original_area(contour, *args, **kwargs)
        with patch('page_alignment.cv2.contourArea', side_effect=concentrated_matches):
            result = align_page(reference, {'method': 'orb_ransac'}, reference_image=reference)
        self.assertEqual(result.status, 'aligned', result.diagnostics)
        self.assertTrue(result.diagnostics['alignment_assisted'])
        self.assertEqual(result.diagnostics['feature_support']['inlier_page_coverage'], .0156)
        self.assertTrue(result.diagnostics['printed_layout_passed'])

    def test_low_coverage_without_valid_frame_still_has_no_output(self):
        reference = self._registration_sheet()
        original_area = cv2.contourArea
        first = True
        def concentrated_matches(contour, *args, **kwargs):
            nonlocal first
            if first:
                first = False
                return reference.shape[0] * reference.shape[1] * .0156
            return original_area(contour, *args, **kwargs)
        with patch('page_alignment.cv2.contourArea', side_effect=concentrated_matches), patch('page_alignment._large_printed_frame', return_value=None):
            result = align_page(reference, {'method': 'orb_ransac'}, reference_image=reference)
        self.assertEqual(result.status, 'failed')
        self.assertIsNone(result.image)
        self.assertEqual(result.diagnostics['alignment'], 'feature_quality_gate')

    def test_weak_feature_count_or_ratio_never_uses_frame_assistance(self):
        reference = self._registration_sheet()
        for limits in ({'min_inliers': 100000}, {'min_inlier_ratio': 1.01}):
            with patch('page_alignment._align_printed_frame') as assisted:
                result = align_page(reference, {'method': 'orb_ransac', **limits}, reference_image=reference)
            self.assertEqual(result.status, 'failed')
            self.assertIsNone(result.image)
            assisted.assert_not_called()

    def test_bad_reprojection_error_never_uses_frame_assistance(self):
        reference = self._registration_sheet()
        project = cv2.perspectiveTransform
        with patch('page_alignment.cv2.perspectiveTransform', side_effect=lambda points, matrix: project(points, matrix) + 100), patch('page_alignment._align_printed_frame') as assisted:
            result = align_page(reference, {'method': 'orb_ransac'}, reference_image=reference)
        self.assertEqual(result.status, 'failed')
        self.assertIsNone(result.image)
        self.assertGreater(result.diagnostics['p95_reprojection_error_norm'], .006)
        assisted.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
