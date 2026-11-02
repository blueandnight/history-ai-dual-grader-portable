"""Synthetic exam-copy isolation; never opens business databases or providers."""
import io
import json
import unittest
import zipfile
from contextlib import closing
from unittest.mock import patch
import app
import exam_copy
from tests import test_no_identity_workflow as workflow
from tests import test_exam_layout_profile as layout_fixture


class ExamCopyTests(unittest.TestCase):
    def setUp(self):
        self.f=workflow.NoIdentityWorkflowTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
    def copy(self,name='第二场合成考试'):
        status,data=self.f.http.request_json_status('/api/copy-exam',{'assignment_id':self.f.aid,'name':name})
        self.assertEqual(status,201,data)
        return data
    def test_scores_and_exports_are_isolated_without_changing_original(self):
        f=self.f;old_a,old_e=f.aid,f.exam
        f.grade(f.f.sid);f.confirm(f.f.sid,0);old_o=f.objective()
        self.assertEqual(f.client.post('/api/answer-links',json=dict(exam_id=old_e,objective_submission_id=old_o,subjective_submission_id=f.f.sid,confirmed=True,originals_checked=True)).status_code,201)
        tables=('assignments','grading_exams','submissions','objective_assessments','objective_submissions','objective_jobs','objective_answers','exam_answer_links')
        with closing(app.get_db()) as c:
            before={t:{tuple(r) for r in c.execute('SELECT * FROM '+t)} for t in tables}
            original=dict(c.execute('SELECT * FROM assignments WHERE id=?',(old_a,)).fetchone())
        copied=self.copy(original['name']);new_a,new_e=copied['assignment']['id'],copied['exam_id']
        self.assertNotEqual(old_a,new_a);self.assertNotEqual(old_e,new_e)
        with closing(app.get_db()) as c:
            new=c.execute('SELECT * FROM assignments WHERE id=?',(new_a,)).fetchone()
            for key in ('question','standard_answer','criteria_json','sub_questions_json'):self.assertEqual(new[key],original[key])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM submissions WHERE assignment_id=?',(new_a,)).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM objective_assessments WHERE source_exam_id=?',(new_e,)).fetchone()[0],0)
        self.assertEqual(f.client.get('/api/unified-scores?exam_id='+new_e+'&records=all').get_json()['rows'],[])
        f.aid,f.exam=new_a,new_e;f.key=f.f.f.snapshot(new_e).get_json()['assessment_id']
        sid=f.subject();f.grade(sid);f.confirm(sid,4);oid=f.objective()
        self.assertEqual(f.client.post('/api/answer-links',json=dict(exam_id=new_e,objective_submission_id=oid,subjective_submission_id=sid,confirmed=True,originals_checked=True)).status_code,201)
        cross=f.client.post('/api/answer-links',json=dict(exam_id=old_e,objective_submission_id=oid,subjective_submission_id=f.f.sid,confirmed=True,originals_checked=True))
        self.assertEqual(cross.status_code,400,cross.get_json())
        for eid,total in ((old_e,60),(new_e,64)):
            data=f.client.get('/api/unified-scores?exam_id='+eid+'&records=all').get_json()
            self.assertEqual(len(data['rows']),1);self.assertEqual(data['rows'][0]['total_score'],total)
            report=f.client.get('/api/unified-scores/report?exam_id='+eid+'&records=all').get_json()
            self.assertEqual(report['complete_count'],1);self.assertEqual(report['mean'],total)
            self.assertEqual(report['objective_questions']['1']['count'],1)
            exported=f.client.get('/api/unified-scores/export.xlsx?exam_id='+eid+'&records=all')
            self.assertEqual(exported.status_code,200)
            with zipfile.ZipFile(io.BytesIO(exported.data)) as z:self.assertIn('统一成绩',z.read('xl/workbook.xml').decode())
        with closing(app.get_db()) as c:
            for t in tables:self.assertTrue(before[t].issubset({tuple(r) for r in c.execute('SELECT * FROM '+t)}),t)
            a=json.loads(c.execute('SELECT import_metadata_json FROM assignments WHERE id=?',(new_a,)).fetchone()[0]);b=json.loads(original['import_metadata_json'])
            for key in ('objective_questions','objective_answer_key','objective_points','material_questions','teacher_confirmation_status'):self.assertEqual(a[key],b[key])
        f.f.f.provider.assert_not_called()
    def test_bad_source_and_name_create_nothing(self):
        with closing(app.get_db()) as c:before=c.execute('SELECT COUNT(*) FROM assignments').fetchone()[0]
        for source,name in ((self.f.aid,''),('missing','valid'),(self.f.aid,'x'*201)):
            status,_=self.f.http.request_json_status('/api/copy-exam',dict(assignment_id=source,name=name));self.assertEqual(status,400)
        with closing(app.get_db()) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM assignments').fetchone()[0],before)

    def test_copy_requires_authentication_and_csrf(self):
        from tests.test_password_auth import PasswordAuthTests
        f=PasswordAuthTests();f.setUp();self.addCleanup(f.tearDown)
        body={'assignment_id':self.f.aid,'name':'合成考试'}
        self.assertEqual(f.request_json('POST','/api/copy-exam',body,{'Origin':f.origin})[0],401)
        status,_,headers=f.login();self.assertEqual(status,200)
        self.assertEqual(f.request_json('POST','/api/copy-exam',body,{'Origin':f.origin,'Cookie':f.cookie_value(headers)})[0],403)


class ExamCopyLayoutTests(unittest.TestCase):
    def test_layout_assets_and_document_are_independent(self):
        f=layout_fixture.ExamLayoutProfileTests();f.setUp();self.addCleanup(f.tearDown)
        _,status=f._post_regions(f._two_regions());self.assertEqual(status,200)
        with closing(app.get_db()) as c:
            original=app._exam_layout_profile_context(c,f.exam_id)
            metadata=json.loads(c.execute('SELECT import_metadata_json FROM assignments WHERE id=?',(f.assignment_id,)).fetchone()[0])
            doc=app.DATA_DIR/f.assignment_id/'imports'/'content.docx';doc.write_bytes(b'SYNTHETIC DOCUMENT')
            metadata['source']['content_source']={'type':'docx','stored_path':doc.relative_to(app.PORTABLE_ROOT).as_posix()}
            c.execute('UPDATE assignments SET import_metadata_json=? WHERE id=?',(json.dumps(metadata),f.assignment_id));c.commit()
        with patch.object(app,'open_model_request',side_effect=AssertionError('No model calls')):
            copied=exam_copy.duplicate(app,f.assignment_id,'合成新版式考试')
        self.assertTrue(copied['layout_copied'])
        with closing(app.get_db()) as c:
            context=app._exam_layout_profile_context(c,copied['exam_id'])
            self.assertEqual(context['layout_profile']['regions'],original['layout_profile']['regions'])
            self.assertNotEqual(context['layout_profile_id'],original['layout_profile_id'])
            self.assertEqual(context['layout_profile']['assignment_id'],copied['assignment']['id'])
            meta=json.loads(c.execute('SELECT import_metadata_json FROM assignments WHERE id=?',(copied['assignment']['id'],)).fetchone()[0])
        paths=[meta['source']['content_source']['stored_path'],context['source_pdf_relative_path']]+[r['image_file'] for r in context['references']]
        for path in paths:
            self.assertTrue(app.resolve_data_relative_path(path).is_file())
            self.assertNotIn(f.assignment_id,path);self.assertNotIn(f.exam_id,path)
        self.assertEqual(doc.read_bytes(),b'SYNTHETIC DOCUMENT')

    def test_invalid_layout_is_not_reused(self):
        f=layout_fixture.ExamLayoutProfileTests();f.setUp();self.addCleanup(f.tearDown)
        f._post_regions(f._two_regions())
        with closing(app.get_db()) as c:
            context=app._exam_layout_profile_context(c,f.exam_id)
        original_pdf=app.resolve_data_relative_path(context['source_pdf_relative_path'])
        original_pdf.write_bytes(b'SYNTHETIC CHANGED PDF')
        copied=exam_copy.duplicate(app,f.assignment_id,'合成失效版式考试')
        self.assertFalse(copied['layout_copied']);self.assertTrue(copied['warnings'])
        with closing(app.get_db()) as c:
            context=app._exam_layout_profile_context(c,copied['exam_id'])
            self.assertIsNone(context['layout_profile']);self.assertFalse(context['has_pdf_reference'])
        self.assertEqual(original_pdf.read_bytes(),b'SYNTHETIC CHANGED PDF')

if __name__=='__main__':unittest.main()
