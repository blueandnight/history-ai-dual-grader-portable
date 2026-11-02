"""Private server-side model pair configuration; never part of public status."""
import json
import os
from pathlib import Path
import threading
import uuid

LOCK = threading.RLock()
FORMAT = 'history-grader-server-dual-v1'


def read(config_dir: Path) -> dict | None:
    path = config_dir / 'server-secrets' / 'dual-review.json'
    if not path.exists():
        return None
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError()
        if os.name != 'nt' and (path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077):
            raise ValueError()
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('format') != FORMAT or not isinstance(data.get('models'), dict) or not isinstance(data.get('credentials'), dict):
            raise ValueError()
        for side, providers in data['credentials'].items():
            if side not in {'A', 'B'} or not isinstance(providers, dict):
                raise ValueError()
            for entry in providers.values():
                if not isinstance(entry, dict) or not isinstance(entry.get('api_origin'), str) or not isinstance(entry.get('api_key'), str):
                    raise ValueError()
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        raise ValueError('服务器双评配置无法安全读取，请联系管理员检查受限配置。') from None


def key_for(document: dict | None, side: str, spec: dict) -> str:
    entry = (document or {}).get('credentials', {}).get(side, {}).get(spec['provider'], {})
    return str(entry.get('api_key', '')) if entry.get('api_origin') == spec['api_origin'] else ''


def write(config_dir: Path, document: dict) -> None:
    directory = config_dir / 'server-secrets'
    temporary = directory / ('.dual-' + uuid.uuid4().hex + '.tmp')
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if directory.is_symlink() or (os.name != 'nt' and directory.stat().st_mode & 0o077):
            raise ValueError()
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({**document, 'format': FORMAT}, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, directory / 'dual-review.json')
    except (OSError, ValueError):
        raise ValueError('服务器双评配置保存失败，请检查服务账号对受限配置目录的权限。') from None
    finally:
        if temporary.exists():
            temporary.unlink()
