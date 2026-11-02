"""Vue formal upload: real alignment/auth/privacy/worker with synthetic mock models."""
from contextlib import closing
import json,os,shutil,subprocess,sys,time
from pathlib import Path
from unittest.mock import MagicMock,patch
from urllib.request import build_opener,ProxyHandler
import app,password_auth,web_auth
from tests import test_formal_batch_workflow as formal
from tests import test_teacher_accounts as accounts
from tests import test_password_auth as auth

def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    total=0
    for width in (1280,390):
        f=formal.FormalBatchWorkflowTests();f.setUp();old_manager=web_auth.AUTH_MANAGER
        try:
            # main() initializes the instance ID before any provider threads.
            app.ensure_portable_instance_id()
            shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
            if len(sys.argv)>2:shutil.copytree(Path(sys.argv[2]).resolve(),app.STATIC_DIR/'next',dirs_exist_ok=True)
            source=out/'01.png';source.write_bytes(f.source)
            with patch.dict(os.environ,{'HISTORY_GRADER_CLOUD_MODE':'0','HISTORY_GRADER_WEB_AUTH_REQUIRED':'1','HISTORY_GRADER_COOKIE_SECURE':'0','HISTORY_GRADER_TRUST_PROXY':'0',
                'HISTORY_GRADER_ADMIN_USERNAME':auth.TEST_USERNAME,'HISTORY_GRADER_ADMIN_PASSWORD_HASH':password_auth.hash_password(auth.TEST_PASSWORD),'HISTORY_GRADER_AUTH_DB':str(app.DB_PATH)}):
                web_auth.AUTH_MANAGER=web_auth.SingleTeacherAuth()
                helper=accounts.TeacherAccountTests();helper.base=f.fixture.base_url;helper.origin=helper.base;helper.opener=build_opener(ProxyHandler({}))
                helper.create_teacher(helper.admin_headers())
                # Desktop covers an actual roster-bound exam; mobile covers no-roster formal input.
                if width==1280:
                    with closing(app.get_db()) as c,c:
                        c.execute('INSERT INTO roster_classes(id,class_name,created_at) VALUES(?,?,?)',('c'*32,'合成上传班',app.utc_now()))
                        c.execute('INSERT INTO roster_students(id,class_id,class_name,student_no,student_name,identity_code,enabled,created_at) VALUES(?,?,?,?,?,?,1,?)',('b'*32,'c'*32,'合成上传班','01','合成隐私学生','01',app.utc_now()))
                        c.execute('UPDATE grading_exams SET class_id=? WHERE id=?',('c'*32,f.exam_id))
                captured=[]
                def capture(request,timeout):
                    body=json.loads(request.data);captured.append(body);response=MagicMock();response.__enter__.return_value=body;return response
                def respond(body,*args,**kwargs):
                    prompt=body['messages'][1]['content'][0]['text']
                    q=next(q for q in f.questions if f'"id": "{q["id"]}"' in prompt)
                    return json.dumps({'confidence':.9,'sub_questions':[{'sub_question_id':q['id'],'answer_status':'answered','criteria':[{'criterion_id':'C1','decision':'none','suggested_score':0,'student_evidence':'','needs_review':False}]}]})
                with patch.object(app,'open_model_request',side_effect=capture),patch.object(app,'read_model_chat_content',side_effect=respond):
                    p=subprocess.Popen(['node',str(Path(__file__).with_suffix('.js')),f.fixture.base_url,str(source),str(out/f'ui-{width}'),str(width)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
                    processed=set();deadline=time.monotonic()+150
                    try:
                        while p.poll() is None and time.monotonic()<deadline:
                            with closing(app.get_db()) as c:rows=c.execute("SELECT id FROM submissions WHERE state='queued'").fetchall()
                            for row in rows:
                                if row['id'] not in processed:processed.add(row['id']);app.process_submission(row['id'])
                            time.sleep(.1)
                        stdout,stderr=p.communicate(timeout=5)
                        assert p.returncode==0,(stdout,stderr)
                    finally:
                        if p.poll() is None:p.terminate();p.wait(timeout=10)
                if len(captured)!=4:
                    with closing(app.get_db()) as c:
                        diagnostic=[dict(r) for r in c.execute('SELECT id,result_json FROM submissions')]
                    (out/f'provider-failure-{width}.json').write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding='utf-8')
                assert len(captured)==4,len(captured)
                with closing(app.get_db()) as c:
                    rows=c.execute('SELECT state,result_json,photos_json FROM submissions').fetchall()
                    assert len(rows)==1,len(rows)
                    row=rows[0];assert row['state']=='completed',row['state']
                    assert all(p['image_role']=='answer_region' for p in json.loads(row['photos_json']))
                    result=json.loads(row['result_json'])
                    for side in ('A','B'):
                        assert all(r['request_metadata']['input_mode']=='answer_region' for r in result['dual_review']['models'][side]['question_runs'])
                    batch=c.execute('SELECT exam_id,manifest_json FROM grading_batches').fetchone()
                    assert batch['exam_id']==f.exam_id and json.loads(batch['manifest_json'])['items'][0]['page_count']==2
                images={}
                for body in captured:
                    content=body['messages'][1]['content'];q=next(q for q in f.questions if f'"id": "{q["id"]}"' in content[0]['text'])
                    images.setdefault(q['id'],[]).append(content[1]['image_url']['url'])
                    for word in ('student_id','identity_code','合成隐私学生','01.png'):assert word not in json.dumps(body,ensure_ascii=False)
                assert all(len(v)==2 and v[0]==v[1] for v in images.values())
                total+=len(captured)
        finally:web_auth.AUTH_MANAGER=old_manager;f.doCleanups()
    (out/'summary.json').write_text(json.dumps({'desktop_class':'PASS','mobile390_anonymous_formal':'PASS','mock_provider_calls':total,'real_provider_calls':0,'whole_paper_fallback':0,'production_access':False},indent=2),encoding='utf-8')
    print('Vue formal upload desktop/class + mobile/no-roster PASS; 8 mock calls; real provider 0')

if __name__=='__main__':main()
