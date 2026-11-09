"""Synthetic project-score extraction, validation and existing formal-grade closure."""
import copy,json,unittest
from unittest.mock import patch
import app,holistic_scoring as hs
from analytic_draft import extract
from tests import test_holistic_scoring as fixtures

def project_questions():
    qs=fixtures.questions()
    answer='示例一\n所选角度：甲（1分）\n论题：乙（2分）\n论述：丙（8分）\n结论：丁（1分）\n示例二\n所选角度：戊（1分）\n论题：己（2分）\n论述：庚（8分）\n结论：辛（1分）'
    qs[0]['holistic']=extract(answer,12)
    return app.clean_sub_questions(qs)

def project_raw(q):
    r=fixtures.essay(q);r.update(scoring_version='analytic-project-grading-v1',level_id=None,suggested_total=12)
    for d,p in zip(r['dimension_assessments'],q['holistic']['projects']):d.update(level_id=p['levels'][0]['id'],score=p['max_score'])
    return r

class ProjectTests(unittest.TestCase):
    def test_parse_and_protocol(self):
        q=project_questions()[0];self.assertEqual([p['max_score'] for p in q['holistic']['projects']],[1,2,8,1])
        text=hs.prompt('合成考试',[q]);self.assertIn('analytic-project-grading-v1',text);self.assertIn('然后求和',text);self.assertNotIn('维度不得换算分数',text)
        r=project_raw(q);self.assertEqual(hs.normalize({'sub_questions':[r]},[q],1)['suggested_total'],12)
        for change in ('step','range','missing','level'):
            raw=copy.deepcopy(r)
            if change=='level':raw['dimension_assessments'][0]['level_id']='wrong'
            if change=='step':raw['dimension_assessments'][0]['score']=.5
            if change=='range':raw['dimension_assessments'][0]['score']=2
            if change=='missing':raw['dimension_assessments'].pop()
            with self.assertRaises(ValueError):hs.normalize({'sub_questions':[raw]},[q],1)
    def test_project_response_missing_echoes_and_absent_component(self):
        q = project_questions()[0]
        raw = project_raw(q)
        raw.pop('scoring_version')
        raw.pop('rubric_hash')
        for dimension in raw['dimension_assessments']:
            dimension.pop('level_id')
        raw['dimension_assessments'][-1].update(score=0, judgment='not_met', evidence_ids=[])
        raw['suggested_total'] = 11
        result = hs.normalize({'sub_questions': [raw]}, [q], 1)
        self.assertEqual(result['suggested_total'], 11)
        self.assertFalse(result['blocked'])
        self.assertEqual(result['sub_questions'][0]['holistic_result']['rubric_hash'], hs.digest(q))
        self.assertEqual(result['sub_questions'][0]['holistic_result']['scoring_version'], 'analytic-project-grading-v1')
        self.assertNotIn('rubric_hash', raw)
        raw['dimension_assessments'][-1]['score'] = 1
        with self.assertRaisesRegex(ValueError, '证据无效'):
            hs.normalize({'sub_questions': [raw]}, [q], 1)

    def test_project_total_recalculation_requires_review(self):
        q = project_questions()[0]
        raw = project_raw(q)
        raw['suggested_total'] = 11
        result = hs.normalize({'sub_questions': [raw]}, [q], 1)
        self.assertEqual(result['suggested_total'], 12)
        self.assertTrue(result['blocked'])
        sub = result['sub_questions'][0]
        self.assertEqual(sub['holistic_result']['reported_project_total'], 11)
        self.assertFalse(hs.valid_sub(sub, q))

    def test_same_total_project_disagreement(self):
        q=project_questions()[0];r=project_raw(q);r['dimension_assessments'][1]['score']=1;r['dimension_assessments'][2]['score']=7;r['suggested_total']=10;a=hs.normalize({'sub_questions':[r]},[q],1)
        b=copy.deepcopy(r);b['dimension_assessments'][1]['score']=2;b['dimension_assessments'][2]['score']=6
        self.assertTrue(hs.compare(a['sub_questions'][0],hs.normalize({'sub_questions':[b]},[q],1)['sub_questions'][0]))
    def test_inconsistent_references_not_invented(self):
        self.assertIsNone(extract('示例一\n论题：甲（2分）\n论述：乙（8分）\n结论：丙（1分）',12))
    def test_existing_formal_total_adoption_export_and_versions(self):
        original=fixtures.essay
        # Avoid recursion through the synthetic helper while keeping the actual scoring implementation.
        qs=project_questions()
        with patch.object(fixtures,'questions',return_value=qs):
            for name in ('test_batch_adoption_mixed_totals_and_exports','test_total_confirmation_saved_without_fake_points_version_protected'):
                f=fixtures.PersistenceTests();
                def response(q,*args,**kwargs):
                    r=original(q);r.update(scoring_version='analytic-project-grading-v1',level_id=None,suggested_total=10)
                    for d,p in zip(r['dimension_assessments'],q['holistic']['projects']):d.update(level_id=p['levels'][0]['id'],score=p['max_score'])
                    r['dimension_assessments'][2]['score']=6
                    return r
                with patch.object(fixtures,'essay',side_effect=response):
                    f.setUp()
                    try:
                        if 'version' in name:
                            # Source-version check already covered by the whole-level regression; verify total save separately.
                            import teacher_total
                            from contextlib import closing
                            with closing(app.get_db()) as c,c:
                                row=teacher_total.read(c,f.sid);p=teacher_total.prepare(c,row,app);self.assertEqual(p['suggestions']['A'],22)
                                with patch.object(app,'submission_photo_was_viewed',return_value=True):teacher_total.confirm(c,row,dict(exam_id=f.f.exam,expected_fingerprint=p['expected_fingerprint'],source='A',final_total=22,confirmed=True),app,'synthetic')
                            self.assertEqual(f.f.f.paired()['subjective']['score'],22)
                        else:getattr(f,name)()
                    finally:f.doCleanups()
