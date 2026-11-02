"""Isolated five-paper workspace acceptance; real OMR, mocked subjective providers, actual teacher actions."""
from contextlib import closing
import hashlib,io,json,os,random,shutil,subprocess,time,sys
from pathlib import Path
from unittest.mock import MagicMock,patch
from urllib.request import build_opener,ProxyHandler
from PIL import Image,ImageDraw
import app,password_auth,web_auth
from tests import test_formal_batch_workflow as formal,test_teacher_accounts as accounts,test_password_auth as auth
from tests.test_docx_question_import import make_docx
from objective.synthetic import render_omr_card,photograph
from objective.templates import BUILTIN_TEMPLATES
from objective.app import _decode_image

out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
f=formal.FormalBatchWorkflowTests();f.setUp();old_manager=web_auth.AUTH_MANAGER
try:
 app.ensure_portable_instance_id();shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True);shutil.copytree(Path(sys.argv[2]).resolve(),app.STATIC_DIR/'next',dirs_exist_ok=True)
 f.fixture.server.objective_wsgi_app=app.create_integrated_objective_wsgi_app();f.fixture.server.restarting=False;f.fixture.server.stopping=False
 with patch.dict(os.environ,{'HISTORY_GRADER_CLOUD_MODE':'0','HISTORY_GRADER_WEB_AUTH_REQUIRED':'1','HISTORY_GRADER_COOKIE_SECURE':'0','HISTORY_GRADER_TRUST_PROXY':'0','HISTORY_GRADER_ADMIN_USERNAME':auth.TEST_USERNAME,'HISTORY_GRADER_ADMIN_PASSWORD_HASH':password_auth.hash_password(auth.TEST_PASSWORD),'HISTORY_GRADER_AUTH_DB':str(app.DB_PATH)}):
  web_auth.AUTH_MANAGER=web_auth.SingleTeacherAuth();helper=accounts.TeacherAccountTests();helper.base=f.fixture.base_url;helper.origin=helper.base;helper.opener=build_opener(ProxyHandler({}));helper.create_teacher(helper.admin_headers())
  lines=['完全合成历史试题','一、单项选择题']+[f'{i}．合成历史制度比较题干用于软件验收。 A．选项甲 B．选项乙 C．选项丙 D．选项丁' for i in range(1,21)]
  lines+=['二、材料题','21．阅读材料，完成下列要求。（4分）材料：合成材料甲。','（1）概括特点。（4分）','22．阅读材料，完成下列要求。（4分）材料：合成材料乙。','（1）分析原因。（4分）','参考答案','题号 | '+' | '.join(map(str,range(1,21))),'答案 | '+' | '.join(['A']*20),'21．（1）答案甲；答案乙。','22．（1）答案丙；答案丁。']
  word=out/'mixed.docx';word.write_bytes(make_docx(lines));hashes=[]
  for i in range(1,6):
   choices=['B' if i==2 else 'A']*20
   card=render_omr_card(BUILTIN_TEMPLATES[0]['config'],choices,{'double_mark_question':1} if i==5 else {})
   ImageDraw.Draw(card).text((150,120),f'SYNTHETIC TEST-{i}',fill='black')
   raw=photograph(card,random.Random(100+i));(out/f'omr-{i}.png').write_bytes(raw);hashes.append(hashlib.sha256(_decode_image(raw)[0]).hexdigest())
   image=Image.new('RGB',(1000,1400),'white');draw=ImageDraw.Draw(image);draw.text((70,70),f'SYNTHETIC TEST-{i}',fill='black');draw.text((80,350),'Q21.1 Synthetic answer A; answer B',fill='black');draw.text((80,850),'Q22.1 Synthetic answer C; answer D',fill='black');image.save(out/f'subjective-{i}.png')
  p=subprocess.run(['node',str(Path(__file__).with_name('unified_exam_browser_smoke.js')),f.fixture.base_url,str(word),str(out/'omr-1.png'),str(out/'create'),'1280','5'],capture_output=True,text=True,encoding='utf-8',timeout=160)
  assert p.returncode==0,p.stdout+'\n'+p.stderr
  saved=json.loads((out/'create.json').read_text(encoding='utf-8'))['saved']
  deadline=time.monotonic()+30
  while time.monotonic()<deadline:
   with closing(app.get_db()) as c:pending=c.execute("SELECT COUNT(*) FROM objective_submissions WHERE state IN ('queued','processing')").fetchone()[0]
   if not pending:break
   time.sleep(.1)
  with closing(app.get_db()) as c:
   rows=c.execute('SELECT s.anonymous_code,s.score,s.state,p.image_sha256 FROM objective_submissions s JOIN objective_submission_pages p ON p.submission_id=s.submission_id').fetchall()
   manifest={'objective_codes':[next(r['anonymous_code'] for r in rows if r['image_sha256']==h) for h in hashes]}
   questions=json.loads(c.execute('SELECT sub_questions_json FROM assignments WHERE id=?',(saved['assignment']['id'],)).fetchone()[0])
  (out/'manifest.json').write_text(json.dumps(manifest))
  captured=[]
  def capture(request,timeout):
   body=json.loads(request.data);captured.append(body);response=MagicMock();response.__enter__.return_value=body;return response
  def respond(body,*a,**k):
   return json.dumps({'confidence':.99,'sub_questions':[{'sub_question_id':q['id'],'answer_status':'answered','criteria':[{'criterion_id':p['id'],'decision':'full','suggested_score':p['score'],'student_evidence':'Synthetic evidence','needs_review':False} for p in q['criteria']]} for q in questions]})
  with patch.object(app,'open_model_request',side_effect=capture),patch.object(app,'read_model_chat_content',side_effect=respond):
   p=subprocess.Popen(['node',str(Path(__file__).with_suffix('.js')),f.fixture.base_url,str(out/'create.json'),str(out),str(out/'chain')],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8');processed=set();deadline=time.monotonic()+240
   try:
    while p.poll() is None and time.monotonic()<deadline:
     with closing(app.get_db()) as c:queued=c.execute("SELECT id FROM submissions WHERE state='queued'").fetchall()
     for row in queued:
      if row['id'] not in processed:processed.add(row['id']);app.process_submission(row['id'])
     time.sleep(.1)
    stdout,stderr=p.communicate(timeout=5);assert p.returncode==0,stdout+'\n'+stderr
   finally:
    if p.poll() is None:p.terminate();p.wait(timeout=10)
  assert len(captured)==10,len(captured)
  with closing(app.get_db()) as c:
   assert c.execute('SELECT COUNT(*) FROM objective_assessments WHERE source_exam_id=?',(saved['exam_id'],)).fetchone()[0]==1
   counts={t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('objective_submissions','submissions','exam_answer_links')}
  (out/'chain-summary.json').write_text(json.dumps(dict(passed=True,counts=counts,mock_provider_calls=len(captured),real_provider_calls=0,manifest=manifest),indent=2))
  print('FULL CHAIN PASS',counts,'mock calls',len(captured),'real provider calls 0')
finally:web_auth.AUTH_MANAGER=old_manager;f.doCleanups()
