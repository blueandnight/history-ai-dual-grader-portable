"""Isolated admin/system Vue writes with synthetic credentials and fake key storage."""
from contextlib import closing
import json,os,shutil,subprocess,sys
from pathlib import Path
from unittest.mock import patch
import app
from tests import test_teacher_accounts as accounts

def main():
 out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
 for width in (1280,390):
  f=accounts.TeacherAccountTests();f.setUp()
  try:
   for k,v in {'HISTORY_GRADER_COOKIE_SECURE':'0','HISTORY_GRADER_TRUST_PROXY':'0','HISTORY_GRADER_CLOUD_MODE':'0','HISTORY_GRADER_WEB_AUTH_REQUIRED':'1'}.items():os.environ[k]=v
   f.origin=f.base;shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
   # No machine credentials: API validation stays real, only OS secret storage is fake.
   with patch.object(app,'read_dpapi_api_key',return_value='SYNTHETIC_UI_STORAGE_ONLY'),patch.object(app,'save_dpapi_api_key') as saved,patch.object(app,'local_api_key_exists',return_value=True),patch.object(app,'open_model_request',side_effect=AssertionError('provider forbidden')) as provider:
    p=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.base,str(out/f'ui-{width}'),str(width)],capture_output=True,text=True,encoding='utf-8',timeout=160)
    if p.returncode:raise AssertionError(p.stdout+'\n'+p.stderr)
    os.environ['HISTORY_GRADER_WEB_AUTH_REQUIRED']='0'
    p=subprocess.run(['node',str(Path(__file__).with_name('vue_local_system_browser_smoke.js')),f.base],capture_output=True,text=True,encoding='utf-8',timeout=80)
    if p.returncode:raise AssertionError(p.stdout+'\n'+p.stderr)
    provider.assert_not_called()
    assert saved.call_count==2,saved.call_count
   with closing(app.get_db()) as c:
    u=c.execute("SELECT enabled,role,email FROM auth_users WHERE username='vue_stage3'").fetchone();assert u['enabled']==1 and u['role']=='teacher' and u['email'] is None
    assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
   assert len(list(app.BACKUP_DIR.glob('*.zip')))>=2
  finally:f.tearDown()
 (out/'summary.json').write_text(json.dumps({'desktop':'PASS','mobile390':'PASS','user_session_revocation':'PASS','backup_restore':'PASS','real_provider_calls':0,'production_access':False,'os_key_storage':'synthetic mock'},indent=2),encoding='utf-8')
 print('Vue users/system desktop/mobile PASS; real provider 0; isolated backup/restore PASS')

if __name__=='__main__':main()
