"""New configuration/support boundaries using isolated data and synthetic providers."""
from contextlib import closing
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import zipfile
import app
from tests import test_batch_order as batch_fixture
from tests import test_teacher_accounts as account_fixture


def custom(preset='custom-aaaaaaaaaaaa', base='https://example.invalid/v1'):
    return {'id':preset,'label':'合成模型','base_url':base,'model':'synthetic-vision','note':'合成'}


class DesktopFeatureTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ,{'HISTORY_GRADER_CLOUD_MODE':'0','HISTORY_GRADER_WEB_AUTH_REQUIRED':'0'}))
        self.fixture=batch_fixture.OrderedBatchTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.provider=self.enterContext(patch.object(app,'open_model_request',side_effect=AssertionError('network forbidden')))

    def query(self, **extra):
        return self.fixture.request_json_status('/api/custom-models/list',{'base_url':'https://example.invalid/v1','confirm_send':True,**extra})

    def test_registered_custom_pair_and_selected_deletion_guard(self):
        app.write_custom_model_presets([custom()])
        self.assertFalse(app.dual_review_preset_allowed('custom'))
        self.assertFalse(app.dual_review_preset_allowed('custom-unknown'))
        for side in ('A','B'):
            app.write_dual_review_preset_ids(side,'custom-aaaaaaaaaaaa')
            self.assertEqual(app.dual_review_model_specs()[side]['api_origin'],'https://example.invalid')
        status,data=self.fixture.request_json_status('/api/custom-models/delete',{'preset_id':'custom-aaaaaaaaaaaa'})
        self.assertEqual(status,400);self.assertEqual(len(app.read_custom_model_presets()),1)
        self.provider.assert_not_called()

    def test_catalog_filters_deduplicates_and_uses_only_supplied_key(self):
        data={'data':[{'id':'vision-1'},{'id':'vision-1'},{'id':'<img src=x>'},{'id':'bad\nvalue'}, {'id':123},{}]}
        self.provider.side_effect=None;self.provider.return_value=io.BytesIO(json.dumps(data).encode())
        code,result=self.query(api_key='SYNTHETIC_TYPED_KEY');self.assertEqual(code,200)
        self.assertEqual(result['models'],['vision-1','<img src=x>'])
        req=self.provider.call_args.args[0];self.assertEqual(req.full_url,'https://example.invalid/v1/models')
        self.assertEqual(req.method,'GET');self.assertEqual(req.get_header('Authorization'),'Bearer SYNTHETIC_TYPED_KEY')
        self.assertIsNone(req.data);self.assertEqual(self.provider.call_args.kwargs['timeout'],15)
        self.provider.return_value=io.BytesIO(b'{"data":[]}');self.query()
        self.assertIsNone(self.provider.call_args.args[0].get_header('Authorization'))
        self.provider.return_value=io.BytesIO(b'{"data":[]}');self.query(base_url='https://example.invalid/v1/chat/completions')
        self.assertEqual(self.provider.call_args.args[0].full_url,'https://example.invalid/v1/models')
        with self.assertRaisesRegex(ValueError,'重定向'):
            app.NoRedirectHandler().redirect_request(req,None,302,'',{},'https://other.invalid/models')

    def test_catalog_bad_input_does_not_connect(self):
        for extra in ({'confirm_send':False},{'base_url':'http://192.168.1.2/v1'}, {'base_url':'https://u:p@example.invalid/v1'},
                      {'base_url':'https://example.invalid/v1?token=SYNTHETIC'}, {'api_key':'bad\nkey'}):
            self.assertEqual(self.query(**extra)[0],400)
        self.provider.assert_not_called()

    def test_catalog_response_size_shape_timeout_and_redirect_fail_safely(self):
        for raw in (b'x'*(1024*1024+1),b'not json',b'[]',b'{"data":{}}'):
            self.provider.side_effect=None;self.provider.return_value=io.BytesIO(raw)
            code,data=self.query();self.assertEqual(code,400);self.assertNotIn('not json',json.dumps(data))
        for error in (TimeoutError('PRIVATE_SYNTHETIC'),URLError('PRIVATE_SYNTHETIC'),HTTPError('https://example.invalid',302,'PRIVATE_SYNTHETIC',{},None)):
            self.provider.side_effect=error
            code,data=self.query();self.assertEqual(code,400);self.assertNotIn('PRIVATE_SYNTHETIC',json.dumps(data))

    def test_diagnostic_zip_is_readonly_and_drops_arbitrary_audit_context(self):
        with closing(app.get_db()) as c,c:
            app.append_audit_event(c,'submission_completed','submission','PRIVATE_ENTITY',{
                'grading_context':{'model':'PRIVATE_MODEL','api_key':'PRIVATE_SECRET','nested':{'student':'PRIVATE_STUDENT'}},'answer':'PRIVATE_ANSWER'})
        with closing(app.get_db()) as c:before='\n'.join(c.iterdump())
        with urlopen(self.fixture.base_url+'/api/diagnostics-export',timeout=10) as response:
            self.assertIn('application/zip',response.headers['Content-Type']);body=response.read()
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            self.assertEqual(sorted(z.namelist()),['README.txt','diagnostics.json']);text=z.read('diagnostics.json').decode()
            data=json.loads(text);self.assertTrue(data['audit_chain']['chain_valid']);self.assertEqual(data['counts']['assignments'],1)
            for forbidden in ('PRIVATE_','grading_context','entity_id',str(app.DATA_DIR),'api_key'):
                self.assertNotIn(forbidden,text)
        with closing(app.get_db()) as c:self.assertEqual(before,'\n'.join(c.iterdump()))
        self.provider.assert_not_called()


class CloudFeaturePermissionTests(unittest.TestCase):
    setUp=account_fixture.TeacherAccountTests.setUp
    tearDown=account_fixture.TeacherAccountTests.tearDown
    request_json=account_fixture.TeacherAccountTests.request_json
    login=account_fixture.TeacherAccountTests.login
    cookie_value=staticmethod(account_fixture.TeacherAccountTests.cookie_value)
    admin_headers=account_fixture.TeacherAccountTests.admin_headers
    create_teacher=account_fixture.TeacherAccountTests.create_teacher
    teacher_headers=account_fixture.TeacherAccountTests.teacher_headers

    def test_diagnostics_and_model_catalog_require_operator_and_catalog_is_desktop_only(self):
        self.assertEqual(self.request_json('GET','/api/diagnostics-export')[0],401)
        admin=self.admin_headers();self.create_teacher(admin);teacher=self.teacher_headers()
        with patch.object(app,'open_model_request',side_effect=AssertionError('network forbidden')) as provider:
            for path,method,payload in [('/api/diagnostics-export','GET',None),('/api/custom-models/list','POST',{})]:
                self.assertEqual(self.request_json(method,path,payload,teacher)[0],403)
            self.assertEqual(self.request_json('POST','/api/custom-models/list',{},admin)[0],403)
            self.assertEqual(self.request_json('POST','/api/custom-models/list',{}, {'Cookie':admin['Cookie'],'Origin':self.origin})[0],403)
            with self.opener.open(Request(self.base+'/api/diagnostics-export',headers=admin),timeout=10) as response:
                with zipfile.ZipFile(io.BytesIO(response.read())) as z:
                    self.assertEqual(sorted(z.namelist()),['README.txt','diagnostics.json'])
            provider.assert_not_called()

    def test_cloud_custom_pair_keeps_private_origin_bound_credentials(self):
        admin=self.admin_headers();app.write_custom_model_presets([custom()])
        with patch.object(app,'open_model_request',side_effect=AssertionError('network forbidden')) as provider:
            code,data,_=self.request_json('POST','/api/dual-review/configuration',{'side':'A','preset_id':'custom-aaaaaaaaaaaa','api_key':'SYNTHETIC_CUSTOM_KEY'},admin)
            self.assertEqual(code,200,data);self.assertNotIn('SYNTHETIC_CUSTOM_KEY',json.dumps(data))
            self.assertEqual(app.load_dual_review_model_configs(('A',))['A']['api_key'],'SYNTHETIC_CUSTOM_KEY')
            app.write_custom_model_presets([custom(base='https://other.invalid/v1')])
            self.assertFalse(app.dual_review_key_is_configured('A'))
            with self.assertRaises(ValueError):app.load_dual_review_model_configs(('A',))
            self.assertEqual(self.request_json('POST','/api/dual-review/configuration',{'side':'A','preset_id':'custom-aaaaaaaaaaaa','api_key':''},admin)[0],400)
            provider.assert_not_called()


if __name__=='__main__':unittest.main()
