"""Minimal real browser copy flow with isolated synthetic data."""
import tests
import os,shutil,subprocess,sys
from pathlib import Path
import app
from tests.test_no_identity_workflow import NoIdentityWorkflowTests

for width in (1280,390):
    f=NoIdentityWorkflowTests();f.setUp()
    try:
        shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
        shutil.copytree(Path(sys.argv[1]),app.STATIC_DIR/'next',dirs_exist_ok=True)
        f.http.server.objective_wsgi_app=f.f.f.objective
        f.http.server.stopping=f.http.server.restarting=False
        out=Path('.codex-work/exam-copy-browser');out.mkdir(parents=True,exist_ok=True)
        r=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.http.base_url,f.aid,str(out),str(width)],capture_output=True,text=True,encoding='utf-8',timeout=60,env=dict(os.environ,NO_PROXY='127.0.0.1,localhost'))
        assert r.returncode==0,r.stdout+r.stderr
        print(r.stdout);f.f.f.provider.assert_not_called()
    finally:f.doCleanups()
