"""Version evidence captured by real upload/worker paths, with local model stubs."""
from contextlib import closing
import json
import unittest
from unittest.mock import patch
import app
from tests import test_formal_batch_workflow as formal
from unified_scores import subjective_version


class SubjectiveVersionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.f=formal.FormalBatchWorkflowTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        with closing(app.get_db()) as c,c:
            c.execute("UPDATE grading_exams SET layout_profile_id=NULL,layout_profile_json='{}' WHERE id=?",(self.f.exam_id,))

    def run_worker(self, dual, expected_version=None):
        with patch.object(app,'model_configured',return_value=True):
            body,preview=self.f.original_preview()
            status,batch=self.f.request('/api/batches',dict(assignment_id=self.f.assignment_id,exam_id=self.f.exam_id,grading_input_mode='original_photo',batch_request_id='version-proof',items=[{'page_count':1}]))
            self.assertEqual(status,201,batch)
            upload={**body,'batch_id':batch['batch_id'],'sequence':1,'submission_id':batch['items'][0]['submission_id'],'dual_review_enabled':dual,'ai_privacy_confirmed':True,'privacy_status':'no_identity_visible','ai_privacy_preview_token':preview['preview_token']}
            status,saved=self.f.request('/api/submissions',upload)
            self.assertEqual(status,202,saved)
            sid=saved['submission']['id']
            # Request metadata is normally set by the dual upload UI; pin test mode explicitly.
            with closing(app.get_db()) as c,c:
                queued=json.loads(c.execute('SELECT result_json FROM submissions WHERE id=?',(sid,)).fetchone()[0]);queued['dual_review_requested']=dual
                c.execute('UPDATE submissions SET result_json=? WHERE id=?',(json.dumps(queued),sid))
            def model(assignment,questions,answer,photos,diagnostics,*args):
                diagnostics.update(input_hash='synthetic-identical',prompt_hash='synthetic-identical')
                return dict(mode='vision-model',blocked=False,suggested_total=4,max_total=4,confidence=1,
                    sub_questions=[dict(sub_question_id=q['id'],answer_status='answered',max_total=q['max_score'],suggested_total=q['max_score'],criteria=[dict(criterion_id=p['id'],decision='full',max_score=p['score'],suggested_score=p['score'],student_evidence='synthetic') for p in q['criteria']]) for q in questions])
            with patch.object(app,'call_vision_model',side_effect=model) as calls:
                app.process_submission(sid)
            self.assertEqual(calls.call_count,2 if dual else 1)
        with closing(app.get_db()) as c:
            result=json.loads(c.execute('SELECT result_json FROM submissions WHERE id=?',(sid,)).fetchone()[0])
            assignment=c.execute('SELECT * FROM assignments WHERE id=?',(self.f.assignment_id,)).fetchone()
            source=json.loads(assignment['sub_questions_json'])
            self.assertIn('subjective_version_evidence',result)
            self.assertEqual(bool(result.get('dual_review')),dual)
            ids=result['grading_scope']['graded_question_ids']
            self.assertEqual(subjective_version(result,assignment,source,ids),expected_version)
        return sid,result

    def test_single_model_worker_captures_complete_evidence(self):
        self.run_worker(False)

    def test_dual_model_worker_captures_complete_evidence(self):
        self.run_worker(True)

    def test_evidence_is_frozen_before_model_and_not_current_content(self):
        original=app.grading_core.subjective_version_evidence
        def capture(assignment,source,questions):
            evidence=original(assignment,source,questions)
            with closing(app.get_db()) as c,c:
                c.execute("UPDATE assignments SET question='changed while grading' WHERE id=?",(self.f.assignment_id,))
            return evidence
        with patch.object(app.grading_core,'subjective_version_evidence',side_effect=capture):
            self.run_worker(False,'subjective_content_version_mismatch')
        with closing(app.get_db()) as c:
            result=json.loads(c.execute("SELECT result_json FROM submissions WHERE state='completed'").fetchone()[0])
            assignment=c.execute('SELECT * FROM assignments WHERE id=?',(self.f.assignment_id,)).fetchone()
            self.assertNotEqual(result['subjective_version_evidence']['assignment_question'],assignment['question'])
            self.assertEqual(subjective_version(result,assignment,json.loads(assignment['sub_questions_json']),result['grading_scope']['graded_question_ids']),'subjective_content_version_mismatch')
