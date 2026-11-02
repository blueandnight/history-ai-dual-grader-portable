"""Report UI acceptance in Edge with isolated synthetic database and zero providers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch
import app
from tests.test_exam_report_integration import ExamReportHTTPTests


def main():
    out=Path(sys.argv[1]).resolve(); out.mkdir(parents=True,exist_ok=True)
    fixture=ExamReportHTTPTests(); fixture.setUp()
    try:
        shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
        for width in (1280,390):
            result=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),fixture.fixture.base_url,str(out/f'ui-{width}'),str(width)],capture_output=True,text=True,encoding='utf8',timeout=90)
            if result.returncode: raise AssertionError(result.stdout+'\n'+result.stderr)
        fixture.provider.assert_not_called()
        (out/'summary.json').write_text(json.dumps({'desktop':'PASS','mobile390':'PASS','old_and_vue':'PASS','real_provider_calls':0,'production_access':False},indent=2),encoding='utf8')
        print('REPORT_UI: desktop/mobile390 legacy/Vue PASS; providers 0')
    finally: fixture.doCleanups()


if __name__=='__main__':main()
