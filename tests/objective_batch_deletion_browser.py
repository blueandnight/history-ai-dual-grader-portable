"""Synthetic Vue/iframe batch deletion, including a lost response without retry."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import app
from tests.test_objective_batch_deletion import BatchDeletionTests


def main():
    build,out=map(Path,sys.argv[1:3]);out.mkdir(parents=True,exist_ok=True)
    for width,mode in ((1280,'normal'),(390,'normal'),(390,'lost')):
        f=BatchDeletionTests();f.setUp()
        try:
            server=f.f.f.fixture.f.server;server.objective_wsgi_app=f.f.f.fixture.objective;server.stopping=server.restarting=False
            shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
            shutil.copytree(build,app.STATIC_DIR/'next',dirs_exist_ok=True)
            with closing(app.get_db()) as conn,conn:
                conn.execute("UPDATE objective_jobs SET status='processing' WHERE job_id=?",(f.items[-1]['job_id'],))
                conn.execute("UPDATE objective_submissions SET state='processing' WHERE job_id=?",(f.items[-1]['job_id'],))
                before=[tuple(r) for r in conn.execute('SELECT * FROM submissions')]
            manifest=out/(str(width)+'-'+mode+'.json');manifest.write_text(json.dumps(dict(items=f.items[:10],active_job=f.items[-1]['job_id'],key=f.f.key,exam=f.f.f.exam,assignment=f.f.f.assignment)),encoding='utf-8')
            result=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.f.f.fixture.f.base_url,str(manifest),str(out/(str(width)+'-'+mode)),str(width),mode],capture_output=True,text=True,encoding='utf-8',timeout=120,env=dict(os.environ,NO_PROXY='127.0.0.1,localhost'))
            assert result.returncode==0,result.stdout+result.stderr
            with closing(app.get_db()) as conn:
                assert conn.execute('SELECT COUNT(*) FROM objective_jobs').fetchone()[0]==11
                assert conn.execute('SELECT COUNT(*) FROM objective_assessments').fetchone()[0]==1
                assert before==[tuple(r) for r in conn.execute('SELECT * FROM submissions')]
                assert conn.execute('SELECT COUNT(*) FROM exam_answer_links').fetchone()[0]==0
                assert conn.execute("SELECT COUNT(*) FROM objective_audit_events WHERE event_type='objective_jobs_batch_deleted'").fetchone()[0]==1
            f.f.models.assert_not_called();print(result.stdout)
        finally:f.doCleanups()


if __name__=='__main__':main()
