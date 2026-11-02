"""Actual Vue iframe, isolated DB, three uploads; no model or business data."""
import json,os,shutil,subprocess,sys
from pathlib import Path
from contextlib import closing
import tests
import app
from tests.test_objective_exam_results import ObjectiveExamResultsTests

def main():
    f=ObjectiveExamResultsTests();f.setUp()
    try:
        jobs=[f.upload() for _ in range(3)]
        f.disagree=True;f.upload(1)
        shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
        shutil.copytree(Path(sys.argv[1]),app.STATIC_DIR/'next',dirs_exist_ok=True)
        f.f.f.server.objective_wsgi_app=f.f.objective
        f.f.f.server.stopping=f.f.f.server.restarting=False
        out=Path('.codex-work/objective-exam-results');out.mkdir(parents=True,exist_ok=True)
        r=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.f.f.base_url,f.assignment,f.exam,f.key,jobs[-1],str(out.resolve())],capture_output=True,text=True,encoding='utf-8',timeout=100,env=dict(os.environ,NO_PROXY='127.0.0.1,localhost'))
        assert r.returncode==0,r.stdout+r.stderr
        print(r.stdout);f.f.provider.assert_not_called()
    finally:f.doCleanups()
if __name__=='__main__':main()
