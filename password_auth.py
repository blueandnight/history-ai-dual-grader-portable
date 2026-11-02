from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
from http.cookies import SimpleCookie
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import smtplib
import sqlite3
import ssl
import struct
import sys
import threading
import time
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Iterator
from urllib.parse import urlparse

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


COOKIE_NAME = "history_grader_session"
PASSWORD_SCHEME = "argon2id"
LEGACY_PASSWORD_SCHEME = "pbkdf2_sha256"
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60
EMAIL_OTP_TTL_SECONDS = 5 * 60
EMAIL_OTP_MAX_ATTEMPTS = 5
SESSION_TTL_DEFAULT = 30 * 24 * 60 * 60
SESSION_TTL_MAX = 30 * 24 * 60 * 60
SERVER_OPERATOR_ONLY_PATHS = {"/api/restart", "/api/shutdown"}
_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=65536,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
_DUMMY_HASH: str | None = None
_DUMMY_HASH_LOCK = threading.Lock()
_GLOBAL_LOCK = threading.RLock()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def cloud_mode_enabled(handler: Any | None = None) -> bool:
    return _env_bool("HISTORY_GRADER_CLOUD_MODE", False)


def web_auth_required(handler: Any | None = None) -> bool:
    return _env_bool("HISTORY_GRADER_WEB_AUTH_REQUIRED", False) or cloud_mode_enabled(handler)


def hash_password(password: str, *, iterations: int | None = None) -> str:
    """Return an Argon2id hash. ``iterations`` is accepted for old test helpers only."""
    if len(password) < 12:
        raise ValueError("密码至少需要12个字符")
    if len(password) > 512:
        raise ValueError("密码长度异常")
    return _PASSWORD_HASHER.hash(password)


def _parse_legacy_password_hash(value: str) -> tuple[int, bytes, bytes] | None:
    try:
        scheme, raw_iterations, raw_salt, raw_digest = value.split("$", 3)
        iterations = int(raw_iterations)
        salt = base64.urlsafe_b64decode(raw_salt + "=" * (-len(raw_salt) % 4))
        digest = base64.urlsafe_b64decode(raw_digest + "=" * (-len(raw_digest) % 4))
    except (ValueError, TypeError):
        return None
    if scheme != LEGACY_PASSWORD_SCHEME or not 100_000 <= iterations <= 2_000_000:
        return None
    if not 12 <= len(salt) <= 64 or len(digest) != 32:
        return None
    return iterations, salt, digest


def _is_supported_password_hash(value: str) -> bool:
    if value.startswith("$argon2id$"):
        return True
    # Compatibility for a one-time upgrade from the deployed PBKDF2 hash.
    return _parse_legacy_password_hash(value) is not None


def _verify_password(password: str, encoded_hash: str) -> bool:
    if len(password) > 512:
        return False
    if encoded_hash.startswith("$argon2id$"):
        try:
            return _PASSWORD_HASHER.verify(encoded_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError, ValueError):
            return False
    parsed = _parse_legacy_password_hash(encoded_hash)
    if parsed is None:
        return False
    iterations, salt, expected = parsed
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return secrets.compare_digest(actual, expected)


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        with _DUMMY_HASH_LOCK:
            if _DUMMY_HASH is None:
                _DUMMY_HASH = _PASSWORD_HASHER.hash(secrets.token_urlsafe(32))
    return _DUMMY_HASH


def _valid_totp_secret(secret: str) -> bool:
    if not secret:
        return False
    try:
        decoded = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    except (ValueError, base64.binascii.Error):
        return False
    return len(decoded) >= 10


def _verify_totp(secret: str, supplied: str, now: float | None = None) -> bool:
    if not re.fullmatch(r"\d{6}", supplied or "") or not _valid_totp_secret(secret):
        return False
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    counter = int((time.time() if now is None else now) // 30)
    for offset in (-1, 0, 1):
        digest = hmac.new(key, struct.pack(">Q", counter + offset), hashlib.sha1).digest()
        position = digest[-1] & 0x0F
        value = (struct.unpack(">I", digest[position : position + 4])[0] & 0x7FFFFFFF) % 1_000_000
        if secrets.compare_digest(f"{value:06d}", supplied):
            return True
    return False


def _valid_email_address(value: str) -> bool:
    if not value or len(value) > 254 or "\r" in value or "\n" in value:
        return False
    display_name, address = parseaddr(value)
    return not display_name and address == value and bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address))


@dataclass(frozen=True)
class AuthConfig:
    required: bool
    username: str
    password_hash: str
    cookie_secure: bool
    trust_proxy: bool
    session_ttl: int
    totp_required: bool
    totp_secret: str = field(repr=False)
    email_otp_required: bool
    admin_email: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str = field(repr=False)
    smtp_from: str

    @property
    def configured(self) -> bool:
        return bool(self.username and _is_supported_password_hash(self.password_hash))

    @property
    def password_reset_configured(self) -> bool:
        return bool(
            _valid_email_address(self.admin_email)
            and self.smtp_host
            and 1 <= self.smtp_port <= 65535
            and self.smtp_username
            and self.smtp_password
            and _valid_email_address(self.smtp_from)
        )

    @property
    def fingerprint(self) -> str:
        value = "\0".join((self.username, self.admin_email)).encode("utf-8")
        return hashlib.sha256(value).hexdigest()


def auth_config(handler: Any | None = None) -> AuthConfig:
    try:
        ttl = int(os.getenv("HISTORY_GRADER_SESSION_TTL", str(SESSION_TTL_DEFAULT)))
    except ValueError:
        ttl = SESSION_TTL_DEFAULT
    try:
        smtp_port = int(os.getenv("HISTORY_GRADER_SMTP_PORT", "465").strip())
    except ValueError:
        smtp_port = 0
    smtp_username = os.getenv("HISTORY_GRADER_SMTP_USERNAME", "").strip()
    # Email OTP is retained for recovery and sensitive account changes; it is
    # deliberately never a prerequisite for ordinary password login.
    return AuthConfig(
        required=web_auth_required(handler),
        username=(os.getenv("HISTORY_GRADER_ADMIN_USERNAME", "trial-").strip() or "trial-")[:100],
        password_hash=os.getenv("HISTORY_GRADER_ADMIN_PASSWORD_HASH", "").strip(),
        cookie_secure=(
            _env_bool("HISTORY_GRADER_COOKIE_SECURE", True)
            or _env_bool("HISTORY_GRADER_CLOUD_MODE", False)
            or cloud_mode_enabled(handler)
        ),
        trust_proxy=_env_bool("HISTORY_GRADER_TRUST_PROXY", False),
        session_ttl=max(15 * 60, min(SESSION_TTL_MAX, ttl)),
        totp_required=False,
        totp_secret=os.getenv("HISTORY_GRADER_TOTP_SECRET", "").strip().replace(" ", "").upper(),
        email_otp_required=False,
        admin_email=os.getenv("HISTORY_GRADER_ADMIN_EMAIL", "").strip(),
        smtp_host=os.getenv("HISTORY_GRADER_SMTP_HOST", "").strip(),
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=os.getenv("HISTORY_GRADER_SMTP_PASSWORD", ""),
        smtp_from=os.getenv("HISTORY_GRADER_SMTP_FROM", "").strip() or smtp_username,
    )


def _smtp_auth_login(client: smtplib.SMTP, username: str, password: str) -> None:
    credentials = iter((username, password))

    def auth_response(_challenge) -> str:
        try:
            return next(credentials)
        except StopIteration as exc:
            raise smtplib.SMTPException("SMTP AUTH LOGIN challenge sequence was unexpected") from exc

    client.auth("LOGIN", auth_response, initial_response_ok=False)


def _send_email_otp(
    config: AuthConfig,
    code: str,
    recipient: str | None = None,
    purpose: str = "密码重置",
) -> None:
    message = EmailMessage()
    message["From"] = config.smtp_from
    message["To"] = recipient or config.admin_email
    message["Subject"] = "阅卷工作台安全操作验证码"
    message.set_content(
        f"你的阅卷工作台{purpose}验证码是：{code}\n\n"
        f"验证码 {EMAIL_OTP_TTL_SECONDS // 60} 分钟内有效，只能使用一次。"
        "如果不是你本人操作，请忽略此邮件。"
    )
    context = ssl.create_default_context()
    if config.smtp_port == 465:
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=10, context=context) as client:
            _smtp_auth_login(client, config.smtp_username, config.smtp_password)
            client.send_message(message)
        return
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10) as client:
        client.ehlo()
        client.starttls(context=context)
        client.ehlo()
        _smtp_auth_login(client, config.smtp_username, config.smtp_password)
        client.send_message(message)


@dataclass
class SessionRecord:
    username: str
    csrf_token: str
    expires_at: float
    config_fingerprint: str
    role: str = "admin"


def _database_path(handler: Any | None = None) -> Path:
    configured = os.getenv("HISTORY_GRADER_AUTH_DB", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    module_names = []
    if handler is not None:
        module_names.append(getattr(handler.__class__, "__module__", ""))
    module_names.extend(("app", "__main__"))
    for module_name in module_names:
        module = sys.modules.get(module_name)
        candidate = getattr(module, "DB_PATH", None) if module is not None else None
        if candidate:
            return Path(candidate).expanduser().resolve()
    return Path(__file__).resolve().parent / "data" / "history_grading.sqlite3"


@contextmanager
def _connect(handler: Any | None = None) -> Iterator[sqlite3.Connection]:
    path = _database_path(handler)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        if not existed and os.name != "nt":
            os.chmod(path, 0o600)
        _ensure_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    with _GLOBAL_LOCK:
        try:
            applied = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM auth_schema_migrations"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            applied = 0
        if int(applied or 0) >= 2:
            return
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS auth_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
        )
        applied = conn.execute("SELECT COALESCE(MAX(version), 0) FROM auth_schema_migrations").fetchone()[0]
        if applied < 1:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS auth_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS auth_users ("
                "username TEXT PRIMARY KEY COLLATE NOCASE, email TEXT UNIQUE COLLATE NOCASE, "
                "password_hash TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS auth_sessions ("
                "token_hash TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE, "
                "csrf_token TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS auth_sessions_expiry ON auth_sessions(expires_at)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS auth_otp_challenges ("
                "challenge_hash TEXT PRIMARY KEY, purpose TEXT NOT NULL, username TEXT NOT NULL COLLATE NOCASE, "
                "client_hash TEXT NOT NULL, email_to TEXT NOT NULL, target_email TEXT, "
                "code_digest BLOB NOT NULL, expires_at INTEGER NOT NULL, failed_attempts INTEGER NOT NULL DEFAULT 0, "
                "created_at INTEGER NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS auth_otp_expiry ON auth_otp_challenges(expires_at)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS auth_rate_limits ("
                "bucket_hash TEXT PRIMARY KEY, attempts INTEGER NOT NULL, window_started INTEGER NOT NULL, "
                "blocked_until INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO auth_meta(key, value) VALUES ('rate_limit_salt', ?) ",
                (secrets.token_hex(32),),
            )
            conn.execute(
                "INSERT INTO auth_schema_migrations(version, applied_at) VALUES (1, ?)",
                (int(time.time()),),
            )
        if applied < 2:
            # Existing accounts are administrators; teachers are explicitly created.
            conn.execute("ALTER TABLE auth_users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin' CHECK(role IN ('admin', 'teacher'))")
            conn.execute("ALTER TABLE auth_users ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1))")
            conn.execute("INSERT INTO auth_schema_migrations(version, applied_at) VALUES (2, ?)", (int(time.time()),))
        conn.commit()


class SingleTeacherAuth:
    """Shared workspace accounts using the existing durable session mechanism."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def is_configured(self, config: AuthConfig | None = None, handler: Any | None = None) -> bool:
        config = config or auth_config(handler)
        if config.configured:
            return True
        try:
            with _connect(handler) as conn:
                return conn.execute("SELECT 1 FROM auth_users LIMIT 1").fetchone() is not None
        except sqlite3.Error:
            return False

    def _user_by_identity(self, conn: sqlite3.Connection, identity: str, config: AuthConfig) -> sqlite3.Row | None:
        normalized = identity.strip().casefold()
        if not normalized:
            return None
        return conn.execute(
            "SELECT username, email, password_hash, role, enabled FROM auth_users "
            "WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE LIMIT 1",
            (normalized, normalized),
        ).fetchone()

    @staticmethod
    def _insert_bootstrap_user(conn: sqlite3.Connection, config: AuthConfig, now: int) -> None:
        if not config.username or not _is_supported_password_hash(config.password_hash):
            return
        # The compatibility PBKDF2 hash stays only in the operator's existing
        # environment until the first successful login; database storage is
        # Argon2id only.
        if config.password_hash.startswith("$argon2id$"):
            conn.execute(
                "INSERT OR IGNORE INTO auth_users(username, email, password_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (config.username, config.admin_email or None, config.password_hash, now, now),
            )

    def _rate_keys(
        self, conn: sqlite3.Connection, client_key: str, identity: str, purpose: str = "login"
    ) -> tuple[str, str]:
        salt_row = conn.execute("SELECT value FROM auth_meta WHERE key = 'rate_limit_salt'").fetchone()
        salt = str(salt_row[0] if salt_row else "")
        return tuple(
            hmac.new(salt.encode("ascii"), f"{scope}\0{value.casefold()}".encode("utf-8"), hashlib.sha256).hexdigest()
            for scope, value in ((f"{purpose}:ip", client_key[:200]), (f"{purpose}:account", identity[:254]))
        )  # type: ignore[return-value]

    @staticmethod
    def _retry_after(conn: sqlite3.Connection, keys: tuple[str, str], now: int) -> int:
        retry = 0
        for key in keys:
            row = conn.execute(
                "SELECT attempts, window_started, blocked_until FROM auth_rate_limits WHERE bucket_hash = ?",
                (key,),
            ).fetchone()
            if row is None:
                continue
            if now - int(row["window_started"]) >= LOGIN_WINDOW_SECONDS:
                continue
            retry = max(retry, int(row["blocked_until"]) - now)
            if int(row["attempts"]) >= MAX_LOGIN_ATTEMPTS:
                retry = max(retry, int(row["window_started"]) + LOGIN_WINDOW_SECONDS - now)
        return max(0, retry)

    @staticmethod
    def _record_failure(conn: sqlite3.Connection, keys: tuple[str, str], now: int) -> None:
        for key in keys:
            row = conn.execute(
                "SELECT attempts, window_started FROM auth_rate_limits WHERE bucket_hash = ?",
                (key,),
            ).fetchone()
            if row is None or now - int(row["window_started"]) >= LOGIN_WINDOW_SECONDS:
                attempts, window_started = 1, now
            else:
                attempts, window_started = int(row["attempts"]) + 1, int(row["window_started"])
            # Progressive cooldown after consecutive failures; the fifth failure
            # triggers the full account/IP window lock.
            cooldown = 0 if attempts < 2 else min(8, 2 ** (attempts - 2))
            conn.execute(
                "INSERT INTO auth_rate_limits(bucket_hash, attempts, window_started, blocked_until, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(bucket_hash) DO UPDATE SET "
                "attempts=excluded.attempts, window_started=excluded.window_started, "
                "blocked_until=excluded.blocked_until, updated_at=excluded.updated_at",
                (key, attempts, window_started, now + cooldown, now),
            )

    @staticmethod
    def _record_success(conn: sqlite3.Connection, keys: tuple[str, str]) -> None:
        conn.executemany("DELETE FROM auth_rate_limits WHERE bucket_hash = ?", ((key,) for key in keys))

    def login(
        self,
        username: str,
        password: str,
        client_key: str,
        totp_code: str = "",
        handler: Any | None = None,
        *,
        challenge_id: str = "",
        email_code: str = "",
    ) -> tuple[int, dict[str, Any], str | None]:
        config = auth_config(handler)
        if not config.required:
            return 404, {"error": "网站登录未启用"}, None
        if not self.is_configured(config, handler):
            return 503, {"error": "管理员账号或密码哈希尚未配置"}, None
        identity = (username or "").strip()[:254]
        if not password or len(password) > 512:
            identity_for_limit = identity or "<empty>"
        else:
            identity_for_limit = identity or "<empty>"
        now = int(time.time())
        config_alias = bool(
            identity.casefold() in {config.username.casefold(), config.admin_email.casefold()}
            and _is_supported_password_hash(config.password_hash)
        )
        with self._lock, _connect(handler) as conn:
            self._insert_bootstrap_user(conn, config, now)
            user = self._user_by_identity(conn, identity, config)
            configured_user_exists = conn.execute(
                "SELECT 1 FROM auth_users WHERE username = ? COLLATE NOCASE LIMIT 1", (config.username,)
            ).fetchone() is not None
            # A persistent row takes precedence over bootstrap aliases, so a
            # previously bound email stops working immediately after a change.
            config_hash_allowed = config_alias and not configured_user_exists
            canonical_identity = (
                str(user["username"]) if user is not None
                else config.username if config_hash_allowed
                else identity_for_limit
            )
            keys = self._rate_keys(conn, client_key, canonical_identity)
            retry_after = self._retry_after(conn, keys, now)
            if retry_after:
                return 429, {"error": "登录失败次数过多，请稍后重试", "retry_after": retry_after}, None
            stored_hash = str(user["password_hash"]) if user else (config.password_hash if config_hash_allowed else "")
            valid = _verify_password(password, stored_hash) if stored_hash else _verify_password(password, _dummy_hash())
            if not valid or (user is not None and not user["enabled"]):
                self._record_failure(conn, keys, now)
                retry_after = self._retry_after(conn, keys, now)
                if retry_after:
                    return 429, {"error": "登录失败次数过多，请稍后重试", "retry_after": retry_after}, None
                return 401, {"error": "邮箱/用户名或密码错误"}, None

            if user is None:
                if not config_hash_allowed:
                    self._record_failure(conn, keys, now)
                    return 401, {"error": "邮箱/用户名或密码错误"}, None
                username_for_store = config.username
                email_for_store = config.admin_email or None
                conn.execute(
                    "INSERT INTO auth_users(username, email, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (username_for_store, email_for_store, hash_password(password), now, now),
                )
            elif not str(user["password_hash"]).startswith("$argon2id$") or _PASSWORD_HASHER.check_needs_rehash(str(user["password_hash"])):
                conn.execute(
                    "UPDATE auth_users SET password_hash = ?, updated_at = ? WHERE username = ? COLLATE NOCASE",
                    (hash_password(password), now, user["username"]),
                )

            self._record_success(conn, keys)
            raw_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
            record = SessionRecord(
                username=(str(user["username"]) if user else config.username),
                csrf_token=secrets.token_urlsafe(32),
                expires_at=now + config.session_ttl,
                config_fingerprint=config.fingerprint,
                role=str(user["role"]) if user else "admin",
            )
            conn.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT INTO auth_sessions(token_hash, username, csrf_token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (token_hash, record.username, record.csrf_token, now, int(record.expires_at)),
            )
            return 200, self.public_session(record), self._session_cookie(raw_token, config)

    def manage_users(
        self, session: SessionRecord, action: str, payload: dict[str, Any], handler: Any | None = None
    ) -> tuple[int, dict[str, Any]]:
        """Only administrators manage teachers; administrators cannot be altered here."""
        if session.role != "admin":
            return 403, {"error": "此操作仅限管理员"}
        with self._lock, _connect(handler) as conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = conn.execute("SELECT role, enabled FROM auth_users WHERE username = ? COLLATE NOCASE", (session.username,)).fetchone()
            if actor is None or actor["role"] != "admin" or not actor["enabled"]:
                return 403, {"error": "此操作仅限管理员"}
            if action == "list":
                rows = conn.execute("SELECT username, role, enabled, created_at FROM auth_users ORDER BY created_at, username").fetchall()
                return 200, {"users": [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]}
            username = str(payload.get("username", "")).strip()
            if action == "create":
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,63}", username):
                    return 400, {"error": "用户名需为3–64位，以英文字母开头，可包含数字、下划线、点和短横线"}
                if payload.get("role", "teacher") != "teacher":
                    return 400, {"error": "此入口只创建教师账户"}
            elif action not in {"reset-password", "disable", "enable"}:
                return 404, {"error": "未知用户操作"}
            user = conn.execute("SELECT role FROM auth_users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
            if action == "create" and user is not None:
                return 409, {"error": "用户名已存在"}
            if action != "create" and user is None:
                return 404, {"error": "教师账户不存在"}
            if user is not None and user["role"] != "teacher":
                return 403, {"error": "此入口不能修改管理员账户"}
            now = int(time.time())
            if action in {"create", "reset-password"}:
                try:
                    encoded = hash_password(str(payload.get("password", "")))
                except ValueError as exc:
                    return 400, {"error": str(exc)}
                if action == "create":
                    conn.execute("INSERT INTO auth_users(username, email, password_hash, created_at, updated_at, role, enabled) VALUES (?, NULL, ?, ?, ?, 'teacher', 1)", (username, encoded, now, now))
                else:
                    conn.execute("UPDATE auth_users SET password_hash = ?, updated_at = ? WHERE username = ? COLLATE NOCASE", (encoded, now, username))
            else:
                conn.execute("UPDATE auth_users SET enabled = ?, updated_at = ? WHERE username = ? COLLATE NOCASE", (int(action == "enable"), now, username))
            if action in {"reset-password", "disable"}:
                conn.execute("DELETE FROM auth_sessions WHERE username = ? COLLATE NOCASE", (username,))
                conn.execute("DELETE FROM auth_otp_challenges WHERE username = ? COLLATE NOCASE", (username,))
            return 200, {"ok": True}

    def request_password_reset(
        self, identity: str, client_key: str, handler: Any | None = None
    ) -> tuple[int, dict[str, Any]]:
        config = auth_config(handler)
        if not config.required:
            return 404, {"error": "网站登录未启用"}
        if not config.password_reset_configured:
            return 503, {"error": "密码恢复邮箱服务尚未配置"}
        identity = (identity or "").strip()[:254]
        if not identity:
            return 400, {"error": "请输入账户邮箱或用户名"}
        now = int(time.time())
        challenge_id = secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(1_000_000):06d}"
        sent = False
        with self._lock, _connect(handler) as conn:
            self._insert_bootstrap_user(conn, config, now)
            row = self._user_by_identity(conn, identity, config)
            if row is not None and row["role"] == "teacher":
                return 403, {"error": "教师账户不使用邮箱找回密码，请联系管理员重置。"}
            if row is None and identity.casefold() in {config.username.casefold(), config.admin_email.casefold()}:
                self._insert_bootstrap_user(conn, config, now)
                row = self._user_by_identity(conn, identity, config)
            rate_identity = str(row["username"]) if row is not None else identity
            keys = self._rate_keys(conn, client_key, rate_identity, purpose="reset")
            retry_after = self._retry_after(conn, keys, now)
            if retry_after:
                return 429, {"error": "请求过于频繁，请稍后重试", "retry_after": retry_after}
            if row is not None or identity.casefold() in {config.username.casefold(), config.admin_email.casefold()}:
                target_username = str(row["username"]) if row is not None else config.username
                email = str((row["email"] if row is not None else None) or config.admin_email)
                if _valid_email_address(email):
                    challenge_hash = hashlib.sha256(challenge_id.encode("ascii")).hexdigest()
                    client_hash = hashlib.sha256(client_key[:200].encode("utf-8")).hexdigest()
                    digest = hmac.new(challenge_id.encode("ascii"), code.encode("ascii"), hashlib.sha256).digest()
                    conn.execute(
                        "INSERT INTO auth_otp_challenges(challenge_hash, purpose, username, client_hash, email_to, "
                        "target_email, code_digest, expires_at, created_at) VALUES (?, 'password_reset', ?, ?, ?, NULL, ?, ?, ?)",
                        (challenge_hash, target_username, client_hash, email, digest, now + EMAIL_OTP_TTL_SECONDS, now),
                    )
                    sent = True
            self._record_failure(conn, keys, now)
        if sent:
            try:
                _send_email_otp(config, code, recipient=email, purpose="密码重置")
            except Exception:
                with self._lock, _connect(handler) as conn:
                    conn.execute(
                        "DELETE FROM auth_otp_challenges WHERE challenge_hash = ?",
                        (hashlib.sha256(challenge_id.encode("ascii")).hexdigest(),),
                    )
                return 503, {"error": "验证码邮件发送失败，请稍后重试"}
        # Same response shape for existing and unknown accounts prevents email enumeration.
        return 202, {
            "challenge_id": challenge_id,
            "message": "如果账户存在，已向绑定邮箱发送验证码；请在5分钟内完成重置。",
        }

    def complete_password_reset(
        self,
        challenge_id: str,
        email_code: str,
        new_password: str,
        client_key: str,
        handler: Any | None = None,
    ) -> tuple[int, dict[str, Any]]:
        config = auth_config(handler)
        if not config.required:
            return 404, {"error": "网站登录未启用"}
        if len(new_password) < 12 or len(new_password) > 512:
            return 400, {"error": "新密码至少需要12个字符"}
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", challenge_id or "") or not re.fullmatch(r"\d{6}", email_code or ""):
            return 401, {"error": "验证码无效或已过期"}
        now = int(time.time())
        challenge_hash = hashlib.sha256(challenge_id.encode("ascii")).hexdigest()
        client_hash = hashlib.sha256(client_key[:200].encode("utf-8")).hexdigest()
        with self._lock, _connect(handler) as conn:
            row = conn.execute(
                "SELECT * FROM auth_otp_challenges WHERE challenge_hash = ? AND purpose = 'password_reset'",
                (challenge_hash,),
            ).fetchone()
            if row is None or int(row["expires_at"]) <= now or row["failed_attempts"] >= EMAIL_OTP_MAX_ATTEMPTS:
                conn.execute("DELETE FROM auth_otp_challenges WHERE challenge_hash = ?", (challenge_hash,))
                return 401, {"error": "验证码无效或已过期"}
            if not secrets.compare_digest(str(row["client_hash"]), client_hash):
                return 401, {"error": "验证码无效或已过期"}
            digest = hmac.new(challenge_id.encode("ascii"), email_code.encode("ascii"), hashlib.sha256).digest()
            if not secrets.compare_digest(bytes(row["code_digest"]), digest):
                attempts = int(row["failed_attempts"]) + 1
                if attempts >= EMAIL_OTP_MAX_ATTEMPTS:
                    conn.execute("DELETE FROM auth_otp_challenges WHERE challenge_hash = ?", (challenge_hash,))
                else:
                    conn.execute(
                        "UPDATE auth_otp_challenges SET failed_attempts = ? WHERE challenge_hash = ?",
                        (attempts, challenge_hash),
                    )
                return 401, {"error": "验证码无效或已过期"}
            changed = conn.execute(
                "UPDATE auth_users SET password_hash = ?, updated_at = ? WHERE username = ? COLLATE NOCASE AND role = 'admin' AND enabled = 1",
                (hash_password(new_password), now, row["username"]),
            ).rowcount
            if not changed and conn.execute("SELECT 1 FROM auth_users WHERE username = ? COLLATE NOCASE", (row["username"],)).fetchone():
                return 401, {"error": "验证码无效或已过期"}
            if not changed:
                conn.execute(
                    "INSERT INTO auth_users(username, email, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (row["username"], row["email_to"], hash_password(new_password), now, now),
                )
            conn.execute("DELETE FROM auth_sessions WHERE username = ? COLLATE NOCASE", (row["username"],))
            conn.execute("DELETE FROM auth_otp_challenges WHERE username = ? COLLATE NOCASE", (row["username"],))
        return 200, {"ok": True, "message": "密码已重置，请使用新密码登录。"}

    def request_email_change(
        self,
        session: SessionRecord,
        new_email: str,
        client_key: str,
        handler: Any | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if session.role != "admin":
            return 403, {"error": "此操作仅限管理员"}
        config = auth_config(handler)
        if not config.password_reset_configured:
            return 503, {"error": "安全操作邮箱服务尚未配置"}
        new_email = (new_email or "").strip()
        if not _valid_email_address(new_email):
            return 400, {"error": "新邮箱格式无效"}
        now = int(time.time())
        challenge_id = secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self._lock, _connect(handler) as conn:
            user = conn.execute(
                "SELECT email FROM auth_users WHERE username = ? COLLATE NOCASE", (session.username,)
            ).fetchone()
            if user is None:
                return 404, {"error": "账户不存在"}
            current_email = str(user["email"] or config.admin_email)
            if not _valid_email_address(current_email):
                return 503, {"error": "当前账户未配置有效恢复邮箱"}
            conflict = conn.execute(
                "SELECT 1 FROM auth_users WHERE email = ? COLLATE NOCASE AND username <> ? COLLATE NOCASE",
                (new_email, session.username),
            ).fetchone()
            if conflict:
                return 409, {"error": "新邮箱已绑定其他账户"}
            challenge_hash = hashlib.sha256(challenge_id.encode("ascii")).hexdigest()
            client_hash = hashlib.sha256(client_key[:200].encode("utf-8")).hexdigest()
            digest = hmac.new(challenge_id.encode("ascii"), code.encode("ascii"), hashlib.sha256).digest()
            conn.execute(
                "INSERT INTO auth_otp_challenges(challenge_hash, purpose, username, client_hash, email_to, "
                "target_email, code_digest, expires_at, created_at) VALUES (?, 'email_change', ?, ?, ?, ?, ?, ?, ?)",
                (challenge_hash, session.username, client_hash, current_email, new_email, digest, now + EMAIL_OTP_TTL_SECONDS, now),
            )
        try:
            _send_email_otp(config, code, recipient=current_email, purpose="修改绑定邮箱")
        except Exception:
            with self._lock, _connect(handler) as conn:
                conn.execute("DELETE FROM auth_otp_challenges WHERE challenge_hash = ?", (challenge_hash,))
            return 503, {"error": "验证码邮件发送失败，请稍后重试"}
        return 202, {"challenge_id": challenge_id, "message": "已向当前绑定邮箱发送安全验证码。"}

    def complete_email_change(
        self,
        session: SessionRecord,
        challenge_id: str,
        email_code: str,
        cookie_header: str,
        client_key: str,
        handler: Any | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if session.role != "admin":
            return 403, {"error": "此操作仅限管理员"}
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", challenge_id or "") or not re.fullmatch(r"\d{6}", email_code or ""):
            return 401, {"error": "验证码无效或已过期"}
        now = int(time.time())
        challenge_hash = hashlib.sha256(challenge_id.encode("ascii")).hexdigest()
        client_hash = hashlib.sha256(client_key[:200].encode("utf-8")).hexdigest()
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header or "")
        except Exception:
            cookie = SimpleCookie()
        morsel = cookie.get(COOKIE_NAME)
        current_token_hash = hashlib.sha256(morsel.value.encode("ascii")).hexdigest() if morsel else ""
        with self._lock, _connect(handler) as conn:
            row = conn.execute(
                "SELECT * FROM auth_otp_challenges WHERE challenge_hash = ? AND purpose = 'email_change'",
                (challenge_hash,),
            ).fetchone()
            if (
                row is None
                or int(row["expires_at"]) <= now
                or row["failed_attempts"] >= EMAIL_OTP_MAX_ATTEMPTS
                or str(row["username"]).casefold() != session.username.casefold()
                or not secrets.compare_digest(str(row["client_hash"]), client_hash)
            ):
                return 401, {"error": "验证码无效或已过期"}
            digest = hmac.new(challenge_id.encode("ascii"), email_code.encode("ascii"), hashlib.sha256).digest()
            if not secrets.compare_digest(bytes(row["code_digest"]), digest):
                attempts = int(row["failed_attempts"]) + 1
                if attempts >= EMAIL_OTP_MAX_ATTEMPTS:
                    conn.execute("DELETE FROM auth_otp_challenges WHERE challenge_hash = ?", (challenge_hash,))
                else:
                    conn.execute("UPDATE auth_otp_challenges SET failed_attempts = ? WHERE challenge_hash = ?", (attempts, challenge_hash))
                return 401, {"error": "验证码无效或已过期"}
            try:
                conn.execute(
                    "UPDATE auth_users SET email = ?, updated_at = ? WHERE username = ? COLLATE NOCASE",
                    (row["target_email"], now, session.username),
                )
            except sqlite3.IntegrityError:
                return 409, {"error": "新邮箱已绑定其他账户"}
            conn.execute(
                "DELETE FROM auth_sessions WHERE username = ? COLLATE NOCASE AND token_hash <> ?",
                (session.username, current_token_hash),
            )
            conn.execute("DELETE FROM auth_otp_challenges WHERE username = ? COLLATE NOCASE", (session.username,))
        return 200, {"ok": True, "message": "绑定邮箱已更新。"}

    def authenticate(self, cookie_header: str, handler: Any | None = None) -> SessionRecord | None:
        config = auth_config(handler)
        if not config.required or not self.is_configured(config, handler) or not cookie_header or len(cookie_header) > 8192:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return None
        morsel = cookie.get(COOKIE_NAME)
        raw_token = morsel.value if morsel is not None else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", raw_token):
            return None
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        now = int(time.time())
        with self._lock, _connect(handler) as conn:
            row = conn.execute(
                "SELECT s.username, s.csrf_token, s.expires_at, u.role FROM auth_sessions s "
                "JOIN auth_users u ON u.username = s.username COLLATE NOCASE "
                "WHERE s.token_hash = ? AND u.enabled = 1",
                (token_hash,),
            ).fetchone()
            if row is None or int(row["expires_at"]) <= now:
                return None
            return SessionRecord(
                username=str(row["username"]),
                csrf_token=str(row["csrf_token"]),
                expires_at=float(row["expires_at"]),
                config_fingerprint=config.fingerprint,
                role=str(row["role"]),
            )

    def logout(self, cookie_header: str, handler: Any | None = None) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header or "")
        except Exception:
            cookie = SimpleCookie()
        morsel = cookie.get(COOKIE_NAME)
        if morsel is not None and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", morsel.value):
            token_hash = hashlib.sha256(morsel.value.encode("ascii")).hexdigest()
            with self._lock, _connect(handler) as conn:
                conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))
        return self._expired_cookie(auth_config(handler))

    @staticmethod
    def public_session(record: SessionRecord) -> dict[str, Any]:
        return {
            "authenticated": True,
            "username": record.username,
            "role": record.role,
            "csrf_token": record.csrf_token,
            "expires_at": int(record.expires_at),
        }

    @staticmethod
    def _session_cookie(raw_token: str, config: AuthConfig) -> str:
        parts = [
            f"{COOKIE_NAME}={raw_token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={config.session_ttl}",
        ]
        if config.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    @staticmethod
    def _expired_cookie(config: AuthConfig) -> str:
        parts = [
            f"{COOKIE_NAME}=",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            "Max-Age=0",
        ]
        if config.cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)


AUTH_MANAGER = SingleTeacherAuth()


def _client_key(handler: Any) -> str:
    immediate = str(handler.client_address[0])
    config = auth_config(handler)
    if config.trust_proxy:
        try:
            if ipaddress.ip_address(immediate).is_loopback:
                forwarded = handler.headers.get("X-Real-IP", "").strip()
                if forwarded:
                    return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return immediate


def _same_origin(handler: Any) -> bool:
    origin = handler.headers.get("Origin", "").strip()
    fetch_site = handler.headers.get("Sec-Fetch-Site", "").strip().casefold()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return False
    if not origin:
        return fetch_site == "same-origin"
    parsed = urlparse(origin)
    if parsed.username or parsed.password or not parsed.netloc:
        return False
    config = auth_config(handler)
    scheme = "https" if config.cookie_secure else "http"
    if config.trust_proxy:
        forwarded_proto = handler.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().casefold()
        if forwarded_proto in {"http", "https"}:
            scheme = forwarded_proto
    expected = f"{scheme}://{handler.headers.get('Host', '').strip()}".casefold()
    return origin.rstrip("/").casefold() == expected.rstrip("/")
