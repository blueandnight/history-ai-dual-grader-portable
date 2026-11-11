"""Regression for off-topic answers and invented evidence, using synthetic text."""
import copy
import hashlib
import json
import unittest

import app
import holistic_scoring as hs
from open_response_grounding import LEGACY_PROTOCOL, REFERENCE_PROTOCOL, normalize_grounded, transcriptions
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

    def test_reference_response_uses_original_text_and_valid_project_choices(self):
        item = copy.deepcopy(self.item)
        item.pop('suggested_total')
        for dimension in item['dimension_assessments']:
            dimension.pop('level_id')
        item['problem_flags'].pop('factual_error')
        item['problem_flags'].pop('task_constraint_violation')
        item.pop('critical_factual_issues')
        item['problem_flags']['off_topic'] = False
        item['dimension_assessments'][0].update(score=1, judgment='met')
        evidence = item['evidence'][0]
        evidence.pop('student_quote')
        evidence.update(start=0, end=4)
        item['fact_checks'] = [dict(judgment='correct', reason='按原文核对', evidence_ids=[evidence['id']])]

        def grade(raw):
            return normalize_grounded({'sub_questions': [raw]}, [self.question], self.transcript(), 1,
                                      protocol=REFERENCE_PROTOCOL)['sub_questions'][0]

        score = grade(item)
        saved = score['holistic_result']
        self.assertTrue(hs.valid_sub(score, self.question))
        self.assertEqual(score['suggested_total'], 1)
        self.assertEqual(saved['dimension_assessments'][0]['level_id'], 'range')
        self.assertEqual(saved['evidence'][0]['student_quote'], self.text[:4])
        self.assertEqual(saved['fact_checks'][0]['student_quote'], self.text[:4])
        self.assertFalse(saved['problem_flags']['factual_error'])

        for field, bad in (
            ('span', len(self.text) + 1), ('evidence_id', ['unknown']),
            ('score', .5), ('score', 3), ('level', 'unknown'),
        ):
            with self.subTest(field=field, value=bad):
                invalid = copy.deepcopy(item)
                if field == 'span':
                    invalid['evidence'][0]['end'] = bad
                elif field == 'evidence_id':
                    invalid['fact_checks'][0]['evidence_ids'] = bad
                else:
                    invalid['dimension_assessments'][0]['level_id' if field == 'level' else 'score'] = bad
                self.assertEqual(grade(invalid)['answer_status'], 'scoring_invalid')

        item['fact_checks'][0]['judgment'] = 'incorrect'
        disputed = grade(item)
        self.assertEqual(disputed['suggested_total'], 1)
        self.assertTrue(disputed['blocked'])
        self.assertFalse(hs.valid_sub(disputed, self.question))
        item['factual_status'] = 'issues'
        supported = grade(item)['holistic_result']
        self.assertEqual(supported['critical_factual_issues'], ['按原文核对'])
        self.assertTrue(supported['problem_flags']['factual_error'])

    def test_new_preview_publishes_allowed_choices_and_accepts_existing_signed_plan(self):
        for questions in ([self.question], fixtures.project_questions()):
            args = ('合成考试', questions)
            legacy = app.build_scoring_prompt_text(*args, privacy_mode=True, grounding_protocol=LEGACY_PROTOCOL)
            current = app.build_scoring_prompt_text(*args, privacy_mode=True)
            self.assertNotEqual(legacy, current)
            plan = json.loads(current)
            if 'requests' in plan:
                plan = json.loads(plan['requests'][0]['prompt'])
            self.assertEqual(plan['protocol'], REFERENCE_PROTOCOL)
            header, body = plan['scoring_prompt'].split('\n', 1)
            self.assertIn('不要抄写student_quote', header)
            self.assertIn('程序根据分数匹配唯一档次并求和', header)
            self.assertNotIn('problem_flags必须包含factual_error', header)
            self.assertNotIn('critical_factual_issues', header)
            settings, _ = json.JSONDecoder().raw_decode(body)
            first = settings['questions'][0]['projects'][0]['levels'][0]
            self.assertEqual(first['allowed_scores'], [0, 1])
            for preview, protocol in ((legacy, LEGACY_PROTOCOL), (current, REFERENCE_PROTOCOL)):
                expected = {hashlib.sha256(preview.encode('utf-8')).hexdigest()}
                self.assertEqual(app._approved_scoring_prompt(expected, *args, privacy_mode=True), (preview, protocol))
            with self.assertRaisesRegex(ValueError, '偏离'):
                app._approved_scoring_prompt({'f' * 64}, *args, privacy_mode=True)

    def test_reference_flag_list_is_review_only_and_preserves_off_topic_gate(self):
        item = copy.deepcopy(self.item)
        item['problem_flags'] = ['unsupported_argument']
        item['dimension_assessments'][0].update(score=1, judgment='met')
        item['suggested_total'] = 1

        def grade():
            return normalize_grounded({'sub_questions': [item]}, [self.question], self.transcript(), 1,
                                      protocol=REFERENCE_PROTOCOL)['sub_questions'][0]

        result = grade()
        self.assertEqual(result['suggested_total'], 1)
        self.assertTrue(result['blocked'])
        self.assertFalse(hs.valid_sub(result, self.question))
        for flags, off_topic in (([], True), (['off_topic'], False), (['unknown'], False)):
            with self.subTest(flags=flags, off_topic=off_topic):
                item.update(problem_flags=flags, off_topic=off_topic)
                self.assertEqual(grade()['answer_status'], 'scoring_invalid')
