"""Synthetic completed batch discovery, refresh and batch-specific score workflow."""
from contextlib import closing
import os,json,shutil,subprocess,sys,uuid
from pathlib import Path
from unittest.mock import patch
import app
from tests.test_batch_adoption import AdoptionTests

def main():
 build,out=map(Path,sys.argv[1:3]);out.mkdir(parents=True,exist_ok=True)
 f=AdoptionTests();f.setUp()
 try:
  bids=[uuid.uuid4().hex[:12] for _ in f.ids]
  with closing(app.get_db()) as c,c:
   for i,(bid,sid) in enumerate(zip(bids,f.ids)):
    c.execute('INSERT INTO grading_batches(id,request_id,assignment_id,exam_id,total_sequences,next_sequence,status,manifest_json,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
      (bid,bid,f.assignment,f.exam,1,2 if i==0 else 1,'completed' if i==0 else 'paused','{}',bid,app.utc_now(),app.utc_now()))
    c.execute('UPDATE submissions SET batch_id=?,batch_sequence=1 WHERE id=?',(bid,sid))
    if i==1:c.execute("UPDATE submissions SET state='queued' WHERE id=?",(sid,))
  def snapshot():
   with closing(app.get_db()) as c:return {t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in ('submissions','grading_batches','exam_marking_codes','task_queue')}
  before=snapshot()
  shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True);shutil.copytree(build,app.STATIC_DIR/'next',dirs_exist_ok=True)
  f.http.server.objective_wsgi_app=f.f.f.fixture.objective;f.http.server.restarting=f.http.server.stopping=False
  with patch.object(app,'open_model_request',side_effect=AssertionError('real model forbidden')) as models:
   for width in (1280,390):
    r=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.http.base_url,f.assignment,f.exam,bids[0],f.ids[0],f.ids[1],str(out/str(width)),str(width)],capture_output=True,text=True,encoding='utf-8',env=dict(os.environ,NO_PROXY='127.0.0.1,localhost'),timeout=90)
    assert r.returncode==0,r.stdout+r.stderr
    print(r.stdout)
   models.assert_not_called()
  assert before==snapshot(),'discovery or refresh modified grading records/order'
 finally:f.doCleanups()

if __name__=='__main__':main()
