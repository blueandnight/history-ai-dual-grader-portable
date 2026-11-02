"""Synthetic objective task deletion; no production paths or real recognition."""
from contextlib import closing
import hashlib
import json
import unittest
import uuid
from unittest.mock import patch
from PIL import Image
import app
from objective import app as engine
from tests import test_answer_links as links
from tests.test_unified_scores import seed_answers


class ObjectiveJobDeletionTests(unittest.TestCase):
    def setUp(self):
        self.f=links.AnswerLinkTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.client=self.f.client;self.key='';self.other_job=uuid.uuid4().hex;self.other=uuid.uuid4().hex
        with closing(app.get_db()) as c,c:
            self.job,self.key=c.execute('SELECT job_id,assessment_id FROM objective_jobs LIMIT 1').fetchone()
            c.execute("UPDATE objective_jobs SET processed_submissions=2,complete_submissions=2 WHERE job_id=?",(self.job,))
            c.execute("INSERT INTO objective_jobs(job_id,assessment_id,mode,grouping,status,total_files,total_submissions,created_at) VALUES(?,?,'omr','one_per_image','completed',1,1,?)",(self.other_job,self.key,app.utc_now()))
            c.execute("INSERT INTO objective_submissions(submission_id,job_id,assessment_id,anonymous_code,state,score,max_score,page_count,created_at) VALUES(?,?,?,'SYNTHETIC-OTHER','completed',0,60,1,?)",(self.other,self.other_job,self.key,app.utc_now()))
            for sid in self.f.obj:seed_answers(c,sid)
            for sid in [*self.f.obj,self.other]:
                path=app.DATA_DIR/'objective'/'uploads'/self.key/sid/'synthetic.png';path.parent.mkdir(parents=True,exist_ok=True);Image.new('RGB',(100,100),'white').save(path)
                c.execute("INSERT INTO objective_submission_pages(page_id,submission_id,page_no,stored_path,image_sha256,byte_count,width,height,recognition_state) VALUES(?,?,1,?,?,?,100,100,'completed')",(uuid.uuid4().hex,sid,str(path),hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_size))
            c.execute('INSERT INTO exam_answer_links(id,exam_id,objective_submission_id,subjective_submission_id,method,reason,confirmed_at) VALUES(?,?,?,?,?,?,?)',(uuid.uuid4().hex,self.f.exam,self.f.obj[0],self.f.sub[0],'teacher','synthetic checked',app.utc_now()))
            c.execute("INSERT INTO objective_review_events(review_id,submission_id,question_no,revised_option,reason,created_at) VALUES(?,?,1,'A','synthetic checked',?)",(uuid.uuid4().hex,self.f.obj[0],app.utc_now()))
            engine._insert_audit(c,'synthetic_original_audit',job_id=self.job)
        self.models=self.enterContext(patch.object(app,'open_model_request',side_effect=AssertionError('no real model')))

    def preview(self,**kwargs):
        params=dict(assessment_id=self.key,exam_id=self.f.exam);params.update(kwargs)
        return self.client.get('/api/jobs/'+self.job+'/delete-preview',query_string=params)

    def remove(self,p=None,headers=None,**kwargs):
        p=p or self.preview().get_json();body=dict(assessment_id=self.key,exam_id=self.f.exam,confirmed=True,expected_fingerprint=p['fingerprint']);body.update(kwargs)
        return self.client.post('/api/jobs/'+self.job+'/delete',json=body,headers=headers or {},environ_overrides={'history_grader.actor':'synthetic_teacher'})

    def snapshot(self):
        with closing(app.get_db()) as c:return [line for line in c.iterdump() if not line.startswith('INSERT INTO "schema_meta"')]

    def test_delete_only_selected_objective_task_and_preserve_authority_and_subjective(self):
        before=self.snapshot();r=self.preview();self.assertEqual(r.status_code,200,r.get_json());p=r.get_json();self.assertEqual(before,self.snapshot())
        self.assertEqual((p['submissions'],p['scored'],p['photos'],p['links'],p['reviews']),(2,2,2,1,1))
        with closing(app.get_db()) as c:
            keep={t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in ('assignments','grading_exams','submissions','objective_assessments','objective_omr_templates')}
            other=tuple(c.execute('SELECT * FROM objective_submissions WHERE submission_id=?',(self.other,)).fetchone())
            ddl=[tuple(r) for r in c.execute('SELECT * FROM sqlite_master ORDER BY name')]
        r=self.remove(p);self.assertEqual(r.status_code,200,r.get_json());self.assertFalse(r.get_json()['warning'])
        with closing(app.get_db()) as c:
            for t,rows in keep.items():self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM '+t)],rows,t)
            self.assertEqual(tuple(c.execute('SELECT * FROM objective_submissions WHERE submission_id=?',(self.other,)).fetchone()),other)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM objective_jobs WHERE job_id=?',(self.job,)).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM exam_answer_links').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM objective_answers').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM objective_review_events').fetchone()[0],0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM objective_audit_events WHERE event_type='synthetic_original_audit'").fetchone()[0],1)
            audit=json.loads(c.execute("SELECT details_json FROM objective_audit_events WHERE event_type='objective_job_deleted'").fetchone()[0]);self.assertEqual(audit['actor'],'synthetic_teacher')
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM sqlite_master ORDER BY name')],ddl)
        for sid in self.f.obj:self.assertFalse((app.DATA_DIR/'objective'/'uploads'/self.key/sid).exists())
        self.assertTrue((app.DATA_DIR/'objective'/'uploads'/self.key/self.other).exists())
        self.assertEqual(self.remove(p).status_code,404)
        with patch.object(engine,'_launch_job') as launch:
            self.assertEqual(self.client.post('/api/submissions/'+self.f.obj[0]+'/retry').status_code,404)
            self.assertEqual(self.client.post('/api/submissions/'+self.f.obj[0]+'/answers/1/review',json={'option':'A','reason':'synthetic stale request'}).status_code,404)
            launch.assert_not_called()
        self.models.assert_not_called()

    def test_confirmation_scope_header_and_stale_preview_cannot_delete(self):
        p=self.preview().get_json();before=self.snapshot()
        for kw in ({'confirmed':False},{'exam_id':'f'*32},{'assessment_id':'e'*32},{'expected_fingerprint':'0'*64},{'assessment_id':{}},{'exam_id':None}):self.assertIn(self.remove(p,**kw).status_code,(400,403,404))
        self.assertEqual(self.remove(p,headers={'X-Workspace-Exam-ID':'f'*32}).status_code,400)
        self.assertEqual(self.snapshot(),before)
        with closing(app.get_db()) as c,c:c.execute('UPDATE objective_submissions SET score=0 WHERE submission_id=?',(self.f.obj[0],))
        before=self.snapshot();self.assertEqual(self.remove(p).status_code,400);self.assertEqual(self.snapshot(),before)

    def test_active_persisted_or_live_worker_blocks(self):
        p=self.preview().get_json()
        for status in ('queued','processing'):
            with closing(app.get_db()) as c,c:c.execute('UPDATE objective_jobs SET status=? WHERE job_id=?',(status,self.job))
            self.assertTrue(self.preview().get_json()['blocked']);self.assertEqual(self.remove(p).status_code,400)
        with closing(app.get_db()) as c,c:c.execute("UPDATE objective_jobs SET status='completed' WHERE job_id=?",(self.job,))
        class Worker:
            def is_alive(self):return True
        with patch.dict(engine._JOB_BY_ID,{self.job:Worker()}):
            self.assertTrue(self.preview().get_json()['blocked']);self.assertEqual(self.remove(p).status_code,400)
        self.assertEqual(self.remove().status_code,200)

    def test_foreign_photo_path_and_shared_file_ref_block(self):
        with closing(app.get_db()) as c,c:
            own=c.execute('SELECT stored_path FROM objective_submission_pages WHERE submission_id=?',(self.f.obj[0],)).fetchone()[0]
            other=c.execute('SELECT stored_path FROM objective_submission_pages WHERE submission_id=?',(self.other,)).fetchone()[0]
            c.execute('UPDATE objective_submission_pages SET stored_path=? WHERE submission_id=?',(other,self.f.obj[0]))
        before=self.snapshot();self.assertEqual(self.remove().status_code,400);self.assertEqual(self.snapshot(),before)
        with closing(app.get_db()) as c,c:
            c.execute('UPDATE objective_submission_pages SET stored_path=? WHERE submission_id=?',(own,self.f.obj[0]))
            c.execute('UPDATE objective_submission_pages SET stored_path=? WHERE submission_id=?',(own,self.other))
        before=self.snapshot();self.assertEqual(self.remove().status_code,400);self.assertEqual(self.snapshot(),before)

    def test_database_rollback_and_file_cleanup_warning_are_distinct(self):
        p=self.preview().get_json();before=self.snapshot()
        with patch.object(engine,'_insert_audit',side_effect=ValueError('synthetic failure')):self.assertEqual(self.remove(p).status_code,400)
        self.assertEqual(before,self.snapshot())
        with patch('objective.job_deletion.shutil.rmtree',side_effect=OSError('synthetic filesystem failure')):
            r=self.remove(p);self.assertEqual(r.status_code,200);self.assertIn('图片清理失败',r.get_json()['warning'])
        self.assertTrue((app.DATA_DIR/'objective'/'uploads'/self.key/self.f.obj[0]).exists())

    def test_independent_key_without_source_exam_remains_usable(self):
        r=self.client.post('/api/assessments',json={'title':'Synthetic independent','answer_key':[{'question_no':1,'correct_option':'A','points':2}]})
        self.assertEqual(r.status_code,201,r.get_json());key=r.get_json()['assessment_id'];job=uuid.uuid4().hex;sid=uuid.uuid4().hex
        path=app.DATA_DIR/'objective'/'uploads'/key/sid/'synthetic.png';path.parent.mkdir(parents=True);Image.new('RGB',(100,100),'white').save(path)
        with closing(app.get_db()) as c,c:
            c.execute("INSERT INTO objective_jobs(job_id,assessment_id,mode,grouping,status,total_files,total_submissions,created_at) VALUES(?,?,'omr','one_per_image','completed',1,1,?)",(job,key,app.utc_now()))
            c.execute("INSERT INTO objective_submissions(submission_id,job_id,assessment_id,anonymous_code,state,score,max_score,page_count,created_at) VALUES(?,?,?,?,'completed',0,2,1,?)",(sid,job,key,sid,app.utc_now()))
            seed_answers(c,sid,zero=True)
            c.execute("INSERT INTO objective_submission_pages(page_id,submission_id,page_no,stored_path,image_sha256,byte_count,width,height) VALUES(?,?,1,?,?,?,100,100)",(uuid.uuid4().hex,sid,str(path),hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_size))
        p=self.client.get('/api/jobs/'+job+'/delete-preview',query_string={'assessment_id':key,'exam_id':''}).get_json()
        self.assertEqual(p['scored'],1)
        r=self.client.post('/api/jobs/'+job+'/delete',json={'assessment_id':key,'exam_id':'','confirmed':True,'expected_fingerprint':p['fingerprint']},headers={'X-Workspace-Exam-ID':''})
        self.assertEqual(r.status_code,200,r.get_json());self.assertFalse(path.exists())
        self.assertEqual(self.client.get('/api/assessments/'+key).status_code,200)

    def test_unavailable_source_exam_is_not_disclosed_or_deleted(self):
        isolated=engine.create_app(data_dir=str(app.DATA_DIR/'objective'),database_path=str(app.DB_PATH),upload_dir=str(app.DATA_DIR/'objective'/'uploads'),auth_enabled=False,initialize_schema=False,source_exam_provider=lambda e=None:[]).test_client()
        before=self.snapshot()
        self.assertEqual(isolated.get('/api/jobs/'+self.job+'/delete-preview',query_string={'assessment_id':self.key,'exam_id':self.f.exam}).status_code,403)
        self.assertEqual(isolated.post('/api/jobs/'+self.job+'/delete',json={'assessment_id':self.key,'exam_id':self.f.exam,'confirmed':True,'expected_fingerprint':'0'*64}).status_code,403)
        self.assertEqual(self.snapshot(),before)


class ObjectiveJobDeletionAuthTests(unittest.TestCase):
    def test_main_session_same_origin_and_csrf_are_required(self):
        from tests.test_teacher_accounts import TeacherAccountTests
        f=TeacherAccountTests();f.setUp()
        try:
            f.server.objective_wsgi_app=app.create_integrated_objective_wsgi_app()
            url='/objective/api/jobs/'+'a'*32
            self.assertEqual(f.request_json('GET',url+'/delete-preview?assessment_id='+'b'*32+'&exam_id=')[0],401)
            self.assertIn(f.request_json('POST',url+'/delete',{})[0],(401,403))
            for path in ('/objective/api/jobs/batch-delete-preview','/objective/api/jobs/batch-delete'):
                self.assertIn(f.request_json('POST',path,{})[0],(401,403))
            f.create_teacher(f.admin_headers());valid=f.teacher_headers()
            self.assertEqual(f.request_json('POST','/objective/api/jobs/batch-delete-preview',{'items':[]},valid)[0],400)
            headers=dict(valid);headers.pop('X-CSRF-Token',None)
            for path in ('/objective/api/jobs/batch-delete-preview','/objective/api/jobs/batch-delete'):
                self.assertEqual(f.request_json('POST',path,{},headers)[0],403)
            self.assertEqual(f.request_json('POST',url+'/delete',{},headers)[0],403)
        finally:f.tearDown()
