"""Quick upload/recovery in isolated real HTTP/auth/DB, synthetic provider boundary."""
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
 counts=[]
 for width in (1280,390):
  f=formal.FormalBatchWorkflowTests();f.setUp();old=web_auth.AUTH_MANAGER
  try:
   # Match real main() startup, before concurrent model diagnostic preflight.
   app.ensure_portable_instance_id()
   shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
   if len(sys.argv)>2:shutil.copytree(Path(sys.argv[2]).resolve(),app.STATIC_DIR/'next',dirs_exist_ok=True)
   source=out/'synthetic.png';source.write_bytes(f.source)
   with closing(app.get_db()) as c,c:
    row=c.execute('SELECT * FROM assignments WHERE id=?',(f.assignment_id,)).fetchone()
    c.execute('INSERT INTO assignments(id,name,question,standard_answer,criteria_json,sub_questions_json,import_metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)',('c'*12,'合成快速匿名','q','a',row['criteria_json'],row['sub_questions_json'],json.dumps({'teacher_confirmation_status':'confirmed_by_teacher','teacher_confirmed_at':app.utc_now()}),app.utc_now()))
   def reserve(a,pages,request):
    status,b=f.request('/api/batches',{'assignment_id':a,'identity_mode':'anonymous','batch_request_id':request,'items':[{'page_count':pages}],**({'exam_id':f.exam_id} if a==f.assignment_id else {})});assert status==201,b
    status,r=f.request('/api/batches/upload-failed',{'batch_id':b['batch_id'],'sequence':1});assert status==200,r
    return b
   quick=reserve('c'*12,1,'resume-quick');formal_batch=reserve(f.assignment_id,2,'resume-formal');skipped=reserve('c'*12,1,'skip-upload')
   config={'quick':quick['batch_id'],'formal':formal_batch['batch_id'],'skip':skipped['batch_id']}
   (out/f'ids-{width}.json').write_text(json.dumps(config),encoding='utf-8')
   captured=[];seen_quick=[];trace=[];current={'unmatched':False};original_grade=app.grade_submission
   def grade(row,*args,**kwargs):
    if row['assignment_id']=='c'*12 and row['id'] not in seen_quick:
     seen_quick.append(row['id'])
     if len(seen_quick)==1:
      return {'mode':'processing-error','dual_review_requested':True,'blocked':True,'needs_review':True,'summary':'合成本机中断','suggested_total':0,'max_total':4,'sub_questions':[]}
    current['unmatched']=bool(len(seen_quick)==4 and row['id']==seen_quick[-1] and not args[7])
    before_calls=len(captured);r=original_grade(row,*args,**kwargs)
    trace.append({'id':row['id'],'dual_requested':args[8],'mapping':args[7],'mock_calls':len(captured)-before_calls,'mode':r.get('mode'),'dual_errors':r.get('dual_review',{}).get('errors')})
    return r
   def capture(request,timeout):
    body=json.loads(request.data);captured.append(body);r=MagicMock();r.__enter__.return_value=body;return r
   def respond(body,*args,**kwargs):
    prompt=body['messages'][1]['content'][0]['text'];qs=[q for q in f.questions if f'"id": "{q["id"]}"' in prompt]
    return json.dumps({'confidence':.9,'sub_questions':[{'sub_question_id':q['id'],'answer_status':'question_unmatched' if current['unmatched'] and q['id']=='Q17.2' else 'answered','criteria':[{'criterion_id':'C1','decision':'none','suggested_score':0,'student_evidence':'','needs_review':False}]} for q in qs],'unmapped_answers':[{'answer_id':'A1','evidence':{'photo_index':1,'x':.2,'y':.3,'width':.2,'height':.1}}] if current['unmatched'] else []})
   with patch.dict(os.environ,{'HISTORY_GRADER_CLOUD_MODE':'0','HISTORY_GRADER_WEB_AUTH_REQUIRED':'1','HISTORY_GRADER_COOKIE_SECURE':'0','HISTORY_GRADER_TRUST_PROXY':'0','HISTORY_GRADER_ADMIN_USERNAME':auth.TEST_USERNAME,'HISTORY_GRADER_ADMIN_PASSWORD_HASH':password_auth.hash_password(auth.TEST_PASSWORD),'HISTORY_GRADER_AUTH_DB':str(app.DB_PATH)}):
    web_auth.AUTH_MANAGER=web_auth.SingleTeacherAuth();helper=accounts.TeacherAccountTests();helper.base=f.fixture.base_url;helper.origin=helper.base;helper.opener=build_opener(ProxyHandler({}));helper.create_teacher(helper.admin_headers())
    with patch.object(app,'open_model_request',side_effect=capture),patch.object(app,'read_model_chat_content',side_effect=respond),patch.object(app,'grade_submission',side_effect=grade):
     p=subprocess.Popen(['node',str(Path(__file__).with_suffix('.js')),f.fixture.base_url,str(source),str(out/f'ui-{width}'),str(width),str(out/f'ids-{width}.json')],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
     deadline=time.monotonic()+240
     try:
      while p.poll() is None and time.monotonic()<deadline:
       with closing(app.get_db()) as c:rows=c.execute("SELECT id FROM submissions WHERE state='queued'").fetchall()
       for row in rows:app.process_submission(row['id'])
       time.sleep(.1)
      stdout,stderr=p.communicate(timeout=5)
      if p.returncode:
       with closing(app.get_db()) as c:
        debug=[dict(r) for r in c.execute('SELECT id,state,batch_id,result_json FROM submissions')]
       (out/f'failed-state-{width}.json').write_text(json.dumps({'records':debug,'seen_quick':seen_quick,'calls':len(captured)},ensure_ascii=False,indent=2),encoding='utf-8')
      assert p.returncode==0,(stdout,stderr)
     finally:
      if p.poll() is None:p.terminate();p.wait(timeout=10)
   with closing(app.get_db()) as c:
    rows=c.execute('SELECT id,state,result_json,confirmed_at,final_total,batch_id,photos_json FROM submissions').fetchall()
    assert len(rows)==5,len(rows);assert all(r['state']=='completed' for r in rows);assert all(r['confirmed_at'] is None and r['final_total'] is None for r in rows)
    assert c.execute('SELECT count(*) FROM grading_batches').fetchone()[0]==5
    assert c.execute("SELECT status FROM grading_batches WHERE id=?",(skipped['batch_id'],)).fetchone()[0]=='completed'
    events=c.execute('SELECT event_type FROM audit_events').fetchall();assert sum(e[0]=='submission_retry_requested' for e in events)==1
    formal_row=next(r for r in rows if r['batch_id']==formal_batch['batch_id']);assert all(p['image_role']=='answer_region' for p in json.loads(formal_row['photos_json']))
    remapped=json.loads(next(r['result_json'] for r in rows if r['id']==seen_quick[-1]));assert not remapped.get('requires_question_mapping');assert remapped.get('teacher_question_mapping')
   (out/f'provider-trace-{width}.json').write_text(json.dumps(trace,ensure_ascii=False,indent=2),encoding='utf-8')
   assert len(captured)==14,(len(captured),trace)
   for body in captured:
    text=json.dumps(body,ensure_ascii=False)
    for word in ('identity_code','student_id','synthetic.png'):assert word not in text
   counts.append(len(captured))
  finally:web_auth.AUTH_MANAGER=old;f.doCleanups()
 (out/'summary.json').write_text(json.dumps({'desktop':'PASS','mobile390':'PASS','mock_provider_calls':counts,'real_provider_calls':0,'original_retry_audit_per_width':1,'formal_reupload_mode':'answer_region','whole_paper_formal_fallback':0,'remap_real_privacy_receipt':True,'teacher_final_automatically_written':False},indent=2),encoding='utf-8')
 print('Vue quick/recovery desktop/mobile PASS; mock boundary only, formal answer_region preserved')
if __name__=='__main__':main()
