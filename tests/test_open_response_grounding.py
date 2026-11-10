"""Regression for off-topic answers and invented evidence, using synthetic text."""
import copy
import unittest

import holistic_scoring as hs
from open_response_grounding import normalize_grounded, transcriptions
from tests import test_project_scoring as fixtures


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.question = fixtures.project_questions()[0]
        self.text = '改革开放推动了经济发展。'
        self.item = fixtures.project_raw(self.question)
        self.item['evidence'][0].update(line_id='L1', student_quote=self.text)
        self.item['fact_checks'] = []
        self.item['problem_flags']['off_topic'] = True
        self.item['suggested_total'] = 0
        for dimension in self.item['dimension_assessments']:
            dimension.update(score=0, judgment='not_met')

    def transcript(self, text=None, status='readable'):
        item = dict(sub_question_id=self.question['id'], status=status, confidence=.98,
                    image_readable=True, region_complete=True, question_matched=True,
                    lines=[dict(id='L1', text=text or self.text, location=self.item['evidence'][0]['location'])])
        return transcriptions({'sub_questions': [item]}, [self.question], 1)

    def grade(self, item=None, transcript=None):
        return normalize_grounded({'sub_questions': [item or self.item]}, [self.question], transcript or self.transcript(), 1)['sub_questions'][0]

    def test_transcript_accepts_full_printed_subnumber_without_q_prefix(self):
        payload = {'sub_questions': list(self.transcript().values())}
        payload['sub_questions'][0]['sub_question_id'] = ' 19.1 '
        self.assertIn('Q19.1', transcriptions(payload, [self.question], 1))
        payload['sub_questions'][0]['sub_question_id'] = '19'
        with self.assertRaisesRegex(ValueError, '题号缺失'):
            transcriptions(payload, [self.question], 1)

    def test_readable_transcript_below_confidence_gate_gets_review_only_score(self):
        for confidence in (.72, .70):
            payload = {'sub_questions': list(self.transcript().values())}
            payload['sub_questions'][0]['confidence'] = confidence
            transcript = transcriptions(payload, [self.question], 1)
            self.assertEqual(transcript[self.question['id']]['status'], 'readable')
            score = self.grade(transcript=transcript)
            self.assertEqual(score['suggested_total'], 0)
            self.assertTrue(score['needs_review'])
            self.assertFalse(hs.valid_sub(score, self.question))

    def test_readable_off_topic_is_zero_and_not_blank(self):
        score = self.grade()
        self.assertEqual(score['answer_status'], 'answered')
        self.assertEqual(score['suggested_total'], 0)
        self.assertTrue(hs.valid_sub(score, self.question))

    def test_missing_version_echoes_preserve_score_but_missing_judgment_is_invalid(self):
        item = copy.deepcopy(self.item)
        item.pop('scoring_version')
        item.pop('rubric_hash')
        self.assertEqual(self.grade(item)['suggested_total'], 0)
        item.pop('problem_flags')
        result = self.grade(item)
        self.assertEqual(result['answer_status'], 'scoring_invalid')
        self.assertIsNone(result['suggested_total'])
        self.assertTrue(result['blocked'])
        self.assertTrue(result['holistic_result']['image_readable'])
        self.assertIn('关键问题标记无效', result['block_reasons'])

    def test_redundant_judgments_keep_verified_score_for_review_only(self):
        item = copy.deepcopy(self.item)
        item['constraint_status'] = 'uncertain'
        item['problem_flags']['task_constraint_violation'] = True
        item['factual_status'] = 'uncertain'
        item['critical_factual_issues'] = ['史实仍待核对']
        item.pop('needs_review')
        result = self.grade(item)
        self.assertEqual(result['answer_status'], 'answered')
        self.assertEqual(result['suggested_total'], 0)
        self.assertTrue(result['blocked'])
        self.assertFalse(hs.valid_sub(result, self.question))
        saved = result['holistic_result']
        self.assertEqual(saved['constraint_status'], 'uncertain')
        self.assertEqual(saved['reported_conflicting_flags'], {'task_constraint_violation': True})
        self.assertEqual(saved['critical_factual_issues'], ['史实仍待核对'])
        self.assertFalse(saved['problem_flags']['task_constraint_violation'])

    def test_fabricated_quotes_block_five_or_eight_points(self):
        for score, quote in ((5, '推恩令'), (8, '安西都护府')):
            item = copy.deepcopy(self.item)
            item['evidence'][0]['student_quote'] = quote
            item['suggested_total'] = score
            result = self.grade(item)
            self.assertEqual(result['answer_status'], 'scoring_invalid')
            self.assertIsNone(result['suggested_total'])
            self.assertIn('不在对应的答卷转写行', result['block_reasons'][0])

    def test_fact_quote_spanning_adjacent_lines_requires_literal_evidence(self):
        transcript = self.transcript('材料中涉及隋朝')
        lines = transcript[self.question['id']]['lines']
        lines.append(dict(id='L2', text='开凿大运河。', location=copy.deepcopy(lines[0]['location'])))
        item = copy.deepcopy(self.item)
        item['problem_flags']['off_topic'] = False
        item['evidence'][0].update(line_id='L2', student_quote='开凿大运河')
        item['fact_checks'] = [dict(student_quote='隋朝开凿大运河', judgment='correct',
                                   reason='按作答核对', evidence_ids=[item['evidence'][0]['id']])]
        self.assertEqual(self.grade(item, transcript)['suggested_total'], 0)
        altered = copy.deepcopy(item)
        altered['fact_checks'][0]['student_quote'] = '唐朝开凿大运河'
        self.assertIsNone(self.grade(altered, transcript)['suggested_total'])
        lines[1]['location']['photo_index'] = 2
        self.assertIsNone(self.grade(item, transcript)['suggested_total'])

    def test_off_topic_with_positive_score_is_blocked(self):
        item = copy.deepcopy(self.item)
        item['dimension_assessments'][0]['score'] = 1
        item['suggested_total'] = 1
        self.assertIsNone(self.grade(item)['suggested_total'])

    def test_uncertain_transcription_has_no_score(self):
        transcript = self.transcript('改革开放□□')
        result = normalize_grounded({'sub_questions': []}, [self.question], transcript, 1)
        self.assertTrue(result['blocked'])
        self.assertIsNone(result['sub_questions'][0]['suggested_total'])
        self.assertEqual(result['sub_questions'][0]['answer_status'], 'image_unreadable')

    def test_on_topic_partial_credit_and_dual_transcription_conflict(self):
        item = copy.deepcopy(self.item)
        item['problem_flags']['off_topic'] = False
        item['dimension_assessments'][0].update(score=1, judgment='met')
        item['suggested_total'] = 1
        item['evidence'][0]['student_quote'] = '地方治理'
        a = self.grade(item, self.transcript('地方治理受到中央控制。'))
        b = self.grade(item, self.transcript('地方治理与朝廷有关。'))
        self.assertEqual(a['suggested_total'], 1)
        self.assertIn('holistic_transcription_disagreement', hs.compare(a, b))
