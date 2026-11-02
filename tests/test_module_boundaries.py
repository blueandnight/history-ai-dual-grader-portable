"""Exercise extracted boundaries without application-global injection."""
from contextlib import closing
import io,json,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import app,grading_core,model_gateway,storage


class ModuleBoundaryTests(unittest.TestCase):
    def test_modules_import_without_loading_application_or_opening_database(self):
        code="import sqlite3,socket; sqlite3.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('database side effect')); socket.socket.connect=lambda *a: (_ for _ in ()).throw(AssertionError('network side effect')); import grading_core,storage,model_gateway,sys; assert 'app' not in sys.modules"
        subprocess.run([sys.executable,'-B','-c',code],cwd=Path(app.__file__).parent,check=True,capture_output=True)

    def test_scoring_entry_points_delegate_and_bank_cap_is_unchanged(self):
        q={'id':'Q','max_score':8,'scoring_mode':'point_bank','point_value':2,'criteria':[{'id':str(i),'score':2} for i in range(8)]}
        decisions=[{'question_id':'Q','rubric_point_id':str(i),'decision':'full'} for i in range(8)]
        with patch.object(grading_core,'teacher_deterministic_score',wraps=grading_core.teacher_deterministic_score) as run:
            result=app.teacher_deterministic_score([q],decisions)
        run.assert_called_once_with([q],decisions)
        self.assertEqual(result['final_total'],8)
        self.assertEqual(result['question_subtotals'][0]['raw_total'],16)

    def test_storage_rolls_back_and_closes_a_failed_schema_callback(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);db=root/'database.sqlite3';opened=[]
            def fail(c,existed):
                opened.append(c);self.assertFalse(existed)
                c.execute('BEGIN');c.execute('CREATE TABLE synthetic(id)')
                raise RuntimeError('synthetic schema failure')
            with self.assertRaisesRegex(RuntimeError,'synthetic schema failure'):
                storage.connect_database(root,db,fail)
            with self.assertRaises(sqlite3.ProgrammingError):opened[0].execute('SELECT 1')
            with closing(sqlite3.connect(db)) as c:self.assertFalse(c.execute("SELECT name FROM sqlite_master WHERE name='synthetic'").fetchall())

    def test_gateway_keeps_redirect_rejection_and_app_mock_seam(self):
        with self.assertRaisesRegex(ValueError,'重定向'):
            model_gateway.NoRedirectHandler().redirect_request(None,None,302,'',{},'https://elsewhere.test')
        fake=Mock()
        with patch.object(app,'build_opener',return_value=fake) as factory:
            app.open_model_request('synthetic-request',timeout=7)
        factory.assert_called_once_with(app.NoRedirectHandler)
        fake.open.assert_called_once_with('synthetic-request',timeout=7)
        raw={'choices':[{'message':{'content':'{"synthetic":true}'},'finish_reason':'stop'}],'usage':{'prompt_tokens':2,'completion_tokens':3}}
        diagnostics={}
        self.assertEqual(app.read_model_chat_content(io.BytesIO(json.dumps(raw).encode()),diagnostics),'{"synthetic":true}')
        self.assertEqual(diagnostics['token_usage']['input_tokens'],2)
        self.assertIs(app.ModelProviderResponseError,model_gateway.ModelProviderResponseError)
