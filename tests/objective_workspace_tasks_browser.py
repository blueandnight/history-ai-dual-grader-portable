"""Actual main-service iframe restoration, synthetic DB, no writes/provider calls."""
from contextlib import closing
import os,shutil,subprocess,sys
from pathlib import Path
from unittest.mock import patch
import app
from tests.test_batch_adoption import AdoptionTests

def main():
    f=AdoptionTests();f.setUp()
    try:
        shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
        shutil.copytree(Path(sys.argv[1]),app.STATIC_DIR/'next',dirs_exist_ok=True)
        f.http.server.objective_wsgi_app=f.f.f.fixture.objective
        f.http.server.stopping=f.http.server.restarting=False
        with closing(app.get_db()) as c:
            job,key=c.execute('SELECT job_id,assessment_id FROM objective_jobs LIMIT 1').fetchone()
            before=[tuple(r) for r in c.execute('SELECT * FROM objective_jobs')]
        with patch.object(app,'open_model_request',side_effect=AssertionError('real models forbidden')) as calls:
            r=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.http.base_url,f.assignment,f.exam,key,job],capture_output=True,text=True,encoding='utf-8',timeout=90,env=dict(os.environ,NO_PROXY='127.0.0.1,localhost'))
            assert r.returncode==0,r.stdout+r.stderr
            print(r.stdout);calls.assert_not_called()
        with closing(app.get_db()) as c:assert before==[tuple(r) for r in c.execute('SELECT * FROM objective_jobs')]
    finally:f.doCleanups()

if __name__=='__main__':main()
