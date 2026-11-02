"""Exam-wide objective results from real upload jobs, mock recognition only."""
import csv,io,json,unittest
from contextlib import closing
from unittest.mock import patch
from PIL import Image
import app
from objective.app import _process_job
from tests import test_unified_exam as fixtures

class ObjectiveExamResultsTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.UnifiedExamTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        _,saved=self.f.create();self.exam=saved['exam_id'];self.assignment=saved['assignment']['id']
        self.key=self.f.snapshot(self.exam).get_json()['assessment_id'];self.client=self.f.client
        self.disagree=False
        def recognize(paths,key):
            return {'models':{s:{'run_status':'ok','answers':{str(q['question_no']):{'status':'answered','option':'B' if self.disagree and s=='B' else 'A','confidence':.99,'page_no':1} for q in key}} for s in ('A','B')}}
        self.f.objective.config['OBJECTIVE_VISION_GRADER']=recognize
        self.f.objective.config['OBJECTIVE_VISION_STATUS']=lambda:{'available':True}
        self.enterContext(patch('objective.app._launch_job',side_effect=lambda application,jid:_process_job(application,jid)))
    def upload(self,count=2,key=None):
        def image():
            b=io.BytesIO();Image.new('RGB',(100,100),'white').save(b,'PNG');b.seek(0);return b
        r=self.client.post('/api/uploads',data={'assessment_id':key or self.key,'mode':'vision','grouping':'one_per_image','privacy_status':'no_identity_visible','files':[(image(),f'synthetic-{i}.png') for i in range(count)]})
        self.assertEqual(r.status_code,202,r.get_json());return r.get_json()['job_id']
    def get(self,path):
        r=self.client.get('/api/'+path+'?exam_id='+self.exam);self.assertEqual(r.status_code,200,r.get_json());return r.get_json()
    def test_three_uploads_versions_review_and_side_export(self):
        jobs=[self.upload() for _ in range(3)]
        self.assertEqual(len(set(jobs)),3)
        for _ in range(2):
            p=self.get('unified-scores');self.assertEqual(len(p['rows']),6)
            self.assertEqual({r['objective']['job_id'] for r in p['rows']},set(jobs))
            report=self.get('unified-scores/report');self.assertEqual(report['objective_summary']['count'],6)
            self.assertEqual(report['objective_questions']['1']['count'],6)
            self.assertEqual(report['objective_questions']['1']['correct_rate'],1)
        self.assertFalse(any(r['complete'] for r in p['rows'])) # no subject/link required for objective export
        url='/api/unified-scores/export.csv?exam_id='+self.exam+'&side=objective&scope=complete'
        exported=list(csv.reader(io.StringIO(self.client.get(url).data.decode('utf-8-sig'))))
        self.assertEqual(len(exported),7)
        self.disagree=True;self.upload(1)
        self.assertEqual(self.get('unified-scores/report')['objective_summary']['count'],6)
        self.assertEqual(self.get('review-queue')['count'],20)
        with closing(app.get_db()) as c,c:
            m=json.loads(c.execute('SELECT import_metadata_json FROM assignments WHERE id=?',(self.assignment,)).fetchone()[0]);m['objective_answer_key']['1']='B'
            c.execute('UPDATE assignments SET import_metadata_json=? WHERE id=?',(json.dumps(m),self.assignment))
        newkey=self.f.snapshot(self.exam).get_json()['assessment_id'];self.disagree=False;self.upload(1,newkey)
        p=self.get('unified-scores');self.assertEqual(len(p['rows']),8)
        self.assertEqual(sum(bool(r['objective']['valid'] and not r['objective']['complete']) for r in p['rows']),6)
        report=self.get('unified-scores/report');self.assertEqual(report['objective_summary']['count'],1)
        self.assertEqual(report['objective_questions']['1']['correct_rate'],0)
        self.assertEqual(len(list(csv.reader(io.StringIO(self.client.get(url).data.decode('utf-8-sig'))))),2)
        self.assertEqual(self.client.get('/api/unified-scores/export.xlsx?exam_id='+self.exam+'&side=objective').status_code,200)
        self.assertEqual(self.client.get('/api/review-queue?exam_id='+'f'*32).status_code,403)
        self.f.provider.assert_not_called()

    def test_review_without_reason_scores_and_preserves_history(self):
        self.disagree=True;self.upload(1)
        queue=self.get('review-queue');self.assertEqual(queue['count'],20)
        sid=queue['items'][0]['submission_id']
        url=lambda q:'/api/submissions/'+sid+'/answers/'+str(q)+'/review'
        # Existing notes are retained in the immutable review history.
        self.assertEqual(self.client.post(url(1),json={'option':'B','reason':'synthetic previous note'}).status_code,200)
        with closing(app.get_db()) as c:
            historical=tuple(c.execute('SELECT * FROM objective_review_events WHERE submission_id=?',(sid,)).fetchone())
        for body in ({}, {'reason':''}, {'option':None}, {'option':' '}, {'option':'E'}, {'option':'A','reason':'x'*201}):
            self.assertEqual(self.client.post(url(1),json=body).status_code,400,body)
        for q in range(1,21):
            selected={1:'A',2:'B',3:'C',4:'D',5:''}.get(q,'A')
            body={'option':selected}
            if q==5:body['reason']=''
            if q==6:body['reason']=None
            response=self.client.post(url(q),json=body)
            self.assertEqual(response.status_code,200,response.get_json())
        self.assertEqual(self.get('review-queue')['count'],0)
        with closing(app.get_db()) as c:
            record=c.execute('SELECT score,state FROM objective_submissions WHERE submission_id=?',(sid,)).fetchone()
            self.assertEqual(record['score'],48);self.assertEqual(record['state'],'completed')
            blank=c.execute('SELECT final_option,awarded_points,state FROM objective_answers WHERE submission_id=? AND question_no=5',(sid,)).fetchone()
            self.assertEqual(tuple(blank),(None,0,'manual_blank'))
            events=[tuple(r) for r in c.execute('SELECT * FROM objective_review_events WHERE submission_id=? ORDER BY rowid',(sid,))]
            self.assertEqual(events[0],historical);self.assertEqual(len(events),21)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM objective_review_events WHERE submission_id=? AND reason=''",(sid,)).fetchone()[0],20)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM objective_review_items WHERE submission_id=? AND state='open'",(sid,)).fetchone()[0],0)
        self.assertEqual(self.get('unified-scores/report')['objective_summary']['mean'],48)
        self.f.provider.assert_not_called()

if __name__=='__main__':unittest.main()
