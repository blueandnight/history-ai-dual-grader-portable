"""Synthetic persisted queue tests, including stop during an in-flight mock call."""
from contextlib import closing
import json
import threading
import unittest
from unittest.mock import patch

import app
from tests.test_batch_order import OrderedBatchTests
from batch_grading_control import stopped


class BatchControlTests(unittest.TestCase):
    def setUp(self):
        self.f = OrderedBatchTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.no_model = self.enterContext(patch.object(app, 'open_model_request', side_effect=AssertionError('no real model')))
        self.enterContext(patch.object(app, 'model_configured', return_value=False))
        self.enqueue = self.enterContext(patch.object(app, 'enqueue_submission', return_value=True))
        self.batch = self.f.make_batch(['合成甲', '合成乙', '合成丙'])
        self.f.upload_batch(self.batch)
        with closing(app.get_db()) as c:
            self.row = c.execute('SELECT * FROM grading_batches WHERE id=?', (self.batch['batch_id'],)).fetchone()

    def control(self, action='stop', **kwargs):
        body = dict(batch_id=self.row['id'], assignment_id=self.row['assignment_id'], exam_id=self.row['exam_id'] or '', action=action, confirmed=True)
        body.update(kwargs)
        return self.f.request_json_status('/api/batches/grading-control', body)

    def test_stop_persists_prevents_model_even_if_stale_memory_queue_and_resumes(self):
        with closing(app.get_db()) as c:
            before = [tuple(r) for r in c.execute('SELECT id,photos_json,result_json,final_total FROM submissions ORDER BY id')]
        self.assertEqual(self.control()[0], 200)
        self.assertEqual(self.control()[0], 200)
        with patch.object(app, 'grade_submission') as grade:
            for item in self.batch['items']:
                app.process_submission(item['submission_id'])
            grade.assert_not_called()
        with closing(app.get_db()) as c:
            self.assertTrue(stopped(c.execute('SELECT * FROM grading_batches').fetchone()))
            app.recover_interrupted_grade_tasks(c)
            self.assertIsNone(app._enqueue_current_batch_submission(c, self.row['id']))
            self.assertEqual(before, [tuple(r) for r in c.execute('SELECT id,photos_json,result_json,final_total FROM submissions ORDER BY id')])
        self.assertEqual(self.control('resume')[0], 200)
        with patch.object(app, 'grade_submission', return_value=self.f.mock_result()) as grade:
            app.process_submission(self.batch['items'][0]['submission_id'])
            self.assertEqual(grade.call_count, 1)
        self.no_model.assert_not_called()

    def test_inflight_finishes_but_never_starts_next_until_explicit_resume(self):
        entered, release = threading.Event(), threading.Event()
        def grade(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return self.f.mock_result()
        with patch.object(app, 'grade_submission', side_effect=grade):
            worker = threading.Thread(target=app.process_submission, args=(self.batch['items'][0]['submission_id'],))
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                status, result = self.control()
                self.assertEqual(status, 200, result)
                self.assertEqual(result['running'], 1)
                self.assertEqual(self.control('resume')[0], 400)
            finally:
                release.set()
                worker.join(10)
        with closing(app.get_db()) as c:
            batch = c.execute('SELECT * FROM grading_batches').fetchone()
            self.assertTrue(stopped(batch)); self.assertEqual(batch['status'], 'paused'); self.assertEqual(batch['next_sequence'], 2)
            first = c.execute('SELECT state,result_json FROM submissions WHERE id=?', (self.batch['items'][0]['submission_id'],)).fetchone()
            self.assertEqual(first['state'], 'completed')
            self.assertEqual(json.loads(first['result_json'])['suggested_total'], 2)
        with patch.object(app, 'grade_submission') as grade:
            app.process_submission(self.batch['items'][1]['submission_id'])
            grade.assert_not_called()
        self.assertEqual(self.control('resume')[0], 200)
        with patch.object(app, 'grade_submission', return_value=self.f.mock_result()) as grade:
            app.process_submission(self.batch['items'][0]['submission_id'])
            grade.assert_not_called()
            app.process_submission(self.batch['items'][1]['submission_id'])
            self.assertEqual(grade.call_count, 1)

    def test_scope_confirmation_and_bypass_guards(self):
        for bad in ({'confirmed':False}, {'assignment_id':'f'*12}, {'exam_id':'f'*32}, {'batch_id':'f'*32}):
            self.assertEqual(self.control(**bad)[0], 400)
        self.assertEqual(self.control()[0], 200)
        first = self.batch['items'][0]
        self.assertEqual(self.f.request_json_status('/api/batches/skip', dict(batch_id=self.row['id'], sequence=1, submission_id=first['submission_id'], confirm_skip=True))[0], 400)
        self.assertEqual(self.f.request_json_status('/api/retry', {'submission_id':first['submission_id']})[0], 400)
        # The list distinguishes stopped waiting papers without inventing a score.
        from urllib.request import urlopen
        with urlopen(self.f.base_url+'/api/assignment-submissions?assignment_id='+self.row['assignment_id']) as response:
            rows = json.load(response)['submissions']
        self.assertTrue(all(r['grading_stopped'] for r in rows))
        self.assertTrue(all(r['suggested_total'] is None for r in rows))

    def test_stop_during_failed_request_keeps_recovery_gate(self):
        def fail(*args, **kwargs):
            self.assertEqual(self.control()[0], 200)
            return self.f.mock_result('vision-model-error')
        with patch.object(app, 'grade_submission', side_effect=fail):
            app.process_submission(self.batch['items'][0]['submission_id'])
        self.assertEqual(self.control('resume')[0], 200)
        with closing(app.get_db()) as c:
            batch = c.execute('SELECT * FROM grading_batches').fetchone()
            self.assertFalse(stopped(batch)); self.assertEqual(batch['status'], 'paused'); self.assertEqual(batch['next_sequence'], 1)
            self.assertIsNone(app._enqueue_current_batch_submission(c, batch['id']))

    def test_new_upload_is_blocked_but_existing_idempotent_upload_stays_readable(self):
        other = self.f.make_batch(['合成待上传'])
        with closing(app.get_db()) as c:
            self.row = c.execute('SELECT * FROM grading_batches WHERE id=?', (other['batch_id'],)).fetchone()
        self.assertEqual(self.control()[0], 200)
        status, _ = self.f.request_json_status('/api/submissions', self.f.submission_payload(other, 0))
        self.assertEqual(status, 400)
        with closing(app.get_db()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM submissions WHERE batch_id=?', (other['batch_id'],)).fetchone()[0], 0)
        self.assertEqual(self.control('resume')[0], 200)
        self.assertEqual(self.f.request_json_status('/api/submissions', self.f.submission_payload(other, 0))[0], 202)
        self.assertEqual(self.control()[0], 200)
        self.assertEqual(self.f.request_json_status('/api/submissions', self.f.submission_payload(other, 0))[0], 200)


class BatchControlAuthTests(unittest.TestCase):
    def test_existing_http_authentication_and_csrf_guards(self):
        from tests.test_teacher_accounts import TeacherAccountTests
        f = TeacherAccountTests(); f.setUp()
        try:
            body = dict(batch_id='f'*32, assignment_id='a'*12, exam_id='', confirmed=True, action='stop')
            self.assertIn(f.request_json('POST', '/api/batches/grading-control', body)[0], (401, 403))
            headers = f.admin_headers()
            self.assertEqual(f.request_json('POST', '/api/batches/grading-control', body, {k:v for k,v in headers.items() if k != 'X-CSRF-Token'})[0], 403)
            self.assertEqual(f.request_json('POST', '/api/batches/grading-control', body, headers)[0], 400)
            f.create_teacher(headers)
            self.assertEqual(f.request_json('POST', '/api/batches/grading-control', body, f.teacher_headers())[0], 400)
            self.assertNotIn('/api/batches/grading-control', app.AppHandler.MOBILE_WRITE_PATHS)
        finally:
            f.tearDown()
