import hashlib
import copy
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from page_alignment import PageAlignmentResult
from layout_profile import crop_subjective_regions_from_aligned_page
from student_auto_crop_service import record_manual_review, run_student_auto_crop


def png_bytes(image):
    ok, encoded = cv2.imencode('.png', image)
    if not ok:
        raise AssertionError('PNG encoding failed')
    return encoded.tobytes()


class StudentAutoCropServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.references = []
        self.reference_bytes = {}
        for page in range(1, 5):
            image = np.full((40, 80, 3), page * 20, dtype=np.uint8)
            raw = png_bytes(image)
            self.reference_bytes[page] = raw
            self.references.append({
                'page_number': page,
                'page_role': 'exam',
                'reference_width': 80,
                'reference_height': 40,
                'reference_sha256': hashlib.sha256(raw).hexdigest(),
                'render_dpi': 150,
                'image_file': f'ref-{page}.png',
            })
        self.regions = [
            {'region_id': 'subjective-17.1', 'region_type': 'subjective_answer', 'question_id': '17.1', 'page_number': 3,
             'bbox': {'x1': 0.1, 'y1': 0.5, 'x2': 0.8, 'y2': 0.75}},
            {'region_id': 'subjective-17.2', 'region_type': 'subjective_answer', 'question_id': '17.2', 'page_number': 3,
             'bbox': {'x1': 0.1, 'y1': 0.7, 'x2': 0.8, 'y2': 0.95}},
        ]
        self.profile = {
            'schema_version': 'layout-profile-v1',
            'layout_profile_id': 'profile-test-1',
            'name': 'test',
            'exam_id': 'e' * 32,
            'assignment_id': 'a' * 12,
            'source_pdf_sha256': 'a' * 64,
            'reference_width': 80,
            'reference_height': 40,
            'reference_pages': self.references,
            'alignment_config': {'method': 'orb_ransac'},
            'regions': self.regions,
            'created_at': '2026-10-04T00:00:00Z',
            'updated_at': '2026-10-04T00:00:00Z',
        }
        self.context = {
            'exam_id': 'e' * 32,
            'assignment_id': 'a' * 12,
            'has_pdf_reference': True,
            'source_pdf_sha256': 'a' * 64,
            'pdf_hash_mismatch': False,
            'profile_hash_mismatch': False,
            'layout_profile_id': 'profile-test-1',
            'layout_profile': self.profile,
            'references': self.references,
        }
        # Each half has a distinct marker. The deterministic alignment mock
        # passes only left→PDF page 3, and never alters the production gate.
        self.student = np.zeros((40, 80, 3), dtype=np.uint8)
        self.student[:, :40] = 50
        self.student[:, 40:] = 100
        self.student_bytes = png_bytes(self.student)
        self.profile_before = copy.deepcopy(self.profile)

    def tearDown(self):
        self.temp.cleanup()

    def _loader(self, page):
        return self.reference_bytes[int(page['page_number'])]

    @staticmethod
    def _passed(image):
        return PageAlignmentResult('aligned', image.copy(), {
            'alignment': 'aligned', 'method': 'orb_ransac',
            'source_keypoints': 190, 'reference_keypoints': 320,
            'good_matches': 141, 'inlier_count': 49, 'inlier_ratio': 49 / 141,
            'inlier_page_coverage': 0.0952,
            'median_reprojection_error_norm': 0.001, 'p95_reprojection_error_norm': 0.004,
        })

    def _aligner(self, ambiguous=False, force_fail=False):
        def align(candidate, config, *, reference_image=None):
            page = int(round(float(reference_image[0, 0, 0]) / 20))
            marker = int(candidate[0, 0, 0])
            if force_fail:
                return PageAlignmentResult('failed', None, {'alignment': 'feature_quality_gate', 'good_matches': 4})
            if ambiguous and page in {3, 4}:
                return self._passed(reference_image)
            if marker == 50 and page == 3:
                return self._passed(reference_image)
            return PageAlignmentResult('failed', None, {
                'alignment': 'feature_quality_gate', 'good_matches': 12,
                'inlier_count': 3, 'inlier_ratio': 0.25,
                'inlier_page_coverage': 0.01,
            })
        return align

    def _run(self, *, mode='double_spread', context=None, profile=None, aligner=None, regions=None, run_id='1' * 32):
        if context is None:
            context = dict(self.context)
        if profile is not None:
            context['layout_profile'] = profile
        if regions is not None:
            context['layout_profile'] = {**context['layout_profile'], 'regions': regions}
        output = self.root / run_id
        result = run_student_auto_crop(
            exam_context=context,
            student_image_bytes=self.student_bytes,
            input_mode=mode,
            reference_loader=self._loader,
            output_dir=output,
            source_extension='.png',
            aligner=aligner or self._aligner(),
            run_id=run_id,
        )
        return result, output

    def test_double_spread_matches_left_to_page3_and_crops_profile_rois(self):
        result, output = self._run()
        self.assertEqual(result['status'], 'cropped')
        self.assertTrue(result['partial_processing'])
        self.assertEqual([(item['candidate_id'], item['matched_page_number']) for item in result['page_candidates']], [('left', 3), ('right', None)])
        self.assertEqual([item['question_id'] for item in result['crops']], ['17.1', '17.2'])
        self.assertEqual([item['pixel_bbox'] for item in result['crops']], [
            {'x1': 8, 'y1': 19, 'x2': 64, 'y2': 31},
            {'x1': 8, 'y1': 27, 'x2': 64, 'y2': 39},
        ])
        self.assertEqual(result['crops'][0]['configured_bbox'], self.regions[0]['bbox'])
        self.assertEqual(result['crops'][0]['effective_crop_bbox'], {
            'x1': 0.1, 'y1': 0.49, 'x2': 0.8, 'y2': 0.755,
        })
        self.assertEqual(result['crops'][0]['padding'], {'top': 0.01, 'bottom': 0.005, 'left': 0.0, 'right': 0.0})
        self.assertEqual(result['crops'][1]['padding'], result['crops'][0]['padding'])
        self.assertEqual(self.profile, self.profile_before)
        self.assertEqual(result['crops'][0]['layout_profile_id'], self.profile['layout_profile_id'])
        self.assertEqual(result['crops'][0]['alignment_run_id'], result['alignment_run_id'])
        self.assertEqual(result['crops'][0]['source_student_image_hash'], hashlib.sha256(self.student_bytes).hexdigest())
        self.assertEqual(result['crops'][0]['reference_page_hash'], self.references[2]['reference_sha256'])
        self.assertEqual(result['accepted_pages'][0]['page_number'], 3)
        self.assertEqual(result['accepted_pages'][0]['alignment_metrics']['good_matches'], 141)
        self.assertEqual(result['model_api_calls'], {'deepseek': 0, 'mimo': 0, 'qwen': 0})
        original_after = cv2.imdecode(np.frombuffer((output / 'source_student_image.png').read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
        np.testing.assert_array_equal(original_after, self.student)
        self.assertTrue((output / '17_1_crop.png').is_file())
        self.assertTrue((output / '17_2_crop.png').is_file())
        self.assertTrue((output / 'roi_overlay_page_3.png').is_file())
        self.assertTrue((output / 'padding_validation' / '17_1_crop_padded.png').is_file())
        self.assertTrue((output / 'padding_validation' / '17_2_crop_padded.png').is_file())
        self.assertTrue((output / 'padding_validation' / 'configured_roi_overlay.png').is_file())
        self.assertTrue((output / 'padding_validation' / 'effective_crop_overlay.png').is_file())
        validation = (output / 'padding_validation' / 'PADDING_VALIDATION.md').read_text(encoding='utf-8')
        self.assertIn('pending_teacher_review', validation)
        self.assertIn('source_student_image_hash', validation)
        self.assertIn('reference_page_hash', validation)
        self.assertEqual(result['padding_validation']['status'], 'pending_teacher_review')
        self.assertEqual(result['padding_validation']['crops'][0]['visual_status'], 'pending_teacher_review')

    def test_single_page_candidate_is_matched_without_filename_or_order_guess(self):
        self.student[:, :] = 50
        self.student_bytes = png_bytes(self.student)
        result, _ = self._run(mode='single_page', run_id='2' * 32)
        self.assertEqual(result['status'], 'cropped')
        self.assertEqual(result['accepted_pages'][0]['page_number'], 3)

    def test_missing_profile_and_stale_hash_stop_before_artifacts(self):
        missing = dict(self.context, layout_profile_id='', layout_profile=None)
        result, output = self._run(context=missing, run_id='3' * 32)
        self.assertEqual(result['status'], 'layout_profile_missing')
        self.assertFalse(output.exists())
        stale = dict(self.context, profile_hash_mismatch=True)
        result, output = self._run(context=stale, run_id='4' * 32)
        self.assertEqual(result['status'], 'layout_profile_stale')
        self.assertEqual(result['crops'], [])
        self.assertFalse(output.exists())

    def test_missing_formal_reference_asset_stops_before_alignment_and_crop(self):
        context = dict(self.context, reference_asset_status='reference_asset_missing')
        aligner = self._aligner()
        calls = []
        def count_calls(*args, **kwargs):
            calls.append(True)
            return aligner(*args, **kwargs)
        result, output = self._run(context=context, aligner=count_calls, run_id='9' * 32)
        self.assertEqual(result['status'], 'reference_asset_missing')
        self.assertEqual(result['crops'], [])
        self.assertFalse(output.exists())
        self.assertEqual(calls, [])

    def test_existing_profile_without_pdf_reference_metadata_reports_asset_missing(self):
        context = dict(
            self.context,
            has_pdf_reference=False,
            reference_asset_status='reference_asset_missing',
        )
        result, output = self._run(context=context, run_id='a' * 32)
        self.assertEqual(result['status'], 'reference_asset_missing')
        self.assertFalse(output.exists())

    def test_failed_alignment_and_ambiguous_pages_never_crop(self):
        result, _ = self._run(aligner=self._aligner(force_fail=True), run_id='5' * 32)
        self.assertEqual(result['status'], 'alignment_failed')
        self.assertEqual(result['crops'], [])
        result, _ = self._run(aligner=self._aligner(ambiguous=True), run_id='6' * 32)
        self.assertEqual(result['status'], 'page_match_ambiguous')
        self.assertEqual(result['crops'], [])

    def test_page_without_subjective_roi_is_reported_without_crop(self):
        page4_only = [
            {'region_id': 'subjective-18.1', 'region_type': 'subjective_answer', 'question_id': '18.1', 'page_number': 4,
             'bbox': {'x1': .1, 'y1': .5, 'x2': .8, 'y2': .75}},
        ]
        result, _ = self._run(regions=page4_only, run_id='7' * 32)
        self.assertEqual(result['status'], 'roi_missing')
        self.assertEqual(result['crops'], [])
        self.assertEqual(result['page_candidates'][0]['status'], 'roi_missing')

    def test_layout_mismatch_reports_actionable_code_and_never_crops(self):
        result, _ = self._run(aligner=lambda *_a, **_kw: PageAlignmentResult(
            'failed', None, {'alignment': 'page_layout_mismatch'}), run_id='b' * 32)
        self.assertEqual(result['status'], 'alignment_failed')
        self.assertEqual(result['failure_code'], 'page_layout_mismatch')
        self.assertEqual(result['crops'], [])
        self.assertEqual(result['accepted_pages'], [])
        self.assertTrue(all(count == 0 for count in result['model_api_calls'].values()))

    def test_assisted_alignment_is_labelled_and_still_needs_teacher_acceptance(self):
        base = self._aligner()
        def assisted(*args, **kwargs):
            result = base(*args, **kwargs)
            if result.status == 'aligned':
                return PageAlignmentResult(result.status, result.image, {
                    **result.diagnostics, 'alignment_assisted': True,
                    'method': 'printed_frame_teacher_assisted'})
            return result
        self.student[:, :] = 50
        self.student_bytes = png_bytes(self.student)
        result, _ = self._run(mode='single_page', aligner=assisted, run_id='c' * 32)
        self.assertEqual(result['status'], 'cropped')
        self.assertTrue(result['alignment_assisted'])
        self.assertEqual(result['human_review'], 'pending_teacher_review')
        self.assertEqual(result['padding_validation']['status'], 'pending_teacher_review')
        self.assertTrue(all(count == 0 for count in result['model_api_calls'].values()))

    def test_local_manual_review_updates_only_diagnostic_artifact(self):
        result, output = self._run(run_id='8' * 32)
        reviewed = record_manual_review(self.root, result['alignment_run_id'], 'passed')
        self.assertEqual(reviewed['human_review'], 'passed')
        self.assertEqual(__import__('json').loads((output / 'run.json').read_text(encoding='utf-8'))['human_review'], 'passed')

    def test_safety_padding_clamps_and_keeps_configured_bbox(self):
        page = np.zeros((100, 200, 3), dtype=np.uint8)
        region = {
            'region_id': 'subjective-edge', 'region_type': 'subjective_answer',
            'question_id': 'edge', 'page_number': 1,
            'bbox': {'x1': 0.005, 'y1': 0.008, 'x2': 0.996, 'y2': 0.997},
        }
        before = copy.deepcopy(region)
        item = crop_subjective_regions_from_aligned_page(
            page, [region], safety_padding={'top': 0.01, 'bottom': 0.01, 'left': 0.02, 'right': 0.02},
        )[0]
        self.assertEqual(item['configured_bbox'], before['bbox'])
        self.assertEqual(region, before)
        self.assertEqual(item['effective_crop_bbox'], {'x1': 0.0, 'y1': 0.0, 'x2': 1.0, 'y2': 1.0})
        self.assertEqual(item['pixel_bbox'], {'x1': 0, 'y1': 0, 'x2': 200, 'y2': 100})


if __name__ == '__main__':
    unittest.main()
