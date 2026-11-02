"""Opt-in Vue preview files; authentication is enforced by the existing wrapper."""
from pathlib import Path
from urllib.parse import unquote


def serve_preview(handler, path: str, static_dir: Path) -> bool:
    if path not in {'/', '/index.html', '/next', '/next/', '/next/login', '/next/index.html'} and not path.startswith('/next/assets/'):
        return False
    root = (static_dir / 'next').resolve()
    if path.startswith('/next/assets/'):
        relative = unquote(path.removeprefix('/next/'))
        target = (root / relative).resolve()
        if '\\' in relative or '..' in Path(relative).parts or not target.is_relative_to(root / 'assets') or target.suffix not in {'.js', '.css', '.svg'}:
            handler.send_error(404)
            return True
        kind = {'.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}[target.suffix]
    else:
        target, kind = root / 'index.html', 'text/html; charset=utf-8'
    if not target.is_file():
        handler.send_error(503 if target.name == 'index.html' else 404, 'Preview build unavailable')
        return True
    handler.serve_file(target, kind, no_store=True)
    return True
