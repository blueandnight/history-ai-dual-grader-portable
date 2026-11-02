"""Isolated real HTTP/Vue stop/resume acceptance; no model calls."""
from contextlib import closing
import os
from pathlib import Path
import shutil
import subprocess
import sys

import app
from tests.test_batch_grading_control import BatchControlTests


def main():
    fixture = BatchControlTests()
    fixture.setUp()
    try:
        build, out = map(Path, sys.argv[1:3])
        out.mkdir(parents=True, exist_ok=True)
        shutil.copytree(Path(app.__file__).parent / 'static', app.STATIC_DIR, dirs_exist_ok=True)
        shutil.copytree(build, app.STATIC_DIR / 'next', dirs_exist_ok=True)
        server = fixture.f.server
        server.objective_wsgi_app = app.create_integrated_objective_wsgi_app()
        server.restarting = server.stopping = False
        with closing(app.get_db()) as c:
            before = [tuple(r) for r in c.execute('SELECT id,result_json,photos_json,final_total,confirmed_at FROM submissions ORDER BY id')]
        for width in (1280, 390):
            result = subprocess.run(['node', str(Path(__file__).with_suffix('.js')), fixture.f.base_url, fixture.row['assignment_id'], str(out / str(width)), str(width)], capture_output=True, text=True, encoding='utf-8', env=dict(os.environ, NO_PROXY='127.0.0.1,localhost'), timeout=90)
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout)
        with closing(app.get_db()) as c:
            assert before == [tuple(r) for r in c.execute('SELECT id,result_json,photos_json,final_total,confirmed_at FROM submissions ORDER BY id')]
        fixture.no_model.assert_not_called()
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
