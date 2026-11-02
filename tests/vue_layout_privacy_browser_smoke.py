"""Vue layout/privacy: real local API and synthetic pages, never provider calls."""
from contextlib import closing
import json,os,shutil,subprocess,sys
from pathlib import Path
from unittest.mock import patch
from urllib.request import build_opener,ProxyHandler
import app,password_auth,web_auth
from tests import test_formal_batch_workflow as formal
from tests import test_teacher_accounts as accounts
from tests import test_password_auth as auth

def main():
 out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
 for width in (1280,390):
  f=formal.FormalBatchWorkflowTests();f.setUp();old=web_auth.AUTH_MANAGER
  try:
   shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
   source=out/'synthetic-page.png';source.write_bytes(f.source)
   with closing(app.get_db()) as c,c:
    row=c.execute('SELECT * FROM assignments WHERE id=?',(f.assignment_id,)).fetchone()
    before=(row['sub_questions_json'],row['criteria_json'],row['import_metadata_json'])
    c.execute('INSERT INTO assignments(id,name,question,standard_answer,criteria_json,sub_questions_json,import_metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)',('c'*12,'合成快速隐私','q','a',row['criteria_json'],row['sub_questions_json'],json.dumps({'teacher_confirmation_status':'confirmed_by_teacher','teacher_confirmed_at':app.utc_now()}),app.utc_now()))
   with patch.dict(os.environ,{'HISTORY_GRADER_CLOUD_MODE':'0','HISTORY_GRADER_WEB_AUTH_REQUIRED':'1','HISTORY_GRADER_COOKIE_SECURE':'0','HISTORY_GRADER_TRUST_PROXY':'0','HISTORY_GRADER_ADMIN_USERNAME':auth.TEST_USERNAME,'HISTORY_GRADER_ADMIN_PASSWORD_HASH':password_auth.hash_password(auth.TEST_PASSWORD),'HISTORY_GRADER_AUTH_DB':str(app.DB_PATH)}):
    web_auth.AUTH_MANAGER=web_auth.SingleTeacherAuth()
    helper=accounts.TeacherAccountTests();helper.base=f.fixture.base_url;helper.origin=helper.base;helper.opener=build_opener(ProxyHandler({}));helper.create_teacher(helper.admin_headers())
    with patch.object(app,'open_model_request',side_effect=AssertionError('provider forbidden')) as provider:
     p=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.fixture.base_url,str(source),str(out/f'ui-{width}'),str(width),auth.TEST_USERNAME,auth.TEST_PASSWORD],capture_output=True,text=True,encoding='utf-8',timeout=180)
     if p.returncode:raise AssertionError(p.stdout+'\n'+p.stderr)
     provider.assert_not_called()
    with closing(app.get_db()) as c:
     row=c.execute('SELECT * FROM assignments WHERE id=?',(f.assignment_id,)).fetchone();assert (row['sub_questions_json'],row['criteria_json'],row['import_metadata_json'])==before
     assert c.execute('SELECT count(*) FROM submissions').fetchone()[0]==0
     assert c.execute('SELECT count(*) FROM grading_batches').fetchone()[0]==0
     layout=json.loads(c.execute('SELECT layout_profile_json FROM grading_exams WHERE id=?',(f.exam_id,)).fetchone()[0]);assert len(layout['regions'])==2
  finally:web_auth.AUTH_MANAGER=old;f.doCleanups()
 (out/'summary.json').write_text(json.dumps({'desktop':'PASS','mobile390':'PASS','layout_source_and_rubric_unchanged':True,'provider_calls':0,'submissions':0,'batches':0,'real_alignment':True,'real_privacy_preview':True},indent=2),encoding='utf-8')
 print('Vue layout/privacy desktop/mobile PASS; real alignment and preview; provider 0')
if __name__=='__main__':main()
