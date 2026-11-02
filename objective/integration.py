from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .app import MAX_TOTAL_BYTES, _init_db, create_app


OBJECTIVE_MOUNT_PREFIX = "/objective"


def initialize_shared_database(database_path: str | Path, *, set_journal_mode: bool = False) -> None:
    """Apply only the namespaced objective migrations to the main SQLite file."""
    _init_db(Path(database_path).resolve(), set_journal_mode=set_journal_mode)


def create_integrated_app(data_dir: str, database_path: str, *, vision_grader=None, vision_status=None, source_exam_provider=None):
    """Create the objective WSGI application embedded in the existing service."""
    return create_app(
        data_dir=data_dir,
        database_path=database_path,
        upload_dir=f"{data_dir}/uploads",
        auth_enabled=False,
        mount_prefix=OBJECTIVE_MOUNT_PREFIX,
        initialize_schema=False,
        set_journal_mode=False,
        vision_grader=vision_grader,
        vision_status=vision_status,
        source_exam_provider=source_exam_provider,
    )


def dispatch_wsgi_request(handler: Any, application: Callable, *, mount_prefix: str = OBJECTIVE_MOUNT_PREFIX) -> bool:
    """Forward one already-authenticated main-server request into Flask WSGI."""
    split = urlsplit(handler.path)
    prefix = "/" + mount_prefix.strip("/")
    if split.path == prefix and handler.command in {"GET", "HEAD"}:
        handler.send_response(308)
        handler.send_header("Location", prefix + "/")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True
    if split.path != prefix and not split.path.startswith(prefix + "/"):
        return False

    path_info = split.path[len(prefix):] or "/"
    if not path_info.startswith("/"):
        path_info = "/" + path_info
    host = handler.headers.get("Host", "localhost")
    host_parts = urlsplit("//" + host)
    try:
        content_length = int(handler.headers.get("Content-Length", "0") or "0")
    except ValueError:
        content_length = -1
    maximum_request_bytes = MAX_TOTAL_BYTES + (1024 * 1024)
    if content_length < 0 or content_length > maximum_request_bytes:
        handler.close_connection = True
        body = b'{"error":"request_body_size_invalid"}'
        handler.send_response(413 if content_length > maximum_request_bytes else 400)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True

    # BaseHTTPRequestHandler.rfile is a buffered socket stream. Passing it
    # straight to Werkzeug can leave the multipart parser waiting on a socket
    # read after a keep-alive boundary. Copy exactly Content-Length bytes into
    # a disk-spooling stream, with a 1 MiB memory ceiling, then close it after
    # the synchronous WSGI response has been consumed.
    request_body = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    remaining = content_length
    try:
        while remaining:
            chunk = handler.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                handler.close_connection = True
                body = b'{"error":"request_body_incomplete"}'
                request_body.close()
                handler.send_response(400)
                handler.send_header("Content-Type", "application/json; charset=utf-8")
                handler.send_header("Cache-Control", "no-store")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
                return True
            request_body.write(chunk)
            remaining -= len(chunk)
        request_body.seek(0)
    except Exception:
        request_body.close()
        raise

    environ: dict[str, Any] = {
        "REQUEST_METHOD": handler.command,
        "SCRIPT_NAME": prefix,
        "PATH_INFO": unquote(path_info, errors="replace"),
        "QUERY_STRING": split.query,
        "SERVER_NAME": host_parts.hostname or "localhost",
        "SERVER_PORT": str(host_parts.port or (443 if handler.headers.get("X-Forwarded-Proto", "").casefold() == "https" else 80)),
        "SERVER_PROTOCOL": handler.request_version,
        "REMOTE_ADDR": str(handler.client_address[0]),
        "REMOTE_PORT": str(handler.client_address[1]),
        "CONTENT_TYPE": handler.headers.get("Content-Type", ""),
        "CONTENT_LENGTH": str(content_length),
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "https" if handler.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().casefold() == "https" else "http",
        "wsgi.input": request_body,
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "wsgi.input_terminated": False,
    }
    for name, value in handler.headers.items():
        key = name.upper().replace("-", "_")
        if key in {"CONTENT_TYPE", "CONTENT_LENGTH"}:
            continue
        header_key = "HTTP_" + key
        if header_key in environ:
            environ[header_key] = f"{environ[header_key]},{value}"
        else:
            environ[header_key] = value

    session = getattr(handler, '_web_auth_session', None)
    if session is not None:
        environ['history_grader.actor'] = str(getattr(session, 'username', '')) or None

    state: dict[str, Any] = {"status": "500 Internal Server Error", "headers": []}

    def start_response(status: str, headers: list[tuple[str, str]], exc_info=None):
        if exc_info is not None and state.get("started"):
            raise exc_info[1].with_traceback(exc_info[2])
        state["status"] = status
        state["headers"] = headers
        state["started"] = True
        return lambda _data: None

    iterable = None
    try:
        iterable = application(environ, start_response)
        body = b"".join(iterable)
        code = int(str(state["status"]).split(" ", 1)[0])
        headers = list(state["headers"])
        if code not in {204, 304} and not any(name.casefold() == "content-length" for name, _value in headers):
            headers.append(("Content-Length", str(len(body))))
        handler.send_response(code)
        for name, value in headers:
            if name.casefold() not in {"connection", "transfer-encoding", "server", "date"}:
                handler.send_header(name, value)
        handler.end_headers()
        if body and handler.command != "HEAD":
            handler.wfile.write(body)
    except Exception as exc:
        print(f"[{__import__('datetime').datetime.now().isoformat(timespec='seconds')}] objective WSGI request failed (type={type(exc).__name__})")
        body = b'{"error":"internal_server_error"}'
        try:
            handler.send_response(500)
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
    finally:
        if iterable is not None and hasattr(iterable, "close"):
            iterable.close()
        request_body.close()
    return True
