"""Actual Vue/API deletion with disposable synthetic batches and photos."""
from contextlib import closing
import os,shutil,subprocess,sys
from pathlib import Path
from tests.test_batch_deletion import BatchDeletionTests
import app

def main():
    build,out=map(Path,sys.argv[1:3]);out.mkdir(parents=True,exist_ok=True)
    for width,lost in ((1280,False),(390,False),(390,True)):
        f=BatchDeletionTests();f.setUp()
        try:
            shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True);shutil.copytree(build,app.STATIC_DIR/'next',dirs_exist_ok=True)
            f.f.http.server.objective_wsgi_app=f.f.f.f.fixture.objective;f.f.http.server.stopping=f.f.http.server.restarting=False
            with closing(app.get_db()) as c:
                objective=[tuple(r) for r in c.execute('SELECT * FROM objective_submissions')]
            args=['node',str(Path(__file__).with_suffix('.js')),f.f.http.base_url,f.f.assignment,f.f.exam,f.bid,f.other_bid,str(out/str(width)),str(width)]+(['lost'] if lost else [])
            r=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',timeout=90,env=dict(os.environ,NO_PROXY='127.0.0.1,localhost'))
            assert r.returncode==0,r.stdout+r.stderr
            print(r.stdout)
            with closing(app.get_db()) as c:
                assert not c.execute('SELECT 1 FROM grading_batches WHERE id=?',(f.bid,)).fetchone()
                assert c.execute('SELECT 1 FROM grading_batches WHERE id=?',(f.other_bid,)).fetchone()
                assert [tuple(r) for r in c.execute('SELECT * FROM objective_submissions')]==objective
            assert not (app.DATA_DIR/f.f.assignment/'students'/f.sid).exists()
            assert (app.DATA_DIR/f.f.assignment/'students'/f.other).exists()
            f.models.assert_not_called()
        finally:f.doCleanups()

if __name__=='__main__':main()
