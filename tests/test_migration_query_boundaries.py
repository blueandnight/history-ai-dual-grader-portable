from contextlib import closing
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import app,grading_core,model_gateway,storage
from tests import test_rubric_score_consistency as fixtures


class MigrationQueryBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.previous=app.APP_PATHS
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name);app.configure_paths(root/'runtime',root/'program')
    def tearDown(self):
        app._install_paths(self.previous);self.temp.cleanup()

    def test_normalizers_keep_runtime_callbacks_and_teacher_scoring_consistent(self):
        q=fixtures.question(maximum=4)
        with patch.object(app,'normalize_model_result',wraps=app.normalize_model_result) as single:
            result=app.normalize_sub_question_result(fixtures.model_output(q,{0,1}),[q])
        self.assertEqual(result['suggested_total'],4);self.assertEqual(single.call_count,1)
        with patch.object(grading_core,'normalize_sub_question_result',wraps=grading_core.normalize_sub_question_result) as core:
            app.normalize_sub_question_result(fixtures.model_output(q,{0}),[q])
        self.assertIs(core.call_args.kwargs['normalize_model_result'],app.normalize_model_result)

    def test_public_record_serialization_keeps_scope_and_anonymous_identity_redaction(self):
        qs=[{'id':'Q1','criteria':[]},{'id':'Q2','criteria':[]}]
        result={'grading_scope':{'graded_question_ids':['Q2']},'student_identity':{'student_name':'合成身份字段'}}
        with closing(app.get_db()) as c:
            c.execute("INSERT INTO assignments(id,name,question,standard_answer,criteria_json,sub_questions_json,created_at) VALUES(?,?,?,?,?,?,?)",('a'*12,'合成','题','答','[]',json.dumps(qs),app.utc_now()))
            c.execute("INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state) VALUES(?,?,?,?,?,?,?,?)",('b'*12,'a'*12,'合成标签','','[]',json.dumps(result),app.utc_now(),'completed'))
            c.commit();row=dict(c.execute('SELECT * FROM submissions').fetchone())
        row.update(marking_code_id='in-memory-anonymous-case',marking_code='ANON-SYNTHETIC')
        actual=app.submission_public(row)
        self.assertEqual([q['id'] for q in actual['teacher_scoring_questions']],['Q2'])
        self.assertNotIn('student_identity',actual['result']);self.assertEqual(actual['student_label'],'ANON-SYNTHETIC')
        self.assertEqual(actual['student_name'],'');self.assertIn('student_identity',json.loads(row['result_json']))
        self.assertEqual(app.submission_list_public(row)['student_name'],'')
        self.assertIsNotNone(app.load_submission_public('b'*12))

    def test_query_failure_closes_the_injected_connection(self):
        connection=Mock();connection.execute.side_effect=RuntimeError('synthetic database read error')
        with self.assertRaisesRegex(RuntimeError,'synthetic database'):
            storage.submission_public({'result_json':'{}','state':'completed','assignment_id':'synthetic'},get_db=lambda:connection)
        connection.close.assert_called_once()
        connection.reset_mock()
        with self.assertRaisesRegex(RuntimeError,'synthetic database'):
            storage.load_submission_public('synthetic',get_db=lambda:connection,submission_public=lambda row:row)
        connection.close.assert_called_once()

    def test_read_queries_keep_parameters_and_identity_namespaces(self):
        with closing(app.get_db()) as c:
            c.execute('INSERT INTO roster_students(id,class_name,student_no,student_name,identity_code,created_at) VALUES(?,?,?,?,?,?)',('student-id','合成 & 特殊班','number-1','合成学生','07',app.utc_now()));c.commit()
            classes=storage.roster_class_rows(c)
            self.assertEqual(classes[0]['student_count'],1)
            row=storage.roster_student_rows(c,'合成 & 特殊班')[0]
            self.assertEqual((row['student_id'],row['student_no'],row['identity_code']),('student-id','number-1','07'))
            self.assertEqual(storage.roster_student_rows(c,"' OR 1=1 --"),[])
            self.assertEqual(storage.assignment_submission_rows(c,"' OR 1=1 --"),[])

    def test_gateway_endpoint_keeps_https_validation_and_config_callback(self):
        for url in ['http://outside.example/v1','https://user:password@example.test/v1']:
            with self.assertRaises(ValueError):app.validate_model_base_url(url)
        self.assertEqual(app.model_chat_completions_endpoint({'base_url':'http://127.0.0.1:1234/v1'}),'http://127.0.0.1:1234/v1/chat/completions')
        with patch.object(app,'validate_model_base_url',return_value=('https://synthetic.test/custom','https://synthetic.test')) as callback:
            self.assertEqual(app.model_chat_completions_endpoint({'base_url':'synthetic'}),'https://synthetic.test/custom/chat/completions')
        callback.assert_called_once_with('synthetic')
