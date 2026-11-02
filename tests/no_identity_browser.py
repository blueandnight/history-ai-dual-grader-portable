"""Real Vue/iframe + synthetic HTTP records, no external provider calls."""
import tests
import os,shutil,subprocess,sys,json
from pathlib import Path
import app
from tests.test_no_identity_workflow import NoIdentityWorkflowTests

def main():
    for width in (1280,390):
        f=NoIdentityWorkflowTests();f.setUp()
        try:
            sid1=f.f.sid;sid2=f.subject();sid3=f.subject()
            f.grade(sid1);f.grade(sid2,True);f.grade(sid3)
            oid1=f.objective(sid1);oid2=f.objective();oid3=f.objective()
            preview=f.http.get_json('/api/batch-adoption?assignment_id='+f.aid+'&exam_id='+f.exam)
            status,saved=f.http.request_json_status('/api/batch-adoption',dict(assignment_id=f.aid,exam_id=f.exam,preview_token=preview['preview_token'],items=[dict(id=p['id'],exam_id=f.exam) for p in preview['papers'] if p['state']=='adoptable'],confirmed=True));assert status==200
            shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True);shutil.copytree(Path(sys.argv[1]),app.STATIC_DIR/'next',dirs_exist_ok=True)
            f.http.server.objective_wsgi_app=f.f.f.objective;f.http.server.stopping=f.http.server.restarting=False
            out=Path('.codex-work/no-identity-browser');out.mkdir(exist_ok=True,parents=True)
            manifest=out/('manifest-'+str(width)+'.json');manifest.write_text(json.dumps(dict(assignment=f.aid,exam=f.exam,key=f.key,subject=sid2,objective=oid2)))
            r=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.http.base_url,str(manifest),str(out),str(width)],capture_output=True,text=True,encoding='utf-8',timeout=100,env=dict(os.environ,NO_PROXY='127.0.0.1,localhost'))
            assert r.returncode==0,r.stdout+r.stderr
            print(r.stdout);f.f.f.provider.assert_not_called()
        finally:f.doCleanups()
if __name__=='__main__':main()
