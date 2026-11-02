"""Model catalog/custom pair controls and support downloads, synthetic desktop only."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch
import app
from tests.test_custom_pair_and_diagnostics import DesktopFeatureTests


def main():
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    for width in (1280,390):
        fixture=DesktopFeatureTests();fixture.setUp()
        try:
            shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
            def reply(request,timeout):
                assert request.method=='GET' and request.full_url=='https://example.invalid/v1/models'
                assert request.get_header('Authorization')=='Bearer SYNTHETIC_TYPED'
                return io.BytesIO(json.dumps({'data':[{'id':'synthetic-vision'},{'id':'<img src=x>'}]}).encode())
            fixture.provider.side_effect=reply
            with patch.dict(os.environ,{'LOCALAPPDATA':str(Path(fixture.fixture.temp.name)/'local-profile')}):
                result=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),fixture.fixture.base_url,str(out/f'ui-{width}'),str(width)],capture_output=True,text=True,encoding='utf8',timeout=90)
            assert result.returncode==0,result.stdout+'\n'+result.stderr
            assert fixture.provider.call_count==2
        finally:fixture.doCleanups()
    (out/'summary.json').write_text(json.dumps({'desktop':True,'mobile390':True,'new_and_legacy':True,'real_provider_calls':0,'synthetic_catalog_gets':4},indent=2),encoding='utf8')
    print('MODEL_SUPPORT_UI: desktop/mobile390 legacy/Vue PASS; real providers 0')


if __name__=='__main__':main()
