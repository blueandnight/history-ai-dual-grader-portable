"""Real Vue/iframe/API objective deletion with synthetic-only databases."""
from contextlib import closing,nullcontext
import os,shutil,subprocess,sys
from pathlib import Path
from unittest.mock import patch
import app
from tests.test_objective_job_deletion import ObjectiveJobDeletionTests

def main():
    build,out=map(Path,sys.argv[1:3]);out.mkdir(parents=True,exist_ok=True)
    for width,mode in ((1280,'current'),(390,'history'),(390,'lost'),(390,'warning')):
        f=ObjectiveJobDeletionTests();f.setUp()
        try:
            server=f.f.fixture.f.server;server.objective_wsgi_app=f.f.fixture.objective;server.stopping=server.restarting=False
            shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True);shutil.copytree(build,app.STATIC_DIR/'next',dirs_exist_ok=True)
            with closing(app.get_db()) as c:before=[tuple(r) for r in c.execute('SELECT * FROM submissions')]
            cm=patch('objective.job_deletion.shutil.rmtree',side_effect=OSError('synthetic cleanup failure')) if mode=='warning' else nullcontext()
            with cm:
                r=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.f.fixture.f.base_url,f.f.assignment,f.f.exam,f.key,f.job,f.other_job,str(out/(str(width)+'-'+mode)),str(width),mode],capture_output=True,text=True,encoding='utf-8',timeout=90,env=dict(os.environ,NO_PROXY='127.0.0.1,localhost'))
            assert r.returncode==0,r.stdout+r.stderr
            print(r.stdout)
            with closing(app.get_db()) as c:
                assert not c.execute('SELECT 1 FROM objective_jobs WHERE job_id=?',(f.job,)).fetchone()
                assert c.execute('SELECT 1 FROM objective_jobs WHERE job_id=?',(f.other_job,)).fetchone()
                assert c.execute('SELECT 1 FROM objective_assessments WHERE assessment_id=?',(f.key,)).fetchone()
                assert before==[tuple(r) for r in c.execute('SELECT * FROM submissions')]
            assert (app.DATA_DIR/'objective'/'uploads'/f.key/f.other).exists()
            for sid in f.f.obj:assert (app.DATA_DIR/'objective'/'uploads'/f.key/sid).exists()==(mode=='warning')
            f.models.assert_not_called()
        finally:f.doCleanups()

if __name__=='__main__':main()
