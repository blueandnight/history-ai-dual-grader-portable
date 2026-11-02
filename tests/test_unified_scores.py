"""Synthetic read-only unified results; no real students or model requests."""
from contextlib import closing
import json
import copy
import sqlite3
import unittest
from unittest.mock import patch
import app
from tests import test_answer_links as links
from tests.test_unified_exam import mixed_draft


def seed_final(conn, sid, partial=False, zero=False):
    assignment = conn.execute('SELECT a.* FROM submissions s JOIN assignments a ON a.id=s.assignment_id WHERE s.id=?', (sid,)).fetchone()
    questions = json.loads(conn.execute('SELECT a.sub_questions_json FROM submissions s JOIN assignments a ON a.id=s.assignment_id WHERE s.id=?', (sid,)).fetchone()[0])
    source_questions = copy.deepcopy(questions)
    if partial:
        questions = questions[:1]
    ids = [q['id'] for q in questions]
    maximum = sum(q['max_score'] for q in questions)
    total = 0 if zero else maximum
    scope = dict(graded_question_ids=ids, scoped_max_score=maximum)
    result = dict(mode='vision-model', grading_scope=scope,
                  sub_questions=[dict(sub_question_id=q['id'], max_total=q['max_score'], answer_status='answered') for q in questions],
                  dual_review=dict(teacher_final=dict(final_total=total, grading_scope=scope,
                      question_subtotals=[dict(question_id=q['id'], subtotal=0 if zero else q['max_score']) for q in questions])))
    # Synthetic evidence is fixed when this fixture simulates a grading run.
    evidence=app.grading_core.subjective_version_evidence(assignment,source_questions,app.clean_sub_questions(questions))
    result['subjective_version_evidence']=evidence
    result['grading_context']={'rubric_sha256':evidence['rubric_hash']}
    result['dual_review']['rubric_hash']=evidence['rubric_hash']
    conn.execute('UPDATE submissions SET result_json=?,final_total=?,confirmed_at=? WHERE id=?', (json.dumps(result), total, app.utc_now(), sid))


def seed_answers(conn, oid, zero=False):
    key = json.loads(conn.execute('SELECT a.answer_key_json FROM objective_submissions s JOIN objective_assessments a ON a.assessment_id=s.assessment_id WHERE s.submission_id=?', (oid,)).fetchone()[0])
    for k in key:
        conn.execute("INSERT INTO objective_answers(submission_id,question_no,final_option,max_points,awarded_points,state,source) VALUES(?,?,?,?,?,'auto','omr')", (oid,k['question_no'],'B' if zero else k['correct_option'],k['points'],0 if zero else k['points']))
    conn.execute('UPDATE objective_submissions SET score=?,max_score=? WHERE submission_id=?', (0 if zero else sum(k['points'] for k in key),sum(k['points'] for k in key),oid))


class UnifiedScoreTests(unittest.TestCase):
    def setUp(self):
        self.f = links.AnswerLinkTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.client = self.f.client
        with closing(app.get_db()) as c,c:
            for oid,sid in zip(self.f.obj,self.f.sub):
                seed_answers(c,oid); seed_final(c,sid)
        self.link = self.f.write().get_json()['link']

    def get(self):
        r = self.client.get('/api/unified-scores?exam_id='+self.f.exam)
        self.assertEqual(r.status_code,200,r.get_json())
        return r.get_json()['rows']

    def paired(self):
        return next(r for r in self.get() if r['link_id']==self.link['id'])

    def test_complete_readonly_and_unlinked(self):
        with closing(sqlite3.connect(app.DB_PATH)) as c:
            before = list(c.iterdump())
        row = self.paired()
        self.assertEqual((row['total_score'],row['total_max']),(68,68))
        self.assertTrue(row['complete'])
        self.assertEqual(len(self.get()),3)
        self.assertTrue(all(r['total_score'] is None for r in self.get() if not r['link_id']))
        with closing(sqlite3.connect(app.DB_PATH)) as c:
            self.assertEqual(before,list(c.iterdump()))

    def test_side_export_keeps_unlinked_grades_and_deduplicates_ids(self):
        import score_export
        projection=self.client.get('/api/unified-scores?exam_id='+self.f.exam).get_json()
        projection['rows'].append(copy.deepcopy(projection['rows'][0]))
        for side in ('objective','subjective'):
            data=score_export.rows(projection,'Synthetic',False,side=side)
            self.assertEqual(len(data),3)
            self.assertEqual(len({r[-1] for r in data[1:]}),2)
            self.assertTrue(all(r[6] is not None for r in data[1:]))
            r=self.client.get('/api/unified-scores/export.csv?exam_id='+self.f.exam+'&side='+side+'&scope=complete')
            self.assertEqual(r.status_code,200)
            self.assertEqual(len(r.data.decode('utf-8-sig').splitlines()),3)

    def test_real_teacher_confirmation_path_produces_eligible_record(self):
        with closing(app.get_db()) as c,c:
            questions=json.loads(c.execute('SELECT sub_questions_json FROM assignments WHERE id=?',(self.f.assignment,)).fetchone()[0])
            result=json.loads(c.execute('SELECT result_json FROM submissions WHERE id=?',(self.f.sub[0],)).fetchone()[0])
            result['dual_review']={'results':{'A':{},'B':{}},'rubric_hash':result['dual_review']['rubric_hash']}
            c.execute('UPDATE submissions SET result_json=?,confirmed_at=NULL,final_total=NULL WHERE id=?',(json.dumps(result),self.f.sub[0]))
        choices=[dict(question_id=q['id'],rubric_point_id=p['id'],choice='teacher_full') for q in questions for p in q['criteria']]
        status,saved=self.f.fixture.f.request_json_status('/api/confirm',dict(submission_id=self.f.sub[0],teacher_point_decisions=choices))
        self.assertEqual(status,200,saved)
        self.assertEqual(self.paired()['total_score'],68)

    def test_unconfirmed_and_reconfirmation_block(self):
        for sql in ("UPDATE submissions SET confirmed_at=NULL WHERE id=?", "UPDATE submissions SET confirmed_at='synthetic',result_json=json_set(result_json,'$.teacher_reconfirmation_required',json('true')) WHERE id=?"):
            with closing(app.get_db()) as c,c:
                c.execute(sql,(self.f.sub[0],))
            row=self.paired();self.assertIsNone(row['total_score']);self.assertIsNone(row['subjective']['score']);self.assertEqual(row['objective']['score'],60)

    def test_objective_states_and_unresolved_answers(self):
        for state in ('needs_review','recoverable','failed','processing'):
            with closing(app.get_db()) as c,c:
                c.execute('UPDATE objective_submissions SET state=? WHERE submission_id=?',(state,self.f.obj[0]))
            self.assertIsNone(self.paired()['total_score'])
        with closing(app.get_db()) as c,c:
            c.execute("UPDATE objective_submissions SET state='completed' WHERE submission_id=?",(self.f.obj[0],))
            c.execute("UPDATE objective_answers SET state='needs_review' WHERE submission_id=? AND question_no=1",(self.f.obj[0],))
        self.assertIsNone(self.paired()['total_score'])

    def test_partial_scope_does_not_claim_full_exam(self):
        with closing(app.get_db()) as c,c:seed_final(c,self.f.sub[0],partial=True)
        row=self.paired();self.assertEqual(row['subjective']['score'],4);self.assertEqual(row['subjective']['max_score'],4);self.assertIsNone(row['total_score'])

    def test_unlink_and_wrong_exam_or_identity(self):
        _, other = self.f.fixture.create()
        with closing(app.get_db()) as c,c:
            c.execute('UPDATE submissions SET assignment_id=? WHERE id=?',(other['assignment']['id'],self.f.sub[0]))
        self.assertIsNone(self.paired()['total_score']);self.assertIn('关联异常',self.paired()['status'])
        with closing(app.get_db()) as c,c:c.execute('UPDATE submissions SET assignment_id=? WHERE id=?',(self.f.assignment,self.f.sub[0]))
        self.f.write(action='unlink',link_id=self.link['id'])
        self.assertTrue(all(r['total_score'] is None for r in self.get()))

    def test_changed_snapshot_keeps_historical_score(self):
        with closing(app.get_db()) as c,c:
            c.execute("UPDATE assignments SET import_metadata_json=json_set(import_metadata_json,'$.objective_points.\"1\"',4) WHERE id=?",(self.f.assignment,))
        row=self.paired();self.assertEqual(row['objective']['score'],60);self.assertEqual(row['objective']['max_score'],60);self.assertIsNone(row['total_score']);self.assertIn('版本不同',row['status'])

    def test_zero_is_valid_and_null_is_not(self):
        with closing(app.get_db()) as c,c:
            c.execute('DELETE FROM objective_answers WHERE submission_id=?',(self.f.obj[0],));seed_answers(c,self.f.obj[0],True);seed_final(c,self.f.sub[0],zero=True)
        self.assertEqual(self.paired()['total_score'],0)
        with closing(app.get_db()) as c,c:c.execute('UPDATE submissions SET final_total=NULL WHERE id=?',(self.f.sub[0],))
        self.assertIsNone(self.paired()['total_score'])

    def test_pure_subjective_and_pure_objective(self):
        # A single-type exam does not need a dummy opposite submission or link.
        with closing(app.get_db()) as c,c:
            c.execute('UPDATE exam_answer_links SET active=0')
            c.execute('DELETE FROM objective_answers');c.execute('DELETE FROM objective_submissions')
            c.execute("UPDATE assignments SET import_metadata_json=json_set(import_metadata_json,'$.objective_questions',json('[]')) WHERE id=?",(self.f.assignment,))
        self.assertTrue(all(r['complete'] and r['total_score']==8 for r in self.get()))
        # Separate fully synthetic pure-objective formal exam.
        d,confirmation=mixed_draft(subjective=False)
        _,saved=self.f.fixture.create(d,confirmation);self.f.fixture.snapshot(saved['exam_id'])
        with closing(app.get_db()) as c,c:
            old=c.execute('SELECT * FROM objective_jobs LIMIT 1').fetchone()
            aid=c.execute('SELECT assessment_id FROM objective_assessments WHERE source_exam_id=?',(saved['exam_id'],)).fetchone()[0]
            c.execute('UPDATE objective_jobs SET assessment_id=? WHERE job_id=?',(aid,old['job_id']))
            c.execute("INSERT INTO objective_submissions(submission_id,job_id,assessment_id,anonymous_code,state,score,max_score,page_count,created_at) VALUES('pure',?,?, 'pure','completed',60,60,1,?)",(old['job_id'],aid,app.utc_now()))
            seed_answers(c,'pure')
        result=self.client.get('/api/unified-scores?exam_id='+saved['exam_id']).get_json()['rows'][0]
        self.assertTrue(result['complete']);self.assertEqual(result['total_score'],60)

    def test_denied_and_missing_exam(self):
        self.assertEqual(self.client.get('/api/unified-scores?exam_id='+'f'*32).status_code,404)
        from objective.app import create_app
        denied=create_app(data_dir=app.DATA_DIR/'denied-scores',database_path=app.DB_PATH,initialize_schema=False,auth_enabled=False,source_exam_provider=lambda _=None:(_ for _ in ()).throw(PermissionError('outside scope'))).test_client()
        self.assertEqual(denied.get('/api/unified-scores?exam_id='+self.f.exam).status_code,403)

    def test_open_review_blocks_completed_objective(self):
        with closing(app.get_db()) as c,c:
            job=c.execute('SELECT job_id FROM objective_submissions WHERE submission_id=?',(self.f.obj[0],)).fetchone()[0]
            c.execute("INSERT INTO objective_review_items(review_item_id,dedupe_key,job_id,submission_id,question_no,reason,state,created_at) VALUES('review','synthetic',?,?,1,'synthetic','open',?)",(job,self.f.obj[0],app.utc_now()))
        self.assertIsNone(self.paired()['total_score'])

    def test_invalid_numeric_and_missing_answer_records_block(self):
        for field,value in [('score',None),('score',-1),('score',float('inf')),('score',59),('max_score',61)]:
            with closing(app.get_db()) as c,c:
                c.execute('UPDATE objective_submissions SET score=60,max_score=60 WHERE submission_id=?',(self.f.obj[0],))
                c.execute('UPDATE objective_submissions SET '+field+'=? WHERE submission_id=?',(value,self.f.obj[0]))
            self.assertIsNone(self.paired()['total_score'])
        with closing(app.get_db()) as c,c:
            c.execute('UPDATE objective_submissions SET score=60,max_score=60 WHERE submission_id=?',(self.f.obj[0],))
            c.execute('DELETE FROM objective_answers WHERE submission_id=? AND question_no=1',(self.f.obj[0],))
        self.assertIsNone(self.paired()['total_score'])

    def test_subjective_blocks_and_current_maximum_change(self):
        for field,value in [('simulation_model',True),('blocked',True),('unmapped_answers',[{'question':'unknown'}])]:
            with closing(app.get_db()) as c,c:
                seed_final(c,self.f.sub[0])
                row=c.execute('SELECT result_json FROM submissions WHERE id=?',(self.f.sub[0],)).fetchone()
                r=json.loads(row[0]);r[field]=value
                c.execute('UPDATE submissions SET result_json=? WHERE id=?',(json.dumps(r),self.f.sub[0]))
            self.assertIsNone(self.paired()['total_score'])
        with closing(app.get_db()) as c,c:
            seed_final(c,self.f.sub[0])
            c.execute("UPDATE assignments SET sub_questions_json=json_set(sub_questions_json,'$[0].max_score',5) WHERE id=?",(self.f.assignment,))
        row=self.paired();self.assertEqual(row['subjective']['score'],8);self.assertIsNone(row['total_score'])

    def test_corrupt_cross_exam_link_never_exposes_external_submission(self):
        _,other=self.f.fixture.create()
        with closing(app.get_db()) as c,c:
            c.execute('UPDATE submissions SET exam_id=?,assignment_id=? WHERE id=?',(other['exam_id'],other['assignment']['id'],self.f.sub[0]))
        row=self.paired();self.assertIsNone(row['subjective']);self.assertIsNone(row['total_score']);self.assertIn('关联异常',row['status'])

    def test_same_ids_and_max_content_changes_block_original_reproduction(self):
        self.assertEqual(self.paired()['total_score'],68)
        with closing(app.get_db()) as c:
            original=c.execute('SELECT sub_questions_json FROM assignments WHERE id=?',(self.f.assignment,)).fetchone()[0]
        for change in ('question','rubric_text','answer','distribution','mode','material'):
            with self.subTest(change=change):
                questions=json.loads(original)
                if change=='question':questions[0]['question']='不同题干、相同题号和满分'
                elif change=='rubric_text':questions[0]['criteria'][0]['label']='不同评分点内容'
                elif change=='answer':questions[0]['standard_answer']='不同参考答案'
                elif change=='distribution':
                    questions[0]['criteria'][0]['score']=1;questions[0]['criteria'][1]['score']=3
                elif change=='mode':questions[0]['scoring_mode']='point_bank'
                else:questions[0]['question']+='\n不同材料上下文'
                with closing(app.get_db()) as c,c:
                    c.execute('UPDATE assignments SET sub_questions_json=? WHERE id=?',(json.dumps(questions),self.f.assignment))
                row=self.paired();self.assertFalse(row['complete']);self.assertIsNone(row['total_score'])
                self.assertEqual(row['subjective']['score'],8);self.assertEqual(row['objective']['score'],60)
                self.assertEqual(row['subjective']['version_status'],'subjective_content_version_mismatch')
        with closing(app.get_db()) as c,c:c.execute('UPDATE assignments SET sub_questions_json=? WHERE id=?',(original,self.f.assignment))
        self.assertEqual(self.paired()['total_score'],68)

    def test_assignment_level_context_and_answer_changes_block(self):
        for field in ('question','standard_answer'):
            with self.subTest(field=field):
                with closing(app.get_db()) as c,c:
                    old=c.execute('SELECT '+field+' FROM assignments WHERE id=?',(self.f.assignment,)).fetchone()[0]
                    c.execute('UPDATE assignments SET '+field+'=? WHERE id=?',('changed context',self.f.assignment))
                self.assertEqual(self.paired()['subjective']['version_status'],'subjective_content_version_mismatch')
                with closing(app.get_db()) as c,c:c.execute('UPDATE assignments SET '+field+'=? WHERE id=?',(old,self.f.assignment))

    def test_missing_or_inconsistent_historical_evidence_is_not_backfilled(self):
        with closing(app.get_db()) as c:
            original=c.execute('SELECT result_json FROM submissions WHERE id=?',(self.f.sub[0],)).fetchone()[0]
        for change in ('no_evidence','no_context','no_dual_hash','wrong_context_hash','corrupt_snapshot','wrong_scope','no_content_hash'):
            with self.subTest(change=change):
                result=json.loads(original)
                if change=='no_evidence':result.pop('subjective_version_evidence')
                elif change=='no_context':result.pop('grading_context')
                elif change=='no_dual_hash':result['dual_review'].pop('rubric_hash')
                elif change=='wrong_context_hash':result['grading_context']['rubric_sha256']='0'*64
                elif change=='corrupt_snapshot':result['subjective_version_evidence']['scoring_sub_questions'][0]['question']='tampered'
                elif change=='wrong_scope':
                    e=result['subjective_version_evidence'];e['scoring_sub_questions']=e['scoring_sub_questions'][:1];e['content_hash']=app.rubric_fingerprint([{k:v for k,v in e.items() if k!='content_hash'}])
                else:result['subjective_version_evidence'].pop('content_hash')
                with closing(app.get_db()) as c,c:
                    c.execute('UPDATE submissions SET result_json=? WHERE id=?',(json.dumps(result),self.f.sub[0]))
                with closing(sqlite3.connect(app.DB_PATH)) as c:before=list(c.iterdump())
                row=self.paired();self.assertIsNone(row['total_score']);self.assertEqual(row['subjective']['score'],8)
                self.assertEqual(row['subjective']['version_status'],'subjective_version_unverifiable')
                with closing(sqlite3.connect(app.DB_PATH)) as c:self.assertEqual(before,list(c.iterdump()))

    def test_single_model_evidence_and_serialization_order_are_valid(self):
        with closing(app.get_db()) as c,c:
            result=json.loads(c.execute('SELECT result_json FROM submissions WHERE id=?',(self.f.sub[0],)).fetchone()[0])
            result['teacher_final']=result.pop('dual_review')['teacher_final']
            c.execute('UPDATE submissions SET result_json=? WHERE id=?',(json.dumps(result,sort_keys=True),self.f.sub[0]))
        self.assertEqual(self.paired()['total_score'],68)


if __name__=='__main__':unittest.main()
