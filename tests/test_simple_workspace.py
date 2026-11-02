"""Isolated synthetic acceptance of independent results and same-upload provenance."""
import base64
from contextlib import closing
import io
import json
import time
import unittest
import uuid
from unittest.mock import patch
import app
from tests import test_unified_scores as scores
from tests import test_unified_exam as exams


class IndependentResultsTests(unittest.TestCase):
    def setUp(self):
        self.f = scores.UnifiedScoreTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.client = self.f.client; self.exam = self.f.f.exam

    def test_fifty_objective_forty_subjective_without_identity(self):
        with closing(app.get_db()) as conn, conn:
            conn.execute('DELETE FROM exam_answer_links')
            objective = dict(conn.execute('SELECT * FROM objective_submissions LIMIT 1').fetchone())
            subjective = dict(conn.execute('SELECT * FROM submissions LIMIT 1').fetchone())
            for i in range(48):
                row = {**objective, 'submission_id':uuid.uuid4().hex,'anonymous_code':'SYN-'+str(i)}
                conn.execute('INSERT INTO objective_submissions('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))
                scores.seed_answers(conn,row['submission_id'])
            for i in range(38):
                row = {**subjective,'id':uuid.uuid4().hex[:12]}
                conn.execute('INSERT INTO submissions('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))
        report = self.client.get('/api/unified-scores/report?exam_id='+self.exam).get_json()
        self.assertEqual(report['objective_summary']['count'],50)
        self.assertEqual(report['subjective_summary']['count'],40)
        self.assertEqual(report['complete_count'],0)
        self.assertTrue(all(v['count']==50 and v['correct_rate']==1 for v in report['objective_questions'].values()))
        exported = self.client.get('/api/unified-scores/export.csv?exam_id='+self.exam)
        import csv
        rows = list(csv.DictReader(io.StringIO(exported.data.decode('utf-8-sig'))))
        workbook=self.client.get('/api/unified-scores/export.xlsx?exam_id='+self.exam)
        import zipfile
        from xml.etree import ElementTree as ET
        with zipfile.ZipFile(io.BytesIO(workbook.data)) as z:
            for name in z.namelist():ET.fromstring(z.read(name))
            self.assertIn('客观题统计',z.read('xl/workbook.xml').decode())
            self.assertIn('<v>50</v>',z.read('xl/worksheets/sheet3.xml').decode())
        self.assertEqual(len(rows),90)
        self.assertTrue(all(r['整卷总分']=='' for r in rows))
        self.assertEqual(len(self.client.get('/api/unified-scores/export.csv?scope=complete&exam_id='+self.exam).data.decode('utf-8-sig').splitlines()),1)

    def test_required_review_excluded_without_other_side_and_zero_preserved(self):
        with closing(app.get_db()) as conn, conn:
            conn.execute('DELETE FROM exam_answer_links')
            conn.execute('UPDATE objective_submissions SET state=? WHERE submission_id=?',('needs_review',self.f.f.obj[0]))
            conn.execute('DELETE FROM objective_answers WHERE submission_id=?',(self.f.f.obj[1],))
            scores.seed_answers(conn,self.f.f.obj[1],zero=True)
        report = self.client.get('/api/unified-scores/report?exam_id='+self.exam).get_json()
        self.assertEqual(report['objective_summary']['count'],1)
        self.assertEqual(report['objective_summary']['excluded_count'],1)
        self.assertEqual(report['objective_summary']['mean'],0)
        self.assertTrue(all(v['correct_rate']==0 for v in report['objective_questions'].values()))


class SharedUploadTests(unittest.TestCase):
    def setUp(self):
        self.f=exams.UnifiedExamTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        _,saved=self.f.create(); self.exam=saved['exam_id']; self.aid=saved['assignment']['id']
        self.key=self.f.snapshot(self.exam).get_json()['assessment_id'];self.client=self.f.client
        self.f.objective.config['OBJECTIVE_VISION_STATUS']=lambda:{'available':True}
        self.f.objective.config['OBJECTIVE_VISION_GRADER']=lambda paths,key:{'models':{s:{'run_status':'ok','answers':{str(q['question_no']):{'status':'answered','option':'A','confidence':.99,'page_no':1} for q in key}} for s in ('A','B')}}
        self.enterContext(patch.object(app,'enqueue_submission',return_value=None))
        self.enterContext(patch.object(app,'dual_review_key_is_configured',return_value=True))
        body=dict(assignment_id=self.aid,exam_id=self.exam,grading_input_mode='original_photo',student_label='SYNTHETIC',dual_review_enabled=True,photos=[dict(data_url=self.f.f.image_url,ai_redactions=[])],answer_regions=[])
        # Real preview and upload endpoints issue and verify the privacy proof.
        self.specs={s:{'provider':'synthetic','model':'synthetic-'+s,'base_url':'https://example.invalid/v1','provider_label':'synthetic','api_origin':'https://example.invalid','preset_id':'synthetic','label':'synthetic'} for s in ('A','B')}
        with patch.object(app,'dual_review_model_specs',return_value=self.specs):
            status,preview=self.f.f.request_json_status('/api/ai-payload-preview',body)
            self.assertEqual(status,200,preview)
            status,batch=self.f.f.request_json_status('/api/batches',dict(assignment_id=self.aid,exam_id=self.exam,grading_input_mode='original_photo',batch_request_id=uuid.uuid4().hex,items=[{'page_count':1}]))
            self.assertEqual(status,201,batch)
            body.update(batch_id=batch['batch_id'],sequence=1,submission_id=batch['items'][0]['submission_id'])
            status,saved=self.f.f.request_json_status('/api/submissions',{**body,'ai_privacy_confirmed':True,'privacy_status':'no_identity_visible','ai_privacy_preview_token':preview['preview_token'],'ai_privacy_fingerprint':preview['preview_fingerprint']})
        self.assertEqual(status,202,saved);self.sid=saved['submission']['id']
        self.image=base64.b64decode(preview['images'][0]['data_url'].split(',',1)[1])

    def upload(self,**override):
        data=dict(assessment_id=self.key,mode='vision',grouping='multi_page_all',privacy_status='no_identity_visible',source_submission_id=self.sid,combined_confirmed='true',files=(io.BytesIO(self.image),'synthetic.jpg'))
        data.update(override)
        return self.client.post('/api/uploads',data=data,content_type='multipart/form-data')

    def test_one_source_auto_pair_idempotent_and_unlink_preserves_results(self):
        response=self.upload();self.assertEqual(response.status_code,202,response.get_json())
        job_id=response.get_json()['job_id'];deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            job=self.client.get('/api/jobs/'+job_id).get_json()
            if job['job']['status'] not in ('queued','processing'):break
            time.sleep(.05)
        self.assertEqual(job['submissions'][0]['score'],60,job)
        with closing(app.get_db()) as conn, conn:
            scores.seed_final(conn,self.sid)
            conn.execute("UPDATE submissions SET state='completed' WHERE id=?",(self.sid,))
        projection=self.client.get('/api/unified-scores?exam_id='+self.exam).get_json()
        row=next(r for r in projection['rows'] if r['subjective'] and r['subjective']['id']==self.sid)
        self.assertTrue(row['complete'],row);self.assertEqual(row['total_score'],68)
        self.assertEqual(self.client.get('/api/unified-scores/report?exam_id='+self.exam).get_json()['participant_count'],1)
        self.assertEqual(self.upload().get_json()['job_id'],job_id)
        removed=self.client.post('/api/answer-links',json=dict(action='unlink',exam_id=self.exam,link_id=row['link_id'],confirmed=True,reason='synthetic unlink'))
        self.assertEqual(removed.status_code,200,removed.get_json())
        self.assertTrue(all(not r['complete'] for r in self.client.get('/api/unified-scores?exam_id='+self.exam).get_json()['rows']))
        self.assertTrue(self.upload().get_json()['reused'])
        with closing(app.get_db()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM objective_submissions').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT score FROM objective_submissions').fetchone()[0],60)

    def test_other_photo_missing_consent_and_wrong_exam_cannot_claim_source(self):
        self.assertEqual(self.upload(combined_confirmed='false').status_code,400)
        from tests.synthetic_pages import page_bytes as qr_bytes
        self.assertEqual(self.upload(files=(io.BytesIO(qr_bytes('SYN')),'other.png')).status_code,400)
        with closing(app.get_db()) as conn,conn:
            conn.execute('UPDATE submissions SET exam_id=? WHERE id=?',('f'*32,self.sid))
        self.assertEqual(self.upload().status_code,400)
        with closing(app.get_db()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM objective_submissions').fetchone()[0],0)

    def test_multi_page_is_one_paper_and_page_order_is_proven(self):
        from tests.synthetic_pages import page_bytes as qr_bytes
        body=dict(assignment_id=self.aid,exam_id=self.exam,grading_input_mode='original_photo',student_label='SYNTHETIC',dual_review_enabled=True,
                  photos=[{'data_url':self.f.f.image_url,'ai_redactions':[]},{'data_url':app.bytes_to_data_url(qr_bytes('PAGE2'),'image/png'),'ai_redactions':[]}],answer_regions=[])
        with patch.object(app,'dual_review_model_specs',return_value=self.specs):
            status,preview=self.f.f.request_json_status('/api/ai-payload-preview',body);self.assertEqual(status,200,preview)
            status,batch=self.f.f.request_json_status('/api/batches',dict(assignment_id=self.aid,exam_id=self.exam,grading_input_mode='original_photo',batch_request_id=uuid.uuid4().hex,items=[{'page_count':2}]))
            self.assertEqual(status,201,batch)
            status,saved=self.f.f.request_json_status('/api/submissions',{**body,'batch_id':batch['batch_id'],'sequence':1,'submission_id':batch['items'][0]['submission_id'],'ai_privacy_confirmed':True,'privacy_status':'no_identity_visible','ai_privacy_preview_token':preview['preview_token'],'ai_privacy_fingerprint':preview['preview_fingerprint']})
        self.assertEqual(status,202,saved);self.sid=saved['submission']['id']
        images=[base64.b64decode(im['data_url'].split(',',1)[1]) for im in preview['images']]
        wrong=[(io.BytesIO(im),'page.png') for im in reversed(images)]
        self.assertEqual(self.upload(files=wrong).status_code,400)
        response=self.upload(files=[(io.BytesIO(im),'page.png') for im in images])
        self.assertEqual(response.status_code,202,response.get_json())
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            job=self.client.get('/api/jobs/'+response.get_json()['job_id']).get_json()
            if job['job']['status'] not in ('queued','processing'):break
            time.sleep(.05)
        self.assertEqual(job['submissions'][0]['score'],60,job)
        with closing(app.get_db()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM objective_submissions').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT page_count FROM objective_submissions').fetchone()[0],2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM exam_answer_links WHERE subjective_submission_id=?',(self.sid,)).fetchone()[0],1)
