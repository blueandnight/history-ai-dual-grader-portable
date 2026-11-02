import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
import app
import server_dual_config
from tests import test_teacher_accounts as account_fixture


class ServerDualConfigurationTests(unittest.TestCase):
    setUp = account_fixture.TeacherAccountTests.setUp
    tearDown = account_fixture.TeacherAccountTests.tearDown
    request_json = account_fixture.TeacherAccountTests.request_json
    login = account_fixture.TeacherAccountTests.login
    cookie_value = staticmethod(account_fixture.TeacherAccountTests.cookie_value)
    admin_headers = account_fixture.TeacherAccountTests.admin_headers
    create_teacher = account_fixture.TeacherAccountTests.create_teacher
    teacher_headers = account_fixture.TeacherAccountTests.teacher_headers
    # Reuse real login/CSRF/teacher fixtures; only these dedicated methods run here.
    def test_admin_replace_reuse_restart_and_origin_binding(self):
        admin = self.admin_headers()
        secret = 'SYNTHETIC_NEW_QWEN_KEY'
        with patch.object(app, 'open_model_request', side_effect=AssertionError('network forbidden')) as provider:
            status, body, _ = self.request_json('POST', '/api/dual-review/configuration', {'side':'A','preset_id':'qwen35-plus','api_key':secret}, admin)
            self.assertEqual(status, 200, body)
            self.assertNotIn(secret, json.dumps(body))
            self.assertEqual(app.load_dual_review_model_configs(('A',))['A']['api_key'], secret)
            path=app.CONFIG_DIR/'server-secrets/dual-review.json'
            self.assertTrue(path.is_file())
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            # No in-memory/env mutation is required to reload the saved pair.
            self.assertEqual(app.read_dual_review_preset_ids()['A'], 'qwen35-plus')
            for key in ('', 'SYNTHETIC_REPLACEMENT_QWEN_KEY'):
                status, body, _ = self.request_json('POST', '/api/dual-review/configuration', {'side':'A','preset_id':'qwen36-plus','api_key':key}, admin)
                self.assertEqual(status, 200, body)
                self.assertEqual(app.load_dual_review_model_configs(('A',))['A']['api_key'], key or secret)
            status, data, _=self.request_json('GET','/api/dual-review/status',headers=admin)
            self.assertEqual(status,200)
            self.assertEqual(data['preset_a'],'qwen36-plus')
            self.assertNotIn('SYNTHETIC_REPLACEMENT_QWEN_KEY',json.dumps(data))
            with patch.dict(os.environ,{'OPENAI_API_KEY':'','HISTORY_GRADER_PROVIDER':'deepseek','HISTORY_GRADER_API_KEY':'SYNTHETIC_OTHER_PROVIDER'}):
                before=path.read_bytes()
                status, _, _=self.request_json('POST','/api/dual-review/configuration',{'side':'A','preset_id':'openai-gpt-41','api_key':''},admin)
                self.assertEqual(status,400)
                self.assertEqual(path.read_bytes(),before)
            conn=app.get_db()
            try:
                audit=''.join(r[0] for r in conn.execute('SELECT payload_json FROM audit_events'))
            finally:conn.close()
            self.assertNotIn(secret,audit);self.assertNotIn('SYNTHETIC_REPLACEMENT_QWEN_KEY',audit)
            provider.assert_not_called()

    def test_teacher_anonymous_csrf_origin_and_invalid_targets_are_rejected(self):
        admin=self.admin_headers();self.create_teacher(admin);teacher=self.teacher_headers()
        payload={'side':'B','preset_id':'mimo-v26-flash','api_key':'SYNTHETIC_MIMO_NEW'}
        for headers,expected in [({'Origin':self.origin},401),(teacher,403),({k:v for k,v in admin.items() if k!='X-CSRF-Token'},403),({**admin,'Origin':'https://attacker.invalid'},403)]:
            status,_,_=self.request_json('POST','/api/dual-review/configuration',payload,headers)
            self.assertEqual(status,expected)
        for extra in [{'base_url':'https://attacker.invalid'}, {'side':'C'}, {'preset_id':'custom'}, {'api_key':'bad\nkey'}]:
            status,_,_=self.request_json('POST','/api/dual-review/configuration',{**payload,**extra},admin)
            self.assertEqual(status,400)
        self.assertFalse((app.CONFIG_DIR/'server-secrets/dual-review.json').exists())

    def test_side_scoping_env_precedence_and_store_failure_preserve_configuration(self):
        admin=self.admin_headers()
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'SYNTHETIC_OLD_ENV_KEY'}):
            for side,key in [('A','SYNTHETIC_A_NEW'),('B','SYNTHETIC_B_NEW')]:
                status,data,_=self.request_json('POST','/api/dual-review/configuration',{'side':side,'preset_id':'deepseek-v41-flash','api_key':key},admin)
                self.assertEqual(status,200,data)
            configs=app.load_dual_review_model_configs()
            self.assertEqual(configs['A']['api_key'],'SYNTHETIC_A_NEW');self.assertEqual(configs['B']['api_key'],'SYNTHETIC_B_NEW')
            self.assertEqual(os.environ['DEEPSEEK_API_KEY'],'SYNTHETIC_OLD_ENV_KEY')
        path=app.CONFIG_DIR/'server-secrets/dual-review.json';before=path.read_bytes()
        with patch.object(server_dual_config.os,'replace',side_effect=OSError('synthetic denied')):
            status,body,_=self.request_json('POST','/api/dual-review/configuration',{'side':'A','preset_id':'qwen35-plus','api_key':'SYNTHETIC_FAILED_KEY'},admin)
        self.assertEqual(status,400);self.assertEqual(path.read_bytes(),before);self.assertNotIn('SYNTHETIC_FAILED_KEY',json.dumps(body))
        path.write_text('{invalid',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'无法安全读取'):app.load_dual_review_model_configs()
