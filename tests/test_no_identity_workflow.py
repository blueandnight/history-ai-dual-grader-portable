"""Synthetic no-identity grading closure. Never opens business data or providers."""
import copy,csv,io,json,uuid,zipfile,unittest
from contextlib import closing
from unittest.mock import patch
from urllib.request import urlopen
import app
from objective.app import _process_job
from tests import test_simple_workspace as fixtures
from tests.synthetic_pages import page_bytes

class NoIdentityWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.SharedUploadTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.http=self.f.f.f;self.client=self.f.client;self.exam=self.f.exam;self.aid=self.f.aid;self.key=self.f.key
        self.enterContext(patch('objective.app._launch_job',side_effect=lambda application,jid:_process_job(application,jid)))
    def objective(self,source=None,key=None):
        if source==self.f.sid:
            r=self.f.upload()
        else:
            r=self.client.post('/api/uploads',data={'assessment_id':key or self.key,'mode':'vision','grouping':'one_per_image','privacy_status':'no_identity_visible','files':(io.BytesIO(page_bytes('objective different image')),'023.jpg')})
        self.assertEqual(r.status_code,202,r.get_json())
        data=self.client.get('/api/jobs/'+r.get_json()['job_id']).get_json()
        self.assertEqual(data['job']['status'],'completed',data)
        return data['submissions'][0]['submission_id']
    def subject(self,exam=None):
        eid=exam or self.exam
        body=dict(assignment_id=self.aid,exam_id=eid,grading_input_mode='original_photo',student_label='Synthetic anonymous',dual_review_enabled=True,photos=[dict(data_url=app.bytes_to_data_url(page_bytes(uuid.uuid4().hex),'image/png'),ai_redactions=[])],answer_regions=[])
        with patch.object(app,'dual_review_model_specs',return_value=self.f.specs):
            status,p=self.http.request_json_status('/api/ai-payload-preview',body);self.assertEqual(status,200,p)
            status,b=self.http.request_json_status('/api/batches',dict(assignment_id=self.aid,exam_id=eid,grading_input_mode='original_photo',batch_request_id=uuid.uuid4().hex,items=[{'page_count':1}]))
            self.assertEqual(status,201,b)
            status,saved=self.http.request_json_status('/api/submissions',{**body,'batch_id':b['batch_id'],'sequence':1,'submission_id':b['items'][0]['submission_id'],'ai_privacy_confirmed':True,'privacy_status':'no_identity_visible','ai_privacy_preview_token':p['preview_token'],'ai_privacy_fingerprint':p['preview_fingerprint']})
        self.assertEqual(status,202,saved);self.assertEqual(saved['submission']['exam_id'],eid)
        return saved['submission']['id']
    def grade(self,sid,disagree=False):
        def simulated(row,questions,*args):
            def model(full):
                raw=dict(confidence=.99,sub_questions=[dict(id=q['id'],answer_status='answered',confidence=.99,summary='合成可辨答案',criteria=[dict(criterion_id=p['id'],decision='full' if full else 'none',suggested_score=p['score'] if full else 0,student_evidence='合成作答证据' if full else '',reason='合成证据符合' if full else '合成作答不符',confidence=.99,evidence=dict(photo_index=1,x=.05,y=.05,width=.8,height=.8) if full else None) for p in q['criteria']]) for q in questions])
                result=app.normalize_sub_question_result(raw,questions);result['mode']='vision-model';return result
            a,b=model(True),model(not disagree)
            result=copy.deepcopy(a)
            result['dual_review']=dict(status='complete',rubric_hash=app.grading_core.rubric_fingerprint(questions),results={'A':a,'B':b},models={s:dict(status='ok',provider=s,model_id=s,input_hash='synthetic-shared-input') for s in ('A','B')},comparison=app.compare_dual_review_results(a,b))
            return result
        with patch.object(app,'grade_submission',side_effect=simulated),patch.object(app,'model_configuration_is_synthetic',return_value=False):
            app.process_submission(sid)
        with closing(app.get_db()) as c:
            row=c.execute('SELECT * FROM submissions WHERE id=?',(sid,)).fetchone()
            self.assertEqual(row['state'],'completed');self.assertNotIn('student_identity',json.loads(row['result_json']))
    def projection(self,eid=None,all_records=False):
        r=self.client.get('/api/unified-scores?exam_id='+(eid or self.exam)+('&records=all' if all_records else ''));self.assertEqual(r.status_code,200,r.get_json());return r.get_json()
    def confirm(self,sid,value=0):
        with urlopen(self.http.base_url+'/api/submission-photo?submission_id='+sid+'&photo_index=1') as r:self.assertEqual(r.status,200)
        p=self.http.get_json('/api/total-confirmation?submission_id='+sid)
        self.assertNotIn('identity_code',p)
        status,saved=self.http.request_json_status('/api/confirm-total',dict(submission_id=sid,exam_id=p['exam_id'],expected_fingerprint=p['expected_fingerprint'],source='teacher',final_total=value,confirmed=True))
        self.assertEqual(status,200,saved)
    def test_full_closure_three_uploads_manual_total_link_and_exports(self):
        sid1=self.f.sid;sid2=self.subject();sid3=self.subject()
        self.grade(sid1);self.grade(sid2,True);self.grade(sid3)
        oid1=self.objective(sid1);oid2=self.objective();oid3=self.objective()
        preview=self.http.get_json('/api/batch-adoption?assignment_id='+self.aid+'&exam_id='+self.exam)
        ids=[p['id'] for p in preview['papers'] if p['state']=='adoptable'];self.assertEqual(set(ids),{sid1,sid3},preview)
        status,saved=self.http.request_json_status('/api/batch-adoption',dict(assignment_id=self.aid,exam_id=self.exam,preview_token=preview['preview_token'],items=[dict(id=s,exam_id=self.exam) for s in ids],confirmed=True));self.assertEqual(status,200,saved)
        self.confirm(sid2,0)
        before=self.projection();self.assertEqual(sum(r['complete'] for r in before['rows']),1)
        r=self.client.post('/api/answer-links',json=dict(exam_id=self.exam,objective_submission_id=oid2,subjective_submission_id=sid2,confirmed=True,originals_checked=True));self.assertEqual(r.status_code,201,r.get_json());link=r.get_json()['link']['id']
        self.assertTrue(self.client.post('/api/answer-links',json=dict(exam_id=self.exam,objective_submission_id=oid2,subjective_submission_id=sid2,confirmed=True,originals_checked=True)).get_json()['reused'])
        p=self.projection(all_records=True);self.assertEqual(len(p['rows']),4);self.assertEqual(sorted(r['total_score'] for r in p['rows'] if r['complete']),[60,68])
        report=self.client.get('/api/unified-scores/report?exam_id='+self.exam+'&records=all').get_json()
        self.assertEqual((report['objective_summary']['count'],report['subjective_summary']['count'],report['complete_count']),(3,3,2))
        self.assertEqual(report['objective_questions']['1']['count'],3);self.assertEqual(report['objective_questions']['1']['correct_rate'],1)
        self.assertTrue(all(v['count']==2 for v in report['subjective_questions'].values())) # no invented totals-only subtotals
        for side in ('objective','subjective','all'):
            response=self.client.get('/api/unified-scores/export.xlsx?exam_id='+self.exam+'&records=all&side='+side);self.assertEqual(response.status_code,200)
            with zipfile.ZipFile(io.BytesIO(response.data)) as z:
                text=z.read('xl/worksheets/sheet1.xml').decode();self.assertNotIn('身份码',text)
                self.assertIn('匿名答卷编号',text)
            exported=self.client.get('/api/unified-scores/export.csv?exam_id='+self.exam+'&records=all&side='+side)
            data=list(csv.reader(io.StringIO(exported.data.decode('utf-8-sig'))));self.assertEqual(len(data),5 if side=='all' else 4)
        self.assertEqual(self.client.post('/api/answer-links',json=dict(exam_id=self.exam,action='unlink',link_id=link)).status_code,200)
        self.assertEqual(sum(r['complete'] for r in self.projection()['rows']),1)
        with closing(app.get_db()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM exam_answer_identities').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM exam_marking_codes').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT final_total FROM submissions WHERE id=?',(sid2,)).fetchone()[0],0)
        self.f.f.provider.assert_not_called()
    def test_same_source_records_aggregate_versions_exclude_and_other_sources_do_not_merge(self):
        sibling=uuid.uuid4().hex
        with closing(app.get_db()) as c,c:c.execute('INSERT INTO grading_exams(id,assignment_id,created_at) VALUES(?,?,?)',(sibling,self.aid,app.utc_now()))
        newkey=self.f.f.snapshot(sibling).get_json()['assessment_id']
        oid1=self.objective();oid2=self.objective(key=newkey)
        self.assertEqual(len(self.projection()['rows']),2)
        together=self.projection(all_records=True);self.assertEqual(len([r for r in together['rows'] if r['objective']]),2)
        sid=self.subject(sibling);self.grade(sid);self.confirm(sid,0)
        linked=self.client.post('/api/answer-links',json=dict(exam_id=self.exam,objective_submission_id=oid1,subjective_submission_id=sid,confirmed=True,originals_checked=True));self.assertEqual(linked.status_code,201,linked.get_json())
        repeated=self.client.post('/api/answer-links',json=dict(exam_id=sibling,objective_submission_id=oid1,subjective_submission_id=sid,confirmed=True,originals_checked=True))
        self.assertEqual(repeated.status_code,200,repeated.get_json());self.assertTrue(repeated.get_json()['reused'])
        combined=self.projection(all_records=True);self.assertTrue(any(r['complete'] for r in combined['rows']))
        # Exact exam-only reads cannot accidentally claim a missing sibling side.
        self.assertFalse(any(r['complete'] for r in self.projection()['rows']))
        with closing(app.get_db()) as c,c:
            m=json.loads(c.execute('SELECT import_metadata_json FROM assignments WHERE id=?',(self.aid,)).fetchone()[0]);m['objective_points']['1']=4;c.execute('UPDATE assignments SET import_metadata_json=? WHERE id=?',(json.dumps(m),self.aid))
        report=self.client.get('/api/unified-scores/report?exam_id='+self.exam+'&records=all').get_json();self.assertEqual(report['objective_summary']['count'],0)
        self.assertTrue(all(not r['complete'] for r in self.projection(all_records=True)['rows']))
    def test_ai_references_and_diagnostics_use_the_same_valid_questions(self):
        sid=self.f.sid;self.grade(sid)
        with closing(app.get_db()) as c:
            original=json.loads(c.execute('SELECT result_json FROM submissions WHERE id=?',(sid,)).fetchone()[0])
        cases=('valid_zero','partial_blocked','all_blocked','model_blocked','failed','uncertain','missing','partial_missing','duplicate',
               'wrong_max','missing_max','bad_score','unknown_status','simulated','root_simulated','demo','truncated','no_version','content_changed')
        for case in cases:
            with self.subTest(case=case):
                result=copy.deepcopy(original);model=result['dual_review']['results']['A']
                for q in model['sub_questions']:q['suggested_total']=0
                if case=='partial_blocked':model['sub_questions'][0]['blocked']=True;model['blocked']=True
                if case=='model_blocked':model['blocked']=True
                if case=='all_blocked':
                    for q in model['sub_questions']:q['blocked']=True
                if case=='failed':result['dual_review']['models']['A']['status']='failed'
                if case in ('uncertain','unknown_status'):
                    for q in model['sub_questions']:q['answer_status']='unanswered_candidate' if case=='uncertain' else 'unknown'
                if case=='missing':model['sub_questions']=[]
                if case=='partial_missing':model['sub_questions'].pop()
                if case=='duplicate':model['sub_questions']*=2
                if case=='wrong_max':
                    for q in model['sub_questions']:q['max_total']=999
                if case=='missing_max':
                    for q in model['sub_questions']:q.pop('max_total')
                if case=='bad_score':
                    for q in model['sub_questions']:q['suggested_total']=True
                if case=='simulated':model['simulation_model']=True
                if case=='root_simulated':result['simulation_model']=True
                if case=='demo':model['mode']='rules-demo'
                if case=='truncated':result['dual_review']['models']['A']['finish_reason']=['stop','length']
                if case=='no_version':result.pop('subjective_version_evidence')
                with closing(app.get_db()) as c,c:
                    c.execute('UPDATE submissions SET result_json=? WHERE id=?',(json.dumps(result),sid))
                    question=c.execute('SELECT question FROM assignments WHERE id=?',(self.aid,)).fetchone()[0]
                    if case=='content_changed':c.execute('UPDATE assignments SET question=? WHERE id=?',(question+' changed',self.aid))
                with closing(app.get_db()) as c:before=list(c.iterdump())
                side=next(r['subjective'] for r in self.projection()['rows'] if r.get('subjective'))
                report=self.client.get('/api/unified-scores/report?exam_id='+self.exam+'&records=all').get_json()
                with closing(app.get_db()) as c:self.assertEqual(before,list(c.iterdump()))
                diagnostics=report['ai_question_diagnostics'];a=[v for k,v in diagnostics.items() if k.endswith('AI-A')]
                expected='完整AI参考' if case=='valid_zero' else '部分AI参考' if case in ('partial_blocked','partial_missing') else '无有效AI参考'
                self.assertEqual(side['ai_reference_state']['A'],expected)
                self.assertEqual(side['ai_reference']['A'],0 if a else None)
                if case in ('valid_zero','partial_blocked','all_blocked'):
                    exported=self.client.get('/api/unified-scores/export.csv?exam_id='+self.exam)
                    entries=list(csv.DictReader(io.StringIO(exported.data.decode('utf-8-sig'))))
                    self.assertEqual(entries[0]['主观题AI-A参考分'],'0.0' if a else '')
                    self.assertEqual(entries[0]['主观题确认分'],'')
                    self.assertIn(expected,entries[0]['AI参考范围说明'])

                self.assertEqual(len(a),2 if case=='valid_zero' else 1 if case in ('partial_blocked','partial_missing') else 0)
                self.assertTrue(all(v['mean']==0 for v in a))
                invalid_b=case in ('root_simulated','no_version','content_changed')
                self.assertEqual(side['ai_reference']['B'],None if invalid_b else 8)
                self.assertEqual(any(k.endswith('AI-B') for k in diagnostics),not invalid_b)
                if case=='content_changed':
                    with closing(app.get_db()) as c,c:c.execute('UPDATE assignments SET question=? WHERE id=?',(question,self.aid))

    def test_retired_apis_and_historical_codes_do_not_gate_scores(self):
        for route in ('identities','roster-identity/profile','roster-identity/scan','roster-identity/cloud-preview'):
            self.assertEqual(self.client.get('/api/'+route+'?exam_id='+self.exam).status_code,404)
        status,_=self.http.request_json_status('/api/class-upload-preview',{});self.assertEqual(status,410)
        sid=self.f.sid;self.grade(sid,True)
        with closing(app.get_db()) as c,c:
            c.execute("INSERT INTO exam_answer_identities VALUES('subjective',?,?,'retired','conflict','historical','{}','old',?,NULL)",(sid,self.exam,app.utc_now()))
        self.confirm(sid,0)
        r=next(r for r in self.projection()['rows'] if r['subjective']);self.assertTrue(r['subjective']['valid']);self.assertEqual(r['subjective']['score'],0)
        with closing(app.get_db()) as c:self.assertEqual(c.execute('SELECT status FROM exam_answer_identities').fetchone()[0],'conflict')

if __name__=='__main__':unittest.main()

