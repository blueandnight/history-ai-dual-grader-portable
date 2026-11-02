"""Read-only task discovery and scoping against isolated synthetic records."""
from contextlib import closing
import unittest
import app
from tests import test_answer_links as links


class ObjectiveTaskTests(unittest.TestCase):
    def setUp(self):
        self.f=links.AnswerLinkTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.client=self.f.client
        with closing(app.get_db()) as c:
            self.job,self.key=c.execute('SELECT job_id,assessment_id FROM objective_jobs LIMIT 1').fetchone()

    def snapshot(self):
        with closing(app.get_db()) as c:
            return {t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in ('objective_jobs','objective_submissions','objective_assessments','objective_answers','objective_audit_events')}

    def test_list_restore_and_export_never_write_or_regrade(self):
        before=self.snapshot()
        for query in ('assessment_id='+self.key,'exam_id='+self.f.exam,'exam_id='+self.f.exam+'&assessment_id='+self.key):
            r=self.client.get('/api/jobs?'+query);self.assertEqual(r.status_code,200,r.get_json())
            task=r.get_json()['jobs'][0];self.assertEqual(task['job_id'],self.job);self.assertEqual(task['assessment_id'],self.key)
            self.assertNotIn('stored_path',task);self.assertNotIn('anonymous_code',task)
        r=self.client.get('/api/jobs/'+self.job+'?assessment_id='+self.key)
        self.assertEqual(r.status_code,200);self.assertEqual(len(r.get_json()['submissions']),2)
        rows=self.client.get('/api/submissions?assessment_id='+self.key).get_json()['submissions']
        self.assertTrue(all(r['job_id']==self.job for r in rows))
        r=self.client.get('/api/export.csv?assessment_id='+self.key+'&job_id='+self.job)
        self.assertEqual(r.status_code,200);self.assertEqual(len(r.data.decode('utf-8-sig').splitlines()),3)
        self.assertEqual(self.snapshot(),before)

    def test_wrong_exam_key_and_task_cannot_be_silently_restored(self):
        before=self.snapshot()
        for path in ('/api/jobs','/api/jobs?exam_id='+'f'*32,'/api/jobs?assessment_id='+'f'*32,'/api/jobs?assessment_id='+self.key+'&exam_id='+'f'*32,'/api/jobs/'+self.job+'?assessment_id='+'f'*32,'/api/export.csv?assessment_id='+self.key+'&job_id='+'f'*32):
            self.assertIn(self.client.get(path).status_code,(400,403,404,409),path)
        self.assertEqual(self.snapshot(),before)

    def test_task_export_excludes_other_upload_groups(self):
        with closing(app.get_db()) as c,c:
            second='d'*32
            c.execute("INSERT INTO objective_jobs(job_id,assessment_id,mode,grouping,status,total_files,total_submissions,created_at) VALUES(?,?,'omr','one_per_image','completed',1,1,?)",(second,self.key,app.utc_now()))
            c.execute("INSERT INTO objective_submissions(submission_id,job_id,assessment_id,anonymous_code,state,score,max_score,page_count,created_at) VALUES(?,?,?,'SYNTHETIC-OTHER','completed',0,60,1,?)",('e'*32,second,self.key,app.utc_now()))
        before=self.snapshot()
        csv=self.client.get('/api/export.csv?assessment_id='+self.key+'&job_id='+self.job).data.decode('utf-8-sig')
        self.assertNotIn('SYNTHETIC-OTHER',csv)
        other=self.client.get('/api/export.csv?assessment_id='+self.key+'&job_id='+second).data.decode('utf-8-sig')
        self.assertIn('SYNTHETIC-OTHER,completed,0.0,',other)
        self.assertEqual(len(other.splitlines()),2)
        self.assertEqual(self.snapshot(),before)
