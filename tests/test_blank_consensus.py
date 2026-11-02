"""Synthetic postprocessing and isolated persistence; no provider calls."""
import copy
import json
import unittest
from contextlib import closing
from unittest.mock import patch

import app
import blank_consensus as blank


class BlankPolicyTests(unittest.TestCase):
    def setUp(self):
        self.questions = app.clean_sub_questions([dict(id=f'Q{i}', label=f'17.{i}', question='合成题',
            standard_answer='合成答案', max_score=2,
            criteria=[dict(id='C1', label='合成评分点', score=2)]) for i in range(1, 4)])
        self.raw = dict(confidence=.99, sub_questions=[dict(id=q['id'], answer_status='unanswered_candidate',
            summary='本题答题区域完整可见，图像清晰，确认未作答', criteria=[]) for q in self.questions])
        self.metadata = {s:dict(status='ok', provider=s, model_id=s, input_hash='a'*64, finish_reason='stop') for s in ('A','B')}

    def normalized(self, raw=None):
        return app.normalize_sub_question_result(raw or self.raw, self.questions)

    def resolve(self, a=None, b=None, photos=None, errors=None):
        return blank.resolve({'A':a or self.normalized(), 'B':b or self.normalized()},
            self.metadata, errors or {}, self.questions, photos or [{'quality':{'ok':True}}])

    def test_three_blanks_zero_and_raw_retained(self):
        a = self.normalized(); before=copy.deepcopy(a)
        effective, receipt = self.resolve(a=a)
        self.assertEqual(a, before)
        self.assertEqual(receipt['question_ids'], ['Q1','Q2','Q3'])
        for side in effective.values():
            self.assertFalse(side['blocked']); self.assertEqual(side['suggested_total'],0)
            self.assertTrue(all(q['answer_status']=='blank_confirmed' for q in side['sub_questions']))
        self.assertEqual(app.compare_dual_review_results(**dict(result_a=effective['A'],result_b=effective['B']))['risk_level'],'NO_CONFLICT')

    def test_partial_and_disagreement(self):
        raw=copy.deepcopy(self.raw)
        raw['sub_questions'][1]=dict(id='Q2',answer_status='answered',criteria=[dict(id='C1',decision='full',suggested_score=2,student_evidence='合成作答')])
        a=self.normalized(raw)
        effective, receipt=self.resolve(a=a,b=copy.deepcopy(a))
        self.assertEqual(receipt['question_ids'],['Q1','Q3'])
        self.assertEqual(effective['A']['sub_questions'][1],a['sub_questions'][1])
        _,receipt=self.resolve(a=a)
        self.assertEqual(receipt['question_ids'],['Q1','Q3'])

    def test_uncertain_blur_missing_and_failures_never_zero(self):
        for text in ('疑似空白，图像清晰，答题区域完整','图像模糊，无法识别','缺页，未作答','未作答'):
            raw=copy.deepcopy(self.raw)
            for q in raw['sub_questions']:q['summary']=text
            self.assertIsNone(self.resolve(a=self.normalized(raw),b=self.normalized(raw))[1])
        self.assertIsNone(self.resolve(photos=[{'quality':{'ok':False}}])[1])
        self.assertIsNone(self.resolve(errors={'B':{'error_category':'timeout'}})[1])
        raw=copy.deepcopy(self.raw);raw['sub_questions']=[]
        self.assertIsNone(self.resolve(a=self.normalized(raw))[1])
        raw=copy.deepcopy(self.raw);raw['sub_questions'].pop()
        effective,receipt=self.resolve(a=self.normalized(raw),b=self.normalized(raw))
        self.assertEqual(receipt['question_ids'],['Q1','Q2'])
        self.assertTrue(effective['A']['blocked'])
        self.assertIsNone(effective['A']['sub_questions'][2]['suggested_total'])

    def test_no_evidence_status_alone_and_alias_not_confirmed(self):
        for change in ({'summary':''},{'confidence':.4},{'answer_status':'blank'}, {'region_complete':False}, {'suggested_total':2}, {'criteria':[{'student_evidence':'存在答案','decision':'full','suggested_score':2}]}):
            raw=copy.deepcopy(self.raw)
            for q in raw['sub_questions']:q.update(change)
            self.assertIsNone(self.resolve(a=self.normalized(raw),b=self.normalized(raw))[1])
        self.metadata['B']['finish_reason']=['stop','length']
        self.assertIsNone(self.resolve()[1])

    def test_region_mapping_flag_clears_only_resolved_blanks(self):
        a=self.normalized();a['requires_question_mapping']=True
        effective,_=self.resolve(a=a,b=copy.deepcopy(a))
        self.assertFalse(effective['A']['requires_question_mapping'])

    def test_prompt_unchanged(self):
        import hashlib
        from pathlib import Path
        source=Path(app.__file__).read_text(encoding='utf-8')
        start=source.index('def build_vision_prompt_text(')
        prompt=source[start:source.index('\ndef ',start+5)]
        self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(), '5740f7f5497cbbe084eea8ff4776b07c97ceb04780544620d06ae53213ef933c')

    def test_actual_dual_entry_and_repeated_scope_projection(self):
        configs={s:dict(provider=s,model=s) for s in ('A','B')}
        def call(*args):
            args[4].update(input_hash='a'*64,finish_reason='stop')
            return self.normalized()
        with patch.object(app,'load_dual_review_model_configs',return_value=configs), patch.object(app,'call_vision_model',side_effect=call):
            result=app.grade_submission_dual({'id':'synthetic'},self.questions,'',[],[{'quality':{'ok':True}}],True,None,True,None)
        self.assertTrue(blank.valid(result))
        self.assertEqual(result['suggested_total'],0)
        self.assertFalse(result['blocked'])
        self.assertEqual(result['dual_review']['raw_results']['A']['sub_questions'][0]['answer_status'],'unanswered_candidate')
        scope=dict(result['grading_scope'],source='answer_regions')
        app.apply_grading_scope_to_result(result,self.questions,scope)
        self.assertTrue(blank.valid(result));self.assertEqual(result['suggested_total'],0)


class BlankPersistenceTests(unittest.TestCase):
    def setUp(self):
        from tests.test_batch_adoption import AdoptionTests
        self.f=AdoptionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_zero_persists_projects_exports_and_teacher_can_override(self):
        import teacher_total
        import unified_scores
        sid=self.f.ids[0]
        with closing(app.get_db()) as conn,conn:
            row=teacher_total.read(conn,sid); result=json.loads(row['result_json'])
            questions=self.f.questions
            raw=dict(confidence=.99,sub_questions=[dict(id=q['id'],answer_status='unanswered_candidate',summary='答题区域完整可见且图像清晰，确认未作答',criteria=[]) for q in questions])
            normalized=app.normalize_sub_question_result(raw,questions)
            originals={s:copy.deepcopy(normalized) for s in ('A','B')}
            dual=result['dual_review'];dual['models']={s:dict(status='ok',provider=s,model_id=s,input_hash='a'*64) for s in ('A','B')}
            effective,receipt=blank.resolve(originals,dual['models'],{},questions,[{'quality':{'ok':True}}])
            dual.update(results=effective,raw_results=originals,blank_resolution=receipt,comparison=app.compare_dual_review_results(effective['A'],effective['B']))
            result.update(copy.deepcopy(effective['A']));result['dual_review']=dual
            conn.execute('UPDATE submissions SET result_json=? WHERE id=?',(json.dumps(result),sid))
            self.assertTrue(blank.auto_confirm(conn,sid,app));self.assertFalse(blank.auto_confirm(conn,sid,app))
            row=teacher_total.read(conn,sid);result=json.loads(row['result_json'])
            self.assertEqual(row['final_total'],0);self.assertTrue(row['confirmed_at'])
            self.assertNotIn('point_decisions',result['automatic_blank_zero'])
            source=json.loads(row['sub_questions_json']); maxima={unified_scores._question_key(q['id']):q['max_score'] for q in source}
            side=unified_scores.subjective(row,maxima,row,source)
            self.assertTrue(side['complete']);self.assertEqual(side['score'],0)
            self.assertTrue(teacher_total.current_confirmation(row,row,app))
            before=list(conn.iterdump());unified_scores.subjective(row,maxima,row,source);self.assertEqual(before,list(conn.iterdump()))
            conn.commit()
            paper=next(p for p in self.f.preview()['papers'] if p['id']==sid)
            self.assertEqual(paper['state'],'confirmed')
            self.assertEqual(paper['final_total'],0)
            paired=next(r for r in self.f.f.get() if r.get('subjective',{}).get('id')==sid)
            self.assertTrue(paired['complete']);self.assertEqual(paired['subjective']['score'],0)
            self.assertEqual(paired['subjective']['ai_reference'],{'A':0,'B':0})
            self.assertEqual(set(paired['subjective']['ai_reference_state'].values()),{'完整AI参考'})
            # A label alone is not proof of blankness: tampering invalidates the receipt.
            tampered=copy.deepcopy(result);tampered['dual_review']['blank_resolution']['raw_results_sha256']='invalid'
            self.assertEqual(unified_scores.valid_ai_subscores(tampered,row,source,result['grading_scope']['graded_question_ids']),{'A':{},'B':{}})

            import score_export
            exported=score_export.csv_bytes(score_export.rows({'exam_id':self.f.exam,'rows':[paired]},'合成考试',False)).decode('utf-8-sig')
            self.assertIn('明确空白自动0分',exported)
            # Scope/content changes do not reinterpret the historical zero.
            changed=dict(row);changed['question']='不同题干'
            self.assertFalse(unified_scores.subjective(row,maxima,changed,source)['complete'])
            result['dual_review']['results']['B']['sub_questions'][0]['suggested_total']=2
            altered=dict(row);altered['result_json']=json.dumps(result)
            self.assertFalse(unified_scores.subjective(altered,maxima,row,source)['valid'])
            prepared=teacher_total.prepare(conn,row,app)
            with patch.object(app,'submission_photo_was_viewed',return_value=True):
                teacher_total.confirm(conn,row,dict(exam_id=row['exam_id'],expected_fingerprint=prepared['expected_fingerprint'],confirmed=True,source='teacher',final_total=1),app,'synthetic-teacher')
            self.assertEqual(teacher_total.read(conn,sid)['final_total'],1)


if __name__ == '__main__':
    unittest.main()

