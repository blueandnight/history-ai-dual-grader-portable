"""Authenticated desktop/mobile acceptance on an isolated synthetic database."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from unittest.mock import patch
import app
from tests.test_teacher_accounts import TeacherAccountTests
from tests.test_unified_exam import mixed_draft
from tests.test_unified_scores import seed_answers, seed_final


def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    f=TeacherAccountTests();f.setUp()
    try:
        os.environ.update(HISTORY_GRADER_COOKIE_SECURE='0',HISTORY_GRADER_TRUST_PROXY='0',HISTORY_GRADER_CLOUD_MODE='0',HISTORY_GRADER_WEB_AUTH_REQUIRED='1')
        f.origin=f.base
        shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
        shutil.copytree(Path(sys.argv[2]).resolve(),app.STATIC_DIR/'next',dirs_exist_ok=True)
        f.server.objective_wsgi_app=app.create_integrated_objective_wsgi_app();f.server.restarting=False;f.server.stopping=False
        f.create_teacher(f.admin_headers());headers=f.teacher_headers()
        d,c=mixed_draft();draft=uuid.uuid4().hex;root=app.DATA_DIR/'exam_import_drafts'/draft;root.mkdir(parents=True)
        (root/'exam_import_draft.json').write_text(json.dumps(d),encoding='utf-8')
        with patch.object(app,'open_model_request',side_effect=AssertionError('real models forbidden')) as model:
            status,saved,_=f.request_json('POST','/api/create-imported-exam',dict(draft_id=draft,confirmation=c),headers)
            assert status==201,saved
            exam,aid=saved['exam_id'],saved['assignment']['id']
            status,snapshot,_=f.request_json('POST','/objective/api/assessments/from-exam',dict(exam_id=exam),headers)
            assert status in (200,201),snapshot
            with closing(app.get_db()) as conn,conn:
                job=uuid.uuid4().hex
                conn.execute("INSERT INTO objective_jobs(job_id,assessment_id,mode,grouping,status,total_files,total_submissions,created_at) VALUES(?,?,'omr','one_per_image','completed',2,2,?)",(job,snapshot['assessment_id'],app.utc_now()))
                for i in range(3):
                    sid=uuid.uuid4().hex[:12]
                    conn.execute("INSERT INTO submissions(id,assignment_id,exam_id,marking_code,student_label,answer_text,photos_json,result_json,created_at,state) VALUES(?,?,?,?,'DO_NOT_EXPOSE_NAME','','[]','{}',?,'completed')",(sid,aid,exam,'匿名0'+str(i+1),app.utc_now()))
                    if i!=1:seed_final(conn,sid)
                    if i<2:
                        oid=uuid.uuid4().hex
                        conn.execute("INSERT INTO objective_submissions(submission_id,job_id,assessment_id,anonymous_code,state,score,max_score,page_count,created_at) VALUES(?,?,?,?,'completed',60,60,1,?)",(oid,job,snapshot['assessment_id'],'客观匿名'+str(i),app.utc_now()))
                        seed_answers(conn,oid)
                        conn.execute("INSERT INTO exam_answer_links(id,exam_id,objective_submission_id,subjective_submission_id,method,reason,actor,confirmed_at) VALUES(?,?,?,?,'teacher_original_review','synthetic explicit pairing','synthetic_teacher',?)",(uuid.uuid4().hex,exam,oid,sid,app.utc_now()))
            tables=('assignments','grading_exams','submissions','objective_assessments','objective_submissions','objective_answers','objective_review_items','objective_review_events','exam_answer_links')
            def stored(conn):
                return {t:[tuple(r) for r in conn.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in tables}
            with closing(app.get_db()) as conn:before=stored(conn)
            for width in (1280,390):
                result=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.base,aid,exam,str(out/f'ui-{width}'),str(width)],capture_output=True,text=True,encoding='utf-8',timeout=90)
                assert result.returncode==0,result.stdout+'\n'+result.stderr
            # Authentication writes are expected; grading, review and links stay read-only.
            with closing(app.get_db()) as conn:
                assert before==stored(conn),'Read-only score workflow mutated stored records'
            model.assert_not_called()
        print('Unified scores desktop/mobile: complete + pending + unlinked; GET-only details; model calls 0 PASS')
    finally:f.tearDown()


if __name__=='__main__':main()
