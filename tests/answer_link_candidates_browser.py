"""Real local HTTP/Vue browser test using only isolated synthetic papers."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import app
from tests.test_answer_link_candidates import CandidateTests


def main():
    fixture = CandidateTests()
    fixture.setUp()
    try:
        build, output = map(Path, sys.argv[1:3])
        output.mkdir(parents=True, exist_ok=True)
        shutil.copytree(Path(app.__file__).parent / 'static', app.STATIC_DIR, dirs_exist_ok=True)
        shutil.copytree(build, app.STATIC_DIR / 'next', dirs_exist_ok=True)
        server = fixture.fixture.f.server
        server.objective_wsgi_app = fixture.fixture.objective
        server.restarting = server.stopping = False
        env = dict(os.environ, NO_PROXY='127.0.0.1,localhost')
        for width in (1280, 390):
            result = subprocess.run(['node', str(Path(__file__).with_suffix('.js')), fixture.fixture.f.base_url, fixture.assignment, fixture.exam, str(output / str(width)), str(width)], env=env, capture_output=True, text=True, encoding='utf-8', timeout=100)
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout)
            # Unlink between desktop and mobile using the existing teacher endpoint.
            for link in fixture.client.get('/api/answer-links?exam_id=' + fixture.exam).get_json()['links']:
                if link['active']:
                    assert fixture.write(action='unlink', link_id=link['id']).status_code == 200
        fixture.fixture.provider.assert_not_called()
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
