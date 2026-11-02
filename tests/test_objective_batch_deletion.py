"""Atomic batch cleanup of synthetic objective jobs, retaining shared authority."""
from contextlib import closing
import hashlib
import json
import unittest
import uuid
from unittest.mock import patch
from PIL import Image
import app
from objective import app as engine
from tests.test_objective_job_deletion import ObjectiveJobDeletionTests
from tests.test_unified_scores import seed_answers


class BatchDeletionTests(unittest.TestCase):
    def setUp(self):
        self.f=ObjectiveJobDeletionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.client=self.f.client;self.items=[self.item(self.f.job)];self.directories=[]
        with closing(app.get_db()) as conn,conn:
            prototype=dict(conn.execute('SELECT * FROM objective_jobs WHERE job_id=?',(self.f.job,)).fetchone())
            for i in range(19):
                jid=uuid.uuid4().hex;sid=uuid.uuid4().hex
                job={**prototype,'job_id':jid,'total_files':1,'total_submissions':1,'processed_submissions':1,'complete_submissions':1}
                conn.execute('INSERT INTO objective_jobs('+','.join(job)+') VALUES('+','.join('?' for _ in job)+')',list(job.values()))
                conn.execute("INSERT INTO objective_submissions(submission_id,job_id,assessment_id,anonymous_code,state,score,max_score,page_count,created_at) VALUES(?,?,?,?,'completed',0,60,1,?)",(sid,jid,self.f.key,'SYNTHETIC-'+str(i),app.utc_now()))
                seed_answers(conn,sid,zero=True)
                directory=app.DATA_DIR/'objective'/'uploads'/self.f.key/sid;directory.mkdir(parents=True)
                path=directory/'synthetic.png';Image.new('RGB',(100,100),'white').save(path)
                conn.execute("INSERT INTO objective_submission_pages(page_id,submission_id,page_no,stored_path,image_sha256,byte_count,width,height,recognition_state) VALUES(?,?,1,?,?,?,100,100,'completed')",(uuid.uuid4().hex,sid,str(path),hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_size))
                self.items.append(self.item(jid));self.directories.append(directory)
            for sid in self.f.f.obj:self.directories.append(app.DATA_DIR/'objective'/'uploads'/self.f.key/sid)

    def item(self,jid):return dict(job_id=jid,assessment_id=self.f.key,exam_id=self.f.f.exam)

    def preview(self,items=None,client=None):
        return (client or self.client).post('/api/jobs/batch-delete-preview',json={'items':self.items if items is None else items})

    def remove(self,preview=None,**changes):
        p=preview or self.preview().get_json()
        body=dict(items=self.items,confirmed=True,expected_fingerprint=p['fingerprint']);body.update(changes)
        return self.client.post('/api/jobs/batch-delete',json=body,environ_overrides={'history_grader.actor':'synthetic_teacher'})

    def test_twenty_jobs_one_request_retains_unselected_and_subjective(self):
        before=self.f.snapshot();response=self.preview();self.assertEqual(response.status_code,200,response.get_json());p=response.get_json()
        self.assertEqual(before,self.f.snapshot());self.assertEqual((p['jobs'],p['submissions'],p['photos'],p['links']),(20,21,21,1))
        with closing(app.get_db()) as conn:
            keep={t:[tuple(r) for r in conn.execute('SELECT * FROM '+t)] for t in ('assignments','grading_exams','submissions','objective_assessments','objective_omr_templates')}
            other=tuple(conn.execute('SELECT * FROM objective_submissions WHERE submission_id=?',(self.f.other,)).fetchone())
            ddl=[tuple(r) for r in conn.execute('SELECT * FROM sqlite_master ORDER BY name')]
        response=self.remove(p,items=list(reversed(self.items)));self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(response.get_json()['jobs'],20);self.assertEqual(set(response.get_json()['deleted_job_ids']),{i['job_id'] for i in self.items})
        with closing(app.get_db()) as conn:
            for table,values in keep.items():self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM '+table)],values,table)
            self.assertEqual(tuple(conn.execute('SELECT * FROM objective_submissions WHERE submission_id=?',(self.f.other,)).fetchone()),other)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM objective_jobs').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM exam_answer_links').fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM objective_audit_events WHERE event_type='objective_job_deleted'").fetchone()[0],20)
            audit=json.loads(conn.execute("SELECT details_json FROM objective_audit_events WHERE event_type='objective_jobs_batch_deleted'").fetchone()[0])
            self.assertEqual(audit['actor'],'synthetic_teacher');self.assertEqual(audit['counts']['jobs'],20)
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM sqlite_master ORDER BY name')],ddl)
        self.assertTrue(all(not d.exists() for d in self.directories))
        self.assertTrue((app.DATA_DIR/'objective'/'uploads'/self.f.key/self.f.other).exists())
        rows=self.client.get('/api/unified-scores?exam_id='+self.f.f.exam).get_json()['rows']
        self.assertFalse(any(r['link_id'] for r in rows));self.assertEqual({r['subjective']['id'] for r in rows if r['subjective']},set(self.f.f.sub))
        self.assertEqual(self.remove(p).status_code,404)

    def test_ten_selected_do_not_delete_other_ten_or_keep_job(self):
        items=self.items[:10];p=self.preview(items).get_json();r=self.remove(p,items=items)
        self.assertEqual(r.status_code,200,r.get_json());self.assertEqual(r.get_json()['jobs'],10)
        with closing(app.get_db()) as conn:
            remaining={r[0] for r in conn.execute('SELECT job_id FROM objective_jobs')}
        self.assertEqual(remaining,{i['job_id'] for i in self.items[10:]}|{self.f.other_job})

    def test_invalid_selection_confirmation_and_stale_range_are_atomic(self):
        p=self.preview().get_json();before=self.f.snapshot()
        for items in ([],self.items*6,[self.items[0],self.items[0]],[{**self.items[-1],'assessment_id':'f'*32}],[{**self.items[-1],'exam_id':'f'*32}],[{**self.items[-1],'job_id':None}]):
            self.assertIn(self.preview(items).status_code,(400,403,404))
        for changes in ({'confirmed':False},{'expected_fingerprint':'0'*64},{'items':self.items[:-1]}):
            self.assertEqual(self.remove(p,**changes).status_code,400)
        self.assertEqual(before,self.f.snapshot())
        with closing(app.get_db()) as conn,conn:conn.execute('UPDATE objective_submissions SET score=1 WHERE job_id=?',(self.items[-1]['job_id'],))
        before=self.f.snapshot();self.assertEqual(self.remove(p).status_code,400);self.assertEqual(before,self.f.snapshot())

    def test_active_job_or_worker_blocks_entire_selection(self):
        jid=self.items[-1]['job_id']
        with closing(app.get_db()) as conn,conn:conn.execute("UPDATE objective_jobs SET status='processing' WHERE job_id=?",(jid,))
        p=self.preview().get_json();self.assertTrue(p['blocked']);before=self.f.snapshot();self.assertEqual(self.remove(p).status_code,400);self.assertEqual(before,self.f.snapshot())
        self.assertFalse(next(j for j in self.client.get('/api/jobs?exam_id='+self.f.f.exam).get_json()['jobs'] if j['job_id']==jid)['deletable'])
        with closing(app.get_db()) as conn,conn:conn.execute("UPDATE objective_jobs SET status='completed' WHERE job_id=?",(jid,))
        class Worker:
            def is_alive(self):return True
        with patch.dict(engine._JOB_BY_ID,{jid:Worker()}):
            p=self.preview().get_json();self.assertTrue(p['blocked']);self.assertEqual(self.remove(p).status_code,400)
        with closing(app.get_db()) as conn,conn:conn.execute("UPDATE objective_submissions SET state='queued' WHERE job_id=?",(jid,))
        self.assertTrue(self.preview().get_json()['blocked'])

    def test_last_task_foreign_path_preflight_prevents_any_deletion(self):
        with closing(app.get_db()) as conn,conn:
            foreign=conn.execute('SELECT stored_path FROM objective_submission_pages WHERE submission_id=?',(self.f.other,)).fetchone()[0]
            conn.execute('UPDATE objective_submission_pages SET stored_path=? WHERE submission_id IN (SELECT submission_id FROM objective_submissions WHERE job_id=?)',(foreign,self.items[-1]['job_id']))
        before=self.f.snapshot();self.assertEqual(self.remove().status_code,400);self.assertEqual(before,self.f.snapshot())
        self.assertTrue(all(d.exists() for d in self.directories))

    def test_batch_audit_failure_rolls_back_all_database_deletes_before_cleanup(self):
        original=engine._insert_audit
        def failing(conn,event,**kwargs):
            if event=='objective_jobs_batch_deleted':raise ValueError('synthetic audit failure')
            return original(conn,event,**kwargs)
        p=self.preview().get_json();before=self.f.snapshot()
        with patch.object(engine,'_insert_audit',side_effect=failing):self.assertEqual(self.remove(p).status_code,400)
        self.assertEqual(before,self.f.snapshot());self.assertTrue(all(d.exists() for d in self.directories))

    def test_cleanup_warning_and_unavailable_scope(self):
        isolated=engine.create_app(data_dir=app.DATA_DIR/'objective',database_path=app.DB_PATH,upload_dir=app.DATA_DIR/'objective'/'uploads',auth_enabled=False,initialize_schema=False,source_exam_provider=lambda e=None:[]).test_client()
        before=self.f.snapshot();self.assertEqual(self.preview(client=isolated).status_code,403);self.assertEqual(before,self.f.snapshot())
        with patch('objective.job_deletion.shutil.rmtree',side_effect=OSError('synthetic cleanup failure')):
            response=self.remove();self.assertEqual(response.status_code,200,response.get_json());self.assertTrue(response.get_json()['warning'])
        self.assertTrue(all(d.exists() for d in self.directories))
