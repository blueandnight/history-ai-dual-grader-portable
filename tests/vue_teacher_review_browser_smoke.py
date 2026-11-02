"""Synthetic real-backend Vue teacher final: receipt, zero, cap, scope, audit."""
from contextlib import closing
import copy,io,json,os,shutil,subprocess,sys
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw
import app
from tests import test_teacher_accounts as accounts
from tests import test_teacher_deterministic_final as scoring

def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    for width in (1280,390):
        f=accounts.TeacherAccountTests();f.setUp()
        try:
            for k,v in {'HISTORY_GRADER_COOKIE_SECURE':'0','HISTORY_GRADER_TRUST_PROXY':'0','HISTORY_GRADER_CLOUD_MODE':'0','HISTORY_GRADER_WEB_AUTH_REQUIRED':'1'}.items():os.environ[k]=v
            f.origin=f.base;shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True);f.create_teacher(f.admin_headers())
            if len(sys.argv)>2:shutil.copytree(Path(sys.argv[2]).resolve(),app.STATIC_DIR/'next',dirs_exist_ok=True)
            fixture=scoring.TeacherDeterministicFinalTests();fixture.setUp();qs=fixture.q
            for q in qs:q.update(label=q['id'],question='合成设问',standard_answer='合成参考')
            sub=[{'sub_question_id':q['id'],'answer_status':'answered','suggested_total':q['max_score'],'criteria':[{'criterion_id':p['id'],'decision':'full','suggested_score':p['score'],'student_evidence':'合成引文'} for p in q['criteria']]} for q in qs]
            models={'A':{'suggested_total':14,'sub_questions':sub},'B':{'suggested_total':14,'sub_questions':sub}}
            base={'mode':'vision-model','suggested_total':14,'max_total':14,'blocked':True,'block_reasons':['synthetic_review_required'],'sub_questions':sub,'dual_review':{'status':'complete','results':models,'comparison':{'risk_level':'HIGH'}}}
            image=Image.new('RGB',(500,300),'white');ImageDraw.Draw(image).text((30,50),'SYNTHETIC HANDWRITING TEST',fill='black');buf=io.BytesIO();image.save(buf,'PNG')
            with closing(app.get_db()) as c,c:
                c.execute('INSERT INTO assignments(id,name,question,standard_answer,criteria_json,sub_questions_json,created_at) VALUES(?,?,?,?,?,?,?)',('a'*12,'合成教师终评','q','a','[]',json.dumps(qs),app.utc_now()))
                for sid in ('b'*12,'c'*12,'d'*12):
                    result=copy.deepcopy(base)
                    if sid.startswith('c'):result.update(blocked=False,block_reasons=[],grading_scope={'graded_question_ids':['Q2'],'scoped_max_score':8})
                    if sid.startswith('d'):
                        result.update(block_reasons=['unanswered_candidate'],grading_scope={'graded_question_ids':['Q1'],'scoped_max_score':6})
                        result['sub_questions']=[{**sub[0],'answer_status':'unanswered_candidate','block_reasons':['unanswered_candidate']}]
                    path=app.DATA_DIR/('a'*12)/'students'/sid/'original.png';path.parent.mkdir(parents=True);path.write_bytes(buf.getvalue());relative='data/'+path.relative_to(app.DATA_DIR).as_posix()
                    photos=[{'path':relative,'original_path':relative,'original_content_type':'image/png'}]
                    c.execute('INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state,source_channel) VALUES(?,?,?,?,?,?,?,?,?)',(sid,'a'*12,sid,'',json.dumps(photos),json.dumps(result),app.utc_now(),'completed','computer'))
            with patch.object(app,'open_model_request',side_effect=AssertionError('real provider forbidden')) as provider:
                p=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.base,str(out/f'ui-{width}'),str(width)],capture_output=True,text=True,encoding='utf-8',timeout=140)
                if p.returncode:raise AssertionError(p.stdout+'\n'+p.stderr)
                provider.assert_not_called()
            with closing(app.get_db()) as c:
                rows=c.execute('SELECT id,final_total,result_json FROM submissions ORDER BY id').fetchall();assert [r['final_total'] for r in rows]==[14,8,0],[(r['id'],r['final_total']) for r in rows]
                assert json.loads(rows[0]['result_json'])['dual_review']['results']==models
                assert json.loads(rows[1]['result_json'])['dual_review']['teacher_final']['question_subtotals'][0]['subtotal']==8
                events=c.execute("SELECT count(*) FROM audit_events WHERE event_type='submission_confirmed'").fetchone()[0];assert events==4,events
        finally:f.tearDown()
    (out/'summary.json').write_text(json.dumps({'desktop':'PASS','mobile390':'PASS','zero':0,'pool_cap':8,'reconfirmed':14,'real_provider_calls':0,'production_access':False},indent=2),encoding='utf-8')
    print('Vue teacher final: desktop/mobile, zero/cap/scope/blank/receipt/audit PASS; real provider 0')

if __name__=='__main__':main()
