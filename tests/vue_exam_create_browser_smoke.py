"""Isolated synthetic exam import through real backend and Vue; no model access."""
from contextlib import closing
import json, os, shutil, subprocess, sys
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfWriter
import app
from tests import test_teacher_accounts as accounts
from tests.test_docx_question_import import make_docx

def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    fixture=accounts.TeacherAccountTests();fixture.setUp()
    try:
        for key,value in {'HISTORY_GRADER_COOKIE_SECURE':'0','HISTORY_GRADER_TRUST_PROXY':'0','HISTORY_GRADER_CLOUD_MODE':'0','HISTORY_GRADER_WEB_AUTH_REQUIRED':'1'}.items():os.environ[key]=value
        fixture.origin=fixture.base
        shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
        if len(sys.argv)>2:shutil.copytree(Path(sys.argv[2]).resolve(),app.STATIC_DIR/'next',dirs_exist_ok=True)
        fixture.create_teacher(fixture.admin_headers())
        word=out/'synthetic.docx';word.write_bytes(make_docx(['合成导入考试','9．阅读材料，完成下列要求。（4分）材料：合成材料。','（1）概括特点。（4分）','参考答案','9．（1）答案甲；答案乙。']))
        pdf=out/'blank.pdf';writer=PdfWriter();writer.add_blank_page(width=595,height=842)
        with pdf.open('wb') as stream:writer.write(stream)
        with closing(app.get_db()) as c:before=c.execute('SELECT count(*) FROM assignments').fetchone()[0]
        with patch.object(app,'open_model_request',side_effect=AssertionError('real provider forbidden')) as provider:
            for width in (1280,390):
                result=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),fixture.base,str(out),str(width),str(word),str(pdf)],capture_output=True,text=True,encoding="utf-8",timeout=100)
                if result.returncode:raise AssertionError(result.stdout+'\n'+result.stderr)
            provider.assert_not_called()
        with closing(app.get_db()) as c:
            rows=c.execute('SELECT sub_questions_json FROM assignments').fetchall()
            assert len(rows)==before+2,len(rows)
            for row in rows:
                q=json.loads(row[0])[0]
                if q['scoring_mode']=='point_bank':
                    assert q['max_score']==8 and q['point_value']==2,q
                    assert [p['score'] for p in q['criteria']]==[2]*5,q
                    app._require_answer_region_rubric_ready({'teacher_confirmation_status':'confirmed_by_teacher','teacher_confirmed_at':'2026-10-21T00:00:00Z'},[q],[q['id']])
                else:
                    assert q['scoring_mode']=='weighted' and q['max_score']==4,q
                    assert [p['score'] for p in q['criteria']]==[1,3],q
            assert sorted(json.loads(row[0])[0]['scoring_mode'] for row in rows)==['point_bank','weighted']
            assert c.execute('SELECT count(*) FROM submissions').fetchone()[0]==0
        (out/'summary.json').write_text(json.dumps({'desktop':'PASS','mobile390':'PASS','created_synthetic_exams':2,'submissions':0,'real_provider_calls':0,'production_access':False},indent=2),encoding='utf-8')
        print('Vue exam creation: real synthetic backend, desktop/mobile PASS; model calls 0')
    finally:fixture.tearDown()

if __name__=='__main__':main()
