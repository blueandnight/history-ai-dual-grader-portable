"""Isolated synthetic workspace acceptance. Never uses production paths or models."""
from pathlib import Path
import json,os,shutil,subprocess,sys,uuid
from unittest.mock import patch
import app
from tests.test_teacher_accounts import TeacherAccountTests
from tests.test_unified_exam import mixed_draft

def main():
 out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
 f=TeacherAccountTests();f.setUp()
 try:
  os.environ.update(HISTORY_GRADER_COOKIE_SECURE='0',HISTORY_GRADER_TRUST_PROXY='0',HISTORY_GRADER_CLOUD_MODE='0',HISTORY_GRADER_WEB_AUTH_REQUIRED='1')
  f.origin=f.base;shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True);shutil.copytree(Path(sys.argv[2]).resolve(),app.STATIC_DIR/'next',dirs_exist_ok=True)
  f.server.objective_wsgi_app=app.create_integrated_objective_wsgi_app();f.server.restarting=False;f.server.stopping=False
  f.create_teacher(f.admin_headers());headers=f.teacher_headers()
  draft,confirmation=mixed_draft();ident=uuid.uuid4().hex;root=app.DATA_DIR/'exam_import_drafts'/ident;root.mkdir(parents=True);(root/'exam_import_draft.json').write_text(json.dumps(draft),encoding='utf-8')
  with patch.object(app,'open_model_request',side_effect=AssertionError('Real models forbidden')) as model:
   status,saved,_=f.request_json('POST','/api/create-imported-exam',dict(draft_id=ident,confirmation=confirmation),headers);assert status==201,saved
   for width in (1280,390):
    result=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.base,saved['assignment']['id'],saved['exam_id'],str(out/f'ui-{width}'),str(width)],capture_output=True,text=True,encoding='utf-8',timeout=100)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
   model.assert_not_called()
  print('Workspace entry desktop/mobile PASS; root + old hash + refresh + invalid context + quick + legacy; real models 0')
 finally:f.tearDown()
if __name__=='__main__':main()
