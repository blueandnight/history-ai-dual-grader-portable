"""Four-width visual and interaction audit on an isolated synthetic database."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import hashlib
from unittest.mock import patch
import app
from tests.test_teacher_accounts import TeacherAccountTests
from tests.test_unified_exam import mixed_draft
from tests.test_unified_scores import seed_answers, seed_final
from tests.test_docx_question_import import make_docx


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
        with closing(app.get_db()) as conn,conn:conn.execute("UPDATE auth_users SET role='admin' WHERE username='test_teacher'")
        d,c=mixed_draft();draft=uuid.uuid4().hex;root=app.DATA_DIR/'exam_import_drafts'/draft;root.mkdir(parents=True)
        (root/'exam_import_draft.json').write_text(json.dumps(d),encoding='utf-8')
        with patch.object(app,'open_model_request',side_effect=AssertionError('real models forbidden')) as model:
            status,saved,_=f.request_json('POST','/api/create-imported-exam',dict(draft_id=draft,confirmation=c),headers)
            assert status==201,saved
            exam,aid=saved['exam_id'],saved['assignment']['id']
            lines=['合成历史试卷','一、单项选择题']+[f'{i}．合成历史设问。 A．甲 B．乙 C．丙 D．丁' for i in range(1,21)]+['二、材料题','21．阅读材料。（4分）材料：合成材料甲。','（1）概括特点。（4分）','参考答案','题号 | '+' | '.join(map(str,range(1,21))),'答案 | '+' | '.join(['A']*20),'21．（1）答案甲；答案乙。']
            (out/'draft.docx').write_bytes(make_docx(lines))
            status,snapshot,_=f.request_json('POST','/objective/api/assessments/from-exam',dict(exam_id=exam),headers)
            assert status in (200,201),snapshot
            with closing(app.get_db()) as conn,conn:
                job=uuid.uuid4().hex
                conn.execute("INSERT INTO objective_jobs(job_id,assessment_id,mode,grouping,status,total_files,total_submissions,created_at) VALUES(?,?,'omr','one_per_image','completed',2,2,?)",(job,snapshot['assessment_id'],app.utc_now()))
                for i in range(3):
                    sid=uuid.uuid4().hex[:12]
                    conn.execute("INSERT INTO submissions(id,assignment_id,exam_id,marking_code,student_label,answer_text,photos_json,result_json,created_at,state) VALUES(?,?,?,?,'DO_NOT_EXPOSE_NAME','','[]','{}',?,'completed')",(sid,aid,exam,'匿名0'+str(i+1),app.utc_now()))
                    seed_final(conn,sid)
                    from PIL import Image,ImageDraw
                    import io
                    image=Image.new('RGB',(900,1200),'white');ImageDraw.Draw(image).text((60,100),'SYNTHETIC HISTORY ANSWER - '+str(i+1),fill='black');buf=io.BytesIO();image.save(buf,'PNG')
                    photo=app.DATA_DIR/aid/'students'/sid/'original.png';photo.parent.mkdir(parents=True,exist_ok=True);photo.write_bytes(buf.getvalue());rel='data/'+photo.relative_to(app.DATA_DIR).as_posix()
                    result=json.loads(conn.execute('SELECT result_json FROM submissions WHERE id=?',(sid,)).fetchone()[0]);qs=json.loads(conn.execute('SELECT sub_questions_json FROM assignments WHERE id=?',(aid,)).fetchone()[0])
                    sub=[dict(sub_question_id=q['id'],criteria=[dict(criterion_id=p['id'],decision='full',suggested_score=p['score'],student_evidence='合成答案：制度具有统一性。') for p in q['criteria']]) for q in qs]
                    result['dual_review']['results']={k:dict(suggested_total=8,sub_questions=sub) for k in ('A','B')}
                    conn.execute('UPDATE submissions SET photos_json=?,result_json=? WHERE id=?',(json.dumps([dict(path=rel,original_path=rel,original_content_type='image/png')]),json.dumps(result),sid))
                    if i==1:conn.execute('UPDATE submissions SET confirmed_at=NULL,final_total=NULL WHERE id=?',(sid,))
                    if i<3:
                        oid=uuid.uuid4().hex
                        conn.execute("INSERT INTO objective_submissions(submission_id,job_id,assessment_id,anonymous_code,state,score,max_score,page_count,created_at) VALUES(?,?,?,?,'completed',60,60,1,?)",(oid,job,snapshot['assessment_id'],'客观匿名'+str(i),app.utc_now()))
                        seed_answers(conn,oid)
                        conn.execute("INSERT INTO objective_submission_pages(page_id,submission_id,page_no,stored_path,image_sha256,byte_count,width,height,recognition_state) VALUES(?,?,1,?,?,?,?,?,'completed')",(uuid.uuid4().hex,oid,str(photo),hashlib.sha256(buf.getvalue()).hexdigest(),len(buf.getvalue()),900,1200))
                        if i==0:conn.execute("INSERT INTO exam_answer_links(id,exam_id,objective_submission_id,subjective_submission_id,method,reason,actor,confirmed_at) VALUES(?,?,?,?,'teacher_original_review','synthetic explicit pairing','synthetic_teacher',?)",(uuid.uuid4().hex,exam,oid,sid,app.utc_now()))
            tables=('assignments','grading_exams','submissions','objective_assessments','objective_submissions','objective_answers','objective_review_items','objective_review_events','exam_answer_links')
            def stored(conn):
                return {t:[tuple(r) for r in conn.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in tables}
            with closing(app.get_db()) as conn:before=stored(conn)
            for width in (1440,1280,768,390):
                result=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.base,aid,exam,str(out),str(width),snapshot['assessment_id']],capture_output=True,text=True,encoding='utf-8',timeout=90)
                assert result.returncode==0,result.stdout+'\n'+result.stderr
            # Authentication writes are expected; grading, review and links stay read-only.
            with closing(app.get_db()) as conn:
                assert before==stored(conn),'Read-only score workflow mutated stored records'
            model.assert_not_called()
        print('UI visual audit 1440/1280/768/390 PASS; read-only synthetic grades; real models 0')
    finally:f.tearDown()


if __name__=='__main__':main()
