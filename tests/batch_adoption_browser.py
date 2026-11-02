"""Actual isolated Vue -> preview -> batch adoption -> review/export acceptance."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
from contextlib import closing
from unittest.mock import patch

import app
from tests.test_batch_adoption import AdoptionTests


def main():
    build,out=map(Path,sys.argv[1:3]);out.mkdir(parents=True,exist_ok=True)
    for width in (1280,390):
        f=AdoptionTests();f.setUp()
        try:
            def conflict(r):
                p=r['dual_review']['results']['B']['sub_questions'][0]['criteria'][0]
                p.update(decision='none',suggested_score=0)
            f.mutate(f.ids[1],conflict)
            shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
            shutil.copytree(build,app.STATIC_DIR/'next',dirs_exist_ok=True)
            f.http.server.objective_wsgi_app=f.f.f.fixture.objective
            f.http.server.restarting=f.http.server.stopping=False
            with patch.object(app,'open_model_request',side_effect=AssertionError('real models forbidden')) as model:
                result=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.http.base_url,f.assignment,f.exam,f.ids[1],str(out/str(width)),str(width)],capture_output=True,text=True,encoding='utf-8',env=dict(os.environ,NO_PROXY='127.0.0.1,localhost'),timeout=100)
                assert result.returncode==0,result.stdout+result.stderr
                model.assert_not_called();print(result.stdout)
            with closing(app.get_db()) as c:
                adopted=c.execute('SELECT final_total,confirmed_at,result_json FROM submissions WHERE id=?',(f.ids[0],)).fetchone()
                assert adopted['final_total']==8 and adopted['confirmed_at']
                pending=c.execute('SELECT final_total,confirmed_at FROM submissions WHERE id=?',(f.ids[1],)).fetchone()
                assert pending['final_total'] is None and pending['confirmed_at'] is None
        finally:f.doCleanups()


if __name__=='__main__':main()
