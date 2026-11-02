"""Exercise a built Portable EXE using fresh, disposable local data only."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler, Request


def smoke_environment(root: Path) -> dict[str, str]:
    """Never inherit provider/auth credentials or the user's machine key store."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(('HISTORY_GRADER', 'DEEPSEEK', 'MIMO', 'XIAOMI_MIMO',
                                                 'QWEN', 'OPENAI', 'GOOGLE', 'GEMINI', 'ZHIPU', 'BIGMODEL'))
                   and not key.upper().endswith('_API_KEY')}
    environment['LOCALAPPDATA'] = str(root / 'local-app-data')
    environment['PYTHONPATH'] = ''
    environment['PYTHONUTF8'] = '1'
    return environment


def run_smoke(exe: Path, timeout: float = 90) -> dict:
    exe = exe.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='history-portable-smoke-') as td:
        root = Path(td)
        (root / 'config').mkdir()
        # Shadows a legacy .env in the executable directory, even if present.
        (root / 'config' / '.env').write_text('', encoding='utf-8')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        opener = build_opener(ProxyHandler({}))

        def get(path, json_body=True):
            with opener.open(base + path, timeout=3) as response:
                body = response.read().decode('utf-8')
                if response.status != 200:
                    raise RuntimeError(f'{path}: HTTP {response.status}')
                return json.loads(body) if json_body else body

        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen([str(exe), '--host', '127.0.0.1', '--port', str(port),
                '--portable-root', str(root)], cwd=exe.parent, env=smoke_environment(root),
                stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + timeout
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f'Portable exited before health: {process.returncode}')
                    try:
                        health = get('/api/health')
                        break
                    except (OSError, URLError, ValueError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Portable health timeout')
                        time.sleep(.25)
                if not health.get('ok') or not health.get('pdf_renderer'):
                    raise RuntimeError('Health/PDFium renderer not ready')
                if '历史学科 AI 双评阅卷系统' not in get('/', False):
                    raise RuntimeError('Main page missing')
                if '使用帮助' not in get('/help', False):
                    raise RuntimeError('Offline help missing')
                for asset in ('app.js', 'formal-grading.js', 'privacy-flow.js'):
                    if not get('/static/' + asset, False).strip():
                        raise RuntimeError(f'Static asset missing: {asset}')
                vue = get('/next/', False)
                vue_assets = re.findall(r'(?:src|href)="(/next/assets/[^"?]+\.(?:js|css))"', vue)
                if len(vue_assets) < 2:
                    raise RuntimeError('Current Vue workbench build missing')
                for asset in vue_assets:
                    if not get(asset, False).strip():
                        raise RuntimeError('Vue asset missing: ' + asset)
                status = get('/api/status')
                if status.get('model_configured') or status.get('api_key_configured'):
                    raise RuntimeError('Unexpected real model configuration in blank smoke')
                if get('/api/assignments').get('assignments'):
                    raise RuntimeError('Unexpected assignments in blank smoke')
                db = root / 'data' / 'history_grading.sqlite3'
                conn = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
                try:
                    conn.execute('PRAGMA query_only=ON')
                    schema = conn.execute('SELECT schema_version FROM schema_meta WHERE id=1').fetchone()[0]
                    objective_schema = conn.execute('PRAGMA user_version').fetchone()[0]
                    integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
                    counts = {name: conn.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                                for name in ('assignments', 'submissions', 'roster_students', 'roster_classes',
                                             'objective_assessments', 'objective_submissions', 'exam_answer_links')}
                finally:
                    conn.close()
                if schema != 13 or objective_schema != 5 or integrity != 'ok' or any(counts.values()):
                    raise RuntimeError(f'Blank DB check failed: schema={schema}/{objective_schema}, integrity={integrity}, counts={counts}')
                request = Request(base + '/api/shutdown', data=b'{}', method='POST',
                                  headers={'Content-Type': 'application/json'})
                with opener.open(request, timeout=5) as response:
                    if response.status not in {200, 202} or not json.loads(response.read()).get('ok'):
                        raise RuntimeError('Graceful shutdown rejected')
                process.wait(timeout=15)
                if process.returncode != 0:
                    raise RuntimeError(f'Portable exit code {process.returncode}')
                return {'passed': True, 'exe': str(exe), 'app_version': status.get('app_version'),
                        'schema': schema, 'objective_user_version': objective_schema, 'integrity': integrity,
                        'blank_counts': counts, 'health': 200, 'help': 200,
                        'vue_assets_verified': vue_assets,
                        'graceful_shutdown': True, 'real_provider_calls': 0}
            except Exception as exc:
                output.seek(0)
                tail = output.read().decode('utf-8', 'replace')[-3000:]
                raise RuntimeError(f'{exc}\nIsolated process log:\n{tail}') from exc
            finally:
                # Only the child started above is terminated on a failed smoke.
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', required=True, type=Path)
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('timeout must be positive')
    result = run_smoke(args.exe, args.timeout)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
