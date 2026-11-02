"""Actual Vue deletion and reset against disposable synthetic data only."""
import os
import shutil
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import app
from tests.test_grading_data_deletion import GradingDataDeletionTests


def main():
    build,out=map(Path,sys.argv[1:3]);out.mkdir(parents=True,exist_ok=True)
    mode=sys.argv[3:4]
    for width in (1280,390):
        f=GradingDataDeletionTests();f.setUp()
        try:
            with closing(app.get_db()) as c,c:f.same_upload(c,f.sids[0],f.oids[0])
            shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
            shutil.copytree(build,app.STATIC_DIR/'next',dirs_exist_ok=True)
            f.http.server.objective_wsgi_app=f.f.f.fixture.objective
            f.http.server.stopping=f.http.server.restarting=False
            with patch.object(app,'open_model_request',side_effect=AssertionError('real models forbidden')) as models:
                r=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.http.base_url,f.aid,f.eid,str(out/str(width)),str(width),*mode],capture_output=True,text=True,encoding='utf-8',timeout=100,env=dict(os.environ,NO_PROXY='127.0.0.1,localhost'))
                assert r.returncode==0,r.stdout+r.stderr
                print(r.stdout);models.assert_not_called()
            with closing(app.get_db()) as c:
                assert c.execute('SELECT COUNT(*) FROM submissions WHERE assignment_id=?',(f.aid,)).fetchone()[0]==(2 if mode==['tools'] else 0)
                assert c.execute('SELECT COUNT(*) FROM objective_submissions').fetchone()[0]==(3 if mode==['tools'] else 0)
                assert c.execute('SELECT 1 FROM assignments WHERE id=?',(f.aid,)).fetchone()
                assert c.execute('SELECT 1 FROM grading_exams WHERE id=?',(f.eid,)).fetchone()
                assert c.execute('SELECT 1 FROM objective_assessments WHERE assessment_id=?',(f.f.key,)).fetchone()
        finally:f.doCleanups()


if __name__=='__main__':main()
