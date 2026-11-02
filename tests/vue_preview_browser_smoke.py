"""Real Edge preview acceptance with synthetic accounts and no provider access."""
from contextlib import closing
import json,os,shutil,subprocess,sys
from pathlib import Path
from unittest.mock import patch
import app
from tests.test_teacher_accounts import TeacherAccountTests


def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    fixture=TeacherAccountTests();fixture.setUp()
    try:
        # HTTP is confined to loopback. Production secure-cookie policy is untouched.
        os.environ['HISTORY_GRADER_COOKIE_SECURE']='0'
        os.environ['HISTORY_GRADER_TRUST_PROXY']='0'
        os.environ['HISTORY_GRADER_CLOUD_MODE']='0'
        os.environ['HISTORY_GRADER_WEB_AUTH_REQUIRED']='1'
        fixture.origin=fixture.base
        shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
        if len(sys.argv)>2:shutil.copytree(Path(sys.argv[2]).resolve(),app.STATIC_DIR/'next',dirs_exist_ok=True)
        admin=fixture.admin_headers();fixture.create_teacher(admin)
        questions=[{'id':'Q1','label':'1','question':'合成设问','standard_answer':'合成参考',
                    'scoring_mode':'weighted','max_score':8,'criteria':[{'id':'C1','label':'合成要点一','score':2},{'id':'C2','label':'合成要点二','score':6}]}]
        with closing(app.get_db()) as c:
            c.execute("INSERT INTO assignments(id,name,question,standard_answer,criteria_json,sub_questions_json,created_at) VALUES(?,?,?,?,?,?,?)",
                      ('a'*12,'合成历史考试','合成设问','合成参考','[]',json.dumps(questions),app.utc_now()))
            a={'suggested_total':1.5,'sub_questions':[{'sub_question_id':'Q1','criteria':[{'criterion_id':'C1','decision':'partial','suggested_score':1.5,'student_evidence':'合成A引文'},{'criterion_id':'C2','decision':'none','suggested_score':0}]}]}
            b={'suggested_total':2,'sub_questions':[{'sub_question_id':'Q1','criteria':[{'criterion_id':'C1','decision':'full','suggested_score':2,'student_evidence':'合成B引文'},{'criterion_id':'C2','decision':'none','suggested_score':0}]}]}
            result={'mode':'vision-model','suggested_total':1.5,'max_total':30,'grading_scope':{'graded_question_ids':['Q1'],'scoped_max_score':8},'sub_questions':[],
                    'dual_review':{'status':'complete','models':{'A':{'label':'合成AI-A'},'B':{'label':'合成AI-B'}},'results':{'A':a,'B':b},'comparison':{'risk_level':'HIGH','risk_reasons':['score_disagreement']},'teacher_final':{'point_decisions':[{'question_id':'Q1','rubric_point_id':'C1','decision':'none'}]}}}
            c.execute("INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state,final_total,confirmed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      ('b'*12,'a'*12,'<img src=x onerror=alert(1)>','','[]',json.dumps(result),app.utc_now(),'completed',0,app.utc_now()))
            missing=json.loads(json.dumps(result));missing['dual_review']['models']['B']['status']='error';missing['dual_review']['results'].pop('B')
            c.execute("INSERT INTO submissions(id,assignment_id,student_label,answer_text,photos_json,result_json,created_at,state,final_total) VALUES(?,?,?,?,?,?,?,?,?)",
                      ('c'*12,'a'*12,'合成缺失侧卷','','[]',json.dumps(missing),app.utc_now(),'completed',6))
            c.commit()
        status,_,_=fixture.request_json('POST','/api/roster-student',{'class_name':'合成预览班级','identity_code':'07','display_name':'合成学生'},admin)
        assert status==200,status
        status,_,_=fixture.request_json('POST','/api/batches',{'assignment_id':'a'*12,'batch_request_id':'vue-preview-synthetic','items':[{'page_count':1}]},admin)
        assert status==201,status
        with patch.object(app,'open_model_request',side_effect=AssertionError('real provider forbidden')) as provider:
            for width in (1280,390):
                p=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),fixture.base,str(out/f'ui-{width}'),str(width)],capture_output=True,text=True,timeout=90)
                if p.returncode:raise AssertionError(p.stdout+'\n'+p.stderr)
            provider.assert_not_called()
        (out/'summary.json').write_text(json.dumps({'desktop':'PASS','mobile390':'PASS','authentication':'real synthetic backend sessions','real_provider_calls':0,'production_access':False},indent=2),encoding='utf-8')
        print('Vue preview auth/navigation: desktop and 390px PASS; provider 0')
    finally:fixture.tearDown()


if __name__=='__main__':main()
