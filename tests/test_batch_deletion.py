"""Permanent one-batch removal using synthetic records and photographs only."""
from contextlib import closing
import json
import unittest
import uuid
from urllib.request import urlopen
from urllib.error import HTTPError
from unittest.mock import patch
import app
from tests import test_batch_adoption as fixtures


class BatchDeletionTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AdoptionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.sid,self.other=self.f.ids;self.bid=uuid.uuid4().hex[:12];self.other_bid=uuid.uuid4().hex[:12];self.reserved=uuid.uuid4().hex[:12]
        with closing(app.get_db()) as c,c:
            for bid,ids in [(self.bid,[self.sid,self.reserved]),(self.other_bid,[self.other])]:
                c.execute('INSERT INTO grading_batches(id,request_id,assignment_id,exam_id,total_sequences,status,manifest_json,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (bid,bid,self.f.assignment,self.f.exam,len(ids),'paused' if bid==self.bid else 'completed',json.dumps({'items':[{'submission_id':sid,'sequence':i+1} for i,sid in enumerate(ids)]}),bid,app.utc_now(),app.utc_now()))
            for sid,bid in [(self.sid,self.bid),(self.other,self.other_bid)]:
                c.execute('UPDATE submissions SET batch_id=?,batch_sequence=1 WHERE id=?',(bid,sid))
                c.execute('UPDATE exam_marking_codes SET batch_id=?,batch_sequence=1 WHERE submission_id=?',(bid,sid))
            c.execute('UPDATE submissions SET final_total=0,confirmed_at=? WHERE id=?',(app.utc_now(),self.sid))
            c.execute('INSERT INTO exam_marking_codes(id,exam_id,code,batch_id,batch_sequence,submission_id,created_at) VALUES(?,?,?,?,2,?,?)',(uuid.uuid4().hex,self.f.exam,uuid.uuid4().hex.upper(),self.bid,self.reserved,app.utc_now()))
            c.execute('DELETE FROM exam_answer_links')
            objects=[r[0] for r in c.execute('SELECT submission_id FROM objective_submissions')]
            for sid,oid in zip((self.sid,self.other),objects):
                c.execute('INSERT INTO exam_answer_links(id,exam_id,objective_submission_id,subjective_submission_id,method,reason,confirmed_at) VALUES(?,?,?,?,?,?,?)',(uuid.uuid4().hex,self.f.exam,oid,sid,'teacher','synthetic manual confirmation',app.utc_now()))
        self.models=self.enterContext(patch.object(app,'open_model_request',side_effect=AssertionError('no real model')))

    def preview(self,**changes):
        scope=dict(batch_id=self.bid,assignment_id=self.f.assignment,exam_id=self.f.exam);scope.update(changes)
        from urllib.parse import urlencode
        try:
            with urlopen(self.f.http.base_url+'/api/batch-delete-preview?'+urlencode(scope)) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)

    def remove(self,p=None,**changes):
        p=p or self.preview()[1]
        body=dict(batch_id=self.bid,assignment_id=self.f.assignment,exam_id=self.f.exam,expected_fingerprint=p['fingerprint'],confirmed=True);body.update(changes)
        return self.f.http.request_json_status('/api/delete-batch',body)

    def snapshot(self):
        with closing(app.get_db()) as c:return [line for line in c.iterdump() if not line.startswith('INSERT INTO "schema_meta"')]

    def test_exact_batch_deletion_preserves_exam_other_batch_objective_and_audit(self):
        before=self.snapshot();code,p=self.preview();self.assertEqual(code,200,p);self.assertEqual(before,self.snapshot())
        self.assertEqual((p['submissions'],p['confirmed'],p['unuploaded'],p['links']),(1,1,1,1))
        with closing(app.get_db()) as c:
            other=tuple(c.execute('SELECT * FROM submissions WHERE id=?',(self.other,)).fetchone())
            objective=[tuple(r) for r in c.execute('SELECT * FROM objective_submissions')]
            schema=[tuple(r) for r in c.execute('SELECT * FROM sqlite_master ORDER BY name')]
        root=app.DATA_DIR/self.f.assignment/'students';backup=app.DATA_DIR/'backups'/'synthetic.keep';backup.parent.mkdir(exist_ok=True);backup.write_text('synthetic backup')
        self.assertEqual(self.remove(p)[0],200)
        self.assertFalse((root/self.sid).exists());self.assertTrue((root/self.other).exists());self.assertTrue(backup.exists())
        with closing(app.get_db()) as c:
            for table,where in [('grading_batches','id'),('submissions','id')]:self.assertIsNone(c.execute('SELECT 1 FROM '+table+' WHERE '+where+'=?',(self.bid if table=='grading_batches' else self.sid,)).fetchone())
            self.assertEqual(tuple(c.execute('SELECT * FROM submissions WHERE id=?',(self.other,)).fetchone()),other)
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM objective_submissions')],objective)
            self.assertIsNotNone(c.execute('SELECT 1 FROM grading_exams WHERE id=?',(self.f.exam,)).fetchone())
            self.assertIsNotNone(c.execute('SELECT 1 FROM assignments WHERE id=?',(self.f.assignment,)).fetchone())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM exam_answer_links').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM exam_marking_codes WHERE batch_id=?',(self.bid,)).fetchone()[0],0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='batch_deleted'").fetchone()[0],1)
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM sqlite_master ORDER BY name')],schema)
        app.process_submission(self.sid);self.models.assert_not_called()
        self.assertEqual(self.remove(p)[0],400)

    def test_confirmation_scope_and_stale_preview_reject_without_writes(self):
        p=self.preview()[1];before=self.snapshot()
        for change in ({'confirmed':False},{'expected_fingerprint':'0'*64},{'assignment_id':'f'*12},{'exam_id':'f'*32}):self.assertEqual(self.remove(p,**change)[0],400)
        self.assertEqual(self.preview(exam_id='f'*32)[0],400);self.assertEqual(self.preview(batch_id='bad')[0],400)
        self.assertEqual(self.snapshot(),before)
        with closing(app.get_db()) as c,c:c.execute('UPDATE submissions SET final_total=1 WHERE id=?',(self.sid,))
        changed=self.snapshot();self.assertEqual(self.remove(p)[0],400);self.assertEqual(self.snapshot(),changed)

    def test_waiting_tasks_are_deleted_and_stale_worker_cannot_call_model(self):
        with closing(app.get_db()) as c,c:
            c.execute("UPDATE submissions SET state='queued',confirmed_at=NULL,final_total=NULL WHERE id=?",(self.sid,))
            c.execute("INSERT INTO task_queue(id,submission_id,state,available_at,created_at,updated_at) VALUES(?,?,'waiting_batch',?,?,?)",(uuid.uuid4().hex,self.sid,app.utc_now(),app.utc_now(),app.utc_now()))
        self.assertEqual(self.preview()[1]['pending'],1);self.assertEqual(self.remove()[0],200)
        app.process_submission(self.sid);self.models.assert_not_called()
        with closing(app.get_db()) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM task_queue WHERE submission_id=?',(self.sid,)).fetchone()[0],0)

    def test_running_task_blocks_and_shared_photo_is_never_removed(self):
        with closing(app.get_db()) as c,c:c.execute("UPDATE submissions SET state='grading' WHERE id=?",(self.sid,))
        self.assertTrue(self.preview()[1]['blocked']);before=self.snapshot();self.assertEqual(self.remove()[0],400);self.assertEqual(self.snapshot(),before)
        with closing(app.get_db()) as c,c:
            c.execute("UPDATE submissions SET state='completed' WHERE id=?",(self.sid,))
            photos=c.execute('SELECT photos_json FROM submissions WHERE id=?',(self.sid,)).fetchone()[0]
            c.execute('UPDATE submissions SET photos_json=? WHERE id=?',(photos,self.other))
        before=self.snapshot();self.assertEqual(self.remove()[0],400);self.assertEqual(self.snapshot(),before)
        self.assertTrue((app.DATA_DIR/self.f.assignment/'students'/self.sid).exists())

    def test_only_exclusive_crop_cache_is_removed(self):
        run=uuid.uuid4().hex;folder=app.DATA_DIR/'student_auto_crop'/run;folder.mkdir(parents=True)
        (folder/'run.json').write_text(json.dumps({'exam_id':self.f.exam}),encoding='utf-8');(folder/'synthetic.jpg').write_bytes(b'synthetic only')
        with closing(app.get_db()) as c,c:
            photo=json.loads(c.execute('SELECT photos_json FROM submissions WHERE id=?',(self.sid,)).fetchone()[0]);photo[0]['answer_region_provenance']={'alignment_run_id':run}
            c.execute('UPDATE submissions SET photos_json=? WHERE id=?',(json.dumps(photo),self.sid))
        self.assertEqual(self.remove()[0],200);self.assertFalse(folder.exists())

    def test_shared_crop_cache_remains_and_running_queue_blocks(self):
        run=uuid.uuid4().hex;folder=app.DATA_DIR/'student_auto_crop'/run;folder.mkdir(parents=True)
        (folder/'run.json').write_text(json.dumps({'exam_id':self.f.exam}),encoding='utf-8')
        with closing(app.get_db()) as c,c:
            for sid in (self.sid,self.other):
                photo=json.loads(c.execute('SELECT photos_json FROM submissions WHERE id=?',(sid,)).fetchone()[0]);photo[0]['answer_region_provenance']={'alignment_run_id':run}
                c.execute('UPDATE submissions SET photos_json=? WHERE id=?',(json.dumps(photo),sid))
            c.execute("INSERT INTO task_queue(id,submission_id,state,available_at,created_at,updated_at) VALUES(?,?,'running',?,?,?)",(uuid.uuid4().hex,self.sid,app.utc_now(),app.utc_now(),app.utc_now()))
        self.assertTrue(self.preview()[1]['blocked']);self.assertEqual(self.remove()[0],400)
        with closing(app.get_db()) as c,c:c.execute("UPDATE task_queue SET state='completed' WHERE submission_id=?",(self.sid,))
        status,body=self.remove();self.assertEqual(status,200);self.assertEqual(body['shared_crop_runs_preserved'],1);self.assertTrue(folder.exists())

    def test_transaction_failure_rolls_back_before_removing_files(self):
        p=self.preview()[1];before=self.snapshot()
        with patch.object(app,'append_audit_event',side_effect=ValueError('synthetic failure')):
            self.assertEqual(self.remove(p)[0],400)
        self.assertEqual(self.snapshot(),before)
        self.assertTrue((app.DATA_DIR/self.f.assignment/'students'/self.sid).exists())

    def test_file_cleanup_failure_is_explicit_and_never_claims_erasure(self):
        with patch('batch_deletion.shutil.rmtree',side_effect=OSError('synthetic filesystem failure')):
            status,body=self.remove()
        self.assertEqual(status,200);self.assertIn('文件清理失败',body['warning'])
        self.assertTrue((app.DATA_DIR/self.f.assignment/'students'/self.sid).exists())
        self.assertEqual(self.preview()[0],400)


class BatchDeletionAuthTests(unittest.TestCase):
    def test_existing_session_and_csrf_gates_remain(self):
        from tests.test_teacher_accounts import TeacherAccountTests
        f=TeacherAccountTests();f.setUp()
        try:
            self.assertEqual(f.request_json('GET','/api/batch-delete-preview?batch_id='+'a'*12)[0],401)
            self.assertIn(f.request_json('POST','/api/delete-batch',{})[0],(401,403))
            f.create_teacher(f.admin_headers());headers=f.teacher_headers();headers.pop('X-CSRF-Token',None)
            self.assertEqual(f.request_json('POST','/api/delete-batch',{},headers)[0],403)
        finally:f.tearDown()
