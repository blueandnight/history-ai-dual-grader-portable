"""Synthetic local mobile connection controls without leaving the Vue upload page."""
from pathlib import Path
from unittest.mock import patch
import os,shutil,subprocess,sys
import app
from tests.test_formal_batch_workflow import FormalBatchWorkflowTests

out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
for width in (1280,390):
    f=FormalBatchWorkflowTests();f.setUp()
    try:
        shutil.copytree(Path(app.__file__).parent/'static',app.STATIC_DIR,dirs_exist_ok=True)
        shutil.copytree(Path(sys.argv[2]).resolve(),app.STATIC_DIR/'next',dirs_exist_ok=True)
        image=out/'synthetic.png';image.write_bytes(f.source)
        with patch.object(app,'cached_local_network_interfaces',return_value=[{'address':'192.0.2.10','connection_label':'合成 Wi-Fi','connection_type':'wifi','recommended':True}]):
            p=subprocess.run(['node',str(Path(__file__).with_suffix('.js')),f.fixture.base_url,str(image),str(out/f'ui-{width}'),str(width)],capture_output=True,text=True,encoding='utf8',timeout=90)
            assert p.returncode==0,p.stdout+p.stderr
        f.provider.assert_not_called()
    finally:f.doCleanups()
print('Vue mobile controls desktop/mobile PASS; no legacy navigation, file selection preserved, token consent/revocation; real provider 0')
