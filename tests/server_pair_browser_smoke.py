"""Cloud admin pair configuration, synthetic keys only, no provider calls."""
import json, os, shutil, subprocess, sys
from pathlib import Path
from unittest.mock import patch
import app, web_auth, password_auth
from dataclasses import replace
from contextlib import ExitStack
from tests import test_teacher_accounts as fixtures

def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    for width in (1280,390):
        f=fixtures.TeacherAccountTests();f.setUp();stack=ExitStack()
        try:
            for k,v in {'HISTORY_GRADER_CLOUD_MODE':'1','HISTORY_GRADER_COOKIE_SECURE':'0','HISTORY_GRADER_TRUST_PROXY':'0','HISTORY_GRADER_WEB_AUTH_REQUIRED':'1'}.items():os.environ[k]=v
            # Loopback synthetic HTTP only; production Secure-cookie tests stay unchanged.
            original=password_auth.auth_config
            transport=lambda handler=None:replace(original(handler),cookie_secure=False)
            stack.enter_context(patch.object(password_auth,'auth_config',transport))
            stack.enter_context(patch.object(web_auth,'auth_config',transport))
            f.origin=f.base;f.create_teacher(f.admin_headers())
            shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
            with patch.object(app,'open_model_request',side_effect=AssertionError('provider forbidden')) as provider:
                result=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.base,str(out/f'ui-{width}'),str(width)],capture_output=True,text=True,encoding='utf-8',timeout=140)
                assert result.returncode==0,result.stdout+'\n'+result.stderr
                provider.assert_not_called()
            assert app.load_dual_review_model_configs(('A',))['A']['api_key']=='SYNTHETIC_BROWSER_LEGACY_REPLACEMENT'
        finally:stack.close();f.tearDown()
    (out/'summary.json').write_text(json.dumps({'desktop':'PASS','mobile390':'PASS','legacy_and_vue':'PASS','teacher_denied':True,'real_provider_calls':0,'production_access':False}),encoding='utf-8')

if __name__=='__main__':main()
