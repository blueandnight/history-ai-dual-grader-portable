"""Synthetic explicit link ownership tests; no grading mutations or model calls."""
from contextlib import closing
import json
import unittest
import uuid
import app
from tests import test_unified_exam as stage_one

class AnswerLinkTests(unittest.TestCase):
 def setUp(self):
  self.fixture=stage_one.UnifiedExamTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
  self.client=self.fixture.client
  _,saved=self.fixture.create();self.exam=saved['exam_id'];self.assignment=saved['assignment']['id'];snapshot=self.fixture.snapshot(self.exam).get_json()
  self.obj=[uuid.uuid4().hex for _ in range(2)];self.sub=[uuid.uuid4().hex[:12] for _ in range(2)]
  with closing(app.get_db()) as c,c:
   job=uuid.uuid4().hex
   c.execute("INSERT INTO objective_jobs(job_id,assessment_id,mode,grouping,status,total_files,total_submissions,created_at) VALUES(?,?,'omr','one_per_image','completed',2,2,?)",(job,snapshot['assessment_id'],app.utc_now()))
   for oid,sid in zip(self.obj,self.sub):
    c.execute("INSERT INTO objective_submissions(submission_id,job_id,assessment_id,anonymous_code,state,score,max_score,page_count,created_at) VALUES(?,?,?,?,'completed',60,60,1,?)",(oid,job,snapshot['assessment_id'],oid,app.utc_now()))
    c.execute("INSERT INTO submissions(id,assignment_id,exam_id,student_label,answer_text,photos_json,result_json,created_at,state,final_total) VALUES(?,?,?,'synthetic','','[]','{}',?,'completed',8)",(sid,self.assignment,self.exam,app.utc_now()))
   self.before=self.grades(c)
 def grades(self,c):
  return {t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in ('submissions','objective_submissions','objective_assessments')}
 def write(self,**kwargs):
  body={'exam_id':self.exam,'objective_submission_id':self.obj[0],'subjective_submission_id':self.sub[0],'confirmed':True,'originals_checked':True,'reason':'原图上同一考生的明确标识，经人工核对'};body.update(kwargs)
  return self.client.post('/api/answer-links',json=body,environ_overrides={'history_grader.actor':'synthetic_teacher'})
 def test_link_idempotent_unlink_and_history_keep_scores(self):
  a=self.write();self.assertEqual(a.status_code,201,a.get_json());link=a.get_json()['link'];self.assertEqual(link['actor'],'synthetic_teacher')
  self.assertTrue(self.write().get_json()['reused'])
  listing=self.client.get('/api/answer-links?exam_id='+self.exam).get_json();self.assertEqual(len(listing['links']),1)
  self.assertEqual(self.write(action='unlink',link_id=link['id']).status_code,200)
  self.assertTrue(self.write(action='unlink',link_id=link['id']).get_json()['reused'])
  with closing(app.get_db()) as c:self.assertEqual(self.grades(c),self.before);self.assertEqual(c.execute('SELECT active FROM exam_answer_links').fetchone()[0],0)
 def test_one_to_one_atomic_correction_and_conflict_rollback(self):
  first=self.write().get_json()['link'];self.assertEqual(self.write(subjective_submission_id=self.sub[1]).status_code,409)
  self.assertEqual(self.write(objective_submission_id=self.obj[1]).status_code,409)
  corrected=self.write(action='replace',link_id=first['id'],subjective_submission_id=self.sub[1]);self.assertEqual(corrected.status_code,201)
  second=self.write(objective_submission_id=self.obj[1],subjective_submission_id=self.sub[0]);self.assertEqual(second.status_code,201)
  self.assertEqual(self.write(action='replace',link_id=corrected.get_json()['link']['id'],subjective_submission_id=self.sub[0]).status_code,409)
  with closing(app.get_db()) as c:
   self.assertEqual(c.execute('SELECT COUNT(*) FROM exam_answer_links WHERE active=1').fetchone()[0],2);self.assertEqual(self.grades(c),self.before)
   self.assertEqual(c.execute("SELECT COUNT(*) FROM objective_audit_events WHERE event_type='answer_link_corrected'").fetchone()[0],1)
 def test_cross_exam_nonexistent_and_missing_source_are_blocked(self):
  _,other=self.fixture.create()
  with closing(app.get_db()) as c,c:c.execute('UPDATE submissions SET exam_id=? WHERE id=?',(other['exam_id'],self.sub[0]))
  self.assertEqual(self.write().status_code,400)
  self.assertEqual(self.write(subjective_submission_id='f'*12).status_code,404)
  with closing(app.get_db()) as c,c:
   c.execute('UPDATE submissions SET exam_id=? WHERE id=?',(self.exam,self.sub[0]));c.execute('UPDATE objective_assessments SET source_exam_id=NULL')
  self.assertEqual(self.write().status_code,400)
 def test_explicit_pair_required_but_retired_code_does_not_block(self):
  for field,value in [('confirmed',False),('originals_checked',False)]:self.assertEqual(self.write(**{field:value}).status_code,400)
  with closing(app.get_db()) as c,c:
   c.execute("INSERT INTO exam_marking_codes(id,exam_id,code,submission_id,created_at,identity_issue) VALUES(?,?,?,?,?,'duplicate_submission')",(uuid.uuid4().hex,self.exam,'synthetic',self.sub[0],app.utc_now()))
  self.assertEqual(self.write(reason='').status_code,201)
  with closing(app.get_db()) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM exam_answer_links').fetchone()[0],1)
 def test_get_is_readonly_and_does_not_return_other_exam_candidates(self):
  for _ in range(2):self.assertEqual(self.client.get('/api/answer-links?exam_id='+self.exam).status_code,200)
  self.assertEqual(self.client.get('/api/answer-links?exam_id='+'f'*32).status_code,404)
  with closing(app.get_db()) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM exam_answer_links').fetchone()[0],0)

 def test_scope_denial_and_actor_cannot_be_forged(self):
  from objective.app import create_app
  denied=create_app(data_dir=app.DATA_DIR/'denied-links',database_path=app.DB_PATH,initialize_schema=False,auth_enabled=False,source_exam_provider=lambda _=None:(_ for _ in ()).throw(PermissionError('outside exam scope'))).test_client()
  self.assertEqual(denied.get('/api/answer-links?exam_id='+self.exam).status_code,403)
  self.assertEqual(denied.post('/api/answer-links',json={'exam_id':self.exam}).status_code,403)
  link=self.write(actor='forged-user').get_json()['link'];self.assertEqual(link['actor'],'synthetic_teacher')
  with closing(app.get_db()) as c,c:
   c.execute("UPDATE assignments SET import_metadata_json='{}' WHERE id=?",(self.assignment,))
  # Removing an old association does not depend on today's answer-key readiness.
  self.assertEqual(self.write(action='unlink',link_id=link['id']).status_code,200)
