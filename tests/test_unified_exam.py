"""Stage-one unified exams: synthetic main HTTP + existing objective engine only."""
import copy
from contextlib import closing
import json
import os
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import app
from objective.app import create_app, _init_db
from unified_exam import read_source_exam
from tests import test_batch_order as batch_fixture
from tests.test_exam_import_ui import import_draft, confirmation_for


def mixed_draft(count=20, subjective=True):
    d=import_draft()
    d['objective_questions']=[dict(copy.deepcopy(d['objective_questions'][0]), question_id=str(i)) for i in range(1,count+1)]
    d['objective_answer_key']={str(i):'A' for i in range(1,count+1)}
    c=confirmation_for(d)
    c['objective_answer_key']=dict(d['objective_answer_key'])
    c['objective_points']={str(i):3 for i in range(1,count+1)}
    if subjective:
        major=copy.deepcopy(d['material_questions'][0]);major['label']='22';major['sub_questions'][0]['question_id']='22.1'
        edit=copy.deepcopy(c['material_questions'][0]);edit['label']='22';edit['sub_questions'][0]['question_id']='22.1'
        d['material_questions'].append(major);c['material_questions'].append(edit)
    else:
        d['material_questions']=[];c['material_questions']=[]
    return d,c


class UnifiedExamTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ,{'HISTORY_GRADER_CLOUD_MODE':'0','HISTORY_GRADER_WEB_AUTH_REQUIRED':'0'}))
        self.f=batch_fixture.OrderedBatchTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.provider=self.enterContext(patch.object(app,'open_model_request',side_effect=AssertionError('real provider forbidden')))
        self.objective=app.create_integrated_objective_wsgi_app()
        self.client=self.objective.test_client()

    def create(self,d=None,c=None):
        if d is None:d,c=mixed_draft()
        draft_id=uuid.uuid4().hex;root=app.DATA_DIR/'exam_import_drafts'/draft_id;root.mkdir(parents=True)
        (root/'exam_import_draft.json').write_text(json.dumps(d),encoding='utf-8')
        return self.f.request_json_status('/api/create-imported-exam',{'draft_id':draft_id,'confirmation':c})

    def snapshot(self,e):
        return self.client.post('/api/assessments/from-exam',json={'exam_id':e})

    def test_mixed_create_one_source_two_engine_inputs_and_get_has_no_writes(self):
        status,saved=self.create();self.assertEqual(status,201,saved)
        aid,e=saved['assignment']['id'],saved['exam_id']
        self.assertEqual(saved['assignment']['score_totals'],{'objective':60,'subjective':8,'paper':68})
        self.assertEqual(saved['assignment']['max_score'],8)
        with closing(app.get_db()) as conn:
            row=conn.execute('SELECT sub_questions_json,import_metadata_json FROM assignments WHERE id=?',(aid,)).fetchone()
            self.assertEqual(len(json.loads(row['sub_questions_json'])),2)
            before=conn.execute('SELECT COUNT(*) FROM objective_assessments').fetchone()[0]
        for _ in range(2):
            self.assertEqual(self.client.get('/api/source-exams').status_code,200)
            source=self.client.get('/api/source-exams/'+e).get_json()
            self.assertEqual(len(source['questions']),20);self.assertEqual(source['max_score'],60)
        with closing(app.get_db()) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM objective_assessments').fetchone()[0],before)
        response=self.snapshot(e);self.assertEqual(response.status_code,201,response.get_json())
        snapshot=response.get_json();self.assertEqual(len(snapshot['question_mapping']),20)
        self.assertEqual(snapshot['question_mapping'][19],{'original_question_id':'20','question_no':20})
        self.assertEqual(self.snapshot(e).get_json()['assessment_id'],snapshot['assessment_id'])
        self.assertTrue(self.snapshot(e).get_json()['reused']);self.provider.assert_not_called()

    def test_pure_objective_and_pure_subjective(self):
        for count,subjective in [(20,False),(0,True)]:
            status,saved=self.create(*mixed_draft(count,subjective));self.assertEqual(status,201,saved)
            self.assertEqual(saved['assignment']['max_score'],8 if subjective else 0)
            self.f.get_json('/api/assignments')
            response=self.snapshot(saved['exam_id'])
            self.assertEqual(response.status_code,400 if count==0 else 201)

    def test_bad_creation_never_creates_exam(self):
        d,c=mixed_draft()
        for bad in [None,'',0,-1,True,float('inf'),101]:
            edit=copy.deepcopy(c);edit['objective_points']['1']=bad
            self.assertEqual(self.create(d,edit)[0],400)
        for field,value in [('objective_answer_key',{'1':'Z'}),('objective_points',{})]:
            edit=copy.deepcopy(c);edit[field]=value;self.assertEqual(self.create(d,edit)[0],400)
        for qid in ['2','01','0','21','Q1']:
            draft=copy.deepcopy(d);draft['objective_questions'][0]['question_id']=qid
            self.assertEqual(self.create(draft,c)[0],400)
        draft=copy.deepcopy(d);draft['objective_questions'][0]['question_type']='multiple_choice'
        self.assertEqual(self.create(draft,c)[0],400)
        edit=copy.deepcopy(c);edit['teacher_confirmed']=False;self.assertEqual(self.create(d,edit)[0],400)

    def test_versions_preserve_snapshot_and_existing_results(self):
        _,saved=self.create();e=saved['exam_id'];aid=saved['assignment']['id'];old=self.snapshot(e).get_json()
        # A durable old objective result remains attached to the original key.
        with closing(app.get_db()) as conn,conn:
            conn.execute("INSERT INTO objective_jobs(job_id,assessment_id,mode,grouping,status,total_files,total_submissions,created_at) VALUES(?,?,?,?,'completed',1,1,?)",('j'*32,old['assessment_id'],'omr','one_per_image',app.utc_now()))
            conn.execute("INSERT INTO objective_submissions(submission_id,job_id,assessment_id,anonymous_code,state,score,max_score,page_count,created_at) VALUES(?,?,?,?,'completed',60,60,1,?)",('s'*32,'j'*32,old['assessment_id'],'SYNTHETIC',app.utc_now()))
            old_key=conn.execute('SELECT answer_key_json FROM objective_assessments WHERE assessment_id=?',(old['assessment_id'],)).fetchone()[0]
        for field in ['answer','points','stem']:
            with closing(app.get_db()) as conn,conn:
                meta=json.loads(conn.execute('SELECT import_metadata_json FROM assignments WHERE id=?',(aid,)).fetchone()[0])
                if field=='answer':meta['objective_answer_key']['1']='B'
                elif field=='points':meta['objective_points']['1']=4
                else:meta['objective_questions'][0]['stem']='changed synthetic stem'
                conn.execute('UPDATE assignments SET import_metadata_json=? WHERE id=?',(json.dumps(meta),aid))
            new=self.snapshot(e).get_json();self.assertNotEqual(new['assessment_id'],old['assessment_id'])
        self.assertEqual(new['answer_version'],4)
        with closing(app.get_db()) as conn:
            self.assertEqual(conn.execute('SELECT answer_key_json FROM objective_assessments WHERE assessment_id=?',(old['assessment_id'],)).fetchone()[0],old_key)
            self.assertEqual(tuple(conn.execute('SELECT assessment_id,score FROM objective_submissions').fetchone()),(old['assessment_id'],60))

    def test_unconfirmed_legacy_missing_scores_and_forged_keys_blocked(self):
        _,saved=self.create();e=saved['exam_id'];aid=saved['assignment']['id']
        self.assertEqual(self.snapshot('f'*32).status_code,404)
        self.assertEqual(self.client.post('/api/assessments/from-exam',json={'exam_id':e,'answer_key':[]}).status_code,400)
        self.assertEqual(self.client.post('/api/assessments/from-exam',json=[{'exam_id':e}]).status_code,400)
        with closing(app.get_db()) as conn,conn:
            meta=json.loads(conn.execute('SELECT import_metadata_json FROM assignments WHERE id=?',(aid,)).fetchone()[0]);original=copy.deepcopy(meta)
        for change in ['missing','confirmation','answer','qid','type']:
            meta=copy.deepcopy(original)
            if change=='missing':meta.pop('objective_points')
            elif change=='confirmation':meta['teacher_confirmation_status']='pending'
            elif change=='answer':meta['objective_answer_key']['1']='Z'
            elif change=='qid':meta['objective_questions'][0]['question_id']='3'
            else:meta['objective_questions'][0]['question_type']='multiple_choice'
            with closing(app.get_db()) as conn,conn:conn.execute('UPDATE assignments SET import_metadata_json=? WHERE id=?',(json.dumps(meta),aid))
            self.assertEqual(self.snapshot(e).status_code,400)
        denied=create_app(data_dir=app.DATA_DIR/'denied',database_path=app.DB_PATH,initialize_schema=False,auth_enabled=False,source_exam_provider=lambda _=None:(_ for _ in ()).throw(PermissionError('exam outside accessible workspace'))).test_client()
        self.assertEqual(denied.post('/api/assessments/from-exam',json={'exam_id':e}).status_code,403)

    def test_legacy_independent_keys_stay_unlinked(self):
        r=self.client.post('/api/assessments',json={'title':'synthetic independent','answer_key':[{'question_no':1,'correct_option':'A','points':1}]})
        self.assertEqual(r.status_code,201)
        result=self.client.get('/api/assessments/'+r.get_json()['assessment_id']).get_json()
        self.assertIsNone(result['source_exam_id']);self.assertIsNone(result['source_fingerprint'])
        self.assertEqual(result['answer_key'],[{'question_no':1,'correct_option':'A','points':1.0}])
        _init_db(app.DB_PATH)
        self.assertEqual(self.client.get('/api/assessments/'+r.get_json()['assessment_id']).get_json(),result)

    def test_concurrent_snapshot_creation_is_idempotent(self):
        from concurrent.futures import ThreadPoolExecutor
        _,saved=self.create();e=saved['exam_id']
        def request(_):
            with self.objective.test_client() as client:
                r=client.post('/api/assessments/from-exam',json={'exam_id':e});return r.status_code,r.get_json()
        with ThreadPoolExecutor(max_workers=5) as pool:results=list(pool.map(request,range(5)))
        self.assertEqual(sum(status==201 for status,_ in results),1)
        self.assertEqual(len({r['assessment_id'] for _,r in results}),1)
        self.assertTrue(all(status in (200,201) for status,_ in results))

    def test_v3_upgrade_keeps_existing_records_and_null_sources(self):
        with closing(app.get_db()) as conn,conn:
            conn.execute("INSERT INTO objective_assessments(assessment_id,title,answer_version,answer_key_json,answer_key_sha256,created_at) VALUES('legacy','old',1,'[]','oldhash','oldtime')")
            conn.execute('DROP INDEX idx_objective_source_snapshot')
            conn.execute('ALTER TABLE objective_assessments DROP COLUMN source_exam_id')
            conn.execute('ALTER TABLE objective_assessments DROP COLUMN source_fingerprint')
            conn.execute('DROP TABLE exam_answer_links')
            conn.execute('PRAGMA user_version=3')
            before=tuple(conn.execute('SELECT * FROM objective_assessments').fetchone())
        _init_db(app.DB_PATH)
        with closing(app.get_db()) as conn:
            after=tuple(conn.execute('SELECT * FROM objective_assessments').fetchone())
            self.assertEqual(after[:6],before);self.assertEqual(after[6:],(None,None))
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],5)

    def test_linked_vision_keeps_privacy_gate_and_existing_score_rule(self):
        import base64,io,time
        _,saved=self.create();assessment=self.snapshot(saved['exam_id']).get_json()
        calls=[]
        def fake_vision(paths,key):
            calls.append((paths,key))
            return {'models':{side:{'run_status':'ok','answers':{str(k['question_no']):{'status':'answered','option':'A','confidence':.99,'page_no':1} for k in key}} for side in ('A','B')}}
        self.objective.config['OBJECTIVE_VISION_STATUS']=lambda:{'available':True}
        self.objective.config['OBJECTIVE_VISION_GRADER']=fake_vision
        image=base64.b64decode(self.f.image_url.split(',',1)[1])
        def upload(privacy):
            return self.client.post('/api/uploads',data={'assessment_id':assessment['assessment_id'],'mode':'vision','privacy_status':privacy,'files':(io.BytesIO(image),'synthetic.png')},content_type='multipart/form-data')
        self.assertEqual(upload('unchecked').status_code,400);self.assertEqual(calls,[])
        response=upload('no_identity_visible');self.assertEqual(response.status_code,202,response.get_json())
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            job=self.client.get('/api/jobs/'+response.get_json()['job_id']).get_json()
            if job['job']['status'] not in ('queued','processing'):break
            time.sleep(.05)
        self.assertEqual(job['job']['status'],'completed',job)
        self.assertEqual(job['submissions'][0]['score'],60)
        self.assertEqual(calls[0][1][0]['original_question_id'],'1')
        self.provider.assert_not_called()
