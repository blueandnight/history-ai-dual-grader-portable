"""Isolated synthetic configuration failures; no provider calls or user data."""
from contextlib import closing
import json
import unittest
from urllib.request import urlopen
from unittest.mock import patch
import app
from tests.test_batch_grading_control import BatchControlTests


class ConfigurationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = BatchControlTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.sid = self.f.batch['items'][0]['submission_id']

    def test_batch_never_falls_back_to_demo_without_model(self):
        with closing(app.get_db()) as c:
            row=c.execute('SELECT s.*,a.question,a.standard_answer FROM submissions s JOIN assignments a ON a.id=s.assignment_id WHERE s.id=?',(self.sid,)).fetchone()
            questions=app.clean_sub_questions([{'id':'Q1','question':'合成题干','standard_answer':'合成答案','max_score':2,'criteria':[{'id':'P1','label':'合成要点','score':2}]}])
        result=app.grade_submission(row,questions,'',[app.DATA_DIR/'synthetic-unused.jpg'])
        self.assertEqual(result['mode'],'vision-model-error')
        self.assertEqual(result['diagnostics']['error_category'],'model_not_configured')
        self.assertEqual(result['diagnostics']['model_call_ms'],0)
        self.assertTrue(result['blocked'])
        unbatched=dict(row);unbatched['batch_id']=None
        self.assertEqual(app.grade_submission(unbatched,questions,'',[])['mode'],'rules-demo')
        self.f.no_model.assert_not_called()

    def seed_demo(self):
        with closing(app.get_db()) as c,c:
            c.execute("UPDATE submissions SET state='completed',result_json=?,ai_privacy_confirmed=1,privacy_status='no_identity_visible' WHERE id=?",(json.dumps({'mode':'rules-demo'}),self.sid))
            c.execute("UPDATE grading_batches SET status='paused',next_sequence=1,pause_sequence=1 WHERE id=?",(self.f.row['id'],))
            c.execute("UPDATE task_queue SET state='completed',attempt_count=1,updated_at='2000-01-01T00:00:00+00:00' WHERE submission_id=?",(self.sid,))

    def test_retry_requires_explicit_consent_config_and_original_slot(self):
        self.seed_demo()
        post=self.f.f.request_json_status
        self.assertEqual(post('/api/retry',{'submission_id':self.sid})[0],400)
        self.assertEqual(post('/api/retry',{'submission_id':self.sid,'confirm_model_retry':True})[0],400)
        with closing(app.get_db()) as c:
            self.assertEqual(c.execute('SELECT state FROM submissions WHERE id=?',(self.sid,)).fetchone()[0],'completed')
        with patch.object(app,'model_configured',return_value=True):
            self.assertEqual(post('/api/retry',{'submission_id':self.sid,'confirm_model_retry':True})[0],200)
        with closing(app.get_db()) as c:
            r=json.loads(c.execute('SELECT result_json FROM submissions WHERE id=?',(self.sid,)).fetchone()[0])
            self.assertTrue(r['model_required']);self.assertNotIn('dual_review_requested',r)
            self.assertEqual(c.execute('SELECT next_sequence FROM grading_batches WHERE id=?',(self.f.row['id'],)).fetchone()[0],1)
        self.f.no_model.assert_not_called()

    def test_dual_intent_survives_processing_and_retry_without_marker(self):
        self.seed_demo()
        with closing(app.get_db()) as c,c:
            c.execute("UPDATE submissions SET result_json=? WHERE id=?",(json.dumps({'mode':'rules-demo','dual_review':{'results':{}}}),self.sid))
        with patch.object(app,'load_dual_review_model_configs',return_value={'A':{},'B':{}}) as configs:
            self.assertEqual(self.f.f.request_json_status('/api/retry',{'submission_id':self.sid,'confirm_model_retry':True})[0],200)
            configs.assert_called_once()
        with patch.object(app,'grade_submission',return_value=self.f.f.mock_result('vision-model-error')):
            app.process_submission(self.sid)
        with closing(app.get_db()) as c:
            r=json.loads(c.execute('SELECT result_json FROM submissions WHERE id=?',(self.sid,)).fetchone()[0])
            self.assertTrue(r['dual_review_requested'])
        self.f.no_model.assert_not_called()

    def test_old_demo_is_listed_as_recovery_with_no_ai_score(self):
        self.seed_demo()
        with urlopen(self.f.f.base_url+'/api/batch-adoption?assignment_id='+self.f.row['assignment_id']+'&batch_id='+self.f.row['id']) as response:
            p=next(p for p in json.load(response)['papers'] if p['id']==self.sid)
        self.assertEqual(p['progress_state'],'recovery')
        self.assertTrue(p['model_issues']['A'])
        self.assertNotEqual(p['state'],'adoptable')
        with urlopen(self.f.f.base_url+'/api/recovery-submissions') as response:
            r=next(p for p in json.load(response)['submissions'] if p['id']==self.sid)
        self.assertTrue(r['retryable'])
        self.f.no_model.assert_not_called()
