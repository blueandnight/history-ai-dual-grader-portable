"""Synthetic report HTTP boundaries; no provider connection or real data."""
from contextlib import closing
import copy
import io
import json
import os
import unittest
from unittest.mock import patch
import app
from tests import test_batch_order as batch_fixture
from tests import test_password_auth as auth_fixture


class ExamReportHTTPTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'HISTORY_GRADER_CLOUD_MODE':'0', 'HISTORY_GRADER_WEB_AUTH_REQUIRED':'0'}))
        self.fixture = batch_fixture.OrderedBatchTests(); self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        with closing(app.get_db()) as c, c:
            c.execute("INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state) VALUES(?,?,?,?,?,?,?,?)",
                      ('b'*12,'a'*12,'SYNTHETIC_NAME','SYNTHETIC_ANSWER','[]',json.dumps({'max_total':4,'suggested_total':2,'sub_questions':[]}),app.utc_now(),'completed'))
        self.provider = self.enterContext(patch.object(app,'open_model_request',side_effect=AssertionError('External model forbidden')))

    def post(self, path='/api/exam-report', **extra):
        return self.fixture.request_json_status(path, {'assignment_id':'a'*12, **extra})

    def test_local_report_read_only_and_anonymous(self):
        with closing(app.get_db()) as c: before='\n'.join(c.iterdump())
        code, data = self.post(); self.assertEqual(code,200)
        self.assertEqual(data['report']['average'],2)
        self.assertNotIn('SYNTHETIC_NAME',json.dumps(data)); self.assertNotIn('SYNTHETIC_ANSWER',json.dumps(data))
        with closing(app.get_db()) as c: self.assertEqual(before,'\n'.join(c.iterdump()))
        self.provider.assert_not_called()

    def test_invalid_scope_consent_and_identity_never_call_provider(self):
        for extra in ({'assignment_id':'invalid'},{'exam_id':'e'*32},{'batch_id':'c'*12}):
            self.assertEqual(self.post(**extra)[0],400)
        self.assertEqual(self.post('/api/exam-report/ai')[0],400)
        self.assertEqual(self.post('/api/exam-report/ai',confirm_send=True,objectives='姓名：张测试')[0],400)
        self.provider.assert_not_called()

    def test_ai_request_contains_aggregate_only_and_failure_keeps_local_report(self):
        config={'provider':'deepseek','model':'synthetic','api_key':'SYNTHETIC_ONLY','base_url':'https://example.invalid/v1','timeout_seconds':'1'}
        response=io.BytesIO(json.dumps({'choices':[{'message':{'content':'# 合成分析'}}]}).encode())
        with patch.object(app,'dual_review_key_is_configured',return_value=True), patch.object(app,'load_dual_review_model_configs',return_value={'A':config}):
            self.provider.side_effect=None; self.provider.return_value=response
            code,data=self.post('/api/exam-report/ai',confirm_send=True,objectives='理解制度变迁')
            self.assertEqual(code,200); self.assertEqual(data['markdown'],'# 合成分析')
            outgoing=json.loads(self.provider.call_args.args[0].data)
            self.assertIn('理解制度变迁',outgoing['messages'][1]['content'])
            for forbidden in ('SYNTHETIC_NAME','SYNTHETIC_ANSWER','image_url','assignment_id','batch_id','exam_id'):
                self.assertNotIn(forbidden,json.dumps(outgoing))
            self.provider.side_effect=RuntimeError('SYNTHETIC_PRIVATE_RESPONSE')
            code,data=self.post('/api/exam-report/ai',confirm_send=True)
            self.assertEqual(code,400); self.assertNotIn('SYNTHETIC_PRIVATE_RESPONSE',json.dumps(data))
        self.assertEqual(self.post()[0],200)


class ExamReportAuthTests(unittest.TestCase):
    def test_anonymous_and_missing_csrf_denied(self):
        fixture=auth_fixture.PasswordAuthTests(); fixture.setUp(); self.addCleanup(fixture.tearDown)
        payload={'assignment_id':'a'*12}
        self.assertEqual(fixture.request_json('POST','/api/exam-report',payload, {'Origin':fixture.origin})[0],401)
        status,data,headers=fixture.login(); self.assertEqual(status,200)
        auth={'Cookie':fixture.cookie_value(headers),'Origin':fixture.origin}
        self.assertEqual(fixture.request_json('POST','/api/exam-report',payload,auth)[0],403)


class ReportSafetyTests(unittest.TestCase):
    def test_audit_whitelist_rejects_nested_private_fields_and_nonfinite(self):
        safe=app.safe_grading_context({'model':'synthetic','api_key':'SECRET','student_name':'PRIVATE','provider':{'key':'SECRET'},'model_max_tokens':float('inf')})
        self.assertEqual(safe,{'model':'synthetic'})

    def test_scope_does_not_reintroduce_missing_dual_summary(self):
        questions=app.clean_sub_questions([{'id':'Q1','label':'1','question':'synthetic','standard_answer':'synthetic','max_score':4,'criteria':[{'id':'C1','label':'point','score':4}]}])
        result={'suggested_total':3,'max_total':4,'sub_questions':[{'sub_question_id':'Q1','label':'1','answer_status':'answered','suggested_total':3,'max_total':4}],
                'dual_review':{'status':'degraded','results':{'A':{'suggested_total':3,'sub_questions':[{'sub_question_id':'Q1','label':'1','answer_status':'answered','suggested_total':3,'max_total':4}]}}}}
        outcome=app.apply_grading_scope_to_result(copy.deepcopy(result),questions,{'graded_question_ids':['Q1'],'scoped_max_score':4})
        self.assertIsNone(outcome['suggested_total']); self.assertEqual(outcome['dual_review']['results']['A']['suggested_total'],3)


if __name__=='__main__': unittest.main()
