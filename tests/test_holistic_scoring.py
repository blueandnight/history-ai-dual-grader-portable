"""Synthetic fifth-prompt protocol and real persistence; provider transport is mocked."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from PIL import Image
import app
import holistic_scoring as hs
import blank_consensus
from tests import test_batch_adoption as adoption


def questions():
    return app.clean_sub_questions([
        dict(id='Q19.1',label='19',question='合成历史小论文：自选合理角度，运用史实进行论证。',standard_answer='',max_score=12,scoring_mode='holistic',holistic=dict(score_step=1,dimensions='观点、史实、论证、逻辑',levels=[dict(id='low',min_score=0,max_score=3,description='观点及论证不足'),dict(id='middle',min_score=4,max_score=8,description='观点明确，史实基本准确，论证尚可'),dict(id='high',min_score=9,max_score=12,description='观点清晰，史实准确且论证充分，逻辑清楚')])),
        *[dict(id=f'Q20.{i}',label=f'20.{i}',question='合成纠错' if i==1 else '合成展览设计',standard_answer='合成参考',max_score=n,criteria=[dict(id='C1',label='合成要求',score=n)]) for i,n in ((1,4),(2,8))]])


def essay(q,score=10,level='high'):
    return dict(sub_question_id=q['id'],scoring_version=hs.PROMPT_VERSION,rubric_hash=hs.digest(q),answer_status='answered',level_id=level,suggested_total=score,confidence=.99,image_readable=True,region_complete=True,question_matched=True,needs_review=False,
        dimension_assessments=[dict(dimension_id=d['id'],judgment='met',reason='合成定性判断',evidence_ids=['E1']) for d in q['holistic']['dimension_definitions']],
        strengths=['观点史实形成论证'],weaknesses=['论证尚可深化'],critical_factual_issues=[],factual_status='clear',constraint_status='met',constraint_reason='符合题目限制',problem_flags={k:False for k in hs.PROBLEMS},
        evidence=[dict(id='E1',student_quote='合成制度推动交流并影响社会。',location=dict(photo_index=1,x=.1,y=.2,width=.6,height=.1))],
        fact_checks=[dict(student_quote='合成制度推动交流',judgment='correct',reason='史实符合设问',evidence_ids=['E1'])])


def ordinary(qs):
    return dict(confidence=.99,sub_questions=[dict(sub_question_id=q['id'],answer_status='answered',confidence=.99,criteria=[dict(criterion_id=p['id'],decision='full',suggested_score=p['score'],student_evidence='合成学生答案符合要求',reason='合成核验',confidence=.99,evidence=dict(photo_index=1,x=.1,y=.3,width=.6,height=.1)) for p in q['criteria']]) for q in qs])


class ProtocolTests(unittest.TestCase):
    def setUp(self):self.qs=questions();self.q=self.qs[0];self.raw=essay(self.q)
    def normalized(self,raw=None):return hs.normalize(dict(sub_questions=[raw or self.raw]),[self.q],1)
    def test_routing_and_four_prompts_remain_unchanged(self):
        plan=json.loads(app.build_scoring_prompt_text('合成试卷',self.qs))
        self.assertEqual(len(plan['requests']),2)
        self.assertIn(hs.PROMPT_VERSION,plan['requests'][0]['prompt'])
        self.assertEqual(plan['requests'][1]['prompt'],app.build_vision_prompt_text('合成试卷',self.qs[1:],answer_regions=None))
        self.assertNotIn('criterion_id',plan['requests'][0]['prompt'])
    def test_strict_configuration_no_silent_step(self):
        from holistic_draft import validate
        for field in ('score_step','dimensions'):
            q=copy.deepcopy(self.q);q['holistic'].pop(field);q['holistic'].pop('dimension_definitions',None)
            with self.assertRaises(ValueError):hs.ready(q)
        q=copy.deepcopy(self.q);q['holistic']['levels'][1]['id']='low'
        with self.assertRaises(ValueError):validate(q['holistic'],12)
    def test_invalid_model_fields_are_rejected(self):
        for key,value in [('sub_question_id','Q21.1'),('level_id','unknown'),('suggested_total',13),('suggested_total',9.5),('dimension_assessments',[]),('evidence',[]),('confidence',True)]:
            with self.subTest(key=key,value=value):
                r=copy.deepcopy(self.raw);r[key]=value
                with self.assertRaises(ValueError):self.normalized(r)
        r=copy.deepcopy(self.raw);r['evidence'][0]['location']['photo_index']=2
        with self.assertRaises(ValueError):self.normalized(r)
    def test_metadata_bound_to_request_and_checked_when_reusing_saved_result(self):
        for echoes in ({}, {'scoring_version': 'old', 'rubric_hash': 'stale'}):
            raw = copy.deepcopy(self.raw)
            raw.pop('scoring_version')
            raw.pop('rubric_hash')
            raw.update(echoes)
            hs.check(raw, self.q, 1)
            saved = self.normalized(raw)['sub_questions'][0]
            self.assertEqual(saved['holistic_result']['scoring_version'], hs.PROMPT_VERSION)
            self.assertEqual(saved['holistic_result']['rubric_hash'], hs.digest(self.q))
            self.assertTrue(hs.valid_sub(saved, self.q))
            for field in ('scoring_version', 'rubric_hash'):
                altered = copy.deepcopy(saved)
                altered['holistic_result'][field] = 'stale'
                self.assertFalse(hs.valid_sub(altered, self.q))
                del altered['holistic_result'][field]
                self.assertFalse(hs.valid_sub(altered, self.q))
    def test_consensus_structured_judgments_not_wording(self):
        a=self.normalized();r=copy.deepcopy(self.raw);r['strengths']=['另一种措辞'];r['dimension_assessments'][0]['reason']='不同措辞';r['fact_checks'][0]['student_quote']='合成制度推动交流并影响社会'
        self.assertEqual(hs.compare(a['sub_questions'][0],self.normalized(r)['sub_questions'][0]),[])
        self.assertEqual(app.compare_dual_review_results(a,self.normalized(r))['risk_level'],'NO_CONFLICT')
    def test_level_score_facts_constraints_and_missing_never_consensus(self):
        a=self.normalized()['sub_questions'][0]
        changes=[dict(level_id='middle',suggested_total=8),dict(suggested_total=9),dict(factual_status='issues',critical_factual_issues=['合成史实错误'],problem_flags={**self.raw['problem_flags'],'factual_error':True}),dict(constraint_status='violated',problem_flags={**self.raw['problem_flags'],'task_constraint_violation':True})]
        for change in changes:
            r={**copy.deepcopy(self.raw),**change};self.assertTrue(hs.compare(a,self.normalized(r)['sub_questions'][0]))
        self.assertEqual(hs.compare(a,{}),['holistic_invalid_result'])
        b=copy.deepcopy(a);b['holistic_result']['fact_checks'][0]['judgment']='incorrect'
        self.assertIn('holistic_fact_judgment_disagreement',hs.compare(a,b))
        self.assertTrue(hs.compare(dict(criteria=[]),dict(criteria=[])))
    def test_blank_unreadable_failure_and_partial(self):
        r={**self.raw,'answer_status':'unanswered_candidate','level_id':None,'suggested_total':None,'needs_review':True,'summary':'本题区域完整清晰，确认未作答'}
        a=self.normalized(r);meta={s:dict(status='ok',provider=s,model_id=s,input_hash='same') for s in ('A','B')}
        effective,receipt=blank_consensus.resolve({'A':a,'B':copy.deepcopy(a)},meta,{},[self.q],[dict(quality=dict(ok=True))])
        self.assertEqual(receipt['question_ids'],['Q19.1']);self.assertEqual(effective['A']['suggested_total'],0)
        for state in ('image_unreadable','question_unmatched'):
            r['answer_status']=state;b=self.normalized(r);self.assertIsNone(b['sub_questions'][0]['suggested_total'])
            self.assertIsNone(blank_consensus.resolve({'A':a,'B':b},meta,{},[self.q],[dict(quality=dict(ok=True))])[1])
        self.assertIsNone(blank_consensus.resolve({'A':a},meta,{'B':{}},[self.q],[dict(quality=dict(ok=True))])[1])
    def test_zero_is_legal_but_missing_is_not(self):
        r=essay(self.q,0,'low');self.assertEqual(self.normalized(r)['suggested_total'],0)
        r['suggested_total']=None
        with self.assertRaises(ValueError):self.normalized(r)
    def test_real_dual_dispatch_preview_hash_and_independence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'synthetic.png';Image.new('RGB',(100,100),'white').save(path)
            assignment=dict(id='synthetic',question='合成试卷');calls=[]
            preview=app.build_scoring_prompt_text(assignment['question'],self.qs,privacy_mode=True)
            approved=hashlib.sha256(preview.encode()).hexdigest()
            photo=dict(approved_prompt_sha256=approved,approved_ai_page_sha256=hashlib.sha256(app.prepare_model_image(path,[])).hexdigest(),quality=dict(ok=True))
            specs={s:dict(provider=s,model=s,base_url='https://api.example.test/v1',api_key='synthetic') for s in ('A','B')}
            def transport(request,**kwargs):
                body=json.loads(request.data);calls.append(body);text=body['messages'][1]['content'][0]['text']
                if text.startswith('答卷逐行转写协议'):
                    evidence = essay(self.q)['evidence'][0]
                    raw = {'sub_questions': [dict(sub_question_id=self.q['id'], status='readable', confidence=.99,
                        image_readable=True, region_complete=True, question_matched=True,
                        lines=[dict(id='L1',text=evidence['student_quote'],location=evidence['location'])])]}
                elif hs.PROMPT_VERSION in text:
                    item = essay(self.q)
                    item.pop('scoring_version')
                    item.pop('rubric_hash')
                    for entry in item['evidence']:
                        entry['line_id'] = 'L1'
                    raw = {'sub_questions': [item]}
                else:
                    raw = ordinary(self.qs[1:])
                reply=io.BytesIO(json.dumps(dict(choices=[dict(finish_reason='stop',message=dict(content=json.dumps(raw,ensure_ascii=False)))])).encode());reply.status=200;return reply
            with patch.object(app,'load_dual_review_model_configs',return_value=specs),patch.object(app,'open_model_request',side_effect=transport),patch.object(app,'preflight_private_model_diagnostic_dir',return_value=None):
                result=app.grade_submission_dual(assignment,self.qs,'',[path],photo_meta=[photo],privacy_confirmed=True,anonymous_mode=True,answer_regions=None,question_mapping=None)
                self.assertEqual(result['dual_review']['status'],'complete',result['dual_review']['errors'])
                self.assertEqual(result['suggested_total'],22);self.assertEqual(result['max_total'],24)
                self.assertEqual(result['dual_review']['comparison']['risk_level'],'NO_CONFLICT')
                self.assertEqual(len(calls),6)
                a=[b['messages'] for b in calls if b['model']=='A'];b=[b['messages'] for b in calls if b['model']=='B'];self.assertEqual(a,b)
                plan = json.loads(json.loads(preview)['requests'][0]['prompt'])
                self.assertEqual(a[0][1]['content'][0]['text'], plan['transcription_prompt'])
                self.assertNotIn('reference_examples', a[0][1]['content'][0]['text'])
                self.assertEqual(len(a[1][1]['content']), 1)
                self.assertTrue(a[1][1]['content'][0]['text'].startswith(plan['scoring_prompt']))
                calls.clear()
                with self.assertRaises(ValueError):app.call_vision_model(assignment,self.qs,'',[path],photo_meta=[{**photo,'approved_prompt_sha256':'tampered'}],privacy_confirmed=True,anonymous_mode=True,model_config_override=specs['A'])
                self.assertEqual(calls,[])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.f=adoption.AdoptionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.qs=questions();self.sid=self.f.ids[0];self.http=self.f.http
        with closing(app.get_db()) as c,c:
            c.execute('UPDATE assignments SET sub_questions_json=? WHERE id=?',(json.dumps(self.qs),self.f.assignment))
            assignment=c.execute('SELECT * FROM assignments WHERE id=?',(self.f.assignment,)).fetchone()
            evidence=app.grading_core.subjective_version_evidence(assignment,self.qs,self.qs)
            model=hs.normalize(dict(sub_questions=[essay(self.qs[0])]),self.qs[:1],1)
            other=app.normalize_sub_question_result(ordinary(self.qs[1:]),self.qs[1:],1)
            model.update(sub_questions=model['sub_questions']+other['sub_questions'],suggested_total=22,max_total=24)
            for sid in self.f.ids:
                old=json.loads(c.execute('SELECT result_json FROM submissions WHERE id=?',(sid,)).fetchone()[0]);result={**copy.deepcopy(model),'grading_scope':dict(graded_question_ids=[q['id'] for q in self.qs],scoped_max_score=24),'subjective_version_evidence':evidence,'grading_context':{'rubric_sha256':evidence['rubric_hash']}}
                result['dual_review']={**old['dual_review'],'rubric_hash':evidence['rubric_hash'],'results':{s:copy.deepcopy(model) for s in ('A','B')},'comparison':app.compare_dual_review_results(model,model)}
                c.execute('UPDATE submissions SET result_json=? WHERE id=?',(json.dumps(result),sid))
    def test_batch_adoption_mixed_totals_and_exports(self):
        p=self.f.preview();self.assertTrue(all(x['state']=='adoptable' for x in p['papers']),p)
        status,data=self.f.save(p);self.assertEqual(status,200,data)
        self.assertEqual(self.f.f.paired()['subjective']['score'],22)
        response=self.f.f.client.get('/api/unified-scores/export.xlsx?exam_id='+self.f.exam+'&side=subjective&records=all');self.assertEqual(response.status_code,200)
        with closing(app.get_db()) as c:
            r=json.loads(c.execute('SELECT result_json FROM submissions WHERE id=?',(self.sid,)).fetchone()[0]);self.assertEqual(r['batch_adoption']['policy_version'],'mixed_mode_consensus_v1');self.assertEqual(len(r['batch_adoption']['consensus_points']),2)
    def test_total_confirmation_saved_without_fake_points_version_protected(self):
        import teacher_total
        with closing(app.get_db()) as c,c:
            row=teacher_total.read(c,self.sid);p=teacher_total.prepare(c,row,app);self.assertEqual(p['suggestions'],{'A':22,'B':22})
            with patch.object(app,'submission_photo_was_viewed',return_value=True):
                r=teacher_total.confirm(c,row,dict(exam_id=self.f.exam,expected_fingerprint=p['expected_fingerprint'],source='A',final_total=22,confirmed=True),app,'synthetic-teacher')
            self.assertEqual(r['question_subtotals'],[])
        self.assertEqual(self.f.f.paired()['subjective']['score'],22)
        with closing(app.get_db()) as c,c:
            changed=copy.deepcopy(self.qs);changed[0]['holistic']['levels'][2]['description']='修改后的评分标准'
            c.execute('UPDATE assignments SET sub_questions_json=? WHERE id=?',(json.dumps(changed),self.f.assignment))
        self.assertFalse(self.f.f.paired()['complete'])
    def test_forged_essay_blocks_adoption_but_teacher_manual_zero_allowed(self):
        import teacher_total
        self.f.mutate(self.sid,lambda r:r['dual_review']['results']['B']['sub_questions'][0]['holistic_result'].update(level_id='unknown'))
        self.assertEqual(next(p for p in self.f.preview()['papers'] if p['id']==self.sid)['state'],'review')
        with closing(app.get_db()) as c,c:
            row=teacher_total.read(c,self.sid);p=teacher_total.prepare(c,row,app);self.assertIsNone(p['suggestions']['B'])
            with patch.object(app,'submission_photo_was_viewed',return_value=True):teacher_total.confirm(c,row,dict(exam_id=self.f.exam,expected_fingerprint=p['expected_fingerprint'],source='teacher',final_total=0,confirmed=True),app,'synthetic-teacher')
        self.assertEqual(self.f.f.paired()['subjective']['score'],0)

    def test_mixed_all_blank_auto_confirms_and_remains_exportable(self):
        import teacher_total
        with closing(app.get_db()) as c,c:
            row=teacher_total.read(c,self.sid);result=json.loads(row['result_json']);raw=essay(self.qs[0]);raw.update(answer_status='unanswered_candidate',level_id=None,suggested_total=None,needs_review=True,summary='本题区域完整清晰，确认未作答')
            model=hs.normalize(dict(sub_questions=[raw]),self.qs[:1],1)
            other=app.normalize_sub_question_result(dict(confidence=.99,sub_questions=[dict(sub_question_id=q['id'],answer_status='unanswered_candidate',summary='本题区域完整清晰，确认未作答',criteria=[]) for q in self.qs[1:]]),self.qs[1:],1)
            model.update(sub_questions=model['sub_questions']+other['sub_questions'],max_total=24)
            models=result['dual_review']['models']
            for side in ('A','B'):models[side]['model_id']=side
            original={side:copy.deepcopy(model) for side in ('A','B')}
            effective,receipt=blank_consensus.resolve(original,models,{},self.qs,[dict(quality=dict(ok=True))]);self.assertIsNotNone(receipt)
            result.update(copy.deepcopy(effective['A']));result['dual_review'].update(results=effective,raw_results=original,blank_resolution=receipt,comparison=app.compare_dual_review_results(effective['A'],effective['B']))
            c.execute('UPDATE submissions SET result_json=? WHERE id=?',(json.dumps(result),self.sid))
            self.assertTrue(blank_consensus.auto_confirm(c,self.sid,app))
        self.assertEqual(self.f.f.paired()['subjective']['score'],0)
        with closing(app.get_db()) as c:
            p=teacher_total.prepare(c,teacher_total.read(c,self.sid),app);self.assertEqual(p['suggestions'],{'A':0,'B':0})
