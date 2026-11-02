"""Desktop/mobile permanent deletion and bulk cleanup, isolated synthetic files only."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch
import app
from tests import test_teacher_accounts as accounts
from tests.test_exam_maintenance import seed_workspace


def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    for width in (1280,390):
        f=accounts.TeacherAccountTests();f.setUp()
        try:
            os.environ.update(HISTORY_GRADER_COOKIE_SECURE='0',HISTORY_GRADER_TRUST_PROXY='0',HISTORY_GRADER_CLOUD_MODE='0',HISTORY_GRADER_WEB_AUTH_REQUIRED='1')
            f.origin=f.base
            shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
            shutil.copytree(Path(sys.argv[2]).resolve(),app.STATIC_DIR/'next',dirs_exist_ok=True)
            with patch.object(app,'open_model_request',side_effect=AssertionError('real models forbidden')) as model:
                headers,aid,exam,ids,snapshot=seed_workspace(f)
                with closing(app.get_db()) as conn,conn:
                    import uuid
                    now=app.utc_now();batch=uuid.uuid4().hex[:12]
                    conn.execute('UPDATE submissions SET confirmed_at=NULL WHERE id=?',(ids[1],))
                    conn.execute("INSERT INTO grading_batches(id,request_id,assignment_id,total_sequences,status,manifest_json,request_hash,created_at,updated_at) VALUES(?,?,?,2,'active','{}','synthetic',?,?)",(batch,uuid.uuid4().hex,aid,now,now))
                    for i,sid in enumerate(ids[3:]):
                        conn.execute("UPDATE submissions SET state='queued',batch_id=?,batch_sequence=? WHERE id=?",(batch,i+1,sid))
                        conn.execute("INSERT INTO task_queue(id,submission_id,state,available_at,created_at,updated_at) VALUES(?,?,'waiting_batch',?,?,?)",(uuid.uuid4().hex,sid,now,now,now))
                with closing(app.get_db()) as conn:
                    before_other=tuple(conn.execute('SELECT * FROM assignments WHERE id=?',('e'*12,)).fetchone())
                result=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.base,aid,exam,str(out),str(width)],capture_output=True,text=True,encoding='utf-8',timeout=100)
                assert result.returncode==0,result.stdout+'\n'+result.stderr
                model.assert_not_called()
                with closing(app.get_db()) as conn:
                    assert conn.execute('SELECT count(*) FROM submissions').fetchone()[0]==0
                    assert conn.execute('SELECT count(*) FROM objective_submissions').fetchone()[0]==0
                    assert conn.execute('SELECT count(*) FROM exam_answer_links').fetchone()[0]==0
                    assert conn.execute('SELECT count(*) FROM objective_assessments').fetchone()[0]==1
                    assert before_other==tuple(conn.execute('SELECT * FROM assignments WHERE id=?',('e'*12,)).fetchone())
                    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
                    assert conn.execute("SELECT count(*) FROM audit_events WHERE event_type='submission_photos_deleted'").fetchone()[0]==3
                    assert conn.execute("SELECT count(*) FROM audit_events WHERE event_type='exam_deleted'").fetchone()[0]==1
                assert not (app.DATA_DIR/aid).exists()
                assert (app.DATA_DIR/('e'*12)/'keep.txt').exists()
        finally:f.tearDown()
    print('Maintenance desktop/mobile PASS: permanent mixed-exam deletion, batch cleanup, lost response stops remaining; real models 0')


if __name__=='__main__':main()
