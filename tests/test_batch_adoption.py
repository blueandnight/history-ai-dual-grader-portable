"""Synthetic consensus acceptance; real APIs and unchanged grading engines."""
import copy
import csv
import io
import json
import unittest
import uuid
from unittest.mock import patch
from contextlib import closing
from urllib.request import urlopen

from PIL import Image
import app
from tests.test_unified_scores import UnifiedScoreTests


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.f = UnifiedScoreTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.http = self.f.f.fixture.f
        self.assignment, self.exam = self.f.f.assignment, self.f.f.exam
        self.ids = self.f.f.sub
        with closing(app.get_db()) as c, c:
            self.questions = app.clean_sub_questions(json.loads(c.execute('SELECT sub_questions_json FROM assignments WHERE id=?', (self.assignment,)).fetchone()[0]))
            for sid in self.ids:
                result = json.loads(c.execute('SELECT result_json FROM submissions WHERE id=?', (sid,)).fetchone()[0])
                subs = [dict(sub_question_id=q['id'], label=q['label'], answer_status='answered', suggested_total=q['max_score'], max_total=q['max_score'], criteria=[dict(criterion_id=p['id'], decision='full', suggested_score=p['score'], max_score=p['score'], student_evidence='完全合成的作答证据') for p in q['criteria']]) for q in self.questions]
                result.update(mode='vision-model', suggested_total=sum(q['max_score'] for q in self.questions), max_total=sum(q['max_score'] for q in self.questions), sub_questions=subs)
                result['dual_review'].update(status='complete', teacher_final=None, results={s:dict(mode='vision-model', suggested_total=result['suggested_total'], max_total=result['max_total'], sub_questions=copy.deepcopy(subs)) for s in ('A','B')}, models={s:dict(status='ok',provider=s,input_hash='synthetic-shared-input') for s in ('A','B')})
                result['dual_review']['comparison']=app.compare_dual_review_results(result['dual_review']['results']['A'],result['dual_review']['results']['B'])
                photo = app.DATA_DIR/self.assignment/'students'/sid/'synthetic.png'; photo.parent.mkdir(parents=True,exist_ok=True); Image.new('RGB',(100,100),'white').save(photo)
                photos=json.dumps([dict(path=photo.relative_to(app.PORTABLE_ROOT).as_posix(), original_path=photo.relative_to(app.PORTABLE_ROOT).as_posix())])
                c.execute("UPDATE submissions SET result_json=?,final_total=NULL,confirmed_at=NULL,photos_json=?,ai_privacy_confirmed=1,privacy_status='no_identity_visible' WHERE id=?", (json.dumps(result), photos, sid))
                code_id, code = uuid.uuid4().hex, uuid.uuid4().hex.upper()
                c.execute('INSERT INTO exam_marking_codes(id,exam_id,code,submission_id,created_at) VALUES(?,?,?,?,?)', (code_id,self.exam,code,sid,app.utc_now()))
                c.execute('UPDATE submissions SET marking_code_id=?,marking_code=? WHERE id=?',(code_id,code,sid))

    def preview(self):
        with urlopen(self.http.base_url+'/api/batch-adoption?assignment_id='+self.assignment+'&exam_id='+self.exam) as r:
            return json.load(r)

    def test_current_batch_scope_sequence_and_signed_adoption(self):
        batches=[uuid.uuid4().hex[:12] for _ in self.ids]
        with closing(app.get_db()) as c,c:
            for i,(sid,bid) in enumerate(zip(self.ids,batches)):
                c.execute('INSERT INTO grading_batches(id,request_id,assignment_id,exam_id,total_sequences,status,manifest_json,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (bid,bid,self.assignment,self.exam,1,'completed','{}',bid,app.utc_now(),app.utc_now()))
                c.execute('UPDATE submissions SET batch_id=?,batch_sequence=? WHERE id=?',(bid,i+1,sid))
        def scoped(bid,exam=None):
            with urlopen(self.http.base_url+'/api/batch-adoption?assignment_id='+self.assignment+'&exam_id='+(self.exam if exam is None else exam)+'&batch_id='+bid) as r:return json.load(r)
        p=scoped(batches[0]);self.assertEqual([r['id'] for r in p['papers']],[self.ids[0]])
        self.assertEqual(p['papers'][0]['sequence'],1)
        self.assertEqual(len(self.preview()['papers']),2)
        self.assertEqual(self.save(p,batch_id=batches[1])[0],400)
        self.assertEqual(self.save(p,batch_id=batches[0],items=[dict(id=self.ids[1],exam_id=self.exam)])[0],400)
        self.assertEqual(self.save(p,batch_id=batches[0])[0],200)
        self.assertEqual(len(scoped(batches[1],exam='')['papers']),1)
        with closing(app.get_db()) as c:
            self.assertIsNone(c.execute('SELECT confirmed_at FROM submissions WHERE id=?',(self.ids[1],)).fetchone()[0])
        from batch_adoption import rows
        with closing(app.get_db()) as c:
            with self.assertRaises(ValueError):rows(c,self.assignment,'f'*32,batches[0])
            with self.assertRaises(ValueError):rows(c,'f'*12,'',batches[0])
            with self.assertRaises(ValueError):rows(c,self.assignment,self.exam,'f'*12)

    def save(self, preview, **changes):
        body=dict(assignment_id=self.assignment,exam_id=self.exam,preview_token=preview['preview_token'],items=[dict(id=p['id'],exam_id=p['exam_id']) for p in preview['papers'] if p['state']=='adoptable'],confirmed=True)
        body.update(changes)
        return self.http.request_json_status('/api/batch-adoption',body)

    def mutate(self, sid, change):
        with closing(app.get_db()) as c,c:
            result=json.loads(c.execute('SELECT result_json FROM submissions WHERE id=?',(sid,)).fetchone()[0]);change(result)
            c.execute('UPDATE submissions SET result_json=? WHERE id=?',(json.dumps(result),sid))

    def test_display_blank_candidate_is_not_zero_or_full_total(self):
        def change(r):
            for model in (r,r['dual_review']['results']['A'],r['dual_review']['results']['B']):
                model['suggested_total']=0
                for q in model['sub_questions']:
                    q['suggested_total']=0
                    for p in q['criteria']:p.update(decision='none',suggested_score=0)
            for model in (r,r['dual_review']['results']['A']):
                model['sub_questions'][1].update(answer_status='unanswered_candidate',suggested_total=None,criteria=[])
        self.mutate(self.ids[0],change)
        with closing(app.get_db()) as c:before=list(c.iterdump())
        p=next(p for p in self.preview()['papers'] if p['id']==self.ids[0])
        self.assertEqual(p['questions'][1]['a_status'],'unanswered_candidate')
        self.assertIsNone(p['questions'][1]['a']);self.assertEqual(p['questions'][1]['b'],0)
        self.assertEqual(p['totals_complete'],{'A':False,'B':True});self.assertEqual(p['state'],'review')
        with closing(app.get_db()) as c:self.assertEqual(before,list(c.iterdump()))
        self.assertEqual(self.save(self.preview(),items=[dict(id=self.ids[0],exam_id=self.exam)])[0],400)

    def test_display_content_filter_is_specific_and_sanitized(self):
        def change(r):
            d=r['dual_review'];d['results'].pop('B');d['status']='degraded'
            d['models']['B'].update(status='error',http_status=200,finish_reason='content_filter')
            d['errors']={'B':{'error_category':'request_or_response_validation','message':'SENSITIVE_RESPONSE_DO_NOT_DISPLAY'}}
        self.mutate(self.ids[0],change)
        p=next(p for p in self.preview()['papers'] if p['id']==self.ids[0])
        self.assertIn('服务商内容过滤',p['model_issues']['B']);self.assertNotIn('SENSITIVE_RESPONSE',json.dumps(p))
        self.assertFalse(p['totals_complete']['B']);self.assertIsNone(p['b']);self.assertEqual(p['questions'][0]['b_status'],'missing')
        self.assertEqual(p['state'],'review')

    def test_display_queue_pause_running_and_recovery_are_distinct(self):
        bid=uuid.uuid4().hex[:12]
        with closing(app.get_db()) as c,c:
            c.execute('INSERT INTO grading_batches(id,request_id,assignment_id,exam_id,total_sequences,status,manifest_json,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (bid,bid,self.assignment,self.exam,1,'paused','{}',bid,app.utc_now(),app.utc_now()))
            c.execute("UPDATE submissions SET batch_id=?,batch_sequence=1,state='queued' WHERE id=?",(bid,self.ids[0]))
        def shown():return next(p for p in self.preview()['papers'] if p['id']==self.ids[0])
        self.assertEqual(shown()['progress_state'],'paused');self.assertEqual(shown()['state'],'processing')
        with closing(app.get_db()) as c,c:c.execute("UPDATE grading_batches SET status='active' WHERE id=?",(bid,))
        self.assertEqual(shown()['progress_state'],'queued')
        with closing(app.get_db()) as c,c:c.execute("UPDATE submissions SET state='grading' WHERE id=?",(self.ids[0],))
        self.assertEqual(shown()['progress_state'],'grading')
        with closing(app.get_db()) as c,c:c.execute("UPDATE submissions SET state='recovery_pending' WHERE id=?",(self.ids[0],))
        self.assertEqual(shown()['progress_state'],'recovery');self.assertEqual(shown()['state'],'review')
        with closing(app.get_db()) as c,c:
            c.execute("UPDATE submissions SET state='queued' WHERE id=?",(self.ids[0],))
            c.execute('UPDATE grading_batches SET manifest_json=? WHERE id=?',(json.dumps({'grading_stop_requested':True}),bid))
        self.assertEqual(shown()['progress_state'],'stopped');self.assertEqual(shown()['state'],'stopped')

    def test_batch_adoption_unified_export_report_and_idempotence(self):
        preview=self.preview();self.assertTrue(all(p['state']=='adoptable' for p in preview['papers']),preview)
        self.assertEqual(self.save(preview),(200,{'adopted':2,'reused':0}))
        self.assertEqual(self.save(preview),(200,{'adopted':0,'reused':2}))
        self.assertEqual(self.f.paired()['total_score'],68)
        self.assertIn('批量采纳',self.f.paired()['subjective']['reason'])
        with closing(app.get_db()) as c:
            for r in c.execute('SELECT * FROM submissions'):
                result=json.loads(r['result_json']); self.assertIsNone(result['dual_review']['teacher_final']);self.assertNotIn('point_decisions',result['batch_adoption']);self.assertEqual(r['final_total'],8)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='submission_batch_adopted'").fetchone()[0],2)
        with urlopen(self.http.base_url+'/api/export-anonymous.csv?assignment_id='+self.assignment) as r:
            exported=list(csv.DictReader(io.StringIO(r.read().decode('utf-8-sig'))))
        self.assertEqual(len(exported),2);self.assertIn('批量采纳',exported[0]['确认方式'])
        status, data=self.http.request_json_status('/api/exam-report',dict(assignment_id=self.assignment,exam_id=self.exam))
        self.assertEqual(status,200,data);self.assertTrue(any('批量采纳' in s for s in data['report']['warnings']))

    def test_equal_total_with_different_points_must_not_adopt(self):
        def change(r):
            q=r['dual_review']['results']['B']['sub_questions'][0]
            p=q['criteria'][0];p['decision']='none';p['suggested_score']=0
            # Forged saved LOW risk must not conceal fresh point disagreement.
            r['dual_review']['comparison']['risk_level']='LOW'
        self.mutate(self.ids[0],change)
        preview=self.preview(); paper=next(p for p in preview['papers'] if p['id']==self.ids[0]);self.assertEqual(paper['state'],'review')
        self.assertEqual(self.save(preview,items=[dict(id=self.ids[0],exam_id=self.exam)])[0],400)

    def test_stale_data_version_scope_and_confirmation_atomicity(self):
        preview=self.preview()
        self.mutate(self.ids[1],lambda r:r.update(teacher_reconfirmation_required=True))
        self.assertEqual(self.save(preview)[0],400)
        with closing(app.get_db()) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM submissions WHERE confirmed_at IS NOT NULL').fetchone()[0],0)
        for values in ({'confirmed':False},{'preview_token':'forged'},{'exam_id':'f'*32},{'assignment_id':'f'*12}):self.assertEqual(self.save(preview,**values)[0],400)
        with closing(app.get_db()) as c,c:c.execute("UPDATE assignments SET question=question||'合成改版' WHERE id=?",(self.assignment,))
        self.assertEqual(self.save(preview)[0],400)
        self.assertTrue(all(p['state']!='adoptable' for p in self.preview()['papers']))

    def test_exceptions_and_missing_evidence_are_not_adoptable(self):
        with closing(app.get_db()) as c:self.original=json.loads(c.execute('SELECT result_json FROM submissions WHERE id=?',(self.ids[0],)).fetchone()[0])
        edits=[lambda r:r.update(simulation_model=True),lambda r:r.update(blocked=True),lambda r:r.pop('subjective_version_evidence'),lambda r:r['dual_review'].update(status='degraded'),lambda r:r['dual_review']['models']['B'].update(input_hash='other'),lambda r:r['sub_questions'][0].update(answer_status='unanswered_candidate'),lambda r:r['grading_scope'].update(graded_question_ids=[self.questions[0]['id']]),lambda r:r['dual_review']['results']['B'].update(suggested_total=None)]
        for edit in edits:
            result=copy.deepcopy(self.original);edit(result)
            with closing(app.get_db()) as c,c:c.execute('UPDATE submissions SET result_json=? WHERE id=?',(json.dumps(result),self.ids[0]))
            self.assertEqual(next(p for p in self.preview()['papers'] if p['id']==self.ids[0])['state'],'review')

    def test_point_review_can_supersede_adoption_without_faking_old_review(self):
        self.assertEqual(self.save(self.preview())[0],200)
        choices=[dict(question_id=q['id'],rubric_point_id=p['id'],choice='teacher_none') for q in self.questions for p in q['criteria']]
        status, data=self.http.request_json_status('/api/confirm',dict(submission_id=self.ids[0],exam_id=self.exam,teacher_point_decisions=choices))
        self.assertEqual(status,200,data)
        with closing(app.get_db()) as c:
            row=c.execute('SELECT * FROM submissions WHERE id=?',(self.ids[0],)).fetchone();r=json.loads(row['result_json'])
            self.assertEqual(row['final_total'],0);self.assertEqual(r['confirmation_method'],'teacher_point_review');self.assertIn('previous_batch_adoption',r)
        self.assertEqual(self.f.paired()['total_score'],60)

    def test_zero_is_adoptable_and_missing_is_not_zero(self):
        def zero(r):
            for model in (r,r['dual_review']['results']['A'],r['dual_review']['results']['B']):
                model['suggested_total']=0
                for q in model['sub_questions']:
                    q['suggested_total']=0
                    for p in q['criteria']:p.update(decision='none',suggested_score=0)
        self.mutate(self.ids[0],zero)
        self.assertEqual(self.save(self.preview())[0],200)
        with closing(app.get_db()) as c:self.assertEqual(c.execute('SELECT final_total FROM submissions WHERE id=?',(self.ids[0],)).fetchone()[0],0)
        self.assertEqual(self.f.paired()['total_score'],60)

    def test_changed_exam_after_adoption_keeps_history_but_excludes_current_aggregate(self):
        preview=self.preview();self.assertEqual(self.save(preview)[0],200)
        with closing(app.get_db()) as c,c:c.execute("UPDATE assignments SET question=question||'合成题干改版' WHERE id=?",(self.assignment,))
        self.assertFalse(self.f.paired()['complete'])
        self.assertEqual(self.save(preview)[0],400)
        with urlopen(self.http.base_url+'/api/export-anonymous.csv?assignment_id='+self.assignment) as r:
            self.assertEqual(len(list(csv.DictReader(io.StringIO(r.read().decode('utf-8-sig'))))),0)
        with closing(app.get_db()) as c:self.assertEqual(c.execute('SELECT SUM(final_total) FROM submissions').fetchone()[0],16)

    def test_preview_readonly_expiration_duplicate_and_actor_protection(self):
        with closing(app.get_db()) as c:before=list(c.iterdump())
        preview=self.preview()
        with closing(app.get_db()) as c:self.assertEqual(before,list(c.iterdump()))
        with patch('time.time',return_value=4_000_000_000):self.assertEqual(self.save(preview)[0],400)
        pairs=[dict(id=p['id'],exam_id=p['exam_id']) for p in preview['papers']]
        self.assertEqual(self.save(preview,items=pairs+pairs)[0],400)
        from batch_adoption import TOKENS
        claims=TOKENS.loads(preview['preview_token']);claims['actor']='another_synthetic_teacher'
        self.assertEqual(self.save(preview,preview_token=TOKENS.dumps(claims))[0],400)
        with closing(app.get_db()) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM submissions WHERE confirmed_at IS NOT NULL').fetchone()[0],0)


class AdoptionAuthTests(unittest.TestCase):
    def test_http_auth_csrf_and_mobile_restriction(self):
        from tests.test_teacher_accounts import TeacherAccountTests
        f=TeacherAccountTests();f.setUp()
        try:
            path='/api/batch-adoption'
            self.assertEqual(f.request_json('GET',path+'?assignment_id='+'f'*12)[0],401)
            headers=f.admin_headers()
            self.assertEqual(f.request_json('POST',path,{}, {k:v for k,v in headers.items() if k!='X-CSRF-Token'})[0],403)
            self.assertEqual(f.request_json('GET',path+'?assignment_id='+'f'*12,headers=headers)[0],400)
            self.assertEqual(f.request_json('POST',path,{'confirmed':True},headers)[0],400)
            self.assertNotIn(path,app.AppHandler.MOBILE_WRITE_PATHS)
        finally:f.tearDown()
