"""Synthetic-only permanent exam deletion and existing photo-cleanup boundaries."""
from contextlib import closing
import io
import json
import unittest
import uuid
from unittest.mock import patch
from PIL import Image
import app
from tests import test_teacher_accounts as accounts
from tests.test_unified_exam import mixed_draft
from tests.test_unified_scores import seed_final, seed_answers


def seed_workspace(f):
    f.server.objective_wsgi_app=app.create_integrated_objective_wsgi_app()
    f.create_teacher(f.admin_headers())
    headers = f.teacher_headers()
    draft, confirmation = mixed_draft()
    draft_id = uuid.uuid4().hex
    root = app.DATA_DIR / 'exam_import_drafts' / draft_id
    root.mkdir(parents=True)
    (root / 'exam_import_draft.json').write_text(json.dumps(draft), encoding='utf-8')
    status, saved, _ = f.request_json('POST', '/api/create-imported-exam', {'draft_id': draft_id, 'confirmation': confirmation}, headers)
    assert status == 201, saved
    aid, exam = saved['assignment']['id'], saved['exam_id']
    status, snapshot, _ = f.request_json('POST', '/objective/api/assessments/from-exam', {'exam_id': exam}, headers)
    assert status in (200, 201), snapshot
    image = io.BytesIO()
    Image.new('RGB', (300, 400), 'white').save(image, 'PNG')
    ids = []
    with closing(app.get_db()) as conn, conn:
        for i in range(5):
            sid = uuid.uuid4().hex[:12]; ids.append(sid)
            path = app.DATA_DIR / aid / 'students' / sid / 'original.png'
            path.parent.mkdir(parents=True); path.write_bytes(image.getvalue())
            relative = 'data/' + path.relative_to(app.DATA_DIR).as_posix()
            conn.execute("INSERT INTO submissions(id,assignment_id,exam_id,marking_code,student_label,answer_text,photos_json,result_json,created_at,state) VALUES(?,?,?,?,?,'',?,'{}',?,'completed')", (sid, aid, exam, '匿名0'+str(i+1), 'synthetic', json.dumps([dict(path=relative,original_path=relative)]), app.utc_now()))
            seed_final(conn, sid)
            if i == 0:conn.execute('UPDATE submissions SET final_total=0 WHERE id=?', (sid,))
            if i == 3:conn.execute('UPDATE submissions SET confirmed_at=NULL WHERE id=?', (sid,))
            if i == 4:
                result=json.loads(conn.execute('SELECT result_json FROM submissions WHERE id=?',(sid,)).fetchone()[0]);result['teacher_reconfirmation_required']=True
                conn.execute('UPDATE submissions SET result_json=? WHERE id=?',(json.dumps(result),sid))
        job, oid = uuid.uuid4().hex, uuid.uuid4().hex
        conn.execute("INSERT INTO objective_jobs(job_id,assessment_id,mode,grouping,status,total_files,total_submissions,created_at) VALUES(?,?,'omr','one_per_image','completed',1,1,?)",(job,snapshot['assessment_id'],app.utc_now()))
        conn.execute("INSERT INTO objective_submissions(submission_id,job_id,assessment_id,anonymous_code,state,score,max_score,page_count,created_at) VALUES(?,?,?,'OBJECTIVE-SYNTHETIC','completed',60,60,1,?)",(oid,job,snapshot['assessment_id'],app.utc_now()))
        path=app.DATA_DIR/'objective'/'uploads'/snapshot['assessment_id']/oid/'page-001.png';path.parent.mkdir(parents=True);path.write_bytes(image.getvalue())
        conn.execute("INSERT INTO objective_submission_pages(page_id,submission_id,page_no,stored_path,image_sha256,byte_count,width,height) VALUES(?,?,1,?,'synthetic',?,300,400)",(uuid.uuid4().hex,oid,str(path),len(image.getvalue())))
        seed_answers(conn,oid)
        conn.execute("INSERT INTO exam_answer_links(id,exam_id,objective_submission_id,subjective_submission_id,method,reason,confirmed_at) VALUES(?,?,?,?,'teacher_original_review','synthetic check',?)",(uuid.uuid4().hex,exam,oid,ids[0],app.utc_now()))
        conn.execute("INSERT INTO assignments(id,name,question,standard_answer,criteria_json,sub_questions_json,created_at) VALUES(?,'Unrelated','q','a','[{\"label\":\"synthetic\",\"score\":2,\"keywords\":[\"q\"]}]','[]',?)",('e'*12,app.utc_now()))
        conn.execute("INSERT INTO objective_assessments(assessment_id,title,answer_version,answer_key_json,answer_key_sha256,created_at) VALUES(?,'Independent',1,'[]','synthetic',?)",('f'*32,app.utc_now()))
    untouched=app.DATA_DIR/('e'*12)/'keep.txt';untouched.parent.mkdir();untouched.write_text('synthetic untouched')
    return headers, aid, exam, ids, snapshot


class ExamMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.f=accounts.TeacherAccountTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.model=patch.object(app,'open_model_request',side_effect=AssertionError('real models forbidden'));self.model.start();self.addCleanup(self.model.stop)
        self.headers,self.aid,self.exam,self.ids,self.snapshot=seed_workspace(self.f)

    def preview(self):
        status,data,_=self.f.request_json('GET','/api/assignment-delete-preview?assignment_id='+self.aid,headers=self.headers)
        self.assertEqual(status,200);return data

    def remove(self, **extra):
        p=self.preview()
        payload=dict(assignment_id=self.aid,confirmation_name=p['name'],expected_fingerprint=p['fingerprint'])
        payload.update(extra)
        return self.f.request_json('POST','/api/delete-exam',payload,self.headers)

    def test_permanent_delete_covers_both_engines_and_keeps_other_exams(self):
        p=self.preview();self.assertEqual((p['subjective'],p['objective'],p['photos'],p['links']),(5,1,6,1))
        self.assertEqual(self.remove(confirmation_name='wrong')[0],400)
        with closing(app.get_db()) as conn:
            from exam_deletion import deletion_plan
            tables=[t for t,_ in deletion_plan(conn,self.aid)['specs']]
        self.assertEqual(self.remove()[0],200)
        with closing(app.get_db()) as conn:
            self.assertIsNone(conn.execute('SELECT * FROM assignments WHERE id=?',(self.aid,)).fetchone())
            for table in tables:
                if table in ('assignments','objective_assessments','objective_audit_events'):continue
                self.assertEqual(conn.execute('SELECT count(*) FROM '+table).fetchone()[0],0,table)
            self.assertIsNotNone(conn.execute('SELECT * FROM objective_assessments WHERE assessment_id=?',('f'*32,)).fetchone())
            self.assertFalse(conn.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(conn.execute("SELECT count(*) FROM audit_events WHERE event_type='exam_deleted'").fetchone()[0],1)
        self.assertFalse((app.DATA_DIR/self.aid).exists())
        self.assertFalse((app.DATA_DIR/'objective'/'uploads'/self.snapshot['assessment_id']).exists())
        self.assertTrue((app.DATA_DIR/('e'*12)/'keep.txt').exists())

    def test_stale_preview_and_active_work_block_deletion(self):
        p=self.preview()
        with closing(app.get_db()) as conn,conn:conn.execute('UPDATE submissions SET final_total=1 WHERE id=?',(self.ids[0],))
        self.assertEqual(self.remove(expected_fingerprint=p['fingerprint'])[0],400)
        with closing(app.get_db()) as conn,conn:conn.execute("UPDATE objective_jobs SET status='processing'")
        self.assertTrue(self.preview()['blocked']);self.assertEqual(self.remove()[0],400)
        self.assertTrue((app.DATA_DIR/self.aid).exists())

    def test_waiting_batch_and_stale_memory_ids_do_not_trap_exam(self):
        # Reproduce the reported shape: 13 completed and 39 waiting papers.
        queued=[];now=app.utc_now();batch=uuid.uuid4().hex[:12]
        with closing(app.get_db()) as conn,conn:
            conn.execute("INSERT INTO grading_batches(id,request_id,assignment_id,total_sequences,status,manifest_json,request_hash,created_at,updated_at) VALUES(?,?,?,39,'active','{}','synthetic',?,?)",(batch,uuid.uuid4().hex,self.aid,now,now))
            for i in range(47):
                sid=uuid.uuid4().hex[:12];state='queued' if i<39 else 'completed'
                conn.execute("INSERT INTO submissions(id,assignment_id,exam_id,student_label,answer_text,photos_json,result_json,created_at,state,batch_id,batch_sequence) VALUES(?,?,?,'synthetic','','[]','{}',?,?,?,?)",(sid,self.aid,self.exam,now,state,batch if i<39 else None,i+1 if i<39 else None))
                if i<39:
                    queued.append(sid)
                    conn.execute("INSERT INTO task_queue(id,submission_id,state,available_at,created_at,updated_at) VALUES(?,?,'waiting_batch',?,?,?)",(uuid.uuid4().hex,sid,now,now,now))
        p=self.preview();self.assertFalse(p['blocked']);self.assertEqual(p['subjective'],52)
        self.assertEqual(p['pending_subjective'],39);self.assertEqual(p['unfinished_batches'],1)
        self.assertEqual(self.remove()[0],200)
        # A previously enqueued ID must exit before the model gateway is called.
        for sid in queued:app.process_submission(sid)
        with closing(app.get_db()) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM task_queue').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT count(*) FROM grading_batches').fetchone()[0],0)

    def test_running_subjective_and_running_queue_still_block(self):
        with closing(app.get_db()) as conn,conn:conn.execute("UPDATE submissions SET state='grading' WHERE id=?",(self.ids[0],))
        self.assertTrue(self.preview()['blocked']);self.assertEqual(self.remove()[0],400)
        with closing(app.get_db()) as conn,conn:
            conn.execute("UPDATE submissions SET state='queued' WHERE id=?",(self.ids[0],))
            now=app.utc_now()
            conn.execute("INSERT INTO task_queue(id,submission_id,state,available_at,created_at,updated_at) VALUES(?,?,'running',?,?,?)",(uuid.uuid4().hex,self.ids[0],now,now,now))
        self.assertTrue(self.preview()['blocked']);self.assertEqual(self.remove()[0],400)

    def test_database_failure_restores_files_and_records(self):
        from exam_deletion import delete_exam
        p=self.preview()
        with patch('exam_deletion.shutil.rmtree',side_effect=AssertionError('must not erase rollback files')):
            with self.assertRaises(RuntimeError):
                delete_exam(app.get_db,app.DATA_DIR,dict(assignment_id=self.aid,confirmation_name=p['name'],expected_fingerprint=p['fingerprint']),strict=True,audit=lambda *a:(_ for _ in ()).throw(RuntimeError('synthetic rollback')),actor='test')
        self.assertTrue((app.DATA_DIR/self.aid/'students'/self.ids[0]/'original.png').exists())
        self.assertEqual(self.preview()['subjective'],5)

    def test_unsafe_page_path_blocks_without_touching_other_exam(self):
        with closing(app.get_db()) as conn,conn:
            conn.execute('UPDATE objective_submission_pages SET stored_path=?',(str(app.DATA_DIR/('e'*12)/'keep.txt'),))
        self.assertEqual(self.remove()[0],400)
        self.assertTrue((app.DATA_DIR/('e'*12)/'keep.txt').exists())
        self.assertTrue((app.DATA_DIR/self.aid).exists())

    def test_cleanup_failure_is_reported_without_claiming_files_erased(self):
        with patch('exam_deletion.shutil.rmtree',side_effect=OSError('synthetic locked file')):
            status,result,_=self.remove()
        self.assertEqual(status,200);self.assertTrue(result['warning'])
        self.assertTrue((app.DATA_DIR/self.aid).exists())
        with closing(app.get_db()) as conn:
            self.assertIsNone(conn.execute('SELECT id FROM assignments WHERE id=?',(self.aid,)).fetchone())

    def test_cleanup_scope_and_confirmation_gates_preserves_scores(self):
        def clean(sid, **extra):return self.f.request_json('POST','/api/delete-submission-photos',dict(submission_id=sid,**{'expected_assignment_id':self.aid,'expected_exam_id':self.exam,**extra}),self.headers)
        self.assertEqual(clean(self.ids[0],expected_exam_id='f'*32)[0],400)
        self.assertEqual(clean(self.ids[0],expected_assignment_id='f'*12)[0],400)
        for sid in self.ids[3:]:self.assertEqual(clean(sid)[0],400)
        with closing(app.get_db()) as conn,conn:conn.execute("UPDATE submissions SET state='grading' WHERE id=?",(self.ids[2],))
        self.assertEqual(clean(self.ids[2])[0],400)
        self.assertEqual(clean(self.ids[0])[0],200)
        self.assertEqual(clean(self.ids[0])[0],200)
        with closing(app.get_db()) as conn:
            row=conn.execute('SELECT * FROM submissions WHERE id=?',(self.ids[0],)).fetchone()
            self.assertEqual(row['final_total'],0);self.assertEqual(json.loads(row['photos_json']),[])
            self.assertEqual(conn.execute('SELECT count(*) FROM submissions').fetchone()[0],5)
        self.assertFalse((app.DATA_DIR/self.aid/'students'/self.ids[0]).exists())
        self.assertTrue((app.DATA_DIR/self.aid/'students'/self.ids[1]/'original.png').exists())

    def test_unconfirmed_photo_discard_requires_explicit_consent_and_blocks_final(self):
        sid=self.ids[3];body=dict(submission_id=sid,expected_assignment_id=self.aid,expected_exam_id=self.exam)
        self.assertEqual(self.f.request_json('POST','/api/delete-submission-photos',body,self.headers)[0],400)
        with closing(app.get_db()) as conn:before=dict(conn.execute('SELECT * FROM submissions WHERE id=?',(sid,)).fetchone())
        app.mark_submission_photo_viewed(sid)
        body['discard_unconfirmed']=True
        self.assertEqual(self.f.request_json('POST','/api/delete-submission-photos',body,self.headers)[0],200)
        with closing(app.get_db()) as conn:after=dict(conn.execute('SELECT * FROM submissions WHERE id=?',(sid,)).fetchone())
        self.assertEqual(before['final_total'],after['final_total']);self.assertEqual(before['confirmed_at'],after['confirmed_at'])
        self.assertTrue(json.loads(after['result_json'])['photos_discarded_unconfirmed'])
        self.assertFalse(app.submission_photo_was_viewed(sid))
        status,error,_=self.f.request_json('POST','/api/confirm',dict(submission_id=sid,exam_id=self.exam,teacher_point_decisions=[]),self.headers)
        self.assertEqual(status,400);self.assertIn('照片已弃用',error['error'])
        self.assertEqual(json.loads(before['result_json'])['dual_review'],json.loads(after['result_json'])['dual_review'])

    def test_auth_csrf_and_invalid_delete_payloads(self):
        payload=dict(assignment_id=self.aid,confirmation_name='synthetic',expected_fingerprint='f'*64)
        self.assertIn(self.f.request_json('POST','/api/delete-exam',payload)[0],(401,403))
        no_csrf={k:v for k,v in self.headers.items() if k!='X-CSRF-Token'}
        for path,body in [('/api/delete-exam',payload),('/api/delete-submission-photos',dict(submission_id=self.ids[0]))]:
            self.assertEqual(self.f.request_json('POST',path,body,no_csrf)[0],403)
        self.assertEqual(self.f.request_json('POST','/api/delete-exam',dict(assignment_id='f'*12),self.headers)[0],400)


if __name__=='__main__':unittest.main()
