"""Only isolated synthetic databases and synthetic images are used."""
import copy
import hashlib
import io
import json
import unittest
import uuid
from collections import Counter
from unittest.mock import patch
from contextlib import closing
from urllib.request import urlopen

from PIL import Image
import app
import grading_data_deletion as deletion
from tests.test_objective_job_deletion import ObjectiveJobDeletionTests


class GradingDataDeletionTests(unittest.TestCase):
    def setUp(self):
        self.f=ObjectiveJobDeletionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.aid,self.eid=self.f.f.assignment,self.f.f.exam
        self.http=self.f.f.fixture.f
        self.sids=list(self.f.f.sub);self.oids=list(self.f.f.obj)
        with closing(app.get_db()) as c,c:
            for sid in self.sids:self.photo(c,sid)

    def photo(self,c,sid):
        path=app.DATA_DIR/self.aid/'students'/sid/'synthetic.png';path.parent.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(100,100),'white').save(path)
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        photos=[dict(path=path.relative_to(app.PORTABLE_ROOT).as_posix(),sha256=digest,approved_ai_page_sha256=digest)]
        c.execute("UPDATE submissions SET photos_json=?,ai_privacy_confirmed=1,privacy_status='no_identity_visible' WHERE id=?",(json.dumps(photos),sid))
        return digest

    def body(self,**changes):
        value=dict(assignment_id=self.aid,exam_id=self.eid);value.update(changes);return value

    def plan(self,body):
        with closing(app.get_db()) as c:return deletion.plan(c,body)['preview']

    def remove(self,body,preview=None,**changes):
        body=dict(body,confirmed=True,expected_fingerprint=(preview or self.plan(body))['fingerprint']);body.update(changes)
        return deletion.remove(app,body,actor='synthetic-teacher')

    def snapshot(self):
        with closing(app.get_db()) as c:return list(c.iterdump())

    def same_upload(self,c,sid,oid):
        digest=json.loads(c.execute('SELECT photos_json FROM submissions WHERE id=?',(sid,)).fetchone()[0])[0]['approved_ai_page_sha256']
        c.execute('DELETE FROM exam_answer_links WHERE objective_submission_id=? OR subjective_submission_id=?',(oid,sid))
        c.execute("INSERT INTO exam_answer_links(id,exam_id,objective_submission_id,subjective_submission_id,method,reason,confirmed_at) VALUES(?,?,?,?,'same_upload_pages',?,?)",(uuid.uuid4().hex,self.eid,oid,sid,json.dumps({'approved_pages':[digest],'objective_pages':[digest]}),app.utc_now()))

    def test_same_upload_group_and_single_objective_deletion_preserve_other_records(self):
        with closing(app.get_db()) as c,c:self.same_upload(c,self.sids[0],self.oids[0])
        with closing(app.get_db()) as c:
            groups=deletion.groups(c,self.aid,self.eid)
            pair=next(g for g in groups if 's:'+self.sids[0] in g['members'])
            self.assertEqual((pair['subjective'],pair['objective']),(1,1))
            before={t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in ('assignments','grading_exams','objective_assessments')}
        p=self.plan(self.body(keys=[pair['key']]));self.assertEqual((p['subjective'],p['objective'],p['photos']),(1,1,2))
        report_url='/api/unified-scores/report?exam_id='+self.eid
        self.assertEqual(self.f.client.get(report_url).get_json()['objective_summary']['count'],2)
        result=self.remove(self.body(keys=[pair['key']]),p);self.assertFalse(result['warning'])
        self.assertEqual(self.f.client.get(report_url).get_json()['objective_summary']['count'],1)
        with closing(app.get_db()) as c:
            for t,rows in before.items():self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM '+t)],rows)
            self.assertIsNotNone(c.execute('SELECT 1 FROM submissions WHERE id=?',(self.sids[1],)).fetchone())
            self.assertIsNotNone(c.execute('SELECT 1 FROM objective_submissions WHERE submission_id=?',(self.oids[1],)).fetchone())
            self.assertEqual(c.execute('SELECT COUNT(*) FROM exam_answer_links').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT total_submissions FROM objective_jobs WHERE job_id=?',(self.f.job,)).fetchone()[0],1)
        self.assertFalse((app.DATA_DIR/self.aid/'students'/self.sids[0]).exists())
        self.assertFalse((app.DATA_DIR/'objective'/'uploads'/self.f.key/self.oids[0]).exists())
        self.remove(self.body(keys=['o:'+self.oids[1]]))
        with closing(app.get_db()) as c:
            self.assertIsNone(c.execute('SELECT 1 FROM objective_jobs WHERE job_id=?',(self.f.job,)).fetchone())
            self.assertIsNotNone(c.execute('SELECT 1 FROM objective_jobs WHERE job_id=?',(self.f.other_job,)).fetchone())

    def test_same_upload_origin_survives_unlink_and_prior_photo_cleanup(self):
        with closing(app.get_db()) as c,c:
            self.same_upload(c,self.sids[0],self.oids[0])
            c.execute("UPDATE exam_answer_links SET active=0,ended_at=?",(app.utc_now(),))
            c.execute("UPDATE submissions SET photos_json='[]' WHERE id=?",(self.sids[0],))
            group=next(g for g in deletion.groups(c,self.aid,self.eid) if 's:'+self.sids[0] in g['members'])
            self.assertEqual(set(group['members']),{'s:'+self.sids[0],'o:'+self.oids[0]})
            self.assertEqual(c.execute('SELECT active FROM exam_answer_links LIMIT 1').fetchone()[0],0)

    def test_atomic_selection_stale_preview_running_and_cross_scope(self):
        body=self.body(keys=['s:'+self.sids[0],'o:'+self.oids[0]])
        p=self.plan(body);before=self.snapshot()
        for change in ({'confirmed':False},{'expected_fingerprint':'0'*64},{'keys':['s:'+self.sids[0]]}):
            with self.assertRaises(ValueError):self.remove(body,p,**change)
            self.assertEqual(before,self.snapshot())
        for keys in ([],['x'],['s:'+self.sids[0]]*2):
            with self.assertRaises(ValueError):self.plan(self.body(keys=keys))
        with self.assertRaises(ValueError):self.plan(self.body(exam_id='f'*32,keys=body['keys']))
        with closing(app.get_db()) as c,c:c.execute("UPDATE submissions SET state='grading' WHERE id=?",(self.sids[0],))
        p=self.plan(body);self.assertTrue(p['blocked']);before=self.snapshot()
        with self.assertRaises(ValueError):self.remove(body,p)
        self.assertEqual(before,self.snapshot())
        with closing(app.get_db()) as c:
            self.assertTrue(deletion.plan(c,self.body(keys=['o:'+self.oids[0]]),active_workers={self.f.job})['preview']['blocked'])

    def test_clear_preserves_authority_other_exam_and_allows_new_upload(self):
        other_aid,other_eid=uuid.uuid4().hex[:12],uuid.uuid4().hex
        with closing(app.get_db()) as c,c:
            for table,key,old,new in [('assignments','id',self.aid,other_aid),('grading_exams','id',self.eid,other_eid)]:
                row=dict(c.execute('SELECT * FROM '+table+' WHERE '+key+'=?',(old,)).fetchone());row[key]=new
                if table=='grading_exams':row['assignment_id']=other_aid
                c.execute('INSERT INTO '+table+'('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))
            other_sid=uuid.uuid4().hex[:12]
            c.execute("INSERT INTO submissions(id,assignment_id,exam_id,student_label,answer_text,photos_json,result_json,created_at,state) VALUES(?,?,?,'synthetic','','[]','{}',?,'completed')",(other_sid,other_aid,other_eid,app.utc_now()))
            tables=('assignments','grading_exams','objective_assessments','rubric_versions','roster_students','exam_identity_profiles')
            keep={t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in tables}
            other=dict(c.execute('SELECT * FROM submissions WHERE id=?',(other_sid,)).fetchone())
        body=dict(assignment_id=self.aid,clear=True)
        p=self.plan(body);self.assertEqual((p['subjective'],p['objective']),(2,3));before=self.snapshot();self.plan(body);self.assertEqual(before,self.snapshot())
        self.remove(body,p)
        report=self.f.client.get('/api/unified-scores/report?exam_id='+self.eid).get_json()
        self.assertEqual(report['objective_summary']['count'],0)
        data=io.BytesIO();Image.new('RGB',(300,300),'white').save(data,format='PNG')
        with patch('objective.app._launch_job') as launch:
            upload=self.f.client.post('/api/uploads',data={'assessment_id':self.f.key,'mode':'handwriting','files':(io.BytesIO(data.getvalue()),'synthetic.png')},content_type='multipart/form-data')
            self.assertEqual(upload.status_code,202,upload.get_json());launch.assert_called_once()
        # This accepted new upload is deliberately not graded by any engine.
        with closing(app.get_db()) as c,c:
            for t,rows in keep.items():self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM '+t)],rows,t)
            self.assertEqual(dict(c.execute('SELECT * FROM submissions WHERE id=?',(other_sid,)).fetchone()),other)
            for t in ('objective_answers','objective_review_items','objective_review_events','exam_answer_links'):
                self.assertEqual(c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0],0,t)
            c.execute("INSERT INTO submissions(id,assignment_id,exam_id,student_label,answer_text,photos_json,result_json,created_at,state) VALUES(?,?,?,'new synthetic','','[]','{}',?,'queued')",(uuid.uuid4().hex[:12],self.aid,self.eid,app.utc_now()))
        self.assertTrue((app.DATA_DIR/other_aid).exists() is False)

    def test_clear_exact_formal_exam_preserves_another_record_of_same_assignment(self):
        other_exam,other_sid=uuid.uuid4().hex,uuid.uuid4().hex[:12]
        with closing(app.get_db()) as c,c:
            row=dict(c.execute('SELECT * FROM grading_exams WHERE id=?',(self.eid,)).fetchone());row['id']=other_exam
            c.execute('INSERT INTO grading_exams('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))
            c.execute("UPDATE submissions SET exam_id=? WHERE id=?",(other_exam,self.sids[1]))
            before=dict(c.execute('SELECT * FROM submissions WHERE id=?',(self.sids[1],)).fetchone())
        self.remove(self.body(clear=True))
        with closing(app.get_db()) as c:
            self.assertEqual(dict(c.execute('SELECT * FROM submissions WHERE id=?',(self.sids[1],)).fetchone()),before)
            self.assertTrue((app.DATA_DIR/self.aid/'students'/self.sids[1]).is_dir())

    def test_twenty_subjective_and_objective_records_one_atomic_request(self):
        with closing(app.get_db()) as c,c:
            sub=dict(c.execute('SELECT * FROM submissions WHERE id=?',(self.sids[0],)).fetchone())
            obj=dict(c.execute('SELECT * FROM objective_submissions WHERE submission_id=?',(self.oids[0],)).fetchone())
            keys=[]
            for _ in range(10):
                sid,oid=uuid.uuid4().hex[:12],uuid.uuid4().hex
                s={**sub,'id':sid,'idempotency_key':sid};o={**obj,'submission_id':oid,'anonymous_code':oid}
                for t,r in [('submissions',s),('objective_submissions',o)]:c.execute('INSERT INTO '+t+'('+','.join(r)+') VALUES('+','.join('?' for _ in r)+')',list(r.values()))
                self.photo(c,sid)
                keys.extend(['s:'+sid,'o:'+oid])
        result=self.remove(self.body(keys=keys));self.assertEqual((result['subjective'],result['objective']),(10,10))
        with closing(app.get_db()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM submissions').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM objective_submissions').fetchone()[0],3)

    def test_main_http_endpoints_single_confirmation_and_scope(self):
        body=self.body(keys=['s:'+self.sids[0]])
        code,p=self.http.request_json_status('/api/answer-delete-preview',body);self.assertEqual(code,200,p)
        code,r=self.http.request_json_status('/api/delete-answers',dict(body,confirmed=True,expected_fingerprint=p['fingerprint']));self.assertEqual(code,200,r)
        code,p=self.http.request_json_status('/api/clear-grading-preview',{'assignment_id':self.aid});self.assertEqual(code,200,p)
        code,r=self.http.request_json_status('/api/clear-grading-data',{'assignment_id':self.aid,'confirmed':True,'expected_fingerprint':p['fingerprint']});self.assertEqual(code,200,r)
        with urlopen(self.http.base_url+'/api/answer-management?assignment_id='+self.aid+'&exam_id='+self.eid) as r:self.assertEqual(json.load(r)['groups'],[])

    def test_file_ownership_and_failed_transaction_preserve_unselected_data(self):
        body=self.body(keys=['s:'+self.sids[0]])
        p=self.plan(body);before=self.snapshot()
        with patch.object(app,'append_audit_event',side_effect=ValueError('synthetic audit failure')):
            with self.assertRaises(ValueError):self.remove(body,p)
        self.assertEqual(before,self.snapshot())
        self.assertTrue((app.DATA_DIR/self.aid/'students'/self.sids[0]).exists())
        with closing(app.get_db()) as c,c:
            original=c.execute('SELECT photos_json FROM submissions WHERE id=?',(self.sids[0],)).fetchone()[0]
            c.execute('UPDATE submissions SET photos_json=? WHERE id=?',(original,self.sids[1]))
        before=self.snapshot()
        with self.assertRaises(ValueError):self.remove(body)
        self.assertEqual(before,self.snapshot())

    def test_deleting_future_batch_slot_does_not_break_strict_cursor(self):
        bid=uuid.uuid4().hex[:12]
        items=[dict(sequence=i+1,submission_id=sid,upload_status='uploaded') for i,sid in enumerate(self.sids)]
        with closing(app.get_db()) as c,c:
            c.execute("INSERT INTO grading_batches(id,request_id,assignment_id,exam_id,total_sequences,status,manifest_json,request_hash,created_at,updated_at) VALUES(?,?,?,?,2,'active',?,'synthetic',?,?)",(bid,bid,self.aid,self.eid,json.dumps({'items':items}),app.utc_now(),app.utc_now()))
            for i,sid in enumerate(self.sids):c.execute('UPDATE submissions SET batch_id=?,batch_sequence=? WHERE id=?',(bid,i+1,sid))
        self.remove(self.body(keys=['s:'+self.sids[1]]))
        with closing(app.get_db()) as c,c:
            c.execute('UPDATE grading_batches SET next_sequence=2 WHERE id=?',(bid,))
            self.assertIsNone(app._enqueue_current_batch_submission(c,bid))
            self.assertEqual(tuple(c.execute('SELECT next_sequence,status FROM grading_batches WHERE id=?',(bid,)).fetchone()),(3,'completed'))
            self.assertIsNotNone(c.execute('SELECT 1 FROM submissions WHERE id=?',(self.sids[0],)).fetchone())

    def test_clear_removes_queued_batches_reserved_uploads_and_identity_records(self):
        bid,reserved=uuid.uuid4().hex[:12],uuid.uuid4().hex[:12]
        items=[dict(sequence=1,submission_id=self.sids[0],upload_status='uploaded'),dict(sequence=2,submission_id=reserved,upload_status='pending')]
        folder=app.DATA_DIR/self.aid/'students'/reserved;folder.mkdir(parents=True);(folder/'synthetic.tmp').write_text('synthetic staged image')
        with closing(app.get_db()) as c,c:
            c.execute("INSERT INTO grading_batches(id,request_id,assignment_id,exam_id,total_sequences,status,manifest_json,request_hash,created_at,updated_at) VALUES(?,?,?,?,2,'paused',?,'synthetic',?,?)",(bid,bid,self.aid,self.eid,json.dumps({'items':items}),app.utc_now(),app.utc_now()))
            c.execute("UPDATE submissions SET batch_id=?,batch_sequence=1,state='queued' WHERE id=?",(bid,self.sids[0]))
            c.execute("INSERT INTO task_queue(id,submission_id,state,available_at,created_at,updated_at) VALUES(?,?,'waiting_batch',?,?,?)",(uuid.uuid4().hex,self.sids[0],app.utc_now(),app.utc_now(),app.utc_now()))
            c.execute("INSERT INTO exam_marking_codes(id,exam_id,code,batch_id,submission_id,created_at) VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,self.eid,'SYNTHETIC-CODE',bid,self.sids[0],app.utc_now()))
        self.remove(dict(assignment_id=self.aid,clear=True))
        with closing(app.get_db()) as c:
            for table in ('grading_batches','task_queue','exam_marking_codes','exam_answer_identities','exam_answer_links'):
                self.assertEqual(c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0,table)
        self.assertFalse(folder.exists())

    def test_clear_zero_subjective_eighty_two_objectives_all_source_versions(self):
        """Reproduce the reported counts without copying any actual answer data."""
        with closing(app.get_db()) as c,c:
            c.execute('DELETE FROM exam_answer_links')
            c.execute('DELETE FROM submissions')
            snapshot=dict(c.execute('SELECT * FROM objective_assessments WHERE assessment_id=?',(self.f.key,)).fetchone())
            job=dict(c.execute('SELECT * FROM objective_jobs WHERE job_id=?',(self.f.job,)).fetchone())
            prototype=dict(c.execute('SELECT * FROM objective_submissions LIMIT 1').fetchone())
            keys=[self.f.key]
            for i in range(2):
                key={**snapshot,'assessment_id':uuid.uuid4().hex,'source_fingerprint':uuid.uuid4().hex,'answer_version':100+i}
                c.execute('INSERT INTO objective_assessments('+','.join(key)+') VALUES('+','.join('?' for _ in key)+')',list(key.values()));keys.append(key['assessment_id'])
            jobs=[]
            for key in keys:
                row={**job,'job_id':uuid.uuid4().hex,'assessment_id':key}
                c.execute('INSERT INTO objective_jobs('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()));jobs.append(row['job_id'])
            # Three existing objective papers plus 79 synthetic papers.
            for i in range(79):
                index=i%3;sid=uuid.uuid4().hex
                row={**prototype,'submission_id':sid,'job_id':jobs[index],'assessment_id':keys[index],'anonymous_code':'SYNTHETIC-'+sid}
                c.execute('INSERT INTO objective_submissions('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))
            oids=[r[0] for r in c.execute('SELECT submission_id FROM objective_submissions')]
            self.assertEqual(len(oids),82)
            for i,sid in enumerate(oids):
                code='' if i<66 else 'SYNTHETIC-DUPLICATE' if i<73 else 'SYNTHETIC-CONFLICT-'+str(i)
                status='missing' if i<66 else 'reliable' if i<73 else 'conflict'
                c.execute("INSERT OR REPLACE INTO exam_answer_identities(side,submission_id,exam_id,identity_code,status,source,evidence_json,revision,updated_at) VALUES('objective',?,?,?,?,?,'{}',?,?)",(sid,self.eid,code,status,'teacher_confirmed',uuid.uuid4().hex,app.utc_now()))
            # An independent answer key has no formal source and must survive.
            independent={**snapshot,'assessment_id':uuid.uuid4().hex,'source_exam_id':None,'source_fingerprint':None,'answer_version':200}
            c.execute('INSERT INTO objective_assessments('+','.join(independent)+') VALUES('+','.join('?' for _ in independent)+')',list(independent.values()))

        with closing(app.get_db()) as c:rows=[dict(r) for r in c.execute('SELECT status FROM exam_answer_identities WHERE exam_id=?',(self.eid,))]
        self.assertEqual(Counter(r['status'] for r in rows),dict(missing=66,reliable=7,conflict=9))
        with urlopen(self.http.base_url+'/api/assignments') as r:
            source=next(a for a in json.load(r)['assignments'] if a['id']==self.aid)
        self.assertEqual((source['submission_count'],source['objective_submission_count']),(0,82))
        body=dict(assignment_id=self.aid,clear=True);p=self.plan(body)
        self.assertEqual((p['subjective'],p['objective']),(0,82))
        result=self.remove(body,p)
        self.assertEqual(result['verified_remaining'],dict(subjective=0,objective=0,jobs=0,batches=0,identities=0,links=0))
        self.assertEqual(self.f.client.get('/api/identities?exam_id='+self.eid).status_code,404)
        self.assertEqual(self.f.client.get('/api/unified-scores?exam_id='+self.eid).get_json()['rows'],[])
        self.assertEqual(self.f.client.get('/api/unified-scores/report?exam_id='+self.eid).get_json()['objective_summary']['count'],0)
        with closing(app.get_db()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM objective_assessments').fetchone()[0],4)
            for t in ('objective_submissions','objective_jobs','exam_answer_identities','exam_answer_links'):
                self.assertEqual(c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0],0,t)
        with urlopen(self.http.base_url+'/api/assignments') as r:
            source=next(a for a in json.load(r)['assignments'] if a['id']==self.aid)
        self.assertEqual((source['submission_count'],source['objective_submission_count']),(0,0))

    def test_clear_refuses_false_success_and_rolls_back_any_residual(self):
        with closing(app.get_db()) as c,c:
            # Inject a skipped DELETE to verify the safety check, not a real DB.
            c.execute("INSERT INTO exam_answer_identities(side,submission_id,exam_id,identity_code,status,source,evidence_json,revision,updated_at) VALUES('objective',?,?,'','missing','legacy','{}',?,?)",(self.oids[0],self.eid,uuid.uuid4().hex,app.utc_now()))
            c.execute('CREATE TRIGGER synthetic_keep_identity BEFORE DELETE ON exam_answer_identities BEGIN SELECT RAISE(IGNORE); END')
        body=dict(assignment_id=self.aid,clear=True);p=self.plan(body);before=self.snapshot()
        with self.assertRaisesRegex(ValueError,'整体回滚'):
            self.remove(body,p)
        self.assertEqual(before,self.snapshot())
        self.assertTrue((app.DATA_DIR/self.aid/'students'/self.sids[0]).exists())


class GradingDataAuthTests(unittest.TestCase):
    def test_existing_session_and_csrf_gates_remain(self):
        from tests.test_teacher_accounts import TeacherAccountTests
        f=TeacherAccountTests();f.setUp()
        try:
            self.assertEqual(f.request_json('GET','/api/answer-management?assignment_id='+'a'*12)[0],401)
            for route in ('/api/delete-answers','/api/clear-grading-data'):
                self.assertIn(f.request_json('POST',route,{})[0],(401,403))
            f.create_teacher(f.admin_headers());headers=f.teacher_headers();headers.pop('X-CSRF-Token',None)
            self.assertEqual(f.request_json('POST','/api/clear-grading-data',{},headers)[0],403)
        finally:f.tearDown()


if __name__=='__main__':unittest.main()
