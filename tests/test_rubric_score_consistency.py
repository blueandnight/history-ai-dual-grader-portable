"""Teacher-confirmed weights/caps must survive the entire scoring contract."""
import copy
from contextlib import closing
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

import app
from auto_crop_double_review_poc import rubric_readiness
from tests.test_exam_import_ui import import_draft, confirmation_for
from tests.test_service_control import isolated_mobile_test_servers, request_json


def question(mode='weighted', maximum=8, values=(1, 3), point_value=2):
    return {'id': 'Q1', 'label': '1', 'question': '合成设问', 'standard_answer': '合成参考',
            'scoring_mode': mode, 'max_score': maximum, 'point_value': point_value,
            'criteria': [{'id': f'C{i}', 'label': f'要点{i}', 'score': value}
                         for i, value in enumerate(values, 1)]}


def decisions(q, hits):
    return [{'question_id': q['id'], 'rubric_point_id': p['id'],
             'decision': 'full' if i in hits else 'none'} for i, p in enumerate(q['criteria'])]


def model_output(q, hits):
    return {'mode': 'vision-model', 'confidence': .95, 'sub_questions': [{
        'sub_question_id': q['id'], 'answer_status': 'answered',
        'criteria': [{'criterion_id': p['id'], 'decision': 'full' if i in hits else 'none',
                      'suggested_score': p['score'] if i in hits else 0,
                      'student_evidence': f'独立学生手写内容{i}' if i in hits else '',
                      'confidence': .95, 'needs_review': False,
                      'evidence': {'photo_index': 1, 'x': .1, 'y': .1 + .05*i,
                                   'width': .2, 'height': .04} if i in hits else None}
                     for i, p in enumerate(q['criteria'])]}]}


def readiness(q):
    return rubric_readiness([q], teacher_confirmation_status='confirmed_by_teacher',
        teacher_confirmed_at='2026-10-19T00:00:00Z', required_question_ids=[q['id']])


class RubricScoreConsistencyTests(unittest.TestCase):
    def test_main_import_keeps_teacher_one_three_weights_as_weighted(self):
        draft = import_draft()
        draft['material_questions'][0]['sub_questions'][0]['scoring_mode'] = 'point_bank'
        _, saved, _ = app._confirmed_import_payload(draft, confirmation_for(draft, point_scores=('1', '3')))
        self.assertEqual(saved[0]['scoring_mode'], 'weighted')
        self.assertEqual([p['score'] for p in saved[0]['criteria']], [1, 3])
        self.assertEqual(app.clean_sub_questions(saved), saved)
        self.assertEqual(app.rubric_fingerprint(app.clean_sub_questions(saved)), app.rubric_fingerprint(saved))
        for hits, score in [({0}, 1), ({1}, 3), ({0, 1}, 4), (set(), 0)]:
            self.assertEqual(app.normalize_sub_question_result(model_output(saved[0], hits), saved)['suggested_total'], score)
            self.assertEqual(app.teacher_deterministic_score(saved, decisions(saved[0], hits))['final_total'], score)

    def test_explicit_weighted_caps_are_preserved_and_scoring_is_consistent(self):
        for maximum in (3, 4, 8):
            q = question(maximum=maximum)
            normalized = app.clean_sub_questions([q])
            self.assertEqual(normalized[0]['max_score'], maximum)
            for hits in ({0}, {1}, {0, 1}, set()):
                expected = min(sum(p['score'] for i, p in enumerate(q['criteria']) if i in hits), maximum)
                self.assertEqual(app.normalize_sub_question_result(model_output(q, hits), normalized)['suggested_total'], expected)
                self.assertEqual(app.teacher_deterministic_score([q], decisions(q, hits))['final_total'], expected)
            if maximum != 4:
                self.assertIn('rubric_sum_mismatch:Q1', readiness(q)[1])

    def test_inconsistent_bank_is_rejected_instead_of_rewritten_or_confirmed(self):
        q = question('point_bank', 4)
        before = copy.deepcopy(q)
        for operation in (lambda: app.clean_sub_questions([q]),
                          lambda: app.normalize_sub_question_result(model_output(q, {0}), [q]),
                          lambda: app.teacher_deterministic_score([q], decisions(q, {0}))):
            with self.assertRaisesRegex(ValueError, 'rubric_point_value_mismatch'):
                operation()
        self.assertFalse(readiness(q)[0])
        self.assertTrue(any(reason.startswith('rubric_point_value_mismatch:Q1') for reason in readiness(q)[1]))
        self.assertEqual(q, before)

    def test_uniform_sixteen_point_bank_still_caps_both_calculators_at_eight(self):
        q = question('point_bank', 8, (2,) * 8)
        normalized = app.clean_sub_questions([q])
        self.assertTrue(readiness(q)[0])
        for mask in range(256):
            hits = {i for i in range(8) if mask & (1 << i)}
            expected = min(len(hits)*2, 8)
            self.assertEqual(app.normalize_sub_question_result(model_output(q, hits), normalized)['suggested_total'], expected)
            self.assertEqual(app.teacher_deterministic_score([q], decisions(q, hits))['final_total'], expected)

    def test_legacy_mode_missing_and_explicit_invalid_caps_remain_distinct(self):
        legacy = question(maximum=999)
        legacy.pop('scoring_mode')
        self.assertEqual(app.clean_sub_questions([legacy])[0]['max_score'], 4)
        for invalid in (0, -1, float('nan'), float('inf')):
            with self.assertRaisesRegex(ValueError, '满分'):
                app.clean_sub_questions([question(maximum=invalid)])

    def test_declared_max_and_weights_survive_confirmation_history_and_csv(self):
        with tempfile.TemporaryDirectory() as td, isolated_mobile_test_servers(Path(td)) as (port, _):
            q = question(maximum=8)
            saved = app.clean_sub_questions([q])
            result = app.normalize_sub_question_result(model_output(q, {0, 1}), saved)
            result['grading_scope'] = {'graded_question_ids': ['Q1'], 'scoped_max_score': 8, 'source': 'explicit'}
            with closing(app.get_db()) as conn:
                conn.execute('INSERT INTO assignments(id,name,question,standard_answer,criteria_json,sub_questions_json,created_at) VALUES(?,?,?,?,?,?,?)',
                    ('a'*12, '合成分值一致性', '合成', '合成', '[]', json.dumps(saved), app.utc_now()))
                conn.execute('INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state,source_channel,identity_status,student_name) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    ('b'*12, 'a'*12, '合成学生', '', '[]', json.dumps(result), app.utc_now(), 'completed', 'computer', 'confirmed', '合成学生'))
                conn.commit()
            choices = [{'question_id': 'Q1', 'rubric_point_id': p['id'], 'choice': 'teacher_full'} for p in q['criteria']]
            with patch.object(app, 'open_model_request', side_effect=AssertionError('No real provider')) as provider:
                with self.assertRaises(HTTPError):
                    request_json(port, '/api/confirm', 'POST', payload={'submission_id': 'b'*12,
                        'teacher_point_decisions': choices, 'final_total': 8})
                confirmation = request_json(port, '/api/confirm', 'POST', payload={
                    'submission_id': 'b'*12, 'teacher_point_decisions': choices})
                self.assertEqual(confirmation['final_total'], 4)
                history = request_json(port, '/api/submission?submission_id='+'b'*12)['submission']
                self.assertEqual(history['final_total'], 4)
                self.assertEqual(history['result']['max_total'], 8)
                self.assertEqual([p['max_score'] for p in history['result']['sub_questions'][0]['criteria']], [1, 3])
                with urlopen(f'http://127.0.0.1:{port}/api/export.csv?assignment_id='+'a'*12) as response:
                    rows = list(csv.DictReader(io.StringIO(response.read().decode('utf-8-sig'))))
                self.assertEqual(float(rows[0]['最终确认分']), 4)
                self.assertEqual(float(rows[0]['AI建议总分']), 4)
                self.assertEqual(float(rows[0]['满分']), 8)
                self.assertEqual(float(rows[0]['1 AI建议分']), 4)
                provider.assert_not_called()
            with closing(app.get_db()) as conn:
                self.assertEqual(json.loads(conn.execute('SELECT sub_questions_json FROM assignments').fetchone()[0]), saved)

    def test_api_rejects_bad_bank_without_rewriting_saved_rubric_or_final(self):
        with tempfile.TemporaryDirectory() as td, isolated_mobile_test_servers(Path(td)) as (port, _):
            q = question('point_bank', 4)
            with self.assertRaises(HTTPError) as rejected:
                request_json(port, '/api/assignments', 'POST', payload={
                    'name': '合成异常点池', 'question': '合成', 'sub_questions': [q]})
            self.assertEqual(rejected.exception.code, 400)
            # A historical inconsistent rubric is readable but cannot acquire a
            # new confirmed final by silently changing teacher-saved weights.
            result = {'mode': 'vision-model', 'suggested_total': 2, 'max_total': 4,
                      'blocked': False, 'sub_questions': []}
            with closing(app.get_db()) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM assignments').fetchone()[0], 0)
                conn.execute('INSERT INTO assignments(id,name,question,standard_answer,criteria_json,sub_questions_json,created_at) VALUES(?,?,?,?,?,?,?)',
                    ('a'*12, '合成旧点池', '合成', '合成', '[]', json.dumps([q]), app.utc_now()))
                conn.execute('INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state,source_channel) VALUES(?,?,?,?,?,?,?,?,?)',
                    ('b'*12, 'a'*12, '合成', '', '[]', json.dumps(result), app.utc_now(), 'completed', 'computer'))
                conn.commit()
            with self.assertRaises(HTTPError) as rejected:
                request_json(port, '/api/confirm', 'POST', payload={'submission_id': 'b'*12,
                    'teacher_point_decisions': [{'question_id': 'Q1', 'rubric_point_id': p['id'],
                        'choice': 'teacher_full'} for p in q['criteria']]})
            self.assertEqual(rejected.exception.code, 400)
            with closing(app.get_db()) as conn:
                self.assertEqual(json.loads(conn.execute('SELECT sub_questions_json FROM assignments').fetchone()[0]), [q])
                row = conn.execute('SELECT final_total,confirmed_at,result_json FROM submissions').fetchone()
                self.assertIsNone(row['final_total'])
                self.assertIsNone(row['confirmed_at'])
                self.assertEqual(json.loads(row['result_json']), result)
