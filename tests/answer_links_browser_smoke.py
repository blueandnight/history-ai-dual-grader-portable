"""Synthetic Vue -> formal objective snapshot -> real local OMR browser acceptance."""
from contextlib import closing
import json,os,shutil,subprocess,sys,random
from pathlib import Path
from unittest.mock import patch
import app
from tests import test_teacher_accounts as accounts
from tests.test_docx_question_import import make_docx
from objective.synthetic import render_omr_card,photograph
from objective.templates import BUILTIN_TEMPLATES


def main():
 out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
 f=accounts.TeacherAccountTests();f.setUp()
 try:
  os.environ.update(HISTORY_GRADER_COOKIE_SECURE='0',HISTORY_GRADER_TRUST_PROXY='0',HISTORY_GRADER_CLOUD_MODE='0',HISTORY_GRADER_WEB_AUTH_REQUIRED='1')
  f.origin=f.base;shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
  shutil.copytree(Path(sys.argv[2]).resolve(),app.STATIC_DIR/'next',dirs_exist_ok=True)
  f.server.objective_wsgi_app=app.create_integrated_objective_wsgi_app();f.server.restarting=False;f.server.stopping=False
  f.create_teacher(f.admin_headers())
  lines=['完全合成历史试题','一、单项选择题']+[f'{i}．合成历史制度比较题干用于软件验收。 A．选项甲 B．选项乙 C．选项丙 D．选项丁' for i in range(1,21)]
  lines+=['二、材料题','21．阅读材料，完成下列要求。（4分）材料：合成材料甲。','（1）概括特点。（4分）','22．阅读材料，完成下列要求。（4分）材料：合成材料乙。','（1）分析原因。（4分）','参考答案','题号 | '+' | '.join(map(str,range(1,21))),'答案 | '+' | '.join(['A']*20),'21．（1）答案甲；答案乙。','22．（1）答案丙；答案丁。']
  word=out/'synthetic-mixed.docx';word.write_bytes(make_docx(lines))
  photo=out/'synthetic-omr.png';photo.write_bytes(photograph(render_omr_card(BUILTIN_TEMPLATES[0]['config'],['A']*20),random.Random(100)))
  with patch.object(app,'dual_review_key_is_configured',return_value=True), patch.object(app,'open_model_request',side_effect=AssertionError('real model forbidden')) as model:
   for width in (1280,390):
    result=subprocess.run(['node',str(Path(__file__).with_name('unified_exam_browser_smoke.js')),f.base,str(word),str(photo),str(out/f'ui-{width}'),str(width)],capture_output=True,text=True,encoding='utf-8',timeout=150)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
    saved=json.loads((out/f'ui-{width}.json').read_text(encoding='utf-8'))['saved']
    exam=saved['exam_id'];aid=saved['assignment']['id']
    with closing(app.get_db()) as c,c:
     from PIL import Image
     import io,uuid
     b=io.BytesIO();Image.new('RGB',(500,300),'white').save(b,'PNG')
     for _ in range(2):
      sid=uuid.uuid4().hex[:12];path=app.DATA_DIR/aid/'students'/sid/'original.png';path.parent.mkdir(parents=True);path.write_bytes(b.getvalue());relative='data/'+path.relative_to(app.DATA_DIR).as_posix()
      c.execute("INSERT INTO submissions(id,assignment_id,exam_id,student_label,answer_text,photos_json,result_json,created_at,state,final_total) VALUES(?,?,?,'synthetic','','PLACEHOLDER','{}',?,'completed',8)".replace("'PLACEHOLDER'","?"),(sid,aid,exam,json.dumps([{'path':relative,'original_path':relative,'original_content_type':'image/png'}]),app.utc_now()))
    linked=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.base,aid,exam,str(out/f'links-{width}'),str(width)],capture_output=True,text=True,encoding='utf-8',timeout=100)
    assert linked.returncode==0,linked.stdout+'\n'+linked.stderr
   model.assert_not_called()
  with closing(app.get_db()) as conn:
   assert conn.execute('SELECT COUNT(*) FROM objective_assessments WHERE source_exam_id IS NOT NULL').fetchone()[0]==2
   assert conn.execute("SELECT COUNT(*) FROM objective_submissions WHERE state='completed' AND score=61 AND max_score=61").fetchone()[0]==2
   assert conn.execute('SELECT COUNT(*) FROM submissions').fetchone()[0]==4
   assert conn.execute('SELECT COUNT(*) FROM exam_answer_links WHERE active=1').fetchone()[0]==0
   assert conn.execute('SELECT COUNT(*) FROM exam_answer_links').fetchone()[0]==4
   assert conn.execute("SELECT COUNT(*) FROM exam_answer_links WHERE actor='test_teacher'").fetchone()[0]==4
  (out/'summary.json').write_text(json.dumps({'desktop':'PASS','mobile390':'PASS','mixed_exams':2,'objective_questions':20,'subjective_questions':2,'objective_score':61,'subjective_max':8,'paper_max':69,'real_provider_calls':0,'link_correct_unlink':True,'active_links':0,'historical_links':4}),encoding='utf-8')
  print('Answer links: desktop/mobile original review -> link -> correct -> unlink PASS; scores unchanged; real models 0')
 finally:f.tearDown()

if __name__=='__main__':main()
