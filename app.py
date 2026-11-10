from __future__ import annotations

import argparse
import base64
import csv
import ctypes
import copy
import difflib
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager
from email import policy as email_policy
from email.parser import BytesParser
from email.message import Message
from http.cookies import SimpleCookie
from functools import wraps
import gc
import hashlib
import hmac
import ipaddress
import io
import json
import math
import os
import queue
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import zipfile
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import parse_qs, urlparse, urlencode
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from xml.etree import ElementTree

from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageStat
from docx_question_import import parse_docx as parse_docx_question_import, extract_docx_plain_text
from exam_layout_profile_service import build_exam_layout_profile, layout_profile_status
from exam_import_ai import (
    AI_PARSE_PROMPT_VERSION,
    build_exam_import_rubric_payload,
    build_exam_import_ai_source,
    merge_exam_import_ai_result,
    normalize_exam_import_ai_result,
)
from exam_import_draft import build_exam_import_draft, layout_reference_page_numbers
from exam_reference_assets import persist_exam_pdf_assets, resolve_exam_reference_page
from grading_schema import validate_grading_response
from answer_region_profiles import answer_region_profile
from version import APP_VERSION, GIT_COMMIT, RUN_SCHEMA_VERSION, TEACHER_REVIEW_SCHEMA_VERSION
from web_auth import cloud_mode_enabled, web_auth_required
import roster_identity
import grading_core
import storage
import model_gateway
import server_dual_config
import exam_report
import support_bundle
from frontend_routes import serve_preview


@dataclass(frozen=True)
class AppPaths:
    """Locations that move together with one portable application instance."""

    program_dir: Path
    portable_root: Path
    static_dir: Path
    data_dir: Path
    config_dir: Path
    db_path: Path
    backup_dir: Path
    env_path: Path
    legacy_env_path: Path
    custom_models_path: Path
    evaluation_dir: Path
    evaluation_manifest_path: Path
    instance_id_path: Path
    service_lock_path: Path
    service_info_path: Path


def default_program_dir() -> Path:
    """Return the directory containing source files or the packaged executable."""
    if getattr(sys, "frozen", False):
        # PyInstaller may place bundled resources in ``_internal`` while the
        # executable and portable data stay in the package root.
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)).resolve()
    return Path(__file__).resolve().parent


def resolve_paths(
    portable_root: str | Path | None = None,
    program_dir: str | Path | None = None,
) -> AppPaths:
    """Resolve portable paths without depending on a drive letter or cwd.

    The launcher supplies ``portable_root`` for a packaged layout where program
    resources may live below the portable root. In source mode the project
    directory remains both locations, preserving the existing data paths.
    """
    program = Path(program_dir or default_program_dir()).expanduser().resolve()
    root_value = portable_root or os.environ.get("HISTORY_GRADER_PORTABLE_ROOT")
    if not root_value and getattr(sys, "frozen", False):
        # A user may double-click the EXE instead of using the launcher. Keep
        # portable data beside the EXE, while bundled static files remain under
        # PyInstaller's _MEIPASS resource directory.
        root_value = Path(sys.executable).resolve().parent
    root_value = root_value or program
    portable = Path(root_value).expanduser().resolve()
    data = portable / "data"
    config = portable / "config"
    return AppPaths(
        program_dir=program,
        portable_root=portable,
        static_dir=program / "static",
        data_dir=data,
        config_dir=config,
        db_path=data / "history_grading.sqlite3",
        backup_dir=portable / "backups",
        env_path=config / ".env",
        legacy_env_path=program / ".env",
        custom_models_path=config / "custom-models.json",
        evaluation_dir=portable / "evaluation",
        evaluation_manifest_path=portable / "evaluation" / "manifest.json",
        instance_id_path=config / "portable-instance-id",
        service_lock_path=config / "service.lock",
        service_info_path=config / "service.json",
    )


def _install_paths(paths: AppPaths) -> None:
    global APP_PATHS, PROGRAM_DIR, PORTABLE_ROOT, STATIC_DIR, DATA_DIR, DB_PATH, BACKUP_DIR
    global CONFIG_DIR, ENV_PATH, LEGACY_ENV_PATH, CUSTOM_MODELS_PATH, PORTABLE_INSTANCE_ID_PATH, SERVICE_LOCK_PATH
    global SERVICE_INFO_PATH, EVALUATION_DIR, EVALUATION_MANIFEST_PATH, ROOT
    APP_PATHS = paths
    PROGRAM_DIR = paths.program_dir
    PORTABLE_ROOT = paths.portable_root
    STATIC_DIR = paths.static_dir
    DATA_DIR = paths.data_dir
    DB_PATH = paths.db_path
    BACKUP_DIR = paths.backup_dir
    CONFIG_DIR = paths.config_dir
    ENV_PATH = paths.env_path
    LEGACY_ENV_PATH = paths.legacy_env_path
    CUSTOM_MODELS_PATH = paths.custom_models_path
    EVALUATION_DIR = paths.evaluation_dir
    EVALUATION_MANIFEST_PATH = paths.evaluation_manifest_path
    PORTABLE_INSTANCE_ID_PATH = paths.instance_id_path
    SERVICE_LOCK_PATH = paths.service_lock_path
    SERVICE_INFO_PATH = paths.service_info_path
    # Keep the old name available for source integrations and existing tests.
    ROOT = PROGRAM_DIR


def configure_paths(
    portable_root: str | Path | None = None,
    program_dir: str | Path | None = None,
) -> AppPaths:
    paths = resolve_paths(portable_root=portable_root, program_dir=program_dir)
    _install_paths(paths)
    return paths


_install_paths(resolve_paths())


def resolve_data_relative_path(
    stored_path: str | Path,
    *,
    base_root: Path | None = None,
    allowed_root: Path | None = None,
) -> Path:
    """Resolve a stored relative path safely across Windows and POSIX hosts.

    Historical records may use either slash style regardless of the platform
    that created them. Interpret both separators uniformly, reject absolute
    paths and traversal components before joining, then resolve symlinks and
    enforce the data-directory boundary.
    """
    try:
        raw = os.fspath(stored_path)
    except TypeError as exc:
        raise ValueError("存储路径无效") from exc
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("存储路径无效")

    normalized = raw.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ValueError("存储路径必须是相对路径")
    if any(part == ".." for part in posix_path.parts):
        raise ValueError("存储路径包含目录穿越")

    root = (base_root or PORTABLE_ROOT).resolve()
    data_root = (allowed_root or DATA_DIR).resolve()
    candidate = root.joinpath(*[part for part in posix_path.parts if part not in {"", "."}]).resolve()
    if not candidate.is_relative_to(data_root):
        raise ValueError("存储路径超出允许的数据目录")
    return candidate

MAX_BODY_BYTES = 40 * 1024 * 1024
MAX_MULTIPART_HEADER_BYTES = 64 * 1024
MAX_MULTIPART_METADATA_BYTES = 2 * 1024 * 1024
MAX_IMAGE_EDGE = 3000
MAX_SINGLE_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_DIMENSION = 12_000
# Pillow can downscale JPEGs during decode via draft(). Keep enough pixels
# for the 3000px stored page and perspective/crop margin without ever
# allocating the full 40MP RGB buffer on the 384MiB service.
PROCESSING_IMAGE_LONG_EDGE = 3840
# PNG/WebP have no equivalent decoder-side downscale. Larger inputs must be
# converted client-side instead of risking an unbounded full RGB decode.
MAX_FULL_DECODE_PIXELS = 20_000_000
# A 20MP PNG plus multipart/parser copies reached 381MiB in the 384MiB
# cgroup, so non-JPEG inputs get a lower explicit-rejection ceiling.
MAX_NON_JPEG_DECODE_PIXELS = 15_000_000
# JPEG draft supports powers of two. Never decode a large JPEG at a scale
# that leaves less than the vision request's useful long edge.
MIN_JPEG_DRAFT_LONG_EDGE = 2_400
# The stored processed image remains available for evidence and teacher review.
# This smaller in-memory copy is used only for the vision request so that a
# phone photo does not consume unnecessary vision tokens or request bandwidth.
MODEL_IMAGE_LONG_EDGE = 2_400
MODEL_IMAGE_JPEG_QUALITY = 82
MAX_IMAGES_PER_SUBMISSION = 10
MAX_AI_REDACTIONS_PER_IMAGE = 64
MAX_SUBMISSION_BYTES = 250 * 1024 * 1024
MIN_FREE_SPACE_RESERVE = 64 * 1024 * 1024
MIN_IMAGE_EDGE = 700
QUALITY_SAMPLE_EDGE = 1400
MIN_LAPLACIAN_VARIANCE = 45.0
MIN_IMAGE_CONTRAST = 10.0
MAX_ROSTER_BYTES = 10 * 1024 * 1024
MAX_BACKUP_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
DB_SCHEMA_VERSION = 13
MODEL_CALL_DIAGNOSTIC_SCHEMA_VERSION = "model-call-diagnostic-v4"
INVOCATION_SCOPES = frozenset({"whole_paper", "single_question", "answer_region"})
PRIVACY_STATUS_VALUES = frozenset({"pre_anonymized", "redacted", "no_identity_visible", "unchecked", "legacy_unknown"})
PRIVACY_STATUS_APPROVED = frozenset({"pre_anonymized", "redacted", "no_identity_visible"})
AUDIT_RECORD_VERSION = "audit-v3"
GRADING_RULE_VERSION = "grading-rules-v3"
VISION_PROMPT_VERSION = "vision-grading-v5-source-no-completion"
EVALUATION_MANIFEST_FORMAT = "history-grader-evaluation-v1"
EVALUATION_MANIFEST_MAX_BYTES = 2 * 1024 * 1024
MAX_EVALUATION_CASES = 2000
MOBILE_TOKEN_TTL_SECONDS = 45 * 60
NON_SECRET_CONFIG_KEYS = {
    "HISTORY_GRADER_PROVIDER",
    "HISTORY_GRADER_BASE_URL",
    "HISTORY_GRADER_MODEL",
    "HISTORY_GRADER_TIMEOUT",
}
KEY_STORE_ERROR = ""
SESSION_ONLY_KEY_PROVIDERS: set[str] = set()
MOBILE_TOKEN_HASH = ""
MOBILE_TOKEN_EXPIRES_AT = 0.0
# A 40MP multipart request can approach the 384MiB cloud cgroup by itself.
# Serialize the parse/decode/preview path instead of allowing two large
# requests to overlap and OOM-kill the whole service.
MAX_ACTIVE_UPLOADS = 1
MAX_HTTP_CONNECTIONS = 16


def release_image_memory() -> None:
    """Return transient image buffers before the next upload starts."""
    gc.collect()
    if os.name != "posix":
        return
    try:
        libc = ctypes.CDLL(None)
        trim = getattr(libc, "malloc_trim", None)
        if trim is not None:
            trim(0)
    except (AttributeError, OSError):
        pass

IMAGE_DECODE_SEMAPHORE = threading.BoundedSemaphore(1)
UPLOAD_SEMAPHORE = threading.BoundedSemaphore(MAX_ACTIVE_UPLOADS)
AI_PREVIEW_SIGNING_KEY = secrets.token_bytes(32)
PREVIEW_TOKEN_CONSUMPTIONS: dict[str, int] = {}
PREVIEW_TOKEN_CONSUMPTIONS_LOCK = threading.Lock()
RESTORE_LOCK = threading.RLock()
RESTORE_IN_PROGRESS = False
BACKUP_LOCK = threading.RLock()
BACKUP_IN_PROGRESS = False
ACTIVE_TASKS_CONDITION = threading.Condition()
ACTIVE_TASKS = 0
TASK_DRAINING = False
SERVICE_DRAIN_TIMEOUT_SECONDS = 300
VIEWED_SUBMISSIONS_LOCK = threading.Lock()
VIEWED_SUBMISSIONS: set[str] = set()
MIGRATION_LOCK = threading.Lock()
MIGRATION_BACKUP_DONE: set[tuple[str, int]] = set()
GRADE_QUEUE: queue.Queue[str] = queue.Queue(maxsize=100)
QUEUE_STOP_EVENT = threading.Event()
QUEUE_THREAD: threading.Thread | None = None
QUEUE_THREADS: list[threading.Thread] = []
QUEUE_THREAD_LOCK = threading.Lock()
ENQUEUED_SUBMISSIONS: set[str] = set()
ENQUEUED_SUBMISSIONS_LOCK = threading.Lock()
GRADING_BATCH_LOCKS_LOCK = threading.Lock()
GRADING_BATCH_LOCKS: dict[str, tuple[threading.Lock, int]] = {}


class ServiceDrainingError(ValueError):
    """Raised when a new write arrives after safe shutdown has started."""


@contextmanager
def active_task(label: str):
    """Track work that must finish before the service is stopped."""
    global ACTIVE_TASKS
    with ACTIVE_TASKS_CONDITION:
        if TASK_DRAINING:
            raise ServiceDrainingError("服务正在等待当前任务完成，请稍候")
        if BACKUP_IN_PROGRESS:
            raise ServiceDrainingError("数据备份正在进行，请稍候")
        if RESTORE_IN_PROGRESS:
            raise ServiceDrainingError("数据恢复正在进行，请稍候")
        ACTIVE_TASKS += 1
    try:
        yield
    finally:
        with ACTIVE_TASKS_CONDITION:
            ACTIVE_TASKS -= 1
            ACTIVE_TASKS_CONDITION.notify_all()


def wait_for_active_tasks(timeout_seconds: float = SERVICE_DRAIN_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout_seconds
    with ACTIVE_TASKS_CONDITION:
        while ACTIVE_TASKS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            ACTIVE_TASKS_CONDITION.wait(timeout=remaining)
    return True


def active_task_count() -> int:
    with ACTIVE_TASKS_CONDITION:
        return ACTIVE_TASKS


def mark_submission_photo_viewed(submission_id: str) -> None:
    with VIEWED_SUBMISSIONS_LOCK:
        VIEWED_SUBMISSIONS.add(submission_id)


def submission_photo_was_viewed(submission_id: str) -> bool:
    with VIEWED_SUBMISSIONS_LOCK:
        return submission_id in VIEWED_SUBMISSIONS


def clear_submission_photo_viewed(submission_id: str) -> None:
    with VIEWED_SUBMISSIONS_LOCK:
        VIEWED_SUBMISSIONS.discard(submission_id)


def track_active_task(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with active_task(function.__name__):
            return function(self, *args, **kwargs)

    return wrapped

MODEL_PRESETS = {
    "deepseek-v41-flash": {
        "label": "DeepSeek V4.1 Flash（正式视觉版）",
        "provider": "deepseek",
        "provider_label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "note": "DeepSeek 已正式发布 V4.1 Flash；官方 API 模型名为 deepseek-flash，原生支持多模态图片。",
        "experimental": False,
    },
    "deepseek-v4-vision": {
        "label": "DeepSeek V4 Flash Vision（兼容旧入口）",
        "provider": "deepseek",
        "provider_label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash-vision-exp",
        "note": "旧模型名兼容入口；官方已将其临时路由到 V4.1 Flash，建议改用正式模型 deepseek-flash。",
        "experimental": True,
    },
    "deepseek-v41-flash-beta": {
        "label": "DeepSeek V4.1 Flash（旧临时 ID，仅兼容）",
        "provider": "deepseek",
        "provider_label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4.1-flash-expires-on-0910",
        "note": "仅用于识别旧配置；请改选正式预设，使用模型名 deepseek-flash。",
        "experimental": True,
    },
    "mimo-v25": {
        "label": "小米 MiMo V2.5（视觉）",
        "provider": "mimo",
        "provider_label": "小米 MiMo",
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5",
        "note": "小米官方视觉文档当前支持 mimo-v2.5，支持 Base64 图片和多图输入。",
        "experimental": False,
    },
    "mimo-v26-flash": {
        "label": "小米 MiMo-V2.6-Flash（视觉）",
        "provider": "mimo",
        "provider_label": "小米 MiMo",
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.6-flash",
        "note": "MiMo-V2.6-Flash OpenAI 兼容视觉模型；支持图像输入和 JSON 输出。",
        "experimental": False,
    },
    "qwen35-flash": {
        "label": "通义千问 Qwen3.5 Flash（视觉）",
        "provider": "qwen",
        "provider_label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.5-flash",
        "note": "速度优先的视觉模型，适合批量试阅；图片通过 Base64 Data URL 发送。",
        "experimental": False,
    },
    "qwen-vl-max": {
        "label": "通义千问 Qwen VL Max（视觉）",
        "provider": "qwen",
        "provider_label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-max",
        "note": "视觉理解能力优先；费用和速度以百炼当前页面为准。",
        "experimental": False,
    },
    "glm-46v": {
        "label": "智谱 GLM-4.6V（视觉）",
        "provider": "zhipu",
        "provider_label": "智谱",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.6v",
        "note": "兼容当前图片请求格式；使用智谱标准 API Key，不是 Coding Plan Key。",
        "experimental": False,
    },
    "openai-gpt-41": {
        "label": "OpenAI GPT-4.1（视觉）",
        "provider": "openai",
        "provider_label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1",
        "note": "支持图像输入，使用 OpenAI Chat Completions 接口。需要单独配置 OpenAI API Key。",
        "experimental": False,
    },
    "openai-gpt-41-mini": {
        "label": "OpenAI GPT-4.1 mini（视觉）",
        "provider": "openai",
        "provider_label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
        "note": "较轻量的图像理解模型，使用 OpenAI Chat Completions 接口。需要单独配置 OpenAI API Key。",
        "experimental": False,
    },
    "gemini-38-flash": {
        "label": "Google Gemini 3.8 Flash（视觉）",
        "provider": "google",
        "provider_label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.8-flash",
        "note": "Google Gemini OpenAI 兼容接口；支持图片输入。需要单独配置 Gemini API Key。",
        "experimental": False,
    },
    "qwen38-max": {
        "label": "通义千问 Qwen3.8-Max（视觉）",
        "provider": "qwen",
        "provider_label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.8-max",
        "note": "实验/可选模型。整卷评分曾出现高超时率及题号未匹配；未来可评估单题第二意见。",
        "experimental": True,
    },
    "qwen35-plus": {
        "label": "通义千问 Qwen3.5 Plus（视觉）",
        "provider": "qwen",
        "provider_label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.5-plus",
        "note": "图文理解模型；沿用 JSON 输出，关闭思考模式。请先用少量人工判分答卷对比。",
        "experimental": False,
    },
    "qwen36-plus": {
        "label": "通义千问 Qwen3.6 Plus（视觉）",
        "provider": "qwen",
        "provider_label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.6-plus",
        "note": "图文理解模型；沿用 JSON 输出，关闭思考模式。请确认百炼账号已开通该模型。",
        "experimental": False,
    },
    "qwen37-plus": {
        "label": "通义千问 Qwen3.7 Plus（视觉）",
        "provider": "qwen",
        "provider_label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.7-plus",
        "note": "图文理解模型；沿用 JSON 输出，关闭思考模式。请先比较漏给分、误给分和读字情况。",
        "experimental": False,
    },
    "qwen3-vl-plus": {
        "label": "通义千问 Qwen3-VL Plus（视觉）",
        "provider": "qwen",
        "provider_label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3-vl-plus",
        "note": "视觉语言模型；沿用 JSON 输出，关闭思考模式。实际手写答卷效果需小样本验证。",
        "experimental": False,
    },
    "gemini-31-pro-preview": {
        "label": "Google Gemini 3.1 Pro Preview（视觉·预览）",
        "provider": "google",
        "provider_label": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.1-pro-preview",
        "note": "预览版图文推理模型，保留服务商默认思考设置；需要 Gemini API Key，耗时及费用可能更高。",
        "experimental": True,
    },
    "openai-gpt-54": {
        "label": "OpenAI GPT-5.4（视觉）",
        "provider": "openai",
        "provider_label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5.4",
        "note": "支持图像输入，使用低推理强度和兼容输出参数；需要 OpenAI API Key。请先做小样本评分对比。",
        "experimental": False,
    },
    "openai-gpt-54-mini": {
        "label": "OpenAI GPT-5.4 mini（视觉）",
        "provider": "openai",
        "provider_label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5.4-mini",
        "note": "支持图像输入的较低成本型号，使用低推理强度；需要 OpenAI API Key，评分效果需单独验证。",
        "experimental": False,
    },
    "custom": {
        "label": "自定义 OpenAI 兼容视觉模型",
        "provider": "custom",
        "provider_label": "自定义服务商",
        "base_url": "",
        "model": "",
        "note": "用于本地 vLLM、SGLang 或其他兼容 Chat Completions 的视觉模型。",
        "experimental": False,
    },
}

MAX_CUSTOM_MODEL_PRESETS = 50
CUSTOM_MODEL_ID_RE = re.compile(r"custom-[0-9a-f]{12}")


def normalize_custom_model_preset(raw: Any, preset_id: str | None = None) -> dict[str, Any]:
    """Validate a user-owned model profile while keeping secrets out of it."""
    if not isinstance(raw, dict):
        raise ValueError("自定义模型预设格式无效")
    value_id = str(preset_id or raw.get("id") or f"custom-{uuid.uuid4().hex[:12]}").strip().lower()
    if not CUSTOM_MODEL_ID_RE.fullmatch(value_id) or value_id in MODEL_PRESETS:
        raise ValueError("自定义模型预设编号无效")
    label = str(raw.get("label") or raw.get("name") or "").strip()
    model = str(raw.get("model") or "").strip()
    note = str(raw.get("note") or "").strip()
    if not label:
        raise ValueError("请填写自定义模型预设名称")
    if not model:
        raise ValueError("请填写自定义模型 ID")
    if len(label) > 80 or len(model) > 200 or len(note) > 300:
        raise ValueError("自定义模型预设文字过长")
    base_url, _ = validate_model_base_url(str(raw.get("base_url") or "").strip())
    return {
        "id": value_id,
        "label": label.replace("\r", " ").replace("\n", " "),
        "provider": "custom",
        "provider_label": "自定义服务商",
        "base_url": base_url,
        "model": model.replace("\r", " ").replace("\n", " "),
        "note": (
            note.replace("\r", " ").replace("\n", " ")
            or "用户保存的 OpenAI 兼容视觉模型；请确认服务商支持图片输入。"
        ),
        "experimental": False,
        "user_defined": True,
    }


def read_custom_model_presets() -> list[dict[str, Any]]:
    """Read non-secret user presets; malformed entries are ignored safely."""
    if not CUSTOM_MODELS_PATH.exists():
        return []
    try:
        document = json.loads(CUSTOM_MODELS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    raw_presets = document.get("presets") if isinstance(document, dict) else None
    if not isinstance(raw_presets, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_presets[:MAX_CUSTOM_MODEL_PRESETS]:
        try:
            preset = normalize_custom_model_preset(raw, str(raw.get("id")) if isinstance(raw, dict) else None)
        except (TypeError, ValueError):
            continue
        if preset["id"] in seen:
            continue
        seen.add(preset["id"])
        result.append(preset)
    return result


def write_custom_model_presets(presets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Atomically write only validated, non-secret custom model fields."""
    if len(presets) > MAX_CUSTOM_MODEL_PRESETS:
        raise ValueError(f"自定义模型预设最多保存 {MAX_CUSTOM_MODEL_PRESETS} 个")
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in presets:
        preset = normalize_custom_model_preset(raw, str(raw.get("id")) if isinstance(raw, dict) else None)
        if preset["id"] in seen:
            raise ValueError("自定义模型预设编号重复")
        seen.add(preset["id"])
        clean.append(preset)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "history-grader-custom-models-v1",
        "presets": clean,
        "updated_at": utc_now(),
    }
    temporary = CUSTOM_MODELS_PATH.with_name(f".{CUSTOM_MODELS_PATH.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(CUSTOM_MODELS_PATH)
    return clean


def model_preset_catalog() -> dict[str, dict[str, Any]]:
    """Return built-in presets plus user profiles, with the freeform option last."""
    catalog: dict[str, dict[str, Any]] = {
        preset_id: dict(preset)
        for preset_id, preset in MODEL_PRESETS.items()
        if preset_id != "custom"
    }
    for preset in read_custom_model_presets():
        catalog[preset["id"]] = preset
    catalog["custom"] = dict(MODEL_PRESETS["custom"])
    return catalog


def read_env_file(path: str | Path | None = None) -> dict[str, str]:
    """Read the canonical portable config, falling back to the legacy source file."""
    if path is None:
        env_path = ENV_PATH if ENV_PATH.exists() else LEGACY_ENV_PATH
    else:
        env_path = Path(path)
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key in NON_SECRET_CONFIG_KEYS:
            values[key] = value
    return values


def write_env_values(updates: dict[str, str]) -> None:
    """Update non-secret model settings in .env without writing an API key."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    env_path = ENV_PATH
    lines = env_path.read_text(encoding="utf-8-sig").splitlines() if env_path.exists() else []
    unknown_keys = set(updates) - NON_SECRET_CONFIG_KEYS
    if unknown_keys:
        raise ValueError("普通配置只能保存非敏感模型设置，API Key 不写入 U 盘")
    clean_updates = {
        str(key): str(value).replace("\r", "").replace("\n", "").strip()
        for key, value in updates.items()
    }
    written: set[str] = set()
    output: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in clean_updates:
                output.append(f"{key}={clean_updates[key]}")
                written.add(key)
                continue
        output.append(raw_line)
    for key, value in clean_updates.items():
        if key not in written:
            output.append(f"{key}={value}")
    atomic_write_text(env_path, "\n".join(output).rstrip() + "\n")


def load_env_file() -> None:
    # Explicit environment variables take precedence over the portable config.
    for key, value in read_env_file().items():
        os.environ.setdefault(key, value)
    # DeepSeek V4.1 Flash is now official. Migrate known temporary or retired
    # model IDs in memory so an existing portable config uses the canonical
    # multimodal model name after restart; the ordinary config is rewritten
    # only when the user saves model settings.
    provider = (os.getenv("HISTORY_GRADER_PROVIDER", "deepseek").strip() or "deepseek").casefold()
    legacy_deepseek_models = {
        "deepseek-v4.1-flash-expires-on-0910",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
    }
    if provider == "deepseek" and os.getenv("HISTORY_GRADER_MODEL", "").strip() in legacy_deepseek_models:
        os.environ["HISTORY_GRADER_MODEL"] = "deepseek-flash"
    # Keep compatibility with the common DeepSeek CLI environment variable,
    # but only for the official DeepSeek origin. Never send that variable to a
    # different provider or a user-edited endpoint by accident.
    provider = (os.getenv("HISTORY_GRADER_PROVIDER", "deepseek").strip() or "deepseek").casefold()
    try:
        _, origin = validate_model_base_url(os.getenv("HISTORY_GRADER_BASE_URL", ""))
    except ValueError:
        origin = ""
    if (
        provider == "deepseek"
        and origin == "https://api.deepseek.com"
        and not os.getenv("HISTORY_GRADER_API_KEY")
        and os.getenv("DEEPSEEK_API_KEY")
    ):
        os.environ["HISTORY_GRADER_API_KEY"] = os.environ["DEEPSEEK_API_KEY"]


def load_runtime_configuration() -> None:
    """Load ordinary config and the target-computer Key at service startup.

    This is deliberately not called during module import. Besides making path
    tests safe, it prevents importing ``app`` from touching a user's local
    environment or DPAPI file.
    """
    global KEY_STORE_ERROR
    load_env_file()
    apply_server_model_selection()
    provider = os.getenv("HISTORY_GRADER_PROVIDER", "deepseek")
    KEY_STORE_ERROR = ""
    try:
        secure_api_key = read_dpapi_api_key(provider, expected_origin=current_api_origin())
    except ApiKeyStoreError as exc:
        KEY_STORE_ERROR = str(exc)
        secure_api_key = ""
    if secure_api_key:
        os.environ["HISTORY_GRADER_API_KEY"] = secure_api_key
        del secure_api_key


def powershell_command() -> str:
    """Prefer PowerShell 7; some Windows PowerShell module paths cannot autoload DPAPI cmdlets."""
    if os.name != "nt":
        return ""
    return shutil.which("pwsh.exe") or shutil.which("powershell.exe") or ""


def deepseek_json_payload_options(provider: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Return only provider request options already supported by the selected model."""
    provider = (provider if provider is not None else os.getenv("HISTORY_GRADER_PROVIDER", "")).strip().casefold()
    model = (model if model is not None else os.getenv("HISTORY_GRADER_MODEL", "")).strip().casefold()
    if provider == "deepseek" and model in {
        "deepseek-flash",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
    }:
        return {"thinking": {"type": "disabled"}}
    if provider == "mimo" and model == "mimo-v2.6-flash":
        return {"thinking": {"type": "disabled"}}
    if provider == "qwen" and model == "qwen3.8-max":
        return {"reasoning_effort": "low"}
    if provider == "qwen" and model in {
        "qwen3.5-plus", "qwen3.6-plus", "qwen3.7-plus", "qwen3-vl-plus",
    }:
        return {"enable_thinking": False}
    if provider == "openai" and model in {"gpt-5.4", "gpt-5.4-mini"}:
        return {"reasoning_effort": "low"}
    return {}


DUAL_REVIEW_MODEL_SPECS = {
    "A": {
        "provider": "deepseek",
        "provider_label": "DeepSeek",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "api_origin": "https://api.deepseek.com",
    },
    "B": {
        "provider": "mimo",
        "provider_label": "小米 MiMo",
        "model": "mimo-v2.6-flash",
        "base_url": "https://api.xiaomimimo.com/v1",
        "api_origin": "https://api.xiaomimimo.com",
    },
}


DUAL_REVIEW_DEFAULT_PRESETS = {"A": "deepseek-v41-flash", "B": "mimo-v26-flash"}
DUAL_REVIEW_AVAILABLE_PRESETS = (
    "deepseek-v41-flash", "mimo-v26-flash", "mimo-v25", "qwen35-flash", "qwen-vl-max", "glm-46v",
    "openai-gpt-41", "openai-gpt-41-mini", "gemini-38-flash", "qwen38-max",
    "qwen35-plus", "qwen36-plus", "qwen37-plus", "qwen3-vl-plus",
    "gemini-31-pro-preview", "openai-gpt-54", "openai-gpt-54-mini",
)


def dual_review_selection_path() -> Path:
    return CONFIG_DIR / "dual-review-models.json"


def dual_review_preset_allowed(preset_id: str) -> bool:
    catalog = model_preset_catalog()
    return preset_id in catalog and (preset_id in DUAL_REVIEW_AVAILABLE_PRESETS or bool(catalog[preset_id].get("user_defined")))


def read_dual_review_preset_ids() -> dict[str, str]:
    """Read the selected pair without exposing credentials."""
    if cloud_mode_enabled():
        document = server_dual_config.read(CONFIG_DIR)
        if document is not None:
            selected = document["models"]
            if set(selected) != {"A", "B"} or any(not dual_review_preset_allowed(v) for v in selected.values()):
                raise ValueError("服务器双评模型配置无效，请联系管理员。")
            return dict(selected)
    try:
        document = json.loads(dual_review_selection_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return dict(DUAL_REVIEW_DEFAULT_PRESETS)
    raw = document.get("models") if isinstance(document, dict) else None
    catalog = model_preset_catalog()
    if not isinstance(raw, dict):
        return dict(DUAL_REVIEW_DEFAULT_PRESETS)
    selected = {}
    for side in ("A", "B"):
        preset_id = str(raw.get(side, "")).strip()
        if not dual_review_preset_allowed(preset_id):
            return dict(DUAL_REVIEW_DEFAULT_PRESETS)
        selected[side] = preset_id
    return selected


def dual_review_model_specs(preset_ids: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    catalog = model_preset_catalog()
    selected = preset_ids or read_dual_review_preset_ids()
    specs: dict[str, dict[str, str]] = {}
    for side in ("A", "B"):
        preset_id = selected.get(side, DUAL_REVIEW_DEFAULT_PRESETS[side])
        preset = catalog.get(preset_id) or catalog[DUAL_REVIEW_DEFAULT_PRESETS[side]]
        base_url, api_origin = validate_model_base_url(str(preset["base_url"]))
        specs[side] = {
            "preset_id": preset_id,
            "provider": str(preset["provider"]),
            "provider_label": str(preset["provider_label"]),
            "label": str(preset["label"]),
            "model": str(preset["model"]),
            "base_url": base_url,
            "api_origin": api_origin,
        }
    return specs


def write_dual_review_preset_ids(side: str, preset_id: str) -> dict[str, str]:
    side = str(side or "").strip().upper()
    preset_id = str(preset_id or "").strip()
    if side not in {"A", "B"}:
        raise ValueError("双评模型侧别无效，请选择 AI-A 或 AI-B")
    if not dual_review_preset_allowed(preset_id):
        raise ValueError("双评模型预设无效，请刷新页面后重试")
    selected = read_dual_review_preset_ids()
    selected[side] = preset_id
    dual_review_model_specs(selected)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = dual_review_selection_path()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = {
        "format": "history-grader-dual-models-v1",
        "models": selected,
        "updated_at": utc_now(),
    }
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    return selected


def dual_review_provider_env_names(provider: str) -> tuple[str, ...]:
    return {
        "qwen": ("QWEN_API_KEY", "HISTORY_GRADER_QWEN_API_KEY"),
        "deepseek": ("DEEPSEEK_API_KEY",),
        "openai": ("OPENAI_API_KEY",),
        "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "mimo": ("MIMO_API_KEY", "XIAOMI_MIMO_API_KEY"),
        "zhipu": ("ZHIPU_API_KEY", "BIGMODEL_API_KEY"),
    }.get(provider, ())


def dual_review_key_store_provider(side: str, provider: str) -> str:
    return f"dual_{str(side).strip().lower()}_{provider}"


def dual_review_key_is_configured(side: str) -> bool:
    """Report whether the selected local model has a key, never returning it."""
    spec = dual_review_model_specs()[side]
    provider = spec["provider"]
    if cloud_mode_enabled() and server_dual_config.key_for(server_dual_config.read(CONFIG_DIR), side, spec):
        return True
    if any(os.getenv(name, "").strip() for name in dual_review_provider_env_names(provider)):
        return True
    active_provider = os.getenv("HISTORY_GRADER_PROVIDER", "deepseek").strip().casefold()
    active_key = os.getenv("HISTORY_GRADER_API_KEY", "").strip()
    if active_provider == provider and active_key:
        try:
            _, active_origin = validate_model_base_url(os.getenv("HISTORY_GRADER_BASE_URL", ""))
            if active_origin == spec["api_origin"]:
                return True
        except ValueError:
            pass
    if cloud_mode_enabled():
        return False
    try:
        scoped = dual_review_key_store_provider(side, provider)
        return (
            local_api_key_exists(scoped, expected_origin=spec["api_origin"])
            or local_api_key_exists(provider, expected_origin=spec["api_origin"])
        )
    except (ApiKeyStoreError, NameError):
        return False


def load_dual_review_model_configs(required_sides: tuple[str, ...] = ("A", "B")) -> dict[str, dict[str, str]]:
    # Selection and bound Key are one snapshot for both sides of a request.
    with server_dual_config.LOCK:
        return _load_dual_review_model_configs(required_sides)


def _load_dual_review_model_configs(required_sides: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """Load selected models from environment or per-side DPAPI storage.

    Callers that only need one side (such as the rubric drafting helper) can
    request that side without requiring the other model's credential.
    """
    normalized_sides = tuple(dict.fromkeys(str(side).strip().upper() for side in required_sides))
    if not normalized_sides or any(side not in {"A", "B"} for side in normalized_sides):
        raise ValueError("模型侧别配置无效")
    configs: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    specs = dual_review_model_specs()
    for side in normalized_sides:
        spec = specs[side]
        provider = str(spec["provider"])
        origin = str(spec["api_origin"])
        api_key = server_dual_config.key_for(server_dual_config.read(CONFIG_DIR), side, spec) if cloud_mode_enabled() else ""
        if not api_key:
            for name in dual_review_provider_env_names(provider):
                api_key = os.getenv(name, "").strip()
                if api_key:
                    break
        active_provider = os.getenv("HISTORY_GRADER_PROVIDER", "deepseek").strip().casefold()
        if not api_key and active_provider == provider and os.getenv("HISTORY_GRADER_API_KEY", "").strip():
            try:
                _, active_origin = validate_model_base_url(os.getenv("HISTORY_GRADER_BASE_URL", ""))
            except ValueError:
                active_origin = ""
            if active_origin == origin:
                api_key = os.getenv("HISTORY_GRADER_API_KEY", "").strip()
        if not api_key and not cloud_mode_enabled():
            try:
                api_key = read_dpapi_api_key(dual_review_key_store_provider(side, provider), expected_origin=origin)
            except ApiKeyStoreError:
                api_key = ""
        if not api_key and not cloud_mode_enabled():
            try:
                # Keep credentials saved before the selectable model pair existed.
                api_key = read_dpapi_api_key(provider, expected_origin=origin)
            except ApiKeyStoreError:
                api_key = ""
        if not api_key:
            missing.append(str(spec["provider_label"]))
            continue
        configs[side] = {
            "provider": provider,
            "provider_label": str(spec["provider_label"]),
            "preset_id": str(spec["preset_id"]),
            "model": str(spec["model"]),
            "base_url": str(spec["base_url"]),
            "api_key": api_key,
        }
    if missing:
        if normalized_sides == ("A", "B"):
            raise ValueError("异模型双评未运行：本机尚未配置 " + "、".join(missing) + " Key。请在本机模型设置中配置后重试。")
        side_labels = "、".join(f"AI-{side}（{specs[side]['provider_label']}）" for side in normalized_sides if side not in configs)
        raise ValueError(f"尚未配置 {side_labels} 的本机 Key。请在双评模型设置中完成配置后重试。")
    return configs


class ApiKeyStoreError(ValueError):
    """A safe, user-facing error from the machine-local Key store."""


def local_app_data_dir() -> Path:
    value = os.environ.get("LOCALAPPDATA", "").strip()
    if not value:
        raise ApiKeyStoreError("无法定位目标电脑的 %LOCALAPPDATA%，不能保存本机 Key")
    return Path(value).expanduser().resolve()


def local_api_key_dir() -> Path:
    return local_app_data_dir() / "HistoryGrader" / "keys" / ensure_portable_instance_id()


def api_key_path(provider: str = "deepseek") -> Path:
    """Return a machine-local, per-portable-instance DPAPI path."""
    normalized = re.sub(r"[^a-z0-9_-]", "", str(provider or "deepseek").strip().lower()) or "deepseek"
    return local_api_key_dir() / f"api-key-{normalized}.dpapi"


def api_key_binding_path(provider: str = "deepseek") -> Path:
    """Return metadata for the local Key's permitted API origin.

    The binding contains no secret. Keeping it beside the DPAPI file, but still
    under %LOCALAPPDATA%, prevents a copied portable config from silently
    reusing a Key against a different endpoint.
    """
    normalized = re.sub(r"[^a-z0-9_-]", "", str(provider or "deepseek").strip().lower()) or "deepseek"
    return local_api_key_dir() / f"api-key-{normalized}.binding.json"


def is_loopback_host(host: str) -> bool:
    return model_gateway.is_loopback_host(host)


def validate_model_base_url(value: str) -> tuple[str, str]:
    return model_gateway.validate_model_base_url(value)


def current_api_origin() -> str:
    try:
        _, origin = validate_model_base_url(os.getenv("HISTORY_GRADER_BASE_URL", ""))
        return origin
    except ValueError:
        return ""


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write a small configuration file without exposing a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding=encoding, newline="") as handle:
            handle.write(content)
        temporary.replace(path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def write_api_key_binding(provider: str, origin: str) -> None:
    path = api_key_binding_path(provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "history-grader-key-binding-v1",
        "provider": str(provider),
        "portable_instance_id": ensure_portable_instance_id(),
        "api_origin": str(origin),
        "created_at": utc_now(),
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def read_api_key_binding(provider: str) -> dict[str, Any] | None:
    path = api_key_binding_path(provider)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiKeyStoreError("本机 Key 绑定信息损坏，请在电脑本机重新输入当前服务商 Key") from exc
    if not isinstance(value, dict) or value.get("format") != "history-grader-key-binding-v1":
        raise ApiKeyStoreError("本机 Key 绑定信息无效，请在电脑本机重新输入当前服务商 Key")
    return value


def local_api_key_exists(provider: str = "deepseek", expected_origin: str | None = None) -> bool:
    try:
        if not api_key_path(provider).exists():
            return False
        binding = read_api_key_binding(provider)
        if binding is None:
            return False
        origin = expected_origin if expected_origin is not None else current_api_origin()
        return binding.get("portable_instance_id") == ensure_portable_instance_id() and binding.get("api_origin") == origin
    except ApiKeyStoreError:
        return False


def read_dpapi_api_key(provider: str = "deepseek", expected_origin: str | None = None) -> str:
    """Decrypt the optional per-Windows-user API key without writing plaintext to disk."""
    secret_path = api_key_path(provider)
    shell = powershell_command()
    if not secret_path.exists():
        return ""
    if not shell:
        raise ApiKeyStoreError("本机 Key 文件存在，但找不到 PowerShell 加密组件；请安装/启用 Windows PowerShell 后重试")
    binding = read_api_key_binding(provider)
    if binding is None:
        raise ApiKeyStoreError("本机 Key 尚未绑定接口地址，请在电脑本机重新输入当前服务商 Key")
    origin = expected_origin if expected_origin is not None else current_api_origin()
    if binding.get("portable_instance_id") != ensure_portable_instance_id() or binding.get("api_origin") != origin:
        raise ApiKeyStoreError("本机 Key 已绑定到其他便携实例或接口地址，请在电脑本机重新输入当前服务商 Key")
    escaped_path = str(secret_path).replace("'", "''")
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$secure = Get-Content -Raw -LiteralPath '"
        + escaped_path
        + "' | ConvertTo-SecureString; "
        "$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure); "
        "try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) } "
        "finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }"
    )
    try:
        completed = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8-sig",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiKeyStoreError("本机 Key 无法解密，请在当前电脑重新输入 Key") from exc
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ApiKeyStoreError(
            "本机 Key 无法解密，可能来自其他 Windows 用户或已损坏；请重新输入当前服务商 Key"
        )
    return completed.stdout.strip().lstrip("\ufeff")


def save_dpapi_api_key(api_key: str, provider: str = "deepseek", api_origin: str | None = None) -> None:
    """Encrypt an API key for this Windows user in %LOCALAPPDATA%."""
    shell = powershell_command()
    if not shell:
        raise ValueError("当前安全保存方式仅支持 Windows")
    secret_path = api_key_path(provider)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    origin = api_origin if api_origin is not None else current_api_origin()
    temporary_path = secret_path.with_name(f".{secret_path.name}.{uuid.uuid4().hex}.tmp")
    escaped_path = str(temporary_path).replace("'", "''")
    script = (
        "$plain = [Console]::In.ReadToEnd(); "
        "$secure = ConvertTo-SecureString $plain -AsPlainText -Force; "
        "$secure | ConvertFrom-SecureString | Set-Content -LiteralPath '"
        + escaped_path
        + "' -Encoding ASCII -NoNewline"
    )
    try:
        completed = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            input=api_key,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if temporary_path.exists():
            temporary_path.unlink()
        raise ValueError("无法调用 Windows 加密服务") from exc
    if completed.returncode != 0 or not temporary_path.exists():
        detail = completed.stderr.strip().splitlines()
        if temporary_path.exists():
            temporary_path.unlink()
        if detail:
            raise ValueError(f"Key 加密保存失败：{' | '.join(line.strip() for line in detail[-5:])[:600]}")
        raise ValueError("Key 加密保存失败：Windows 加密命令没有生成保存文件")
    previous_secret = secret_path.read_bytes() if secret_path.exists() else None
    binding_path = api_key_binding_path(provider)
    previous_binding = binding_path.read_bytes() if binding_path.exists() else None
    try:
        temporary_path.replace(secret_path)
        sid_result = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        sid = sid_result.stdout.strip()
        if sid_result.returncode != 0 or not re.fullmatch(r"S-\d-\d+(?:-\d+)+", sid):
            raise ValueError("无法确定当前 Windows 用户身份，未保存本机 Key")
        acl_result = subprocess.run(
            ["icacls.exe", str(secret_path), "/inheritance:r", "/grant:r", f"*{sid}:(R,W)"],
            capture_output=True,
            text=True,
            check=False,
        )
        if acl_result.returncode != 0:
            raise ValueError("无法限制本机 Key 文件权限，未完成安全保存")
        write_api_key_binding(provider, origin)
        if read_dpapi_api_key(provider, expected_origin=origin) != api_key:
            raise ValueError("本机 Key 保存后校验失败，请重新输入")
    except Exception:
        if temporary_path.exists():
            temporary_path.unlink()
        if previous_secret is None:
            if secret_path.exists():
                secret_path.unlink()
        else:
            secret_path.write_bytes(previous_secret)
        if previous_binding is None:
            if binding_path.exists():
                binding_path.unlink()
        else:
            binding_path.write_bytes(previous_binding)
        raise


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_portable_instance_id() -> str:
    """Read or atomically create the stable ID stored on the portable drive."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if PORTABLE_INSTANCE_ID_PATH.exists():
        value = PORTABLE_INSTANCE_ID_PATH.read_text(encoding="ascii").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("config/portable-instance-id 无效，请恢复有效的便携实例文件")
        return value

    value = uuid.uuid4().hex
    try:
        with PORTABLE_INSTANCE_ID_PATH.open("x", encoding="ascii", newline="") as handle:
            handle.write(value + "\n")
    except FileExistsError:
        value = PORTABLE_INSTANCE_ID_PATH.read_text(encoding="ascii").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise ValueError("config/portable-instance-id 无效，请恢复有效的便携实例文件")
    return value


class SingleInstanceError(RuntimeError):
    """The same portable instance is already running."""


class ServiceLock:
    """Hold an OS-level lock for exactly one service per portable instance."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.handle: Any | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, PermissionError) as exc:
            handle.close()
            raise SingleInstanceError(
                "本便携实例已经在运行；请先关闭已有服务，或检查 config/service.lock 的访问权限"
            ) from exc
        self.handle = handle

    def release(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


def write_service_info(host: str, port: int, portable_instance_id: str) -> None:
    """Write non-secret readiness information for launchers and diagnostics."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        SERVICE_INFO_PATH,
        json.dumps(
            {
                "pid": os.getpid(),
                "host": host,
                "port": port,
                "portable_instance_id": portable_instance_id,
                "started_at": utc_now(),
            },
            ensure_ascii=False,
        ),
    )


def _usable_private_ipv4(value: Any) -> str | None:
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    if address.version != 4 or not address.is_private or address.is_loopback or address.is_link_local:
        return None
    return str(address)


def _network_kind(interface_name: str = "", description: str = "") -> str:
    """Classify an interface for display without exposing hardware identifiers."""
    text = f"{interface_name} {description}".casefold()
    usb_markers = (
        "usb",
        "rndis",
        "remote ndis",
        "android",
        "iphone",
        "apple mobile",
        "personal hotspot",
        "手机",
        "移动热点",
        "个人热点",
    )
    wifi_markers = ("wi-fi", "wifi", "wlan", "wireless", "无线", "802.11")
    if any(marker in text for marker in usb_markers):
        return "usb"
    if any(marker in text for marker in wifi_markers):
        return "wifi"
    if any(marker in text for marker in ("ethernet", "以太网", "lan", "有线")):
        return "ethernet"
    return "network"


def _network_kind_label(kind: str) -> str:
    return {
        "usb": "USB 数据线",
        "wifi": "Wi‑Fi",
        "ethernet": "网线",
        "network": "局域网",
    }.get(kind, "局域网")


def _socket_network_interfaces() -> list[dict[str, Any]]:
    """Fallback address discovery for systems without Windows adapter APIs."""
    candidates: set[str] = set()

    def add_candidate(value: Any) -> None:
        address = _usable_private_ipv4(value)
        if address:
            candidates.add(address)

    # A UDP connect asks the OS which local interface it would use; it does
    # not send application data to these documentation/public probe targets.
    for target in (("192.0.2.1", 9), ("8.8.8.8", 80)):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.settimeout(0.25)
            probe.connect(target)
            add_candidate(probe.getsockname()[0])
        except OSError:
            pass
        finally:
            probe.close()

    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_DGRAM):
            add_candidate(item[4][0])
    except OSError:
        pass

    return [
        {
            "address": address,
            "interface_name": "",
            "connection_type": "network",
            "connection_label": _network_kind_label("network"),
        }
        for address in sorted(candidates)
    ]


def _powershell_network_interfaces() -> list[dict[str, Any]]:
    """Read active IPv4 adapters on Windows, including USB/RNDIS tethering."""
    shell = powershell_command()
    if os.name != "nt" or not shell:
        return []
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$items = @(
  Get-NetAdapter -ErrorAction Stop | ForEach-Object {
    $adapter = $_
    Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -AddressState Preferred -ErrorAction SilentlyContinue |
      ForEach-Object {
        [PSCustomObject]@{
          address = $_.IPAddress
          interface_name = [string]$adapter.Name
          description = [string]$adapter.InterfaceDescription
        }
      }
  }
)
$items | ConvertTo-Json -Compress -Depth 3
"""
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=False,
            timeout=5.0,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    try:
        raw = completed.stdout.decode("utf-8-sig", errors="replace").strip()
        values = json.loads(raw) if raw else []
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if isinstance(values, dict):
        values = [values]
    if not isinstance(values, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        address = _usable_private_ipv4(item.get("address"))
        if not address or address in seen:
            continue
        seen.add(address)
        interface_name = str(item.get("interface_name") or "").strip()
        description = str(item.get("description") or "").strip()
        kind = _network_kind(interface_name, description)
        result.append(
            {
                "address": address,
                "interface_name": interface_name,
                "connection_type": kind,
                "connection_label": _network_kind_label(kind),
            }
        )
    return result


NETWORK_INTERFACE_CACHE_LOCK = threading.Lock()
NETWORK_INTERFACE_CACHE_AT = 0.0
NETWORK_INTERFACE_CACHE: list[dict[str, Any]] = []
NETWORK_INTERFACE_CACHE_TTL = 30.0
NETWORK_INTERFACE_REFRESH_IN_FLIGHT = False


def _start_network_interface_refresh() -> None:
    """Enumerate slow adapters once in the background, never under the cache lock."""
    global NETWORK_INTERFACE_REFRESH_IN_FLIGHT
    with NETWORK_INTERFACE_CACHE_LOCK:
        if NETWORK_INTERFACE_REFRESH_IN_FLIGHT:
            return
        NETWORK_INTERFACE_REFRESH_IN_FLIGHT = True
        started_at = time.monotonic()

    def refresh() -> None:
        global NETWORK_INTERFACE_REFRESH_IN_FLIGHT, NETWORK_INTERFACE_CACHE_AT, NETWORK_INTERFACE_CACHE
        try:
            interfaces = local_network_interfaces()
            with NETWORK_INTERFACE_CACHE_LOCK:
                # A later explicit refresh takes precedence over this older job.
                if NETWORK_INTERFACE_CACHE_AT <= started_at:
                    NETWORK_INTERFACE_CACHE = [dict(item) for item in interfaces]
                    NETWORK_INTERFACE_CACHE_AT = time.monotonic()
        except Exception:
            # Keep the last known interfaces; the next expired poll may retry.
            pass
        finally:
            with NETWORK_INTERFACE_CACHE_LOCK:
                NETWORK_INTERFACE_REFRESH_IN_FLIGHT = False

    try:
        threading.Thread(target=refresh, name="network-interface-refresh", daemon=True).start()
    except Exception:
        with NETWORK_INTERFACE_CACHE_LOCK:
            NETWORK_INTERFACE_REFRESH_IN_FLIGHT = False
        raise


def cached_local_network_interfaces(force_refresh: bool = False) -> list[dict[str, Any]]:
    """Return cached/fast interfaces while slow USB/Wi-Fi labels refresh in background."""
    global NETWORK_INTERFACE_CACHE_AT, NETWORK_INTERFACE_CACHE
    now = time.monotonic()
    with NETWORK_INTERFACE_CACHE_LOCK:
        if not force_refresh and NETWORK_INTERFACE_CACHE_AT > 0 and now - NETWORK_INTERFACE_CACHE_AT < NETWORK_INTERFACE_CACHE_TTL:
            return [dict(item) for item in NETWORK_INTERFACE_CACHE]
        stale = [dict(item) for item in NETWORK_INTERFACE_CACHE]
    if force_refresh:
        interfaces = local_network_interfaces()
        with NETWORK_INTERFACE_CACHE_LOCK:
            NETWORK_INTERFACE_CACHE = [dict(item) for item in interfaces]
            NETWORK_INTERFACE_CACHE_AT = time.monotonic()
            return [dict(item) for item in NETWORK_INTERFACE_CACHE]
    _start_network_interface_refresh()
    return stale or [dict(item) for item in _socket_network_interfaces()]


def local_network_interfaces() -> list[dict[str, Any]]:
    """Return private IPv4 addresses with safe connection labels for phone access."""
    interfaces = _powershell_network_interfaces() or _socket_network_interfaces()
    if not interfaces:
        return []
    has_usb = any(item.get("connection_type") == "usb" for item in interfaces)
    has_wifi = any(item.get("connection_type") == "wifi" for item in interfaces)

    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        kind_order = {"usb": 0 if has_usb else 2, "wifi": 1 if has_usb else 0, "ethernet": 2, "network": 3}
        return (kind_order.get(str(item.get("connection_type")), 4), str(item.get("address")))

    sorted_items = sorted(interfaces, key=sort_key)
    for index, item in enumerate(sorted_items):
        item["recommended"] = bool(
            item.get("connection_type") == "usb" if has_usb else index == 0
        )
    return sorted_items


def local_network_ipv4_addresses() -> list[str]:
    """Backward-compatible list of usable private IPv4 addresses."""
    return [str(item["address"]) for item in local_network_interfaces()]


def remove_service_info(process_id: int) -> None:
    """Remove only this process's readiness file; never touch data files."""
    try:
        if not SERVICE_INFO_PATH.exists():
            return
        payload = json.loads(SERVICE_INFO_PATH.read_text(encoding="utf-8"))
        if int(payload.get("pid", -1)) != process_id:
            return
        SERVICE_INFO_PATH.unlink()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        # The OS lock is the authoritative instance guard. A stale info file
        # is harmless and should not prevent safe shutdown.
        return


def portable_root_from_args(args: list[str]) -> str | None:
    for index, arg in enumerate(args):
        if arg == "--portable-root" and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith("--portable-root="):
            return arg.split("=", 1)[1]
    return None


def build_restart_environment() -> dict[str, str]:
    """Build a child environment without carrying any plaintext API Key."""
    environment = os.environ.copy()
    for key, value in read_env_file().items():
        # An empty placeholder in .env must not erase a setting supplied by
        # the current process.
        if value or key not in environment:
            environment[key] = value
    environment.pop("HISTORY_GRADER_API_KEY", None)
    environment.pop("DEEPSEEK_API_KEY", None)
    return environment


def restart_helper_command(normal_args: list[str], process_id: int) -> list[str]:
    marker = f"--restart-helper-pid={process_id}"
    if getattr(sys, "frozen", False):
        return [sys.executable, *normal_args, marker]
    return [sys.executable, str(Path(__file__).resolve()), *normal_args, marker]


def wait_for_process_exit(
    process_id: int, timeout_seconds: float = SERVICE_DRAIN_TIMEOUT_SECONDS
) -> bool:
    """Wait for the old Windows process before binding the replacement port."""
    if os.name == "nt":
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, process_id)
        if handle:
            result = ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout_seconds * 1000))
            ctypes.windll.kernel32.CloseHandle(handle)
            return result == 0
        return True
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            os.kill(process_id, 0)
        except OSError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def run_restart_helper() -> None:
    marker = next(arg for arg in sys.argv[1:] if arg.startswith("--restart-helper-pid="))
    parent_pid = int(marker.split("=", 1)[1])
    normal_args = [arg for arg in sys.argv[1:] if not arg.startswith("--restart-helper-pid=")]
    configure_paths(portable_root_from_args(normal_args))
    if not wait_for_process_exit(parent_pid):
        print("原服务未能在5分钟内退出，已取消自动重启", file=sys.stderr)
        return 2
    entrypoint = [sys.executable] if getattr(sys, "frozen", False) else [
        sys.executable,
        str(Path(__file__).resolve()),
    ]
    subprocess.Popen(
        [*entrypoint, *normal_args],
        cwd=str(PORTABLE_ROOT),
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return 0


def mobile_token_active() -> bool:
    return bool(MOBILE_TOKEN_HASH and MOBILE_TOKEN_EXPIRES_AT > time.time())


def issue_mobile_token() -> tuple[str, int]:
    global MOBILE_TOKEN_HASH, MOBILE_TOKEN_EXPIRES_AT
    token = secrets.token_urlsafe(24)
    MOBILE_TOKEN_HASH = hashlib.sha256(token.encode("utf-8")).hexdigest()
    MOBILE_TOKEN_EXPIRES_AT = time.time() + MOBILE_TOKEN_TTL_SECONDS
    return token, int(MOBILE_TOKEN_EXPIRES_AT)


def revoke_mobile_token() -> None:
    global MOBILE_TOKEN_HASH, MOBILE_TOKEN_EXPIRES_AT
    MOBILE_TOKEN_HASH = ""
    MOBILE_TOKEN_EXPIRES_AT = 0.0


def valid_mobile_token(token: str) -> bool:
    if not mobile_token_active() or not token:
        return False
    candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return secrets.compare_digest(candidate, MOBILE_TOKEN_HASH)


def folder_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def storage_snapshot() -> dict[str, Any]:
    usage = shutil.disk_usage(PORTABLE_ROOT)
    return {
        "data_dir": str(DATA_DIR),
        "backup_dir": str(BACKUP_DIR),
        "data_bytes": folder_size(DATA_DIR),
        "backup_bytes": folder_size(BACKUP_DIR),
        "disk_total_bytes": usage.total,
        "disk_free_bytes": usage.free,
    }


def configured_retention_days() -> int:
    try:
        value = int(os.getenv("HISTORY_GRADER_RETENTION_DAYS", "0"))
    except (TypeError, ValueError):
        value = 0
    return max(0, min(3650, value))


def retention_report(retention_days: int | None = None) -> dict[str, Any]:
    """Report hypothetical retention candidates; this function never deletes."""
    days = configured_retention_days() if retention_days is None else max(0, min(3650, int(retention_days)))
    cutoff = time.time() - days * 86400 if days else None
    conn = get_db()
    eligible_count = eligible_bytes = held_count = held_bytes = total_images = total_bytes = 0
    try:
        rows = conn.execute(
            "SELECT id, state, confirmed_at, manual_reviewed, created_at, photos_json FROM submissions"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        try:
            created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            created = 0
        reviewed = bool(row["manual_reviewed"]) and bool(row["confirmed_at"]) and row["state"] == "completed"
        old_enough = cutoff is not None and created <= cutoff
        try:
            photos = json.loads(row["photos_json"] or "[]")
        except (TypeError, ValueError):
            photos = []
        for photo in photos if isinstance(photos, list) else []:
            if not isinstance(photo, dict):
                continue
            path_value = str(photo.get("original_path") or photo.get("path") or "")
            try:
                path = resolve_data_relative_path(path_value) if path_value else None
            except ValueError:
                path = None
            if not path or not path.is_file():
                continue
            size = path.stat().st_size
            total_images += 1
            total_bytes += size
            if old_enough and reviewed:
                eligible_count += 1
                eligible_bytes += size
            else:
                held_count += 1
                held_bytes += size
    return {
        "retention_days": days,
        "cutoff": datetime.fromtimestamp(cutoff, timezone.utc).isoformat() if cutoff is not None else None,
        "total_original_images": total_images,
        "total_original_bytes": total_bytes,
        "hypothetical_eligible_images": eligible_count,
        "hypothetical_eligible_bytes": eligible_bytes,
        "held_images": held_count,
        "held_bytes": held_bytes,
        "held_reason": "未完成教师复核/确认、未达到期限、或保留期限未配置的项目均保留",
        "dry_run": True,
        "deletion_performed": False,
        "automatic_deletion_enabled": False,
    }


def backup_archive_path(filename: str) -> Path:
    clean_name = str(filename or "").strip()
    candidate = (BACKUP_DIR / clean_name).resolve()
    if (
        not clean_name
        or Path(clean_name).name != clean_name
        or candidate.parent != BACKUP_DIR.resolve()
        or candidate.suffix.casefold() != ".zip"
    ):
        raise ValueError("备份文件名无效")
    return candidate


def validate_backup_archive(archive_path: Path, extract_dir: Path | None = None) -> dict[str, Any]:
    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("备份文件无法打开或已损坏") from exc
    with archive:
        infos = archive.infolist()
        if not infos:
            raise ValueError("备份文件为空")
        total_uncompressed = 0
        names: set[str] = set()
        for info in infos:
            name = info.filename.replace("\\", "/")
            pure_name = PurePosixPath(name)
            if (
                name != info.filename
                or pure_name.is_absolute()
                or any(part in {"", ".", ".."} for part in pure_name.parts)
                or len(name) > 500
                or name in names
            ):
                raise ValueError("备份包含不安全的文件路径")
            names.add(name)
            lower_name = name.casefold()
            if ".dpapi" in lower_name or "api-key" in lower_name or "api_key" in lower_name:
                raise ValueError("备份包含禁止保存的 API Key 文件")
            if name != "manifest.json" and not name.startswith("data/"):
                raise ValueError("备份只允许包含 manifest.json 和 data/ 数据")
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_BACKUP_UNCOMPRESSED_BYTES:
                raise ValueError("备份解压后的数据超过4GB，已拒绝恢复")
        if archive.testzip() is not None:
            raise ValueError("备份文件校验失败，可能已损坏")
        if "manifest.json" not in names or "data/history_grading.sqlite3" not in names:
            raise ValueError("备份缺少 manifest.json 或数据库文件")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("备份清单无法读取") from exc
        if manifest.get("format") != "history-grader-backup-v1":
            raise ValueError("备份版本不受当前程序支持")
        manifest_files = manifest.get("files")
        if manifest_files is None:
            raise ValueError("备份缺少文件清单")
        if not isinstance(manifest_files, list):
            raise ValueError("备份清单中的文件列表无效")
        manifest_by_path = {}
        for item in manifest_files:
            if not isinstance(item, dict):
                raise ValueError("备份清单中的文件项无效")
            item_name = str(item.get("path", ""))
            if item_name not in names or item_name == "manifest.json":
                raise ValueError("备份清单引用了不存在的文件")
            try:
                expected_size = int(item.get("bytes"))
            except (TypeError, ValueError) as exc:
                raise ValueError("备份清单中的文件大小无效") from exc
            expected_hash = str(item.get("sha256", "")).casefold()
            if expected_size < 0 or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                raise ValueError("备份清单中的文件校验值无效")
            if item_name in manifest_by_path:
                raise ValueError("备份清单中的文件路径重复")
            manifest_by_path[item_name] = (expected_size, expected_hash)
        archive_data_files = {
            info.filename
            for info in infos
            if not info.is_dir() and info.filename != "manifest.json"
        }
        if archive_data_files != set(manifest_by_path):
            raise ValueError("备份文件与清单不一致")
        for info in infos:
            if info.filename == "manifest.json" or info.filename not in manifest_by_path:
                continue
            expected_size, expected_hash = manifest_by_path[info.filename]
            if info.file_size != expected_size:
                raise ValueError("备份文件大小与清单不一致")
            digest = hashlib.sha256()
            with archive.open(info, "r") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected_hash:
                raise ValueError("备份文件校验值与清单不一致")
        if extract_dir is not None:
            extract_dir.mkdir(parents=True, exist_ok=True)
            root = extract_dir.resolve()
            for info in infos:
                if info.is_dir():
                    continue
                target = extract_dir.joinpath(*PurePosixPath(info.filename).parts)
                if not target.resolve().is_relative_to(root):
                    raise ValueError("备份路径超出恢复目录")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
    return manifest


def create_backup_archive() -> Path:
    global BACKUP_IN_PROGRESS
    with BACKUP_LOCK:
        with ACTIVE_TASKS_CONDITION:
            if BACKUP_IN_PROGRESS:
                raise ValueError("已有备份操作正在进行，请稍候")
            BACKUP_IN_PROGRESS = True
            # When called from the tracked HTTP handler, one active task is
            # the backup itself. Existing submissions are allowed to finish;
            # new writes are rejected while this window is open.
            own_active_task = 1 if ACTIVE_TASKS else 0
            deadline = time.monotonic() + SERVICE_DRAIN_TIMEOUT_SECONDS
            while ACTIVE_TASKS > own_active_task:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    BACKUP_IN_PROGRESS = False
                    ACTIVE_TASKS_CONDITION.notify_all()
                    raise ValueError("等待正在进行的批改任务超时，未生成备份")
                ACTIVE_TASKS_CONDITION.wait(timeout=remaining)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_path = BACKUP_DIR / f"history_grader_backup_{timestamp}_{uuid.uuid4().hex[:6]}.zip"
        temporary_archive_path = BACKUP_DIR / f".{archive_path.name}.{uuid.uuid4().hex}.tmp"
        snapshot_path = BACKUP_DIR / f".database-snapshot-{uuid.uuid4().hex}.sqlite3"
        try:
            source = get_db()
            snapshot = sqlite3.connect(snapshot_path)
            try:
                source.backup(snapshot)
            finally:
                snapshot.close()
                source.close()
            data_files: list[tuple[Path, int, str]] = []
            transient_db_files = {
                DB_PATH.name + "-wal",
                DB_PATH.name + "-shm",
                DB_PATH.name + "-journal",
            }
            for item in DATA_DIR.rglob("*"):
                if (
                    item.is_file()
                    and item.resolve() != DB_PATH.resolve()
                    and item.name not in transient_db_files
                ):
                    data_files.append((item, item.stat().st_size, sha256_file(item)))
            manifest_files = [
                {
                    "path": "data/history_grading.sqlite3",
                    "bytes": snapshot_path.stat().st_size,
                    "sha256": sha256_file(snapshot_path),
                }
            ] + [
                {
                    "path": f"data/{item.relative_to(DATA_DIR).as_posix()}",
                    "bytes": size,
                    "sha256": digest,
                }
                for item, size, digest in data_files
            ]
            manifest = {
                "format": "history-grader-backup-v1",
                "created_at": utc_now(),
                "generation": uuid.uuid4().hex,
                "data_file_count": len(manifest_files),
                "data_bytes": sum(item["bytes"] for item in manifest_files),
                "contains_api_key": False,
                "includes_config": False,
                "files": manifest_files,
            }
            with zipfile.ZipFile(temporary_archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                archive.write(snapshot_path, "data/history_grading.sqlite3")
                for item, _, _ in data_files:
                    archive.write(item, f"data/{item.relative_to(DATA_DIR).as_posix()}")
            validate_backup_archive(temporary_archive_path)
            temporary_archive_path.replace(archive_path)
            return archive_path
        except Exception:
            if temporary_archive_path.exists():
                temporary_archive_path.unlink()
            if archive_path.exists() and archive_path.stat().st_size == 0:
                archive_path.unlink()
            raise
        finally:
            if snapshot_path.exists():
                snapshot_path.unlink()
            with ACTIVE_TASKS_CONDITION:
                BACKUP_IN_PROGRESS = False
                ACTIVE_TASKS_CONDITION.notify_all()


def validate_restored_database(path: Path) -> None:
    return storage.validate_restored_database(path, DB_SCHEMA_VERSION)


def restore_backup_archive(filename: str) -> tuple[Path, Path]:
    global RESTORE_IN_PROGRESS
    archive_path = backup_archive_path(filename)
    if not archive_path.exists():
        raise ValueError("备份文件不存在")
    with RESTORE_LOCK:
        if RESTORE_IN_PROGRESS:
            raise ValueError("已有恢复操作正在进行，请稍候")
        RESTORE_IN_PROGRESS = True
        staging = PORTABLE_ROOT / f".restore-staging-{uuid.uuid4().hex}"
        old_data = PORTABLE_ROOT / f".data-before-restore-{uuid.uuid4().hex}"
        moved_current = False
        replacement_installed = False
        safety_backup: Path | None = None
        try:
            validate_backup_archive(archive_path, staging)
            staged_data = staging / "data"
            validate_restored_database(staged_data / "history_grading.sqlite3")
            # RESTORE_IN_PROGRESS is set before the safety snapshot. New
            # worker tasks therefore fail at active_task(), while any worker
            # already inside active_task() is drained by create_backup_archive.
            safety_backup = create_backup_archive()
            if DATA_DIR.exists():
                DATA_DIR.rename(old_data)
                moved_current = True
            staged_data.rename(DATA_DIR)
            replacement_installed = True
            return archive_path, safety_backup
        except Exception as original_error:
            if moved_current and old_data.exists() and not replacement_installed:
                if DATA_DIR.exists():
                    raise RuntimeError(
                        "恢复失败；原数据仍保留在临时目录，检测到 data 目录被其他进程重新创建，请先关闭其他实例"
                    ) from original_error
                try:
                    old_data.rename(DATA_DIR)
                except OSError as rollback_error:
                    raise RuntimeError(
                        "恢复失败；原数据保留在便携根目录的 .data-before-restore-* 临时目录，请勿删除"
                    ) from rollback_error
            raise
        finally:
            if staging.exists():
                try:
                    shutil.rmtree(staging)
                except OSError as exc:
                    print(f"[{utc_now()}] WARNING: 无法清理恢复临时目录 {staging.name}（{type(exc).__name__}）")
            if replacement_installed and old_data.exists():
                try:
                    shutil.rmtree(old_data)
                except OSError as exc:
                    # The safety ZIP already contains the old data. Keep this
                    # directory if Windows still has a file open; never risk
                    # deleting it in a failed-restore path.
                    print(f"[{utc_now()}] WARNING: 恢复完成但无法清理旧数据临时目录 {old_data.name}（{type(exc).__name__}）")
            RESTORE_IN_PROGRESS = False


def list_backups() -> list[dict[str, Any]]:
    if not BACKUP_DIR.exists():
        return []
    result = []
    for item in BACKUP_DIR.glob("*.zip"):
        if not item.is_file():
            continue
        stat = item.stat()
        result.append(
            {
                "filename": item.name,
                "bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            }
        )
    return sorted(result, key=lambda item: item["created_at"], reverse=True)


def _migration_backup_path(version: int) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUP_DIR / f"pre-migration-v{version}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.sqlite3"


def _backup_database_before_migration(version: int) -> None:
    """Keep a recoverable copy before changing an existing database schema."""
    key = (str(DB_PATH.resolve()), version)
    with MIGRATION_LOCK:
        if key in MIGRATION_BACKUP_DONE or not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
            return
        destination = _migration_backup_path(version)
        shutil.copy2(DB_PATH, destination)
        MIGRATION_BACKUP_DONE.add(key)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_database_schema(conn: sqlite3.Connection, database_existed: bool) -> None:
    """Apply additive, transactional migrations without rewriting old records."""
    legacy_tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    if "schema_meta" in legacy_tables:
        version_row = conn.execute("SELECT schema_version FROM schema_meta WHERE id = 1").fetchone()
        current_version = int(version_row[0]) if version_row else 0
    else:
        current_version = 0
    if current_version == 0 and {"assignments", "submissions"}.issubset(legacy_tables):
        # The pre-versioned application already had the base tables. Treat it
        # as v1 after the compatibility columns below are checked.
        current_version = 1
    if current_version > DB_SCHEMA_VERSION:
        raise RuntimeError(f"数据库 schema 版本 {current_version} 高于当前程序支持的 {DB_SCHEMA_VERSION}，请使用更新版本的程序打开，已阻止降级。")
    if database_existed and current_version < DB_SCHEMA_VERSION:
        _backup_database_before_migration(DB_SCHEMA_VERSION)

    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (id INTEGER PRIMARY KEY CHECK (id = 1), schema_version INTEGER NOT NULL, updated_at TEXT NOT NULL)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assignments (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            question TEXT NOT NULL,
            standard_answer TEXT NOT NULL,
            criteria_json TEXT NOT NULL,
            sub_questions_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL,
            student_label TEXT NOT NULL,
            answer_text TEXT NOT NULL,
            photos_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (assignment_id) REFERENCES assignments(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS roster_students (
            id TEXT PRIMARY KEY,
            class_name TEXT NOT NULL,
            student_no TEXT NOT NULL DEFAULT '',
            student_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS grading_exams (
            id TEXT PRIMARY KEY,
            assignment_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            layout_profile_id TEXT,
            layout_profile_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (assignment_id) REFERENCES assignments(id)
        )
        """
    )
    _ensure_column(conn, "grading_exams", "layout_profile_id", "TEXT")
    _ensure_column(conn, "grading_exams", "layout_profile_json", "TEXT NOT NULL DEFAULT '{}' ")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS exam_marking_codes (
            id TEXT PRIMARY KEY,
            exam_id TEXT NOT NULL,
            code TEXT NOT NULL,
            batch_id TEXT,
            batch_sequence INTEGER,
            submission_id TEXT,
            roster_student_id TEXT,
            binding_status TEXT NOT NULL DEFAULT 'unmatched',
            binding_method TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            bound_at TEXT,
            UNIQUE (exam_id, code),
            UNIQUE (batch_id, batch_sequence),
            UNIQUE (submission_id),
            FOREIGN KEY (exam_id) REFERENCES grading_exams(id),
            FOREIGN KEY (roster_student_id) REFERENCES roster_students(id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exam_marking_codes_roster ON exam_marking_codes(exam_id, roster_student_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exam_marking_codes_status ON exam_marking_codes(exam_id, binding_status)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_exam_marking_codes_unique_student "
        "ON exam_marking_codes(exam_id, roster_student_id) WHERE roster_student_id IS NOT NULL"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS privacy_templates (
            assignment_id TEXT NOT NULL,
            page_count INTEGER NOT NULL CHECK (page_count BETWEEN 1 AND 10),
            assignment_fingerprint TEXT NOT NULL,
            template_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (assignment_id, page_count, assignment_fingerprint),
            FOREIGN KEY (assignment_id) REFERENCES assignments(id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_roster_students_class ON roster_students(class_name)")
    for column, definition in {
        "class_name": "TEXT NOT NULL DEFAULT ''",
        "student_no": "TEXT NOT NULL DEFAULT ''",
        "student_name": "TEXT NOT NULL DEFAULT ''",
        "identity_status": "TEXT NOT NULL DEFAULT 'unmatched'",
        "identity_method": "TEXT NOT NULL DEFAULT ''",
        "final_total": "REAL",
        "teacher_note": "TEXT NOT NULL DEFAULT ''",
        "confirmed_at": "TEXT",
        # Existing submissions stay unbatched with NULL sequence values.  We
        # never infer or backfill an order for historical records.
        "batch_id": "TEXT",
        "batch_sequence": "INTEGER",
        "exam_id": "TEXT",
        "marking_code_id": "TEXT",
        "marking_code": "TEXT NOT NULL DEFAULT ''",
        "answer_regions_json": "TEXT NOT NULL DEFAULT '[]'",
        "marking_status": "TEXT NOT NULL DEFAULT ''",
        "privacy_status": "TEXT NOT NULL DEFAULT 'legacy_unknown'",
    }.items():
        _ensure_column(conn, "submissions", column, definition)
    _ensure_column(conn, "assignments", "sub_questions_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "assignments", "import_metadata_json", "TEXT NOT NULL DEFAULT '{}' ")

    if current_version < 2:
        for column, definition in {
            "idempotency_key": "TEXT NOT NULL DEFAULT ''",
            "request_hash": "TEXT NOT NULL DEFAULT ''",
            "state": "TEXT NOT NULL DEFAULT 'completed'",
            "source_channel": "TEXT NOT NULL DEFAULT 'computer'",
            "uncertain_model_call": "INTEGER NOT NULL DEFAULT 0",
            "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
        }.items():
            _ensure_column(conn, "submissions", column, definition)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_queue (
                id TEXT PRIMARY KEY,
                submission_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (submission_id) REFERENCES submissions(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_task_queue_state_time ON task_queue(state, available_at)")

    if current_version < 3:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL DEFAULT '',
                event_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rubric_versions (
                id TEXT PRIMARY KEY,
                assignment_id TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                rubric_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                approved_at TEXT,
                FOREIGN KEY (assignment_id) REFERENCES assignments(id),
                UNIQUE (assignment_id, version_no)
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_idempotency ON submissions(idempotency_key) WHERE idempotency_key <> ''"
        )

    if current_version < 4:
        for column, definition in {
            "manual_reviewed": "INTEGER NOT NULL DEFAULT 0",
            "reviewed_at": "TEXT",
            "review_note": "TEXT NOT NULL DEFAULT ''",
        }.items():
            _ensure_column(conn, "submissions", column, definition)

    if current_version < 5:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS grading_batches (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                assignment_id TEXT NOT NULL,
                class_name TEXT NOT NULL DEFAULT '',
                source_channel TEXT NOT NULL DEFAULT 'computer',
                total_sequences INTEGER NOT NULL,
                next_sequence INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                pause_sequence INTEGER,
                pause_reason TEXT NOT NULL DEFAULT '',
                manifest_json TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (assignment_id) REFERENCES assignments(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_grading_batches_assignment_created ON grading_batches(assignment_id, created_at)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_batch_sequence ON submissions(batch_id, batch_sequence) WHERE batch_id IS NOT NULL"
        )

    if current_version < 6:
        for column, definition in {
            "ai_privacy_confirmed": "INTEGER NOT NULL DEFAULT 0",
            "ai_privacy_reviewed_at": "TEXT",
            "ai_privacy_fingerprint": "TEXT NOT NULL DEFAULT ''",
        }.items():
            _ensure_column(conn, "submissions", column, definition)

    if current_version < 7:
        _ensure_column(conn, "grading_batches", "exam_id", "TEXT")

    roster_identity.migrate(conn, _ensure_column, utc_now())
    import exam_identity
    import answer_links
    answer_links.migrate(conn)
    exam_identity.migrate(conn)
    conn.execute(
        "INSERT INTO schema_meta (id, schema_version, updated_at) VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET schema_version=excluded.schema_version, updated_at=excluded.updated_at WHERE schema_meta.schema_version<>excluded.schema_version",
        (DB_SCHEMA_VERSION, utc_now()),
    )


def get_db() -> sqlite3.Connection:
    return storage.connect_database(DATA_DIR, DB_PATH, _ensure_database_schema)


def _json_object_or_empty(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}") if isinstance(value, (str, bytes, bytearray)) else value
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _exam_layout_profile_context(conn: sqlite3.Connection, exam_id: str) -> dict[str, Any] | None:
    """Load a formal exam's PDF pages, subjective ids and non-stale profile."""
    row = conn.execute(
        "SELECT exams.id AS exam_id, exams.assignment_id, exams.created_at AS exam_created_at, "
        "exams.layout_profile_id, exams.layout_profile_json, exams.class_id, assignments.name, "
        "assignments.import_metadata_json, assignments.sub_questions_json "
        "FROM grading_exams AS exams JOIN assignments ON assignments.id = exams.assignment_id "
        "WHERE exams.id = ?",
        (exam_id,),
    ).fetchone()
    if row is None:
        return None

    import_meta = _json_object_or_empty(row["import_metadata_json"])
    source = import_meta.get("source") if isinstance(import_meta.get("source"), dict) else {}
    layout_source = source.get("layout_source") if isinstance(source.get("layout_source"), dict) else None
    saved_profile = _json_object_or_empty(row["layout_profile_json"])
    expected_pdf_hash = str(
        (layout_source or {}).get("source_pdf_sha256")
        or (layout_source or {}).get("sha256")
        or saved_profile.get("source_pdf_sha256")
        or ""
    ).lower()
    pdf_hash_mismatch = False
    stored_pdf = str((layout_source or {}).get("stored_path") or "")
    if expected_pdf_hash and stored_pdf:
        try:
            stored_pdf_path = resolve_data_relative_path(stored_pdf)
            if stored_pdf_path.is_file():
                digest = hashlib.sha256(stored_pdf_path.read_bytes()).hexdigest()
                pdf_hash_mismatch = digest != expected_pdf_hash
        except (OSError, ValueError):
            # The source file may have been removed after import. Existing
            # immutable reference pages remain usable and retain their source hash.
            pass

    references: list[dict[str, Any]] = []
    reference_data = import_meta.get("layout_reference") if isinstance(import_meta.get("layout_reference"), dict) else {}
    role_by_page = {
        int(item.get("page_number")): str(item.get("page_role") or "unknown")
        for item in reference_data.get("page_roles", [])
        if isinstance(item, dict) and str(item.get("page_number", "")).isdigit()
    }
    exam_pages = {
        int(number) for number in (layout_source or {}).get("exam_pages", [])
        if str(number).isdigit()
    }
    answer_sheet_pages = {
        int(number) for number in (layout_source or {}).get("answer_sheet_pages", [])
        if str(number).isdigit()
    }
    reference_pages = set(layout_reference_page_numbers(layout_source))
    for page in reference_data.get("pages", []):
        if not isinstance(page, dict):
            continue
        try:
            page_number = int(page.get("page_number"))
            width = int(page.get("reference_width"))
            height = int(page.get("reference_height"))
        except (TypeError, ValueError):
            continue
        fallback_role = "exam" if page_number in exam_pages else "answer_sheet" if page_number in answer_sheet_pages else "unknown"
        role = role_by_page.get(page_number, fallback_role)
        if role not in {"exam", "answer_sheet"}:
            continue
        if reference_pages:
            if page_number not in reference_pages:
                continue
        elif exam_pages and page_number not in exam_pages:
            continue
        if width < 1 or height < 1:
            continue
        references.append({
            "page_number": page_number,
            "page_role": role,
            "reference_width": width,
            "reference_height": height,
            "reference_sha256": str(page.get("reference_sha256") or ""),
            "render_dpi": page.get("render_dpi"),
            "render_method": page.get("render_method"),
            "image_file": str(page.get("image_file") or page.get("relative_path") or ""),
            "asset_status": str(page.get("asset_status") or "unresolved"),
        })
    references.sort(key=lambda item: item["page_number"])

    try:
        raw_subquestions = json.loads(row["sub_questions_json"] or "[]")
    except json.JSONDecodeError:
        raw_subquestions = []
    question_ids = []
    for item in raw_subquestions if isinstance(raw_subquestions, list) else []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        question_id = str(item.get("question_id") or "").strip()
        candidate = label if re.fullmatch(r"\d+\.\d+", label) else question_id
        if re.fullmatch(r"\d+\.\d+", candidate):
            question_ids.append(candidate)
    question_ids = list(dict.fromkeys(question_ids))
    objective_question_ids: list[str] = []
    for item in import_meta.get("objective_questions") if isinstance(import_meta.get("objective_questions"), list) else []:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("question_id") or "").strip()
        if re.fullmatch(r"\d+", candidate) and candidate not in objective_question_ids:
            objective_question_ids.append(candidate)
    objective_question_ids.sort(key=int)

    has_reference_source = bool(layout_source and expected_pdf_hash and (reference_pages or references))
    stored_profile_hash = str(saved_profile.get("source_pdf_sha256") or "").lower()
    profile_hash_mismatch = bool(
        saved_profile
        and (pdf_hash_mismatch or not expected_pdf_hash or stored_profile_hash != expected_pdf_hash)
    )
    profile_invalid = False
    active_profile: dict[str, Any] | None = None
    if saved_profile and not profile_hash_mismatch:
        try:
            from layout_profile import validate_layout_profile

            active_profile = validate_layout_profile(saved_profile)
        except (ImportError, TypeError, ValueError, KeyError):
            profile_invalid = True
            active_profile = None
    from exam_layout_profile_service import layout_profile_status

    progress = layout_profile_status(question_ids, active_profile, objective_question_ids)
    return {
        "exam_id": str(row["exam_id"]),
        "assignment_id": str(row["assignment_id"]),
        "exam_name": str(row["name"]),
        "exam_created_at": str(row["exam_created_at"]),
        "class_name": (str((conn.execute("SELECT class_name FROM roster_classes WHERE id=?", (row["class_id"],)).fetchone() or {"class_name": ""})["class_name"]) if row["class_id"] else ""),
        "source_pdf_file_name": str((layout_source or {}).get("source_pdf_file_name") or (layout_source or {}).get("file_name") or ""),
        "source_pdf_sha256": expected_pdf_hash,
        "source_pdf_relative_path": stored_pdf,
        "pdf_hash_mismatch": pdf_hash_mismatch,
        "has_pdf_reference": has_reference_source,
        "reference_asset_status": "unresolved" if has_reference_source else "reference_asset_missing",
        "reference_metadata_available": bool(references),
        "pdf_page_count": int((layout_source or {}).get("page_count") or 0),
        "references": references,
        "subjective_question_ids": question_ids,
        "objective_question_ids": objective_question_ids,
        "layout_profile_id": str(row["layout_profile_id"] or "") if active_profile else "",
        "layout_profile": active_profile,
        # Legacy anonymous exams also have an exam_id. Only an explicit class
        # or layout source opts into the fail-closed formal crop contract.
        "answer_regions_required": bool(row["class_id"] or row["layout_profile_id"] or saved_profile or layout_source),
        "profile_hash_mismatch": profile_hash_mismatch,
        "profile_invalid": profile_invalid,
        **progress,
    }


def _grading_input_mode(payload: dict[str, Any]) -> str:
    mode = str(payload.get("grading_input_mode") or "auto_crop")
    if mode not in {"auto_crop", "original_photo"}:
        raise ValueError("批改图片模式无效，请重新选择")
    return mode


def _request_formal_exam_context(
    conn: sqlite3.Connection, assignment_id: str, payload: dict[str, Any],
    verified: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    exam_id = str(payload.get("exam_id") or "")
    mode = _grading_input_mode(payload)
    if payload.get("batch_id"):
        batch = conn.execute("SELECT exam_id, manifest_json FROM grading_batches WHERE id=? AND assignment_id=?",
                             (payload["batch_id"], assignment_id)).fetchone()
        if not batch:
            raise ValueError("answer_region_unavailable:batch_missing")
        manifest = json.loads(batch["manifest_json"] or "{}")
        if mode != _grading_input_mode(manifest):
            raise ValueError("批次图片模式不能更改，请重新创建批次")
        if mode == "original_photo" and exam_id != str(manifest.get("preview_exam_id", batch["exam_id"]) or ""):
            raise ValueError("考试与批次确认的原照片预览不匹配")
        if exam_id and exam_id != str(batch["exam_id"] or ""):
            raise ValueError("考试与批次不匹配")
        exam_id = str(batch["exam_id"] or "")
    elif not exam_id and verified:
        exam_id = str(next(iter(verified.values())).get("exam_id") or "")
    if exam_id:
        context = _exam_layout_profile_context(conn, exam_id)
        if not context or context["assignment_id"] != assignment_id:
            raise ValueError("answer_region_unavailable:exam_missing")
        return {**context, "grading_input_mode": mode}
    if payload.get("class_name") and conn.execute(
        "SELECT 1 FROM roster_classes WHERE class_name=?", (payload["class_name"],)
    ).fetchone():
        return {"answer_regions_required": True, "layout_profile": None, "grading_input_mode": mode}
    return {"grading_input_mode": mode} if mode == "original_photo" else None


def _require_formal_answer_regions(
    context: dict[str, Any] | None, sub_questions: list[dict[str, Any]],
    regions: list[dict[str, Any]], verified: dict[int, dict[str, Any]],
) -> None:
    """Use accepted crops unless original-photo input was explicitly selected.

    Alignment, crop production and teacher/privacy acceptance stay in the
    existing auto-crop workflow. This gate does not invent ROIs or approve them.
    """
    if context and context.get("grading_input_mode") == "original_photo":
        if regions or verified:
            raise ValueError("原照片批改不能同时使用裁切区域")
        return
    if not context or not context.get("answer_regions_required"):
        return
    profile = context.get("layout_profile")
    if not profile or context.get("profile_invalid") or context.get("profile_hash_mismatch") or context.get("pdf_hash_mismatch"):
        raise ValueError("answer_region_unavailable:layout_profile_missing_or_stale")
    if not regions or any(r.get("invocation_scope") != "answer_region" for r in regions):
        raise ValueError("answer_region_unavailable:accepted_crop_required")
    questions = {str(q["id"]): q for q in sub_questions}
    for region in regions:
        ids = region.get("sub_question_ids") or []
        if len(ids) != 1 or str(ids[0]) not in questions:
            raise ValueError("answer_region_unavailable:question_mapping_missing")
        question = questions[str(ids[0])]
        roi = next((r for r in profile.get("regions", []) if r.get("region_type") == "subjective_answer"
                    and str(r.get("question_id")) in {str(question["id"]), str(question.get("label"))}), None)
        if not roi:
            raise ValueError("answer_region_unavailable:subjective_roi_missing")
        provenance = verified.get(int(region.get("photo_index", 0))) or {}
        if (provenance.get("exam_id") != context.get("exam_id")
                or provenance.get("layout_profile_id") != context.get("layout_profile_id")
                or provenance.get("question_id") != str(ids[0])
                or provenance.get("configured_bbox") != roi.get("bbox")
                or provenance.get("page_number") != roi.get("page_number")
                or provenance.get("source_pdf_sha256") != context.get("source_pdf_sha256")):
            raise ValueError("answer_region_unavailable:crop_provenance_mismatch")
        reference = next((r for r in context.get("references", [])
                          if r.get("page_number") == roi.get("page_number")), None)
        if not reference or provenance.get("reference_page_sha256") != reference.get("reference_sha256"):
            raise ValueError("answer_region_unavailable:source_page_missing_or_stale")
        if (provenance.get("alignment_status") != "aligned" or provenance.get("quality_gate") != "passed"
                or provenance.get("crop_status") != "passed"):
            raise ValueError("answer_region_unavailable:alignment_or_crop_gate_failed")


def _validate_formal_answer_region_attestations(
    photos: list[dict[str, Any]], regions: list[dict[str, Any]],
    sub_questions: list[dict[str, Any]], assignment: Any,
    conn: sqlite3.Connection, payload: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    context = _request_formal_exam_context(conn, str(assignment["id"]), payload, {})
    if _grading_input_mode(payload) == "original_photo":
        if regions or any(not isinstance(p, dict) or p.get("answer_region_attestation")
                          or str(p.get("image_role") or "source_page") != "source_page" for p in photos):
            raise ValueError("原照片批改只能使用整页照片，请清除裁切区域后重新预览")
        for photo in photos:
            photo["_verified_image_role"] = "source_page"
            photo.pop("_verified_answer_region_provenance", None)
        return {}
    try:
        verified = _validate_answer_region_attestations(photos, regions, sub_questions, assignment, conn)
    except ValueError as exc:
        if context and context.get("answer_regions_required"):
            raise ValueError("answer_region_unavailable:" + str(exc)) from exc
        raise
    context = _request_formal_exam_context(conn, str(assignment["id"]), payload, verified)
    _require_formal_answer_regions(context, sub_questions, regions, verified)
    return verified


def _persist_reference_rebuild_metadata(
    conn: sqlite3.Connection,
    context: dict[str, Any],
    page_number: int,
    resolution: dict[str, Any],
) -> None:
    """Record a verified lazy rebuild using relative paths only."""
    row = conn.execute(
        "SELECT import_metadata_json FROM assignments WHERE id = ?",
        (context["assignment_id"],),
    ).fetchone()
    if row is None:
        return
    metadata = _json_object_or_empty(row["import_metadata_json"])
    layout_reference = metadata.get("layout_reference")
    if not isinstance(layout_reference, dict):
        return
    pages = layout_reference.get("pages")
    if not isinstance(pages, list):
        return
    rebuilt_page = resolution.get("page") if isinstance(resolution.get("page"), dict) else {}
    found = False
    for item in pages:
        if not isinstance(item, dict) or int(item.get("page_number", -1)) != page_number:
            continue
        item["image_file"] = str(resolution["relative_path"])
        item["asset_status"] = "ready"
        item["reference_rebuilt"] = True
        item["reference_hash_match"] = True
        item["rebuilt_at"] = str(rebuilt_page.get("rebuilt_at") or utc_now())
        found = True
        break
    if not found:
        return
    metadata["layout_reference"] = layout_reference
    source = metadata.get("source")
    if isinstance(source, dict) and isinstance(source.get("layout_source"), dict):
        source["layout_source"]["asset_status"] = "ready"
    conn.execute(
        "UPDATE assignments SET import_metadata_json = ? WHERE id = ?",
        (json.dumps(metadata, ensure_ascii=False), context["assignment_id"]),
    )
    conn.commit()


def _resolve_exam_reference_assets(
    conn: sqlite3.Connection,
    context: dict[str, Any],
    *,
    page_numbers: set[int] | None = None,
) -> dict[str, Any]:
    """Resolve only formal exam assets; never search POC/report directories."""
    if not context.get("has_pdf_reference"):
        context["reference_asset_status"] = "reference_asset_missing"
        context["reference_asset_reason"] = "pdf_reference_metadata_missing"
        return context
    refs = [
        item for item in context.get("references", [])
        if isinstance(item, dict) and (page_numbers is None or int(item.get("page_number", -1)) in page_numbers)
    ]
    if not refs:
        context["reference_asset_status"] = "reference_asset_missing"
        context["reference_asset_reason"] = "reference_metadata_missing"
        return context
    source_path: Path | None = None
    stored_pdf = str(context.get("source_pdf_relative_path") or "")
    if stored_pdf:
        try:
            candidate = resolve_data_relative_path(stored_pdf)
            if candidate.is_file():
                source_path = candidate
        except (ValueError, OSError):
            source_path = None

    failures: list[dict[str, Any]] = []
    rebuilt_pages: list[int] = []
    for page in refs:
        resolution = resolve_exam_reference_page(
            portable_root=PORTABLE_ROOT,
            exam_id=str(context["exam_id"]),
            source_pdf_sha256=str(context.get("source_pdf_sha256") or ""),
            page=page,
            source_pdf_path=source_path,
            path_resolver=resolve_data_relative_path,
        )
        page["resolved_asset_status"] = resolution.get("status")
        page["asset_reason"] = resolution.get("reason")
        if resolution.get("status") in {"reference_asset_ready", "reference_rebuilt"}:
            page["resolved_image_path"] = str(resolution["path"])
            if resolution.get("status") == "reference_rebuilt":
                page["image_file"] = str(resolution["relative_path"])
                page["reference_rebuilt"] = True
                page["reference_hash_match"] = True
                rebuilt_pages.append(int(page["page_number"]))
                _persist_reference_rebuild_metadata(conn, context, int(page["page_number"]), resolution)
        else:
            failures.append({
                "page_number": int(page["page_number"]),
                "status": resolution.get("status"),
                "reason": resolution.get("reason"),
            })
    if failures:
        # Corrupt/mismatched metadata is more actionable than a plain absence.
        priority = {"reference_rebuild_mismatch": 0, "reference_asset_corrupt": 1, "reference_asset_missing": 2}
        failures.sort(key=lambda item: priority.get(str(item["status"]), 9))
        context["reference_asset_status"] = str(failures[0]["status"])
        context["reference_asset_failures"] = failures
    else:
        context["reference_asset_status"] = "reference_asset_ready"
        context["reference_rebuilt"] = bool(rebuilt_pages)
        context["reference_rebuilt_pages"] = rebuilt_pages
    return context


def append_audit_event(
    conn: sqlite3.Connection,
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any],
) -> None:
    """Append a small tamper-evident local audit record without secrets."""
    audit_payload = dict(payload)
    audit_payload.setdefault("audit_record_version", AUDIT_RECORD_VERSION)
    payload_json = json.dumps(audit_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    previous = conn.execute("SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
    previous_hash = str(previous[0]) if previous else ""
    created_at = utc_now()
    event_hash = hashlib.sha256(
        f"{previous_hash}|{event_type}|{entity_type}|{entity_id}|{created_at}|{payload_json}".encode("utf-8")
    ).hexdigest()
    conn.execute(
        "INSERT INTO audit_events (event_type, entity_type, entity_id, payload_json, previous_hash, event_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_type, entity_type, entity_id, payload_json, previous_hash, event_hash, created_at),
    )


def rubric_fingerprint(sub_questions: list[dict[str, Any]]) -> str:
    return grading_core.rubric_fingerprint(sub_questions)


def grading_version_context(
    mode: str | None = None,
    sub_questions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return non-secret versions/configuration needed to reproduce a grade."""
    provider = os.getenv("HISTORY_GRADER_PROVIDER", "deepseek").strip() or "deepseek"
    model = os.getenv("HISTORY_GRADER_MODEL", "").strip()
    configured = bool(model_configured())
    return {
        "audit_record_version": AUDIT_RECORD_VERSION,
        "app_version": APP_VERSION,
        "git_commit": GIT_COMMIT,
        "data_schema_version": RUN_SCHEMA_VERSION,
        "environment": runtime_environment(),
        "grading_rules_version": GRADING_RULE_VERSION,
        "rubric_sha256": rubric_fingerprint(sub_questions) if sub_questions is not None else None,
        "prompt_version": VISION_PROMPT_VERSION if configured else "none",
        "model_preset_id": current_model_preset_id(),
        "provider": provider,
        "model": model,
        "mode": str(mode or "unknown"),
        "model_max_tokens": configured_model_max_tokens() if configured else None,
        "model_image_long_edge": MODEL_IMAGE_LONG_EDGE if configured else None,
        "model_image_jpeg_quality": MODEL_IMAGE_JPEG_QUALITY if configured else None,
    }


def _safe_evaluation_case_id(value: Any) -> str:
    case_id = str(value or "").strip()
    if not case_id or len(case_id) > 80 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", case_id):
        raise ValueError("评测清单中的 case_id 无效，只允许字母、数字、点、下划线和短横线")
    return case_id


def _evaluation_number(value: Any, label: str, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"评测清单中的 {label} 必须是数字") from exc
    if not number == number or number in {float("inf"), float("-inf")} or number < minimum:
        raise ValueError(f"评测清单中的 {label} 无效")
    return number


def read_evaluation_manifest() -> dict[str, Any]:
    """Read an explicit, anonymous manifest; never scan formal submissions automatically."""
    if not EVALUATION_MANIFEST_PATH.exists():
        return {
            "format": EVALUATION_MANIFEST_FORMAT,
            "name": "尚未配置评测集",
            "severe_error_threshold": 2.0,
            "cases": [],
            "configured": False,
        }
    try:
        if EVALUATION_MANIFEST_PATH.stat().st_size > EVALUATION_MANIFEST_MAX_BYTES:
            raise ValueError("评测清单超过2MiB，已拒绝读取")
        raw = json.loads(EVALUATION_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("评测清单无法读取，请检查 evaluation/manifest.json 的 UTF-8 JSON 格式") from exc
    if not isinstance(raw, dict) or raw.get("format") != EVALUATION_MANIFEST_FORMAT:
        raise ValueError(f"评测清单格式必须为 {EVALUATION_MANIFEST_FORMAT}")
    allowed_keys = {"format", "name", "severe_error_threshold", "cases"}
    unexpected_keys = set(raw) - allowed_keys
    if unexpected_keys:
        raise ValueError("评测清单只能包含 format、name、severe_error_threshold、cases，禁止放入答案、照片或学生个人信息")
    cases = raw.get("cases")
    if not isinstance(cases, list) or len(cases) > MAX_EVALUATION_CASES:
        raise ValueError(f"评测清单 cases 必须是数组且最多 {MAX_EVALUATION_CASES} 条")
    threshold = _evaluation_number(raw.get("severe_error_threshold", 2.0), "severe_error_threshold", minimum=0.5)
    if threshold > 100:
        raise ValueError("评测清单中的 severe_error_threshold 不能超过100")
    normalized_cases: list[dict[str, Any]] = []
    seen_case_ids: set[str] = set()
    seen_submission_ids: set[str] = set()
    for raw_case in cases:
        if not isinstance(raw_case, dict):
            raise ValueError("评测清单中的每条 case 必须是对象")
        if set(raw_case) - {"case_id", "submission_id", "gold_total", "max_total"}:
            raise ValueError("评测 case 只能包含 case_id、submission_id、gold_total、max_total")
        case_id = _safe_evaluation_case_id(raw_case.get("case_id"))
        submission_id = str(raw_case.get("submission_id") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{12}", submission_id):
            raise ValueError("评测清单中的 submission_id 无效")
        if case_id in seen_case_ids or submission_id in seen_submission_ids:
            raise ValueError("评测清单中的 case_id 和 submission_id 不能重复")
        gold_total = _evaluation_number(raw_case.get("gold_total"), "gold_total")
        max_total_value = raw_case.get("max_total")
        max_total = _evaluation_number(max_total_value, "max_total") if max_total_value is not None else None
        if max_total is not None and gold_total > max_total:
            raise ValueError("评测清单中的 gold_total 不能超过 max_total")
        seen_case_ids.add(case_id)
        seen_submission_ids.add(submission_id)
        normalized_cases.append(
            {
                "case_id": case_id,
                "submission_id": submission_id,
                "gold_total": gold_total,
                "max_total": max_total,
            }
        )
    name = str(raw.get("name") or "匿名真实答卷评测集").strip()[:120]
    return {
        "format": EVALUATION_MANIFEST_FORMAT,
        "name": name or "匿名真实答卷评测集",
        "severe_error_threshold": threshold,
        "cases": normalized_cases,
        "configured": True,
    }


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in {float("inf"), float("-inf")} else None


AUDIT_CONTEXT_ALLOWED_FIELDS = frozenset({
    "app_version", "audit_record_version", "data_schema_version", "environment", "git_commit",
    "grading_rules_version", "mode", "model", "model_image_jpeg_quality", "model_image_long_edge",
    "model_max_tokens", "model_preset_id", "prompt_version", "provider", "rubric_sha256",
})


def safe_grading_context(value: Any) -> dict[str, Any] | None:
    """Only known scalar diagnostic fields may leave the audit payload."""
    if not isinstance(value, dict):
        return None
    safe = {}
    for key in AUDIT_CONTEXT_ALLOWED_FIELDS:
        if key not in value:
            continue
        item = value[key]
        if item is None or isinstance(item, (str, int, float, bool)):
            if isinstance(item, float) and not math.isfinite(item):
                continue
            safe[key] = item[:500] if isinstance(item, str) else item
    return safe


def audit_chain_snapshot(limit: int = 40) -> dict[str, Any]:
    """Verify the complete local audit chain and expose only safe metadata."""
    limit = max(1, min(100, int(limit)))
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, event_type, entity_type, entity_id, payload_json, previous_hash, event_hash, created_at FROM audit_events ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    expected_previous = ""
    chain_valid = True
    for row in rows:
        payload_json = str(row["payload_json"] or "")
        expected_hash = hashlib.sha256(
            f"{row['previous_hash']}|{row['event_type']}|{row['entity_type']}|{row['entity_id']}|{row['created_at']}|{payload_json}".encode("utf-8")
        ).hexdigest()
        if str(row["previous_hash"] or "") != expected_previous or str(row["event_hash"] or "") != expected_hash:
            chain_valid = False
        expected_previous = str(row["event_hash"] or "")
    recent_rows = rows[-limit:]
    events: list[dict[str, Any]] = []
    for row in recent_rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        context = payload.get("grading_context")
        safe_context = safe_grading_context(context)
        events.append(
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "created_at": row["created_at"],
                "audit_record_version": payload.get("audit_record_version", "legacy"),
                "grading_context": safe_context,
            }
        )
    return {
        "audit_record_version": AUDIT_RECORD_VERSION,
        "chain_valid": chain_valid,
        "event_count": len(rows),
        "events": list(reversed(events)),
    }


def evaluation_summary() -> dict[str, Any]:
    """Compare explicit gold labels with saved AI suggestions, without invoking a model."""
    manifest = read_evaluation_manifest()
    cases = manifest["cases"]
    metrics = {
        "total_cases": len(cases),
        "matched_cases": 0,
        "scored_cases": 0,
        "missing_cases": 0,
        "failed_cases": 0,
        "excluded_cases": 0,
        "within_one_count": 0,
        "within_one_rate": None,
        "severe_error_count": 0,
        "severe_error_rate": None,
        "average_absolute_error": None,
        "severe_error_threshold": manifest["severe_error_threshold"],
    }
    absolute_errors: list[float] = []
    modes: dict[str, int] = {}
    version_contexts: list[dict[str, Any]] = []
    conn = get_db()
    try:
        for case in cases:
            row = conn.execute("SELECT state, result_json FROM submissions WHERE id = ?", (case["submission_id"],)).fetchone()
            if not row:
                metrics["missing_cases"] += 1
                continue
            metrics["matched_cases"] += 1
            try:
                result = json.loads(row["result_json"] or "{}")
            except (TypeError, ValueError):
                result = {}
            if not isinstance(result, dict):
                result = {}
            mode = str(result.get("mode") or "unknown")
            modes[mode] = modes.get(mode, 0) + 1
            context = result.get("grading_context")
            if isinstance(context, dict) and context not in version_contexts:
                version_contexts.append(context)
            predicted = _finite_number(result.get("suggested_total"))
            if row["state"] != "completed" or mode in {"queued", "vision-model-error", "image-quality-error", "processing-error"}:
                metrics["failed_cases"] += 1
                continue
            if predicted is None:
                metrics["excluded_cases"] += 1
                continue
            metrics["scored_cases"] += 1
            error = abs(predicted - float(case["gold_total"]))
            absolute_errors.append(error)
            if error <= 1.0:
                metrics["within_one_count"] += 1
            if error >= float(manifest["severe_error_threshold"]):
                metrics["severe_error_count"] += 1
    finally:
        conn.close()
    scored = int(metrics["scored_cases"])
    matched = int(metrics["matched_cases"])
    if scored:
        metrics["within_one_rate"] = round(metrics["within_one_count"] / scored, 4)
        metrics["severe_error_rate"] = round(metrics["severe_error_count"] / scored, 4)
        metrics["average_absolute_error"] = round(sum(absolute_errors) / scored, 4)
    metrics["failure_rate"] = round(metrics["failed_cases"] / matched, 4) if matched else None
    return {
        "ok": True,
        "manifest": {
            "configured": bool(manifest["configured"]),
            "format": manifest["format"],
            "name": manifest["name"],
            "relative_path": "evaluation/manifest.json",
            "case_count": len(cases),
            "severe_error_threshold": manifest["severe_error_threshold"],
        },
        "metrics": metrics,
        "modes": modes,
        "version_contexts": version_contexts[:20],
        "audit": audit_chain_snapshot(),
    }


def submission_public(row: sqlite3.Row) -> dict[str, Any]:
    return storage.submission_public(row, get_db=get_db)


def submission_photo_directory(
    assignment_id: str,
    submission_id: str,
    photo_meta: list[dict[str, Any]],
) -> Path | None:
    """Resolve the one generated student directory without trusting arbitrary paths."""
    if not re.fullmatch(r"[0-9a-f]{12}", assignment_id) or not re.fullmatch(r"[0-9a-f]{12}", submission_id):
        raise ValueError("批改记录编号无效")
    data_root = DATA_DIR.resolve()
    students_root = (DATA_DIR / assignment_id / "students").resolve()
    directories: set[Path] = set()
    for item in photo_meta:
        if not isinstance(item, dict):
            continue
        for key in ("path", "original_path"):
            relative = str(item.get(key, ""))
            if not relative:
                continue
            candidate = resolve_data_relative_path(relative)
            if (
                not candidate.is_relative_to(data_root)
                or not candidate.is_relative_to(students_root)
                or candidate.parent.parent != students_root
                or not (
                    candidate.parent.name == submission_id
                    or candidate.parent.name.endswith(f"_{submission_id}")
                )
            ):
                raise ValueError("批改记录引用的照片目录不安全，已拒绝清理")
            directories.add(candidate.parent)
    if len(directories) > 1:
        raise ValueError("批改记录引用了多个学生照片目录，已拒绝清理")
    return next(iter(directories), None)


def submission_list_public(row: sqlite3.Row) -> dict[str, Any]:
    return storage.submission_list_public(row)


def load_submission_public(submission_id: str) -> dict[str, Any] | None:
    return storage.load_submission_public(submission_id, get_db=get_db, submission_public=submission_public)


def queued_result(max_total: float) -> dict[str, Any]:
    return {
        "mode": "queued",
        "suggested_total": None,
        "max_total": max_total,
        "confidence": 0.0,
        "needs_review": True,
        "blocked": True,
        "block_reasons": ["任务尚未完成"],
        "summary": "照片已安全保存，正在等待本机批改队列处理。",
        "student_identity": normalize_student_identity(None),
        "sub_questions": [],
    }


@contextmanager
def _batch_upload_lock(batch_id: str):
    """Serialize a batch's disk writes without retaining locks after use."""
    with GRADING_BATCH_LOCKS_LOCK:
        entry = GRADING_BATCH_LOCKS.get(batch_id)
        lock, users = entry if entry else (threading.Lock(), 0)
        GRADING_BATCH_LOCKS[batch_id] = (lock, users + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with GRADING_BATCH_LOCKS_LOCK:
            current_lock, users = GRADING_BATCH_LOCKS.get(batch_id, (lock, 1))
            if current_lock is lock and users <= 1:
                GRADING_BATCH_LOCKS.pop(batch_id, None)
            elif current_lock is lock:
                GRADING_BATCH_LOCKS[batch_id] = (lock, users - 1)


def _batch_for_submission(conn: sqlite3.Connection, submission_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT b.*, s.batch_sequence, s.state AS submission_state
        FROM submissions s JOIN grading_batches b ON b.id = s.batch_id
        WHERE s.id = ?
        """,
        (submission_id,),
    ).fetchone()


def _batch_sequence_is_runnable(conn: sqlite3.Connection, batch_id: str, sequence: int) -> bool:
    from batch_grading_control import stopped
    batch = conn.execute("SELECT status, next_sequence, manifest_json FROM grading_batches WHERE id = ?", (batch_id,)).fetchone()
    return bool(batch and not stopped(batch) and batch["status"] == "active" and int(batch["next_sequence"]) == int(sequence))


def _enqueue_current_batch_submission(conn: sqlite3.Connection, batch_id: str) -> str | None:
    batch = conn.execute(
        "SELECT status, next_sequence, manifest_json FROM grading_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    from batch_grading_control import stopped
    if not batch or batch["status"] != "active" or stopped(batch):
        return None
    sequence = int(batch["next_sequence"])
    manifest = json.loads(batch["manifest_json"] or "{}")
    deleted = {int(i['sequence']) for i in manifest.get('items', []) if i.get('upload_status') == 'deleted'}
    while sequence in deleted:
        sequence += 1
    if sequence != int(batch['next_sequence']):
        total = conn.execute('SELECT total_sequences FROM grading_batches WHERE id=?',(batch_id,)).fetchone()[0]
        conn.execute("UPDATE grading_batches SET next_sequence=?,status=CASE WHEN ?>total_sequences THEN 'completed' ELSE status END WHERE id=?",(sequence,sequence,batch_id))
        if sequence > total:return None
    row = conn.execute(
        "SELECT id FROM submissions WHERE batch_id = ? AND batch_sequence = ? AND state = 'queued'",
        (batch_id, sequence),
    ).fetchone()
    if not row:
        try:
            manifest = json.loads(batch["manifest_json"] or "{}")
            item = next((value for value in manifest.get("items", []) if int(value.get("sequence", 0)) == sequence), None)
        except (TypeError, ValueError, json.JSONDecodeError):
            item = None
        if item and item.get("upload_status") == "failed":
            conn.execute(
                "UPDATE grading_batches SET status = 'paused', pause_sequence = ?, pause_reason = ?, updated_at = ? WHERE id = ? AND next_sequence = ?",
                (sequence, str(item.get("upload_error") or "答卷上传失败")[:500], utc_now(), batch_id, sequence),
            )
        return None
    submission_id = str(row["id"])
    conn.execute(
        "UPDATE task_queue SET state = 'queued', updated_at = ? WHERE submission_id = ? AND state = 'waiting_batch'",
        (utc_now(), submission_id),
    )
    return submission_id


def _mark_batch_item_upload_status(
    conn: sqlite3.Connection, batch_id: str, sequence: int, status: str, error: str = ""
) -> dict[str, Any]:
    row = conn.execute("SELECT manifest_json FROM grading_batches WHERE id = ?", (batch_id,)).fetchone()
    if not row:
        raise ValueError("批次不存在")
    manifest = json.loads(row["manifest_json"] or "{}")
    item = next((value for value in manifest.get("items", []) if int(value.get("sequence", 0)) == sequence), None)
    if item is None:
        raise ValueError("批次序号不存在")
    item["upload_status"] = status
    if error:
        item["upload_error"] = error[:500]
    else:
        item.pop("upload_error", None)
    conn.execute(
        "UPDATE grading_batches SET manifest_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(manifest, ensure_ascii=False), utc_now(), batch_id),
    )
    return item


def _submission_photo_paths(photo_meta: list[dict[str, Any]]) -> list[Path]:
    paths: list[Path] = []
    for item in photo_meta:
        if not isinstance(item, dict):
            raise ValueError("批改记录引用的照片清单无效")
        relative = str(item.get("path", ""))
        if not relative:
            continue
        path = resolve_data_relative_path(relative)
        if not path.is_file():
            raise ValueError("批改记录引用的照片文件不存在或超出便携根目录")
        paths.append(path)
    return paths


def _validate_persisted_answer_region(
    photo: dict[str, Any],
    path: Path,
    answer_regions: list[dict[str, Any]],
) -> None:
    """Recheck a queued crop's saved files and server-derived provenance."""
    provenance = photo.get("answer_region_provenance")
    if photo.get("image_role") != "answer_region" or not isinstance(provenance, dict):
        raise ValueError("answer_region_invalid:provenance_missing")
    if not path.is_file():
        raise ValueError("answer_region_invalid:file_missing")
    original_relative = str(photo.get("original_path") or "")
    if not original_relative:
        raise ValueError("answer_region_invalid:original_file_missing")
    original_path = resolve_data_relative_path(original_relative)
    if not original_path.is_file():
        raise ValueError("answer_region_invalid:original_file_missing")
    original_bytes = original_path.read_bytes()
    original_hash = hashlib.sha256(original_bytes).hexdigest()
    if (
        original_hash != str(provenance.get("crop_sha256") or "")
        or original_hash != str(photo.get("original_sha256") or "")
        or len(original_bytes) != int(provenance.get("crop_bytes") or 0)
    ):
        raise ValueError("answer_region_invalid:stored_crop_hash_mismatch")
    validate_answer_region_image(
        original_bytes,
        str(photo.get("original_content_type") or ""),
        provenance,
        expected_sha256=original_hash,
        expected_dimensions=(int(provenance.get("crop_width", 0)), int(provenance.get("crop_height", 0))),
    )
    processed_bytes = path.read_bytes()
    if hashlib.sha256(processed_bytes).hexdigest() != str(photo.get("sha256") or ""):
        raise ValueError("answer_region_invalid:processed_file_hash_mismatch")
    validate_answer_region_image(processed_bytes, "image/jpeg", provenance)
    matching = [
        region for region in answer_regions
        if int(region.get("photo_index", 0)) == int(photo.get("page_sequence", 0))
    ]
    if len(matching) != 1:
        raise ValueError("answer_region_invalid:question_mapping_missing")
    region = matching[0]
    if (
        region.get("invocation_scope") != "answer_region"
        or region.get("profile_id") != provenance.get("layout_profile_id")
        or list(region.get("sub_question_ids") or []) != [str(provenance.get("question_id") or "")]
        or any(float(region.get(key, -1)) != expected for key, expected in (
            ("x", 0.0), ("y", 0.0), ("width", 1.0), ("height", 1.0),
        ))
    ):
        raise ValueError("answer_region_invalid:question_mapping_mismatch")


def process_submission(submission_id: str) -> None:
    """Grade one persisted submission and commit its result exactly once."""
    pipeline_started_at = time.perf_counter()
    with active_task("grade_submission"):
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT submissions.*, assignments.question, assignments.standard_answer, assignments.criteria_json, assignments.sub_questions_json, assignments.import_metadata_json FROM submissions JOIN assignments ON assignments.id = submissions.assignment_id WHERE submissions.id = ?",
                (submission_id,),
            ).fetchone()
            if not row:
                conn.rollback()
                return
            if row["state"] not in {"queued", "recovery_pending"}:
                conn.rollback()
                return
            batch_id = str(row["batch_id"] or "")
            if batch_id and not _batch_sequence_is_runnable(conn, batch_id, int(row["batch_sequence"] or 0)):
                conn.rollback()
                return
            claimed = conn.execute(
                "UPDATE submissions SET state = 'grading' WHERE id = ? AND state IN ('queued', 'recovery_pending')",
                (submission_id,),
            )
            if claimed.rowcount != 1:
                conn.rollback()
                return
            conn.execute(
                "UPDATE task_queue SET state = 'running', updated_at = ? WHERE submission_id = ?",
                (utc_now(), submission_id),
            )
            conn.commit()
            saved_sub_questions = json.loads(row["sub_questions_json"] or "[]")
            if saved_sub_questions:
                sub_questions = clean_sub_questions(saved_sub_questions)
            else:
                sub_questions = clean_sub_questions(
                    None,
                    fallback_question=row["question"],
                    fallback_answer=row["standard_answer"],
                    fallback_criteria=json.loads(row["criteria_json"]),
                )
            photo_meta = json.loads(row["photos_json"] or "[]")
            photo_paths = _submission_photo_paths(photo_meta)
            try:
                answer_regions = json.loads(row["answer_regions_json"] or "[]")
            except (KeyError, TypeError, json.JSONDecodeError):
                answer_regions = []
            anonymous_mode = True
            queued_result_diagnostics: dict[str, Any] = {}
            queued_question_mapping: list[dict[str, Any]] = []
            dual_review_requested = False
            queued_result_value = {}
            try:
                queued_result_value = json.loads(row["result_json"] or "{}")
                if isinstance(queued_result_value, dict) and isinstance(queued_result_value.get("diagnostics"), dict):
                    queued_result_diagnostics = dict(queued_result_value["diagnostics"])
                if isinstance(queued_result_value, dict) and isinstance(queued_result_value.get("teacher_question_mapping"), list):
                    queued_question_mapping = list(queued_result_value["teacher_question_mapping"])
                if isinstance(queued_result_value, dict):
                    dual_review_requested = queued_result_value.get("dual_review_requested") is True
            except (TypeError, ValueError):
                queued_result_diagnostics = {}
            diagnostics: dict[str, Any] = {
                **queued_result_diagnostics,
                "photo_count": len(photo_paths),
                "original_photo_bytes": sum(
                    int(item.get("original_bytes") or 0)
                    for item in photo_meta
                    if isinstance(item, dict)
                ),
            }
            quality_started_at = time.perf_counter()
            quality_issues = []
            answer_region_issues = []
            trusted_answer_question_ids = []
            for index, photo in enumerate(photo_meta, start=1):
                if not isinstance(photo, dict):
                    answer_region_issues.append(f"photo_{index}:metadata_invalid")
                    continue
                image_role = str(photo.get("image_role") or "source_page")
                if image_role == "answer_region":
                    provenance = photo.get("answer_region_provenance") or {}
                    trusted_answer_question_ids.append(str(provenance.get("question_id") or ""))
                    try:
                        path_index = int(photo.get("page_sequence") or index) - 1
                        if path_index < 0 or path_index >= len(photo_paths):
                            raise ValueError("answer_region_invalid:file_missing")
                        _validate_persisted_answer_region(photo, photo_paths[path_index], answer_regions)
                    except Exception as exc:
                        answer_region_issues.append(f"{photo.get('name') or index}:{str(exc) or 'answer_region_invalid'}")
                elif image_role == "source_page":
                    quality = photo.get("quality") or {}
                    if not quality.get("ok", True):
                        photo_name = str(photo.get("name") or f"第 {index} 张照片")
                        quality_issues.extend(f"{photo_name}：{issue}" for issue in quality.get("issues", []))
                else:
                    answer_region_issues.append(f"{photo.get('name') or index}:image_role_invalid")
            diagnostics["quality_gate_ms"] = elapsed_ms(quality_started_at)
            grading_questions = sub_questions
            grading_scope = None
            if not answer_region_issues:
                try:
                    grading_questions, grading_scope = resolve_grading_scope(sub_questions, answer_regions)
                except ValueError as exc:
                    answer_region_issues.append(f"grading_scope_invalid:{str(exc)}")
            grade_started_at = time.perf_counter()
            formal_context = _exam_layout_profile_context(conn, str(row["exam_id"] or ""))
            original_photos = [p for p in photo_meta if isinstance(p, dict) and p.get("grading_input_mode") == "original_photo"]
            if original_photos:
                if len(original_photos) != len(photo_meta) or answer_regions or any(
                    p.get("image_role", "source_page") != "source_page" or not p.get("approved_ai_page_sha256")
                    for p in original_photos
                ):
                    answer_region_issues.append("original_photo:invalid_approved_pages")
                else:
                    formal_context = {**(formal_context or {}), "grading_input_mode": "original_photo"}
                    diagnostics["grading_input_mode"] = "original_photo"
                    # The teacher reviewed these exact pages. Heuristic blur /
                    # exposure checks are advisory in this explicit mode.
                    if quality_issues:
                        diagnostics["photo_quality_warnings"] = list(quality_issues)
                        quality_issues = []
            try:
                _require_formal_answer_regions(formal_context, grading_questions, answer_regions, {
                    int(photo.get("page_sequence") or index): photo.get("answer_region_provenance") or {}
                    for index, photo in enumerate(photo_meta, start=1) if isinstance(photo, dict)
                    and photo.get("image_role") == "answer_region"
                })
            except ValueError as exc:
                answer_region_issues.append(str(exc))
            if formal_context and formal_context.get("answer_regions_required") and answer_region_issues:
                answer_region_issues.append("answer_region_unavailable:crop_validation_failed")
            privacy_status = str(row["privacy_status"] or "legacy_unknown")
            scoring_version_evidence = None
            cloud_model_requested = bool(photo_paths and (model_configured() or dual_review_requested))
            rubric_readiness_issues: list[str] = []
            if trusted_answer_question_ids:
                try:
                    _require_answer_region_rubric_ready(
                        row["import_metadata_json"], sub_questions, trusted_answer_question_ids
                    )
                except ValueError as exc:
                    rubric_readiness_issues.append(str(exc))
            scoring_version_evidence = grading_core.subjective_version_evidence(row, saved_sub_questions, grading_questions)
            if answer_region_issues:
                result = answer_region_invalid_result(sub_questions, answer_region_issues)
            elif cloud_model_requested and privacy_status not in PRIVACY_STATUS_APPROVED:
                result = model_failure_result(grading_questions, {
                    "error_category": "privacy_status_unchecked",
                    "privacy_status": privacy_status,
                    "retryable": False,
                })
                result["retryable"] = False
                result["summary"] = "尚未确认答卷隐私状态，已阻止发送给云模型。请重新预览并完成隐私确认后再提交。"
            elif rubric_readiness_issues:
                result = model_failure_result(grading_questions, {
                    "error_category": "rubric_not_ready",
                    "retryable": False,
                    "reasons": rubric_readiness_issues,
                })
                result["retryable"] = False
                result["summary"] = "正式评分细则尚未由教师确认，已阻止发送给云模型。"
            elif quality_issues and cloud_model_requested:
                result = image_quality_failure_result(grading_questions, quality_issues)
            elif photo_paths and not dual_review_requested and not model_configured() and queued_result_value.get('model_required'):
                result = model_failure_result(grading_questions, {'error_category':'model_not_configured','retryable':False})
                result['summary'] = '模型未配置，尚未发起模型调用；请配置后明确确认原位恢复。'
            else:
                scoring_version_evidence = grading_core.subjective_version_evidence(
                    row, saved_sub_questions, grading_questions
                )
                result = grade_submission(
                    row,
                    grading_questions,
                    row["answer_text"],
                    photo_paths,
                    photo_meta,
                    bool(row["ai_privacy_confirmed"]),
                    answer_regions,
                    anonymous_mode,
                    queued_question_mapping,
                    dual_review_requested,
                )
            if grading_scope and not answer_region_issues:
                if grading_scope.get("source") == "answer_regions":
                    result = apply_grading_scope_to_result(result, grading_questions, grading_scope)
                    diagnostics["graded_question_ids"] = list(grading_scope["graded_question_ids"])
                    diagnostics["scoped_max_score"] = grading_scope["scoped_max_score"]
                else:
                    result["grading_scope"] = grading_scope
            if queued_question_mapping:
                result["teacher_question_mapping"] = queued_question_mapping
            if result.get("mode") == "vision-model" and model_configuration_is_synthetic():
                result["simulation_model"] = True
                result["blocked"] = True
                result["needs_review"] = True
                result["block_reasons"] = sorted(set(result.get("block_reasons", [])) | {
                    "当前服务器连接模拟模型；此结果仅用于流程验收，不能作为真实 AI 评分或确认成绩"
                })
                result["summary"] = "模拟模型返回的流程验收结果，不是真实 AI 评分，不能确认或导出为正式成绩。"
            diagnostics.update(result.get("diagnostics") or {})
            if dual_review_requested:
                result['dual_review_requested'] = True
            diagnostics["grade_pipeline_ms"] = elapsed_ms(grade_started_at)
            result["grading_context"] = grading_version_context(result.get("mode"), grading_questions)
            if scoring_version_evidence is not None:
                result["subjective_version_evidence"] = scoring_version_evidence
            evidence_started_at = time.perf_counter()
            if result.get("mode") == "vision-model":
                materialize_evidence_crops(result, photo_meta, photo_paths)
            diagnostics["evidence_ms"] = elapsed_ms(evidence_started_at)
            identity_started_at = time.perf_counter()
            # Scoring never discovers/rebinds a student's identity. Preserve only
            # existing explicit roster data, and remove model identity output.
            identity_match=dict(student_no=row['student_no'],student_name=row['student_name'],status=row['identity_status'],method=row['identity_method'])
            result.pop('student_identity',None)
            student_label=row['student_label'] or row['id']
            diagnostics["identity_ms"] = elapsed_ms(identity_started_at)
            diagnostics["pipeline_ms"] = elapsed_ms(pipeline_started_at)
            result["diagnostics"] = diagnostics
            uncertain_call = int(result.get("mode") == "vision-model-error" and model_configured())
            evidence = [
                item["evidence"]
                for sub_question in result.get("sub_questions", [])
                for item in sub_question.get("criteria", [])
                if isinstance(item.get("evidence"), dict)
            ]
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE submissions SET student_label = ?, student_no = ?, student_name = ?, identity_status = ?, identity_method = ?, result_json = ?, evidence_json = ?, state = 'completed', uncertain_model_call = ? WHERE id = ?",
                (
                    student_label,
                    identity_match["student_no"],
                    identity_match["student_name"],
                    identity_match["status"],
                    identity_match["method"],
                    json.dumps(result, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    uncertain_call,
                    submission_id,
                ),
            )
            import blank_consensus
            blank_consensus.auto_confirm(conn, submission_id, sys.modules[__name__])
            conn.execute(
                "UPDATE task_queue SET state = 'completed', updated_at = ?, last_error = ? WHERE submission_id = ?",
                (utc_now(), "模型调用结果不确定，请人工决定是否重试" if uncertain_call else "", submission_id),
            )
            append_audit_event(
                conn,
                "submission_graded",
                "submission",
                submission_id,
                {
                    "state": "completed",
                    "mode": result.get("mode"),
                    "uncertain_model_call": bool(uncertain_call),
                    "grading_context": result["grading_context"],
                },
            )
            next_batch_submission_id = None
            if batch_id:
                sequence = int(row["batch_sequence"] or 0)
                # A mixed request may report fragments outside its own scoring
                # scope even when all expected questions have been identified.
                needs_mapping = bool(result.get("requires_question_mapping"))
                questions = result.get("sub_questions", [])
                if needs_mapping and any(item.get("scoring_mode") == "holistic" for item in questions):
                    needs_mapping = bool(result.get("unmapped_answers")) and any(
                        item.get("answer_status") == "question_unmatched" for item in questions
                    )
                batch_failed = result.get("mode") in {
                    "vision-model-error", "image-quality-error", "processing-error", "rules-demo"
                } or needs_mapping
                if batch_failed:
                    pause_reason = (
                        "匿名批改未完成；等待教师原位重试或确认跳过"
                        if anonymous_mode
                        else str(result.get("summary") or result.get("mode") or "批改失败")[:500]
                    )
                    paused = conn.execute(
                        "UPDATE grading_batches SET status = 'paused', pause_sequence = ?, pause_reason = ?, updated_at = ? WHERE id = ? AND next_sequence = ?",
                        (sequence, pause_reason, utc_now(), batch_id, sequence),
                    )
                    if paused.rowcount != 1:
                        raise RuntimeError("批次游标已变化，拒绝提交不一致的批改结果")
                else:
                    from batch_grading_control import stopped, STOP_REASON
                    stop_requested = stopped(conn.execute('SELECT manifest_json FROM grading_batches WHERE id=?', (batch_id,)).fetchone())
                    advanced = conn.execute(
                        "UPDATE grading_batches SET next_sequence = ?, status = ?, pause_sequence = NULL, pause_reason = '', updated_at = ? WHERE id = ? AND next_sequence = ?",
                        (
                            sequence + 1,
                            "completed" if sequence >= int(conn.execute("SELECT total_sequences FROM grading_batches WHERE id = ?", (batch_id,)).fetchone()[0]) else "active",
                            utc_now(),
                            batch_id,
                            sequence,
                        ),
                    )
                    if advanced.rowcount != 1:
                        raise RuntimeError("批次游标已变化，拒绝提交不一致的批改结果")
                    if stop_requested and sequence < int(conn.execute('SELECT total_sequences FROM grading_batches WHERE id=?', (batch_id,)).fetchone()[0]):
                        conn.execute("UPDATE grading_batches SET status='paused',pause_sequence=next_sequence,pause_reason=? WHERE id=?", (STOP_REASON, batch_id))
                    next_batch_submission_id = _enqueue_current_batch_submission(conn, batch_id)
            conn.commit()
            if next_batch_submission_id:
                enqueue_submission(next_batch_submission_id)
        except Exception as exc:
            try:
                conn.rollback()
                conn.execute("BEGIN IMMEDIATE")
                processing_diagnostics = {
                    "error_category": "local_processing",
                    "retryable": False,
                    "message": "本机图片处理或结果保存阶段发生错误",
                    "exception_type": type(exc).__name__,
                    "pipeline_ms": elapsed_ms(pipeline_started_at),
                }
                processing_context = grading_version_context("processing-error")
                conn.execute(
                    "UPDATE submissions SET state = 'failed', result_json = ?, uncertain_model_call = 1 WHERE id = ?",
                    (
                        json.dumps(
                            {
                                "mode": "processing-error",
                                "blocked": True,
                                "needs_review": True,
                                "diagnostics": processing_diagnostics,
                                "grading_context": processing_context,
                                "summary": "任务处理失败，请查看诊断信息后重试。",
                            },
                            ensure_ascii=False,
                        ),
                        submission_id,
                    ),
                )
                conn.execute(
                    "UPDATE task_queue SET state = 'failed', last_error = ?, updated_at = ? WHERE submission_id = ?",
                    (
                        "匿名答卷处理失败；请教师查看答卷后原位重试或确认跳过"
                        if ('row' in locals() and row is not None and row["marking_code_id"])
                        else "本机处理失败；请教师查看答卷后原位重试或确认跳过"
                        if cloud_mode_enabled()
                        else f"{type(exc).__name__}: {str(exc)[:500]}",
                        utc_now(),
                        submission_id,
                    ),
                )
                failed_row = conn.execute(
                    "SELECT batch_id, batch_sequence FROM submissions WHERE id = ?", (submission_id,)
                ).fetchone()
                if failed_row and failed_row["batch_id"]:
                    conn.execute(
                        "UPDATE grading_batches SET status = 'paused', pause_sequence = ?, pause_reason = ?, updated_at = ? WHERE id = ? AND next_sequence = ?",
                        (
                            failed_row["batch_sequence"],
                            "本机处理或结果保存失败",
                            utc_now(),
                            failed_row["batch_id"],
                            failed_row["batch_sequence"],
                        ),
                    )
                append_audit_event(
                    conn,
                    "submission_failed",
                    "submission",
                    submission_id,
                    {"error_type": type(exc).__name__, "grading_context": processing_context},
                )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
            print(f"[{utc_now()}] ERROR: submission processing failed (type={type(exc).__name__})")
        finally:
            conn.close()


def configured_grade_worker_count() -> int:
    """Keep provider pressure bounded; local testing may lower this to one."""
    try:
        value = int(os.getenv("HISTORY_GRADER_WORKERS", "1"))
    except (TypeError, ValueError):
        value = 1
    return max(1, min(4, value))


def _enqueue_submission_id(submission_id: str) -> bool:
    normalized = str(submission_id)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT submissions.batch_id, submissions.batch_sequence, submissions.state "
            "FROM submissions JOIN task_queue ON task_queue.submission_id=submissions.id "
            "WHERE submissions.id = ? AND task_queue.state='queued'",
            (normalized,),
        ).fetchone()
        if not row or row["state"] != "queued":
            return False
        if row["batch_id"] and not _batch_sequence_is_runnable(
            conn, str(row["batch_id"]), int(row["batch_sequence"] or 0)
        ):
            return False
    finally:
        conn.close()
    with ENQUEUED_SUBMISSIONS_LOCK:
        if normalized in ENQUEUED_SUBMISSIONS:
            return True
        try:
            GRADE_QUEUE.put_nowait(normalized)
        except queue.Full:
            return False
        ENQUEUED_SUBMISSIONS.add(normalized)
        return True


def _release_enqueued_submission(submission_id: str) -> None:
    with ENQUEUED_SUBMISSIONS_LOCK:
        ENQUEUED_SUBMISSIONS.discard(str(submission_id))


def _queue_worker_loop() -> None:
    while not QUEUE_STOP_EVENT.is_set() or not GRADE_QUEUE.empty():
        try:
            submission_id = GRADE_QUEUE.get(timeout=0.25)
        except queue.Empty:
            refill_persisted_grade_queue()
            continue
        requeued = False
        try:
            process_submission(submission_id)
        except ServiceDrainingError:
            # The persisted queue row remains queued. During shutdown leave
            # it for the next startup; during a backup, put it back after the
            # consistency window closes.
            if not TASK_DRAINING:
                while (BACKUP_IN_PROGRESS or RESTORE_IN_PROGRESS) and not QUEUE_STOP_EVENT.is_set():
                    QUEUE_STOP_EVENT.wait(0.25)
                if not QUEUE_STOP_EVENT.is_set():
                    _release_enqueued_submission(submission_id)
                    requeued = _enqueue_submission_id(submission_id)
                    if not requeued:
                        # The persistent queue remains queued and will be
                        # picked up by refill_persisted_grade_queue().
                        pass
        finally:
            if not requeued:
                _release_enqueued_submission(submission_id)
            GRADE_QUEUE.task_done()


def recover_interrupted_grade_tasks(conn: sqlite3.Connection) -> list[str]:
    """Mark uncertain calls for manual recovery and pause their batch cursor."""
    interrupted = conn.execute(
        "SELECT id, batch_id, batch_sequence FROM submissions WHERE state = 'grading'"
    ).fetchall()
    conn.execute("UPDATE submissions SET state = 'recovery_pending' WHERE state = 'grading'")
    conn.execute(
        "UPDATE task_queue SET state = 'recovery_pending', updated_at = ? WHERE state = 'running'",
        (utc_now(),),
    )
    for item in interrupted:
        if item["batch_id"]:
            conn.execute(
                "UPDATE grading_batches SET status = 'paused', pause_sequence = ?, pause_reason = '服务重启时模型结果未能确认保存；为避免自动重复调用，等待教师决定', updated_at = ? WHERE id = ? AND next_sequence = ?",
                (item["batch_sequence"], utc_now(), item["batch_id"], item["batch_sequence"]),
            )
    active_batches = conn.execute(
        "SELECT id, next_sequence, manifest_json FROM grading_batches WHERE status = 'active'"
    ).fetchall()
    for batch in active_batches:
        sequence = int(batch["next_sequence"])
        has_submission = conn.execute(
            "SELECT 1 FROM submissions WHERE batch_id = ? AND batch_sequence = ?", (batch["id"], sequence)
        ).fetchone()
        if has_submission:
            continue
        try:
            manifest = json.loads(batch["manifest_json"] or "{}")
            item = next((value for value in manifest.get("items", []) if int(value.get("sequence", 0)) == sequence), None)
        except (TypeError, ValueError, json.JSONDecodeError):
            item = None
        if item and item.get("upload_status") == "reserved":
            _mark_batch_item_upload_status(
                conn,
                str(batch["id"]),
                sequence,
                "failed",
                "服务重启时此序号答卷尚未完整保存",
            )
            conn.execute(
                "UPDATE grading_batches SET status = 'paused', pause_sequence = ?, pause_reason = '服务重启时此序号答卷尚未完整保存，请原位重传或由教师确认跳过', updated_at = ? WHERE id = ? AND next_sequence = ?",
                (sequence, utc_now(), batch["id"], sequence),
            )
    return [str(item["id"]) for item in interrupted]


def start_grade_worker() -> None:
    global QUEUE_THREAD, QUEUE_THREADS
    with QUEUE_THREAD_LOCK:
        QUEUE_THREADS = [thread for thread in QUEUE_THREADS if thread.is_alive()]
        if QUEUE_THREADS:
            QUEUE_THREAD = QUEUE_THREADS[0]
            return
        QUEUE_STOP_EVENT.clear()
        conn = get_db()
        try:
            recover_interrupted_grade_tasks(conn)
            # A task interrupted during a model call is recovery_pending and
            # must not be blindly re-submitted: the provider may already have
            # charged the request. Only ordinary queued tasks auto-resume.
            pending = [row[0] for row in conn.execute(
                "SELECT q.submission_id FROM task_queue q JOIN submissions s ON s.id=q.submission_id "
                "LEFT JOIN grading_batches b ON b.id=s.batch_id WHERE q.state='queued' "
                "ORDER BY COALESCE(b.created_at, s.created_at), CASE WHEN s.batch_id IS NULL THEN s.created_at END, s.batch_sequence"
            )]
            conn.commit()
        finally:
            conn.close()
        QUEUE_THREADS = [
            threading.Thread(
                target=_queue_worker_loop,
                name=f"history-grader-worker-{index + 1}",
                daemon=True,
            )
            for index in range(configured_grade_worker_count())
        ]
        QUEUE_THREAD = QUEUE_THREADS[0] if QUEUE_THREADS else None
        for thread in QUEUE_THREADS:
            thread.start()
        for submission_id in pending:
            if not _enqueue_submission_id(str(submission_id)):
                break


def refill_persisted_grade_queue() -> None:
    """Keep persisted queued work progressing after an in-memory queue fills."""
    if QUEUE_STOP_EVENT.is_set() or BACKUP_IN_PROGRESS or RESTORE_IN_PROGRESS:
        return
    conn = get_db()
    try:
        pending = [
            str(row[0])
            for row in conn.execute(
                "SELECT q.submission_id FROM task_queue q JOIN submissions s ON s.id=q.submission_id "
                "LEFT JOIN grading_batches b ON b.id=s.batch_id WHERE q.state='queued' "
                "ORDER BY COALESCE(b.created_at, s.created_at), CASE WHEN s.batch_id IS NULL THEN s.created_at END, s.batch_sequence LIMIT 100"
            )
        ]
    finally:
        conn.close()
    for submission_id in pending:
        if not _enqueue_submission_id(submission_id):
            break


def enqueue_submission(submission_id: str) -> bool:
    start_grade_worker()
    # The database row is already committed. Leave it queued if the bounded
    # in-memory queue is full; the worker will refill it later.
    return _enqueue_submission_id(submission_id)


def stop_grade_worker(timeout_seconds: float = 5.0) -> None:
    global QUEUE_THREAD, QUEUE_THREADS
    QUEUE_STOP_EVENT.set()
    threads = list(QUEUE_THREADS)
    deadline = time.monotonic() + timeout_seconds
    for thread in threads:
        if not thread.is_alive():
            continue
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)
    QUEUE_THREADS = [thread for thread in threads if thread.is_alive()]
    QUEUE_THREAD = QUEUE_THREADS[0] if QUEUE_THREADS else None


def safe_name(value: str, default: str = "student") -> str:
    value = re.sub(r"[^\w\-\u4e00-\u9fff]+", "_", value.strip(), flags=re.UNICODE)
    return value[:60] or default


def compact_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).casefold()


def normalize_student_no(value: Any) -> str:
    return grading_core.normalize_student_no(value)


def normalize_header(value: Any) -> str:
    return re.sub(r"[\s_\-—–()（）【】\[\]]+", "", str(value or "").strip()).casefold()


def xlsx_column_index(reference: str) -> int:
    letters = re.match(r"([A-Za-z]+)", reference or "")
    if not letters:
        return 0
    result = 0
    for char in letters.group(1).upper():
        result = result * 26 + ord(char) - ord("A") + 1
    return result - 1


def read_xlsx_rows(raw: bytes) -> list[list[str]]:
    """Read the first worksheet of a simple .xlsx roster using the stdlib only."""
    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    package_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in names:
                shared_root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in shared_root.findall(f"{{{main_ns}}}si"):
                    shared_strings.append("".join(item.itertext()))

            workbook_path = "xl/workbook.xml"
            workbook_root = ElementTree.fromstring(archive.read(workbook_path))
            sheet = workbook_root.find(f"{{{main_ns}}}sheets/{{{main_ns}}}sheet")
            if sheet is None:
                raise ValueError("Excel 文件没有可读取的工作表")
            relation_id = sheet.attrib.get(f"{{{rel_ns}}}id", "")
            relation_root = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            target = ""
            for relation in relation_root.findall(f"{{{package_rel_ns}}}Relationship"):
                if relation.attrib.get("Id") == relation_id:
                    target = relation.attrib.get("Target", "")
                    break
            if not target:
                target = "worksheets/sheet1.xml"
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = f"xl/{target}"
            worksheet_root = ElementTree.fromstring(archive.read(target))
            rows: list[list[str]] = []
            for row in worksheet_root.findall(f".//{{{main_ns}}}sheetData/{{{main_ns}}}row"):
                cells: dict[int, str] = {}
                for cell in row.findall(f"{{{main_ns}}}c"):
                    index = xlsx_column_index(cell.attrib.get("r", ""))
                    cell_type = cell.attrib.get("t", "")
                    value_node = cell.find(f"{{{main_ns}}}v")
                    if cell_type == "inlineStr":
                        value = "".join(cell.itertext())
                    elif value_node is None:
                        value = ""
                    else:
                        value = value_node.text or ""
                        if cell_type == "s":
                            try:
                                value = shared_strings[int(value)]
                            except (ValueError, IndexError):
                                value = ""
                    cells[index] = str(value).strip()
                if cells:
                    width = max(cells) + 1
                    rows.append([cells.get(index, "") for index in range(width)])
            return rows
    except (KeyError, ValueError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise ValueError("无法读取 Excel 花名册，请另存为 .xlsx 或 .csv 后重试") from exc


def decode_roster_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("花名册文字编码无法识别，请另存为 UTF-8 CSV")


def parse_roster_upload(filename: str, data_url: str, default_class_name: str = "") -> list[dict[str, str]]:
    raw = data_url_bytes(data_url)
    if len(raw) > MAX_ROSTER_BYTES:
        raise ValueError("花名册文件不能超过10MB")
    suffix = Path(filename or "").suffix.casefold()
    if suffix == ".xlsx":
        rows = read_xlsx_rows(raw)
    else:
        text = decode_roster_text(raw)
        sample = "\n".join(text.splitlines()[:5])
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel_tab if "\t" in sample else csv.excel
        rows = [list(row) for row in csv.reader(io.StringIO(text), dialect)]
    rows = [row for row in rows if any(str(cell).strip() for cell in row)]
    if not rows:
        raise ValueError("花名册为空")
    header = [normalize_header(cell) for cell in rows[0]]

    def find_column(names: set[str]) -> int | None:
        for index, value in enumerate(header):
            if value in names:
                return index
        return None

    name_index = find_column({"姓名", "学生姓名", "学生", "name", "studentname", "student_name", "displayname"})
    class_index = find_column(
        {"班级", "班级名称", "班别", "行政班", "新班", "班", "class", "classname", "class_name"}
    )
    seat_index = find_column({"座号", "序号", "座位号", "seatno", "seat_no", "serialno", "serial_no"})
    code_index = find_column({"identitycode", "身份码"})
    number_index = code_index if code_index is not None else seat_index if seat_index is not None else find_column(
        {"学号", "学生号", "学生编号", "编号", "学籍号", "studentno", "student_no", "studentid", "student_id"}
    )
    if name_index is None:
        raise ValueError("花名册至少需要一列“姓名”")
    default_class_name = str(default_class_name or "").strip()
    if class_index is None and not default_class_name:
        raise ValueError("花名册没有“班级”列，请填写默认班级")

    records: list[dict[str, str]] = []
    current_class_name = default_class_name
    for row in rows[1:]:
        def cell(index: int | None) -> str:
            return str(row[index]).strip() if index is not None and index < len(row) else ""

        student_name = cell(name_index)
        class_name = cell(class_index)
        if class_name:
            current_class_name = class_name
        class_name = current_class_name
        student_no = normalize_student_no(cell(number_index))
        if not student_name and code_index is None:
            continue
        if not class_name:
            raise ValueError(f"学生“{student_name}”缺少班级")
        records.append(
            {"class_name": class_name, "student_no": student_no, "student_name": student_name}
        )
    if not records:
        raise ValueError("没有读取到有效学生记录")
    if len(records) > 3000:
        raise ValueError("单个花名册最多支持3000名学生")
    return records


def clean_criteria(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("criteria 必须是数组")
    criteria: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        if not label:
            continue
        try:
            score = float(item.get("score", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第{index}个评分点分值必须是有效数字") from exc
        if not math.isfinite(score):
            raise ValueError(f"第{index}个评分点分值必须是有限数字")
        if score < 0:
            raise ValueError(f"第{index}个评分点分值不能为负数")
        if score <= 0:
            continue
        criterion_id = str(item.get("id") or f"C{index}").strip()
        if not criterion_id:
            raise ValueError(f"第{index}个评分点 ID 不能为空")
        if any(existing["id"] == criterion_id for existing in criteria):
            raise ValueError(f"同一小题中评分点 ID 重复：{criterion_id}")
        keywords = item.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [x.strip() for x in keywords.split(",") if x.strip()]
        if not isinstance(keywords, list):
            keywords = []
        criteria.append(
            {
                "id": criterion_id,
                "label": label,
                "score": score,
                "keywords": [str(x).strip() for x in keywords if str(x).strip()],
                "note": str(item.get("note", "")).strip(),
            }
        )
    if not criteria:
        raise ValueError("至少需要一个有效评分点")
    return criteria


def clean_sub_questions(
    raw: Any,
    fallback_question: str = "",
    fallback_answer: str = "",
    fallback_criteria: Any = None,
) -> list[dict[str, Any]]:
    """Normalize a large question into sub-question -> criterion hierarchy."""
    if raw is None:
        criteria = clean_criteria(fallback_criteria)
        return [
            {
                "id": "Q1",
                "label": "本题",
                "question": fallback_question,
                "standard_answer": fallback_answer,
                "criteria": criteria,
                "scoring_mode": "weighted",
                "point_value": 2.0,
                "max_score": total_score(criteria),
            }
        ]
    if not isinstance(raw, list) or not raw:
        raise ValueError("sub_questions 必须是非空数组")
    sub_questions: list[dict[str, Any]] = []
    seen_sub_question_ids: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        sub_id = str(item.get("id") or f"Q{index}").strip()
        if not sub_id:
            raise ValueError(f"第{index}道小题 ID 不能为空")
        if sub_id in seen_sub_question_ids:
            raise ValueError(f"小题 ID 重复：{sub_id}")
        seen_sub_question_ids.add(sub_id)
        label = str(item.get("label") or f"（{index}）").strip()
        question = str(item.get("question", "")).strip()
        standard_answer = str(item.get("standard_answer", "")).strip()
        if item.get('scoring_mode') == 'holistic':
            from holistic_draft import validate
            config = validate(item.get('holistic'), item.get('max_score'))
            sub_questions.append(dict(id=sub_id, label=label, question=question, standard_answer=standard_answer,
                criteria=[], max_score=float(item['max_score']), scoring_mode='holistic', holistic=config))
            continue
        criteria = clean_criteria(item.get("criteria", []))
        explicit_scoring_mode = item.get("scoring_mode") not in (None, "") or item.get("score_mode") not in (None, "")
        scoring_mode = str(item.get("scoring_mode", item.get("score_mode", "weighted"))).strip().casefold()
        if scoring_mode not in {"weighted", "point_bank"}:
            raise ValueError(f"{label}的评分方式无效")
        if scoring_mode == "point_bank":
            point_labels: set[str] = set()
            for criterion in criteria:
                label_key = re.sub(r"[\W_]+", "", criterion["label"].casefold())
                if label_key in point_labels:
                    raise ValueError(f"{label}存在重复评分点，请合并相同或近义要点")
                point_labels.add(label_key)
        try:
            raw_point_value = item.get("point_value", 2)
            point_value = float(2 if raw_point_value in (None, "") else raw_point_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}的每点分值必须是有效数字") from exc
        if not math.isfinite(point_value) or point_value <= 0:
            raise ValueError(f"{label}的每点分值必须大于0")
        raw_max_score = item.get("max_score")
        try:
            max_score = float(raw_max_score) if raw_max_score not in (None, "") else None
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}的满分必须是有效数字") from exc
        if max_score is not None and (not math.isfinite(max_score) or max_score <= 0):
            # Weighted legacy questions derive their maximum from criterion
            # weights; historically an absent/invalid stored max was ignored.
            # Point-bank questions require a valid explicit cap below.
            if explicit_scoring_mode:
                raise ValueError(f"{label}的满分必须是有限正数")
            max_score = None
        if scoring_mode == "point_bank" and max_score is None:
            raise ValueError(f"{label}使用按得分点封顶时，必须填写本小题满分")
        if scoring_mode == "point_bank":
            # Point-bank scoring uses one configured value for every distinct
            # answer point; custom per-criterion weights belong to weighted mode.
            from auto_crop_double_review_poc import require_consistent_point_bank_values
            require_consistent_point_bank_values({
                "id": sub_id, "scoring_mode": scoring_mode,
                "point_value": point_value, "criteria": criteria,
            })
            criteria = [{**criterion, "score": point_value} for criterion in criteria]
        # Preserve a teacher-declared cap for explicit modes. Only legacy rows
        # without a mode keep their historical criterion-sum interpretation.
        if max_score is None or (scoring_mode == "weighted" and not explicit_scoring_mode):
            max_score = total_score(criteria)
        sub_questions.append(
            {
                "id": sub_id,
                "label": label,
                "question": question,
                "standard_answer": standard_answer,
                "criteria": criteria,
                "scoring_mode": scoring_mode,
                "point_value": point_value,
                "max_score": max_score,
            }
        )
    if not sub_questions:
        raise ValueError("至少需要一个有效小题")
    return sub_questions


def total_sub_questions(sub_questions: list[dict[str, Any]]) -> float:
    return grading_core.total_sub_questions(sub_questions)


def resolve_grading_scope(
    sub_questions: list[dict[str, Any]],
    answer_regions: list[dict[str, Any]] | None = None,
    graded_question_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve this run's rubric subset; absent scope retains whole-exam behavior."""
    by_id = {str(item.get("id") or ""): item for item in sub_questions if item.get("id")}
    if graded_question_ids is not None:
        requested_ids = [str(value) for value in graded_question_ids]
        source = "explicit"
    elif answer_regions and all(item.get("invocation_scope") == "answer_region" for item in answer_regions):
        requested_ids = []
        source = "answer_regions"
        for region in answer_regions:
            region_ids = region.get("sub_question_ids") or []
            if len(region_ids) != 1:
                raise ValueError("答题区评分范围必须且只能关联一道小题")
            requested_ids.append(str(region_ids[0]))
    else:
        requested_ids = list(by_id)
        source = "whole_exam"

    if not requested_ids or len(set(requested_ids)) != len(requested_ids):
        raise ValueError("本次评分范围为空或包含重复题号")
    unknown_ids = sorted(set(requested_ids) - set(by_id))
    if unknown_ids:
        raise ValueError(f"评分范围引用了未配置的小题：{', '.join(unknown_ids)}")
    selected_ids = set(requested_ids)
    scoped_questions = [item for item in sub_questions if str(item.get("id") or "") in selected_ids]
    metadata = {
        "graded_question_ids": [str(item.get("id") or "") for item in scoped_questions],
        "scoped_max_score": total_sub_questions(scoped_questions),
        "source": source,
    }
    return scoped_questions, metadata


def withhold_incomplete_dual_score(result: dict[str, Any]) -> dict[str, Any]:
    if isinstance(result.get("dual_review"), dict) and result["dual_review"].get("status") == "degraded":
        result["suggested_total"] = None
    return result


def apply_grading_scope_to_result(
    result: dict[str, Any],
    scoped_questions: list[dict[str, Any]],
    scope_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Keep an answer-region result, its two model totals, and risk comparison in one scope."""
    scoped_ids = {str(item.get("id") or "") for item in scoped_questions}
    scope = {
        "graded_question_ids": [str(item.get("id") or "") for item in scoped_questions],
        "scoped_max_score": total_sub_questions(scoped_questions),
        "source": str(scope_metadata.get("source") or "explicit"),
    }
    result["grading_scope"] = scope
    result["max_total"] = scope["scoped_max_score"]
    if isinstance(result.get("sub_questions"), list):
        result["sub_questions"] = [
            item for item in result["sub_questions"]
            if str(item.get("sub_question_id") or item.get("id") or "") in scoped_ids
        ]

    dual_review = result.get("dual_review")
    if not isinstance(dual_review, dict):
        return result
    import blank_consensus
    blank_was_valid = blank_consensus.valid(result)
    dual_review["grading_scope"] = scope
    dual_review["question_ids"] = list(scope["graded_question_ids"])
    dual_review["rubric_hash"] = rubric_fingerprint(scoped_questions)
    side_results = dual_review.get("results") if isinstance(dual_review.get("results"), dict) else {}
    for side_result in side_results.values():
        if not isinstance(side_result, dict):
            continue
        if isinstance(side_result.get("sub_questions"), list):
            side_result["sub_questions"] = [
                item for item in side_result["sub_questions"]
                if str(item.get("sub_question_id") or item.get("id") or "") in scoped_ids
            ]
        side_result["grading_scope"] = scope
        side_result["max_total"] = scope["scoped_max_score"]
        scoped_results = side_result.get("sub_questions") or []
        if (
            len(scoped_results) == len(scoped_questions)
            and all(
                (item.get("answer_status") == "answered" or (item.get("answer_status") == "blank_confirmed" and item.get("blank_confirmation_method") == "automatic_blank_zero"))
                and item.get("suggested_total") is not None
                for item in scoped_results
            )
        ):
            side_result["suggested_total"] = sum(float(item["suggested_total"]) for item in scoped_results)
        else:
            side_result["suggested_total"] = None

    if isinstance(side_results.get("A"), dict) and isinstance(side_results.get("B"), dict):
        previous_comparison = dual_review.get("comparison") if isinstance(dual_review.get("comparison"), dict) else {}
        comparison = compare_dual_review_results(side_results["A"], side_results["B"])
        comparison["review_reasons"] = sorted(
            set(comparison.get("review_reasons") or [])
            | set(previous_comparison.get("review_reasons") or [])
        )
        risk_rank = {"NO_CONFLICT": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
        old_risk = str(previous_comparison.get("risk_level") or "NO_CONFLICT")
        new_risk = str(comparison.get("risk_level") or "NO_CONFLICT")
        comparison["risk_level"] = max((old_risk, new_risk), key=lambda value: risk_rank.get(value, 3))
        dual_review["comparison"] = comparison
    base_side = dual_review.get("base_side")
    base_result = side_results.get(base_side) if base_side else None
    result["suggested_total"] = (
        base_result.get("suggested_total")
        if isinstance(base_result, dict)
        else None
    )
    if blank_was_valid:
        dual_review["blank_resolution"]["effective_results_sha256"] = blank_consensus.digest(dual_review["results"])
    return withhold_incomplete_dual_score(result)


def prepare_submission_scope_reconfirmation(
    result: dict[str, Any],
    sub_questions: list[dict[str, Any]],
    graded_question_ids: list[str],
    *,
    source: str,
    previous_final_total: float,
    previous_max_total: float,
    previous_confirmed_at: str | None,
) -> dict[str, Any]:
    """Attach a corrected run scope while keeping an old teacher final historical."""
    if not isinstance(result, dict):
        raise ValueError("批改结果无效")
    scoped_questions, calculated = resolve_grading_scope(
        sub_questions,
        graded_question_ids=graded_question_ids,
    )
    scope = {
        "graded_question_ids": calculated["graded_question_ids"],
        "scoped_max_score": calculated["scoped_max_score"],
        "source": str(source or "explicit"),
    }
    result["grading_scope"] = scope
    result["max_total"] = scope["scoped_max_score"]
    result["teacher_reconfirmation_required"] = True
    # Keep the previous score and denominator intact for audit/display. The
    # current final is not replaced until a teacher explicitly confirms again.
    result["previous_teacher_final"] = {
        "final_total": float(previous_final_total),
        "max_total": float(previous_max_total),
        "confirmed_at": previous_confirmed_at,
    }
    dual_review = result.get("dual_review")
    if isinstance(dual_review, dict):
        dual_review["grading_scope"] = scope
    return scope


def scoring_warnings(sub_questions: list[dict[str, Any]]) -> list[str]:
    """Return teacher-facing warnings when a point bank cannot reach full marks."""
    warnings: list[str] = []
    for item in sub_questions:
        if str(item.get("scoring_mode", "weighted")) != "point_bank":
            continue
        maximum = float(item["max_score"])
        point_value = float(item["point_value"])
        required = math.ceil(maximum / point_value)
        available = len(item.get("criteria", []))
        if available < required:
            warnings.append(
                f"{item['label']}：目前只有 {available} 个候选要点，按每点 {point_value:g} 分需至少 {required} 个才能达到 {maximum:g} 分满分。"
            )
    return warnings


def flatten_criteria(sub_questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened = []
    for sub_question in sub_questions:
        for criterion in sub_question["criteria"]:
            flattened.append(
                {
                    **criterion,
                    "id": f"{sub_question['id']}-{criterion['id']}",
                    "sub_question_id": sub_question["id"],
                }
            )
    return flattened


def bytes_to_data_url(raw: bytes, media_type: str) -> str:
    safe_media_type = str(media_type or "application/octet-stream").split(";", 1)[0].strip().casefold()
    return f"data:{safe_media_type};base64,{base64.b64encode(raw).decode('ascii')}"


def _multipart_boundary(content_type: str) -> bytes:
    """Read and validate only the multipart boundary parameter."""
    header = Message()
    header["Content-Type"] = str(content_type)
    boundary = header.get_param("boundary", header="Content-Type")
    if not boundary:
        raise ValueError("批改上传的 multipart 边界缺失")
    boundary = str(boundary)
    if len(boundary) > 200 or "\r" in boundary or "\n" in boundary:
        raise ValueError("批改上传的 multipart 边界无效")
    try:
        return boundary.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("批改上传的 multipart 边界必须是ASCII") from exc


def _iter_multipart_parts(body: bytes, boundary: bytes) -> list[tuple[bytes, memoryview]]:
    """Split raw multipart bytes without materializing a second full body."""
    delimiter = b"--" + boundary
    first = body.find(delimiter)
    if first < 0:
        raise ValueError("批改上传的 multipart 格式无效")
    parts: list[tuple[bytes, memoryview]] = []
    position = first
    saw_closing = False
    while position < len(body):
        after_delimiter = position + len(delimiter)
        if body.startswith(b"--", after_delimiter):
            saw_closing = True
            break
        if body.startswith(b"\r\n", after_delimiter):
            part_start = after_delimiter + 2
        elif body.startswith(b"\n", after_delimiter):
            part_start = after_delimiter + 1
        else:
            raise ValueError("批改上传的 multipart 边界格式无效")
        header_end = body.find(b"\r\n\r\n", part_start)
        header_separator_size = 4
        if header_end < 0:
            header_end = body.find(b"\n\n", part_start)
            header_separator_size = 2
        if header_end < 0:
            raise ValueError("批改上传的 multipart 部件头无效")
        if header_end - part_start > MAX_MULTIPART_HEADER_BYTES:
            raise ValueError("批改上传的 multipart 部件头过大")
        payload_start = header_end + header_separator_size
        next_crlf = body.find(b"\r\n" + delimiter, payload_start)
        next_lf = body.find(b"\n" + delimiter, payload_start)
        if next_crlf >= 0 and (next_lf < 0 or next_crlf <= next_lf):
            payload_end = next_crlf
            next_position = next_crlf + 2
        elif next_lf >= 0:
            payload_end = next_lf
            next_position = next_lf + 1
        else:
            raise ValueError("批改上传的 multipart 缺少结束边界")
        parts.append((body[part_start:header_end], memoryview(body)[payload_start:payload_end]))
        position = next_position
    if not saw_closing:
        raise ValueError("批改上传的 multipart 缺少结束边界")
    return parts


def parse_multipart_submission(body: bytes, content_type: str) -> dict[str, Any]:
    """Parse a submission without putting duplicate Base64 images on the wire."""
    boundary = _multipart_boundary(content_type)
    metadata: dict[str, Any] | None = None
    file_parts: dict[str, tuple[memoryview, str]] = {}
    for header_bytes, payload in _iter_multipart_parts(body, boundary):
        header = BytesParser(policy=email_policy.default).parsebytes(
            header_bytes + b"\r\n\r\n"
        )
        field_name = header.get_param("name", header="Content-Disposition")
        if not field_name:
            continue
        field_name = str(field_name)
        if field_name == "metadata":
            if len(payload) > MAX_MULTIPART_METADATA_BYTES:
                raise ValueError("批改上传的元数据过大")
            try:
                parsed = json.loads(bytes(payload).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("批改上传的元数据不是有效 JSON") from exc
            if not isinstance(parsed, dict):
                raise ValueError("批改上传的元数据必须是对象")
            metadata = parsed
        elif re.fullmatch(r"(?:processed|original)_\d+", field_name):
            if len(payload) > MAX_SINGLE_IMAGE_BYTES:
                raise ValueError("单张照片不能超过25MiB")
            file_parts[field_name] = (payload, header.get_content_type())
    if metadata is None:
        raise ValueError("批改上传缺少元数据")
    photos = metadata.get("photos", [])
    if not isinstance(photos, list) or len(photos) > MAX_IMAGES_PER_SUBMISSION:
        raise ValueError(f"单份作业最多上传{MAX_IMAGES_PER_SUBMISSION}张照片，请逐份处理")
    normalized_photos: list[dict[str, Any]] = []
    for index, descriptor in enumerate(photos):
        if not isinstance(descriptor, dict):
            raise ValueError("照片元数据格式无效")
        processed = file_parts.get(f"processed_{index}")
        original = file_parts.get(f"original_{index}")
        if not processed or not original:
            raise ValueError(f"第{index + 1}张照片上传不完整，请重试")
        processed_bytes, processed_type = processed
        original_bytes, original_type = original
        if not processed_type.casefold().startswith("image/") or not original_type.casefold().startswith("image/"):
            raise ValueError("上传内容必须是图片")
        normalized_photos.append(
            {
                "name": str(descriptor.get("name") or f"page_{index + 1:02d}.jpg"),
                "corners": descriptor.get("corners"),
                "ai_redactions": normalize_ai_redactions(descriptor.get("ai_redactions", [])),
                "image_role": str(descriptor.get("image_role") or ""),
                "answer_region_attestation": str(descriptor.get("answer_region_attestation") or ""),
                "page_sequence": int(descriptor.get("page_sequence", index + 1)),
                # Keep multipart bytes as zero-copy views. Converting them to
                # bytes or Base64 here would duplicate the request body.
                "_processed_raw_bytes": processed_bytes,
                "_processed_media_type": processed_type,
                "_original_raw_bytes": original_bytes,
                "_original_media_type": original_type,
            }
        )
    page_sequences = [item["page_sequence"] for item in normalized_photos]
    if sorted(page_sequences) != list(range(1, len(normalized_photos) + 1)):
        raise ValueError("多页答卷页序必须从1开始且不可重复")
    normalized_photos.sort(key=lambda item: item["page_sequence"])
    result = dict(metadata)
    result["photos"] = normalized_photos
    return result


def parse_student_auto_crop_upload(body: bytes, content_type: str) -> dict[str, Any]:
    """Parse one local diagnostic image without duplicating it in multipart fields."""
    boundary = _multipart_boundary(content_type)
    metadata: dict[str, Any] | None = None
    image_part: tuple[bytes, str, str] | None = None
    for header_bytes, payload in _iter_multipart_parts(body, boundary):
        header = BytesParser(policy=email_policy.default).parsebytes(header_bytes + b"\r\n\r\n")
        field_name = header.get_param("name", header="Content-Disposition")
        if not field_name:
            continue
        field_name = str(field_name)
        if field_name == "metadata":
            if len(payload) > MAX_MULTIPART_METADATA_BYTES:
                raise ValueError("自动裁切上传元数据过大")
            try:
                parsed = json.loads(bytes(payload).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("自动裁切上传元数据不是有效 JSON") from exc
            if not isinstance(parsed, dict):
                raise ValueError("自动裁切上传元数据必须是对象")
            metadata = parsed
        elif field_name == "student_image":
            if image_part is not None:
                raise ValueError("一次只能上传一张学生答卷照片")
            if len(payload) > MAX_SINGLE_IMAGE_BYTES:
                raise ValueError("单张答卷照片不能超过25MiB")
            media_type = str(header.get_content_type() or "").casefold()
            filename = str(header.get_filename() or "student_image.jpg")
            if not media_type.startswith("image/"):
                raise ValueError("上传内容必须是图片")
            image_part = (bytes(payload), media_type, filename)
        elif header.get_filename() is not None:
            raise ValueError("只接受一个 student_image 图片字段")
    if metadata is None or image_part is None:
        raise ValueError("上传缺少考试信息或学生答卷照片")
    image_bytes, media_type, filename = image_part
    suffix = Path(PureWindowsPath(filename).name).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(media_type, ".img")
    return {**metadata, "student_image_bytes": image_bytes, "source_extension": suffix}


def data_url_bytes(data_url: str, require_image: bool = False) -> bytes:
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        raise ValueError("照片必须是 data URL")
    try:
        header, encoded = data_url.split(",", 1)
        if ";base64" not in header.casefold():
            raise ValueError("照片必须使用 base64 编码")
        media_type = header[5:].split(";", 1)[0].strip().casefold()
        if require_image and not media_type.startswith("image/"):
            raise ValueError("上传内容必须是图片")
        if require_image and media_type in {"image/svg", "image/svg+xml"}:
            raise ValueError("不支持 SVG 图片，请上传 JPG/PNG 等位图")
        # A 25 MiB decoded image is roughly 33.4 MiB as Base64. Rejecting an
        # obviously oversized string before decoding avoids a second memory
        # spike, while the decoded byte count below remains authoritative.
        if len(encoded) > ((MAX_SINGLE_IMAGE_BYTES + 2) * 4 // 3) + 16:
            raise ValueError("单张照片不能超过25MiB")
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        if str(exc) in {
            "单张照片不能超过25MiB",
            "上传内容必须是图片",
            "不支持 SVG 图片，请上传 JPG/PNG 等位图",
            "照片必须使用 base64 编码",
        }:
            raise
        raise ValueError("照片数据无效") from exc
    except base64.binascii.Error as exc:
        raise ValueError("照片数据无效") from exc
    if len(raw) > MAX_SINGLE_IMAGE_BYTES:
        raise ValueError("单张照片不能超过25MiB")
    return raw


SUPPORTED_IMAGE_MIME = {
    "JPEG": {"image/jpeg", "image/jpg", "image/pjpeg"},
    "PNG": {"image/png"},
    "WEBP": {"image/webp"},
}


def data_url_media_type(data_url: str) -> str:
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        raise ValueError("照片必须是 data URL")
    return data_url.split(",", 1)[0][5:].split(";", 1)[0].strip().casefold()


def _validate_upload_media_type(media_type: str) -> str:
    normalized = str(media_type or "").split(";", 1)[0].strip().casefold()
    if not normalized.startswith("image/"):
        raise ValueError("上传内容必须是图片")
    if normalized in {"image/svg", "image/svg+xml"}:
        raise ValueError("不支持 SVG 图片，请上传 JPG/PNG 等位图")
    return normalized


def photo_upload_bytes(
    item: dict[str, Any], original: bool = False
) -> tuple[bytes | bytearray | memoryview, str]:
    """Resolve one uploaded image without re-encoding multipart payloads.

    Multipart submissions carry raw bytes under private keys. Legacy JSON
    submissions still use data URLs and are decoded exactly once per call.
    """
    raw_key = "_original_raw_bytes" if original else "_processed_raw_bytes"
    media_key = "_original_media_type" if original else "_processed_media_type"
    raw = item.get(raw_key)
    if isinstance(raw, (bytes, bytearray, memoryview)):
        # A memoryview from multipart parsing keeps the original request body
        # alive without copying the image. Decoders and hashes accept views.
        return raw, _validate_upload_media_type(str(item.get(media_key) or ""))
    if original:
        data_url = str(item.get("original_data_url") or item.get("data_url") or "")
    else:
        data_url = str(item.get("data_url") or "")
    return data_url_bytes(data_url, require_image=True), _validate_upload_media_type(data_url_media_type(data_url))


def processing_decode_size(width: int, height: int) -> tuple[int, int]:
    """Return a bounded decode target while preserving the source aspect ratio."""
    longest = max(width, height)
    if longest <= PROCESSING_IMAGE_LONG_EDGE:
        return width, height
    scale = PROCESSING_IMAGE_LONG_EDGE / longest
    return max(1, round(width * scale)), max(1, round(height * scale))


def jpeg_draft_size(width: int, height: int) -> tuple[int, int] | None:
    """Pick the smallest power-of-two JPEG draft that stays usable."""
    candidates = [(width, height)]
    scale = 2
    while scale <= 128:
        candidate = (max(1, round(width / scale)), max(1, round(height / scale)))
        if max(candidate) < MIN_JPEG_DRAFT_LONG_EDGE:
            break
        candidates.append(candidate)
        scale *= 2
    viable = [item for item in candidates if item[0] * item[1] <= MAX_FULL_DECODE_PIXELS]
    if not viable:
        return None
    return min(viable, key=lambda item: item[0] * item[1])


def decode_image_for_processing(raw: bytes, declared_media_type: str) -> tuple[Image.Image, tuple[int, int]]:
    """Validate headers, then decode only within the 384MiB service budget.

    JPEG uses Pillow's decoder-side draft scale. Other formats have no safe
    equivalent, so inputs above the full-decode budget are rejected before
    Image.load() can allocate their entire RGB buffer.
    """
    actual_format, (width, height) = validate_image_bytes(raw, declared_media_type)
    if actual_format != "JPEG" and width * height > MAX_NON_JPEG_DECODE_PIXELS:
        raise ValueError(
            f"{actual_format} 大图超过1500万像素的安全解码上限；请转换为JPG后再上传，或先在手机上缩小图片"
        )
    draft_target = jpeg_draft_size(width, height) if actual_format == "JPEG" else None
    if actual_format == "JPEG" and draft_target is None:
        raise ValueError(
            "JPG 大图无法在安全内存内完成缩放；请先缩小图片或降低分辨率后重试"
        )
    try:
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            with Image.open(io.BytesIO(raw)) as source:
                if actual_format == "JPEG" and draft_target != (width, height):
                    source.draft("RGB", draft_target)
                decoded_pixels = source.width * source.height
                if decoded_pixels > MAX_FULL_DECODE_PIXELS:
                    raise ValueError(
                        "JPG 大图的安全预缩放未生效，已拒绝整幅解码；请先缩小图片后重试"
                    )
                source.load()
                orientation = int(source.getexif().get(274, 1) or 1)
                image = ImageOps.exif_transpose(source).convert("RGB")
        if any("decompression bomb" in str(item.message).casefold() for item in caught_warnings):
            raise ValueError("图片疑似解压炸弹，已拒绝处理")
        if max(image.width, image.height) > PROCESSING_IMAGE_LONG_EDGE:
            scale = PROCESSING_IMAGE_LONG_EDGE / max(image.width, image.height)
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
        original_size = (height, width) if orientation in {5, 6, 7, 8} else (width, height)
        return image, original_size
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无法读取或安全解码照片，请重新拍摄或转换为 JPG、PNG、WEBP") from exc


def validate_image_bytes(raw: bytes, declared_media_type: str) -> tuple[str, tuple[int, int]]:
    """Verify bitmap format and decode bounds without allocating pixel data."""
    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                actual_format = str(image.format or "").upper()
                width, height = image.size
                if actual_format not in SUPPORTED_IMAGE_MIME:
                    raise ValueError("只支持 JPG、PNG 或 WEBP 位图；请先将照片转换为这些格式")
                if declared_media_type.casefold() not in SUPPORTED_IMAGE_MIME[actual_format]:
                    raise ValueError("图片声明格式与实际文件格式不一致")
                if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("图片解码后的总像素超过4000万，已拒绝处理")
                if max(width, height) > MAX_IMAGE_DIMENSION:
                    raise ValueError("图片单边尺寸超过12000像素，已拒绝处理")
                if any("decompression bomb" in str(item.message).casefold() for item in caught_warnings):
                    raise ValueError("图片疑似解压炸弹，已拒绝处理")
                image.verify()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("无法安全解析原图或处理图，请重新拍摄或转换为 JPG、PNG、WEBP") from exc
    if any("decompression bomb" in str(item.message).casefold() for item in caught_warnings):
        raise ValueError("图片疑似解压炸弹，已拒绝处理")
    return actual_format, (width, height)


ANSWER_REGION_MIN_EDGE = 48
ANSWER_REGION_MIN_PIXELS = 4096


def validate_answer_region_image(
    raw: bytes | bytearray | memoryview,
    media_type: str,
    provenance: dict[str, Any],
    *,
    expected_sha256: str | None = None,
    expected_dimensions: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Validate a trusted answer crop without applying whole-page heuristics."""
    if not raw:
        raise ValueError("answer_region_invalid:empty_file")
    try:
        _, (width, height) = validate_image_bytes(raw, media_type)
    except ValueError as exc:
        raise ValueError("answer_region_invalid:decode_failed") from exc
    if width < ANSWER_REGION_MIN_EDGE or height < ANSWER_REGION_MIN_EDGE or width * height < ANSWER_REGION_MIN_PIXELS:
        raise ValueError("answer_region_invalid:too_small")
    required_text = (
        "exam_id", "assignment_id", "question_id", "layout_profile_id",
        "source_pdf_sha256", "reference_page_sha256", "source_student_image_sha256",
        "alignment_run_id", "crop_sha256", "alignment_status", "quality_gate",
        "crop_status", "teacher_crop_quality",
    )
    if any(not str(provenance.get(key) or "").strip() for key in required_text):
        raise ValueError("answer_region_invalid:provenance_missing")
    for key in ("source_pdf_sha256", "reference_page_sha256", "source_student_image_sha256", "crop_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get(key) or "")):
            raise ValueError("answer_region_invalid:provenance_hash_invalid")
    if not re.fullmatch(r"[0-9a-f]{32}", str(provenance.get("alignment_run_id") or "")):
        raise ValueError("answer_region_invalid:alignment_run_missing")
    try:
        page_number = int(provenance.get("page_number"))
        crop_width = int(provenance.get("crop_width"))
        crop_height = int(provenance.get("crop_height"))
        crop_bytes = int(provenance.get("crop_bytes"))
    except (TypeError, ValueError) as exc:
        raise ValueError("answer_region_invalid:provenance_dimensions_missing") from exc
    if page_number < 1 or crop_width < ANSWER_REGION_MIN_EDGE or crop_height < ANSWER_REGION_MIN_EDGE or crop_bytes <= 0:
        raise ValueError("answer_region_invalid:provenance_dimensions_invalid")
    for key in ("configured_bbox", "padding", "effective_crop_bbox", "pixel_bbox"):
        if not isinstance(provenance.get(key), dict) or not provenance[key]:
            raise ValueError("answer_region_invalid:roi_provenance_missing")
    if provenance.get("alignment_status") != "aligned" or provenance.get("quality_gate") != "passed":
        raise ValueError("answer_region_invalid:alignment_not_passed")
    if provenance.get("crop_status") != "passed" or provenance.get("teacher_crop_quality") not in {"good", "minor_issue"}:
        raise ValueError("answer_region_invalid:crop_not_accepted")
    actual_hash = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and not secrets.compare_digest(actual_hash, expected_sha256):
        raise ValueError("answer_region_invalid:crop_hash_mismatch")
    if expected_dimensions and (width, height) != tuple(expected_dimensions):
        raise ValueError("answer_region_invalid:crop_dimensions_mismatch")
    if expected_sha256 and actual_hash != str(provenance.get("crop_sha256")):
        raise ValueError("answer_region_invalid:provenance_hash_mismatch")
    if expected_sha256 and len(raw) != crop_bytes:
        raise ValueError("answer_region_invalid:provenance_size_mismatch")
    if expected_sha256 and (width, height) != (crop_width, crop_height):
        raise ValueError("answer_region_invalid:provenance_dimensions_mismatch")
    return {"ok": True, "issues": [], "metrics": {"width": width, "height": height, "role": "answer_region"}}


def assess_photo_quality(image: Image.Image) -> dict[str, Any]:
    """Screen out obviously unusable pages before sending them to a VLM.

    This is intentionally conservative: classical image metrics are useful
    guardrails, not a proof that handwriting is readable. Borderline images
    are left for the model/evidence gate and teacher review.
    """
    width, height = image.size
    sample_scale = min(1.0, QUALITY_SAMPLE_EDGE / max(width, height))
    if sample_scale < 1:
        sample = image.resize(
            (
                max(1, round(width * sample_scale)),
                max(1, round(height * sample_scale)),
            ),
            Image.Resampling.BILINEAR,
        )
    else:
        sample = image
    gray = ImageOps.grayscale(sample)
    stats = ImageStat.Stat(gray)
    mean = float(stats.mean[0])
    contrast = float(stats.stddev[0])
    laplace_kernel = ImageFilter.Kernel(
        (3, 3),
        (-1, -1, -1, -1, 8, -1, -1, -1, -1),
        scale=1,
        offset=128,
    )
    laplacian_variance = float(ImageStat.Stat(gray.filter(laplace_kernel)).var[0])
    issues: list[str] = []
    min_edge = min(width, height)
    if min_edge < MIN_IMAGE_EDGE:
        issues.append(f"图片分辨率过低（短边仅 {min_edge}px）")
    if laplacian_variance < MIN_LAPLACIAN_VARIANCE:
        issues.append("图片整体清晰度不足，疑似失焦或严重运动模糊")
    if contrast < MIN_IMAGE_CONTRAST:
        issues.append("图片对比度过低，文字可能无法辨认")
    if (mean < 28 or mean > 252) and contrast < 18:
        issues.append("图片过暗或过曝，文字可能无法辨认")
    return {
        "ok": not issues,
        "issues": issues,
        "metrics": {
            "width": width,
            "height": height,
            "mean_brightness": round(mean, 2),
            "contrast": round(contrast, 2),
            "laplacian_variance": round(laplacian_variance, 2),
        },
    }


def normalize_photo_corners(raw: Any) -> list[tuple[float, float]] | None:
    """Validate optional normalized TL,TR,BR,BL corners from a capture client."""
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError("透视矫正角点必须正好包含左上、右上、右下、左下四个点")
    corners: list[tuple[float, float]] = []
    for point in raw:
        if isinstance(point, dict):
            values = (point.get("x"), point.get("y"))
        elif isinstance(point, (list, tuple)) and len(point) == 2:
            values = (point[0], point[1])
        else:
            raise ValueError("透视矫正角点格式无效")
        try:
            x, y = float(values[0]), float(values[1])
        except (TypeError, ValueError) as exc:
            raise ValueError("透视矫正角点坐标无效") from exc
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("透视矫正角点必须使用0到1之间的归一化坐标")
        corners.append((x, y))
    if len({(round(x, 6), round(y, 6)) for x, y in corners}) != 4:
        raise ValueError("透视矫正角点不能重复")
    return corners


def rectify_image(image: Image.Image, corners: list[tuple[float, float]] | None) -> tuple[Image.Image, dict[str, Any]]:
    if corners is None:
        return image, {"type": "exif_transpose_and_resize", "version": 1}
    # The capture client uses the intuitive TL, TR, BR, BL order. Pillow's
    # QUAD transform expects TL, BL, BR, TR instead.
    vectors = [
        (corners[(index + 1) % 4][0] - corners[index][0], corners[(index + 1) % 4][1] - corners[index][1])
        for index in range(4)
    ]
    cross_products = [
        vectors[index][0] * vectors[(index + 1) % 4][1]
        - vectors[index][1] * vectors[(index + 1) % 4][0]
        for index in range(4)
    ]
    if any(abs(value) < 1e-6 for value in cross_products) or not (
        all(value > 0 for value in cross_products) or all(value < 0 for value in cross_products)
    ):
        raise ValueError("透视矫正四角必须组成不交叉的凸四边形")
    polygon_area = abs(
        sum(corners[index][0] * corners[(index + 1) % 4][1] for index in range(4))
        - sum(corners[index][1] * corners[(index + 1) % 4][0] for index in range(4))
    ) / 2
    if polygon_area < 1e-4:
        raise ValueError("透视矫正四角围成的区域过小")
    pillow_corners = (corners[0], corners[3], corners[2], corners[1])
    source_quad = tuple(
        value for point in pillow_corners for value in (point[0] * image.width, point[1] * image.height)
    )
    top_width = ((corners[1][0] - corners[0][0]) ** 2 + (corners[1][1] - corners[0][1]) ** 2) ** 0.5 * image.width
    bottom_width = ((corners[2][0] - corners[3][0]) ** 2 + (corners[2][1] - corners[3][1]) ** 2) ** 0.5 * image.width
    left_height = ((corners[3][0] - corners[0][0]) ** 2 + (corners[3][1] - corners[0][1]) ** 2) ** 0.5 * image.height
    right_height = ((corners[2][0] - corners[1][0]) ** 2 + (corners[2][1] - corners[1][1]) ** 2) ** 0.5 * image.height
    output_size = (max(1, round(max(top_width, bottom_width))), max(1, round(max(left_height, right_height))))
    if output_size[0] < 100 or output_size[1] < 100:
        raise ValueError("透视矫正后的答卷区域过小，请重新选择四角")
    transformed = image.transform(output_size, Image.Transform.QUAD, source_quad, resample=Image.Resampling.BICUBIC)
    return transformed, {"type": "manual_quad", "version": 1, "corners": corners}


def save_photo(
    data_url: str,
    target: Path,
    original_target: Path | None = None,
    corners: Any = None,
    original_data_url: str | None = None,
) -> dict[str, Any]:
    raw = data_url_bytes(data_url, require_image=True)
    media_type = data_url_media_type(data_url)
    if original_data_url is None or original_data_url == data_url:
        original_raw = raw
        original_media_type = media_type
    else:
        original_raw = data_url_bytes(original_data_url, require_image=True)
        original_media_type = data_url_media_type(original_data_url)
    return save_photo_bytes(
        raw, media_type, target, original_target, corners, original_raw, original_media_type
    )


def save_photo_item(
    item: dict[str, Any],
    target: Path,
    original_target: Path | None = None,
    corners: Any = None,
    *,
    image_role: str = "source_page",
    answer_region_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist one normalized photo item without a Base64 round trip."""
    raw, media_type = photo_upload_bytes(item)
    original_raw, original_media_type = photo_upload_bytes(item, original=True)
    return save_photo_bytes(
        raw, media_type, target, original_target, corners, original_raw, original_media_type,
        image_role=image_role, answer_region_provenance=answer_region_provenance,
    )


def save_photo_bytes(
    raw: bytes,
    media_type: str,
    target: Path,
    original_target: Path | None = None,
    corners: Any = None,
    original_raw: bytes | None = None,
    original_media_type: str | None = None,
    *,
    image_role: str = "source_page",
    answer_region_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with IMAGE_DECODE_SEMAPHORE:
        return _save_photo_bytes_with_decode_slot(
            raw, media_type, target, original_target, corners, original_raw, original_media_type,
            image_role=image_role, answer_region_provenance=answer_region_provenance,
        )


def _save_photo_bytes_with_decode_slot(
    raw: bytes,
    media_type: str,
    target: Path,
    original_target: Path | None = None,
    corners: Any = None,
    original_raw: bytes | None = None,
    original_media_type: str | None = None,
    *,
    image_role: str = "source_page",
    answer_region_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    media_type = _validate_upload_media_type(media_type)
    if original_raw is None:
        original_raw = raw
        original_media_type = media_type
    original_media_type = _validate_upload_media_type(str(original_media_type or media_type))
    validate_image_bytes(original_raw, original_media_type)
    if image_role not in {"source_page", "answer_region"}:
        raise ValueError("image_role_invalid")
    if image_role == "answer_region":
        if corners is not None or not isinstance(answer_region_provenance, dict):
            raise ValueError("answer_region_invalid:provenance_missing")
        validate_answer_region_image(
            raw, media_type, answer_region_provenance,
            expected_sha256=str(answer_region_provenance.get("crop_sha256") or ""),
            expected_dimensions=(
                int(answer_region_provenance.get("crop_width", 0)),
                int(answer_region_provenance.get("crop_height", 0)),
            ),
        )
        if hashlib.sha256(original_raw).hexdigest() != str(answer_region_provenance.get("crop_sha256") or ""):
            raise ValueError("answer_region_invalid:original_crop_mismatch")
    normalized_corners = normalize_photo_corners(corners)
    try:
        disk_anchor = target.parent
        while not disk_anchor.exists() and disk_anchor != disk_anchor.parent:
            disk_anchor = disk_anchor.parent
        if not disk_anchor.exists():
            disk_anchor = Path.cwd()
        usage = shutil.disk_usage(disk_anchor)
        required_bytes = len(raw) + len(original_raw)
        if usage.free < max(MIN_FREE_SPACE_RESERVE, required_bytes * 2) + MIN_FREE_SPACE_RESERVE:
            raise ValueError("便携磁盘剩余空间不足，已拒绝解码照片")
        image, original_size = decode_image_for_processing(raw, media_type)
        image, transform = rectify_image(image, normalized_corners)
        if image_role == "source_page":
            quality = assess_photo_quality(image)
        else:
            quality = validate_answer_region_image(
                raw, media_type, answer_region_provenance or {},
                expected_sha256=str((answer_region_provenance or {}).get("crop_sha256") or ""),
                expected_dimensions=(
                    int((answer_region_provenance or {}).get("crop_width", 0)),
                    int((answer_region_provenance or {}).get("crop_height", 0)),
                ),
            )
        scale = min(1.0, MAX_IMAGE_EDGE / max(image.width, image.height))
        if scale < 1:
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        image.save(temporary, format="JPEG", quality=86, optimize=True)
        temporary.replace(target)
        if original_target is not None:
            original_target.parent.mkdir(parents=True, exist_ok=True)
            original_temporary = original_target.with_name(
                f".{original_target.name}.{uuid.uuid4().hex}.tmp"
            )
            original_temporary.write_bytes(original_raw)
            original_temporary.replace(original_target)
        try:
            display_path = str(target.relative_to(PORTABLE_ROOT)).replace("\\", "/")
        except ValueError:
            display_path = str(target)
        try:
            original_display_path = (
                str(original_target.relative_to(PORTABLE_ROOT)).replace("\\", "/")
                if original_target is not None
                else ""
            )
        except ValueError:
            original_display_path = str(original_target) if original_target is not None else ""
        return {
            "path": display_path,
            "original_path": original_display_path,
            "width": image.width,
            "height": image.height,
            "original_width": original_size[0],
            "original_height": original_size[1],
            "original_bytes": len(original_raw),
            "original_sha256": hashlib.sha256(original_raw).hexdigest(),
            "original_content_type": original_media_type or media_type,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "quality": quality,
            "transform": transform,
            "image_role": image_role,
            "answer_region_provenance": answer_region_provenance if image_role == "answer_region" else None,
        }
    except ValueError:
        raise
    except Exception as exc:  # Pillow raises several format-specific errors.
        raise ValueError("无法读取照片，请重新拍摄或选择 JPG/PNG/HEIC 转换后的图片") from exc


def total_score(criteria: list[dict[str, Any]]) -> float:
    return grading_core.total_score(criteria)


def normalize_student_identity(raw: Any) -> dict[str, Any]:
    return grading_core.normalize_student_identity(raw)


def normalize_evidence_box(raw: Any) -> dict[str, Any] | None:
    return grading_core.normalize_evidence_box(raw)


def normalize_ai_redactions(raw: Any) -> list[dict[str, float]]:
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > MAX_AI_REDACTIONS_PER_IMAGE:
        raise ValueError("身份遮挡框格式无效或数量过多")
    normalized: list[dict[str, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("身份遮挡框格式无效")
        try:
            x, y, width, height = (float(item[key]) for key in ("x", "y", "width", "height"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("身份遮挡框格式无效") from exc
        if (
            not all(math.isfinite(value) for value in (x, y, width, height))
            or x < 0 or y < 0 or width <= 0 or height <= 0
            or x + width > 1 or y + height > 1
        ):
            raise ValueError("身份遮挡框必须位于图片范围内")
        normalized.append({key: round(value, 7) for key, value in zip(("x", "y", "width", "height"), (x, y, width, height))})
    return normalized


def normalize_answer_regions(
    raw: Any,
    sub_questions: list[dict[str, Any]],
    photo_count: int,
    *,
    require_complete: bool = True,
) -> list[dict[str, Any]]:
    """Validate teacher-selected answer crops on the perspective-corrected pages."""
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("答案区域格式无效，最多100个")
    if not raw:
        return []
    allowed_ids = {str(item.get("id", "")) for item in sub_questions}
    covered: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError("答案区域格式无效")
        try:
            photo_index = int(item.get("photo_index", 0))
            x, y, width, height = (
                float(item.get(key)) for key in ("x", "y", "width", "height")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("答案区域页码或坐标无效") from exc
        raw_units = item.get("sub_question_ids", item.get("unit_ids", []))
        if isinstance(raw_units, str):
            raw_units = [raw_units]
        if not isinstance(raw_units, list):
            raise ValueError("答案区域必须关联至少一道小题")
        unit_ids = list(dict.fromkeys(str(value).strip() for value in raw_units if str(value).strip()))
        if (
            photo_index < 1
            or photo_index > photo_count
            or not unit_ids
            or any(value not in allowed_ids for value in unit_ids)
            or not all(math.isfinite(value) for value in (x, y, width, height))
            or x < 0 or y < 0 or width <= 0 or height <= 0
            or x + width > 1 or y + height > 1
            or width * height < 0.0001
        ):
            raise ValueError("答案区域越界、过小或关联了未知小题；已阻止模型请求")
        covered.update(unit_ids)
        normalized_region = {
            "region_id": str(item.get("region_id") or f"R{index}")[:80],
            "photo_index": photo_index,
            "x": round(x, 7),
            "y": round(y, 7),
            "width": round(width, 7),
            "height": round(height, 7),
            "sub_question_ids": unit_ids,
            "coordinate_system": str(item.get("coordinate_system") or "perspective_corrected_page_normalized"),
            "rotation_ccw": int(item.get("rotation_ccw", 0)),
            "invocation_scope": str(item.get("invocation_scope") or "whole_paper"),
            "profile_id": str(item.get("profile_id") or "")[:100],
        }
        if normalized_region["rotation_ccw"] not in {0, 90, 180, 270}:
            raise ValueError("答案区域旋转角度无效")
        if normalized_region["invocation_scope"] not in {"whole_paper", "answer_region"}:
            raise ValueError("答案区域调用范围无效")
        if normalized_region["coordinate_system"] not in {
            "perspective_corrected_page_normalized",
            "ccw_rotated_page_normalized",
        }:
            raise ValueError("答案区域坐标系无效")
        approved_prompt_sha256 = str(item.get("approved_prompt_sha256") or "")
        if approved_prompt_sha256:
            if not re.fullmatch(r"[0-9a-f]{64}", approved_prompt_sha256):
                raise ValueError("裁切提示词确认指纹无效")
            normalized_region["approved_prompt_sha256"] = approved_prompt_sha256
        approved_hash = str(item.get("approved_model_sha256", ""))
        if approved_hash:
            if not re.fullmatch(r"[0-9a-f]{64}", approved_hash):
                raise ValueError("裁切图片确认指纹无效")
            normalized_region["approved_model_sha256"] = approved_hash
        normalized.append(normalized_region)
    missing = allowed_ids - covered
    if require_complete and missing:
        raise ValueError("以下小题尚未标出答案区域；请补选或将相关区域合并后再预览")
    return normalized


def _require_answer_region_rubric_ready(
    import_metadata_json: Any,
    sub_questions: list[dict[str, Any]],
    question_ids: list[str],
) -> None:
    """Apply the existing teacher-confirmed rubric gate to trusted crop questions."""
    metadata = _json_object_or_empty(import_metadata_json)
    try:
        from auto_crop_double_review_poc import rubric_readiness

        ready, reasons = rubric_readiness(
            sub_questions,
            teacher_confirmation_status=str(metadata.get("teacher_confirmation_status") or ""),
            teacher_confirmed_at=metadata.get("teacher_confirmed_at"),
            required_question_ids=question_ids,
        )
    except Exception as exc:
        raise ValueError("rubric_not_ready:readiness_check_failed") from exc
    if not ready:
        raise ValueError("rubric_not_ready:" + ",".join(reasons))


def _validate_answer_region_attestations(
    photos: list[dict[str, Any]],
    answer_regions: list[dict[str, Any]],
    sub_questions: list[dict[str, Any]],
    assignment: Any,
    conn: sqlite3.Connection,
) -> dict[int, dict[str, Any]]:
    """Derive answer_region roles only from server-signed accepted-crop claims."""
    tokens = [str(item.get("answer_region_attestation") or "") for item in photos]
    declared = [str(item.get("image_role") or "") for item in photos]
    if not any(tokens):
        if any(role == "answer_region" for role in declared):
            raise ValueError("answer_region_invalid:attestation_missing")
        if any(role and role != "source_page" for role in declared):
            raise ValueError("image_role_invalid")
        return {}
    if len(photos) != len(answer_regions) or any(not token for token in tokens):
        raise ValueError("answer_region_invalid:attestation_incomplete")
    regions_by_page = {int(region.get("photo_index", 0)): region for region in answer_regions}
    if len(regions_by_page) != len(answer_regions) or set(regions_by_page) != set(range(1, len(photos) + 1)):
        raise ValueError("answer_region_invalid:region_mapping_incomplete")
    question_by_id = {str(item.get("id") or ""): item for item in sub_questions}
    verified: dict[int, dict[str, Any]] = {}
    for index, item in enumerate(photos, start=1):
        if declared[index - 1] not in {"", "answer_region"}:
            raise ValueError("answer_region_invalid:image_role_mismatch")
        try:
            claims = _read_preview_claims(tokens[index - 1])
            raw, media_type = photo_upload_bytes(item)
            original_raw, _ = photo_upload_bytes(item, original=True)
            if hashlib.sha256(original_raw).hexdigest() != hashlib.sha256(raw).hexdigest():
                raise ValueError("answer_region_invalid:original_crop_mismatch")
            validate_answer_region_image(
                raw, media_type, claims,
                expected_sha256=str(claims.get("crop_sha256") or ""),
                expected_dimensions=(int(claims.get("crop_width", 0)), int(claims.get("crop_height", 0))),
            )
            region = regions_by_page[index]
            question_id = str(claims.get("question_id") or "")
            selected = question_by_id.get(question_id)
            raw_ids = region.get("sub_question_ids") or []
            is_full_crop = (
                len(raw_ids) == 1 and str(raw_ids[0]) == question_id
                and region.get("invocation_scope") == "answer_region"
                and str(region.get("profile_id") or "") == str(claims.get("layout_profile_id") or "")
                and int(region.get("rotation_ccw", 0)) == 0
                and all(float(region.get(key, -1)) == expected for key, expected in (
                    ("x", 0.0), ("y", 0.0), ("width", 1.0), ("height", 1.0),
                ))
            )
            if not selected or not is_full_crop:
                raise ValueError("answer_region_invalid:question_mapping_mismatch")
            if (
                claims.get("kind") != "accepted_answer_region"
                or str(claims.get("assignment_id") or "") != str(assignment["id"])
                or str(claims.get("rubric_sha256") or "") != rubric_fingerprint([selected])
            ):
                raise ValueError("answer_region_invalid:claim_binding_mismatch")
            exam_id = str(claims.get("exam_id") or "")
            context = _exam_layout_profile_context(conn, exam_id) if exam_id else None
            if (
                not context
                or context.get("assignment_id") != str(assignment["id"])
                or context.get("profile_hash_mismatch")
                or context.get("profile_invalid")
                or context.get("layout_profile_id") != claims.get("layout_profile_id")
                or context.get("source_pdf_sha256") != claims.get("source_pdf_sha256")
            ):
                raise ValueError("answer_region_invalid:layout_profile_stale")
            _require_answer_region_rubric_ready(
                assignment["import_metadata_json"], sub_questions, [question_id]
            )
        except ValueError as exc:
            message = str(exc)
            if message.startswith("rubric_not_ready:"):
                raise
            if message.startswith("answer_region_invalid:"):
                raise
            raise ValueError("answer_region_invalid:attestation_invalid") from exc
        except (KeyError, TypeError, IndexError, ValueError, OverflowError) as exc:
            raise ValueError("answer_region_invalid:attestation_invalid") from exc
        item["_verified_image_role"] = "answer_region"
        item["_verified_answer_region_provenance"] = {
            key: claims.get(key) for key in (
                "exam_id", "assignment_id", "question_id", "page_number", "region_type",
                "layout_profile_id", "source_pdf_sha256", "reference_page_sha256",
                "source_student_image_sha256", "alignment_run_id", "alignment_status",
                "quality_gate", "crop_status", "teacher_crop_quality", "configured_bbox",
                "padding", "effective_crop_bbox", "pixel_bbox", "crop_sha256",
                "crop_width", "crop_height", "crop_bytes", "rubric_sha256",
            )
        }
        verified[index] = item["_verified_answer_region_provenance"]
    return verified


def crop_model_image_bytes(model_image_bytes: bytes, region: dict[str, Any]) -> bytes:
    """Crop a model-ready page using normalized coordinates and re-encode canonically."""
    with Image.open(io.BytesIO(model_image_bytes)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        rotation_ccw = int(region.get("rotation_ccw", 0)) % 360
        if rotation_ccw:
            image = image.rotate(rotation_ccw, expand=True)
        left = max(0, math.floor(float(region["x"]) * image.width))
        top = max(0, math.floor(float(region["y"]) * image.height))
        right = min(image.width, math.ceil((float(region["x"]) + float(region["width"])) * image.width))
        bottom = min(image.height, math.ceil((float(region["y"]) + float(region["height"])) * image.height))
        if right <= left or bottom <= top:
            raise ValueError("答案区域裁切后为空；请重新框选并确认")
        crop = image.crop((left, top, right, bottom))
        return _encode_model_image(crop)


def _map_crop_evidence_box(evidence: dict[str, Any], region: dict[str, Any], crop_index: int) -> None:
    x = float(evidence.get("x", 0))
    y = float(evidence.get("y", 0))
    width = float(evidence.get("width", 0))
    height = float(evidence.get("height", 0))
    rotated_x = float(region["x"]) + x * float(region["width"])
    rotated_y = float(region["y"]) + y * float(region["height"])
    rotated_width = width * float(region["width"])
    rotated_height = height * float(region["height"])
    rotation_ccw = int(region.get("rotation_ccw", 0)) % 360
    if rotation_ccw == 90:
        source_x = 1.0 - rotated_y - rotated_height
        source_y = rotated_x
        source_width = rotated_height
        source_height = rotated_width
    elif rotation_ccw == 180:
        source_x = 1.0 - rotated_x - rotated_width
        source_y = 1.0 - rotated_y - rotated_height
        source_width = rotated_width
        source_height = rotated_height
    elif rotation_ccw == 270:
        source_x = rotated_y
        source_y = 1.0 - rotated_x - rotated_width
        source_width = rotated_height
        source_height = rotated_width
    else:
        source_x, source_y = rotated_x, rotated_y
        source_width, source_height = rotated_width, rotated_height
    evidence.update({
        "photo_index": int(region["photo_index"]),
        "x": round(max(0.0, source_x), 7),
        "y": round(max(0.0, source_y), 7),
        "width": round(max(0.0, source_width), 7),
        "height": round(max(0.0, source_height), 7),
        "coordinate_system": "perspective_corrected_page_normalized",
        "source": "teacher_crop_mapping",
        "crop_index": crop_index + 1,
        "answer_region_id": str(region.get("region_id") or ""),
    })


def map_crop_evidence_to_source(result: dict[str, Any], answer_regions: list[dict[str, Any]]) -> None:
    """Map crop-relative evidence or mark full-page evidence coordinates."""
    for question in result.get('sub_questions',[]):
        if question.get('scoring_mode')=='holistic':
            question['holistic_evidence']=copy.deepcopy((question.get('holistic_result') or {}).get('evidence',[]))
            for ev in question['holistic_evidence']:
                box=ev['location'];index=int(box['photo_index'])-1
                if answer_regions and 0<=index<len(answer_regions):_map_crop_evidence_box(box,answer_regions[index],index)
                elif not answer_regions:box.update(coordinate_system='perspective_corrected_page_normalized',source='full_page')
    if not answer_regions:
        # The model saw the complete perspective-corrected page, so its normalized
        # evidence coordinates already refer to that page and must be preserved.
        for sub_question in result.get("sub_questions", []):
            for criterion in sub_question.get("criteria", []):
                evidence = criterion.get("evidence")
                if isinstance(evidence, dict):
                    evidence["coordinate_system"] = "perspective_corrected_page_normalized"
                    evidence["source"] = "full_page"
        for item in result.get("unmapped_answers", []):
            evidence = item.get("evidence") if isinstance(item, dict) else None
            if isinstance(evidence, dict):
                evidence["coordinate_system"] = "perspective_corrected_page_normalized"
                evidence["source"] = "full_page"
        return
    for sub_question in result.get("sub_questions", []):
        for criterion in sub_question.get("criteria", []):
            evidence = criterion.get("evidence")
            if not isinstance(evidence, dict):
                continue
            try:
                crop_index = int(evidence.get("photo_index", 0)) - 1
            except (TypeError, ValueError):
                crop_index = -1
            if crop_index < 0 or crop_index >= len(answer_regions):
                criterion["evidence"] = None
                criterion["evidence_box_missing"] = True
                criterion["needs_review"] = True
                continue
            _map_crop_evidence_box(evidence, answer_regions[crop_index], crop_index)
    for item in result.get("unmapped_answers", []):
        evidence = item.get("evidence") if isinstance(item, dict) else None
        if not isinstance(evidence, dict):
            continue
        try:
            crop_index = int(evidence.get("photo_index", 0)) - 1
        except (TypeError, ValueError):
            crop_index = -1
        if crop_index < 0 or crop_index >= len(answer_regions):
            item["evidence"] = None
            continue
        _map_crop_evidence_box(evidence, answer_regions[crop_index], crop_index)


def materialize_evidence_crops(result: dict[str, Any], photo_meta: list[dict[str, Any]], photo_paths: list[Path]) -> None:
    """Create immutable derived evidence crops; original images are untouched."""
    global_reasons = set(result.get("block_reasons") or [])
    for sub_question in result.get("sub_questions", []):
        sub_reasons = set(sub_question.get("block_reasons") or [])
        for criterion in sub_question.get("criteria", []):
            evidence = criterion.get("evidence")
            try:
                claimed_score = float(criterion.get("claimed_score", criterion.get("suggested_score", 0)) or 0)
            except (TypeError, ValueError):
                claimed_score = 0.0
            is_hit = math.isfinite(claimed_score) and claimed_score > 0
            if not isinstance(evidence, dict):
                if is_hit:
                    criterion["evidence"] = None
                    criterion["evidence_missing"] = not bool(str(criterion.get("student_evidence") or "").strip())
                    criterion["evidence_box_missing"] = True
                    criterion["needs_review"] = True
                    sub_reasons.add("有得分评分点的图片证据缺失或无法生成")
                continue
            try:
                index = int(evidence.get("photo_index", 0)) - 1
            except (TypeError, ValueError):
                index = -1
            if index < 0 or index >= len(photo_paths) or index >= len(photo_meta):
                criterion["evidence"] = None
                if is_hit:
                    criterion["evidence_box_missing"] = True
                    criterion["needs_review"] = True
                    sub_reasons.add("有得分评分点引用的照片不存在")
                continue
            source = photo_paths[index]
            try:
                with Image.open(source) as image:
                    image = image.convert("RGB")
                    left = max(0, round(float(evidence["x"]) * image.width))
                    top = max(0, round(float(evidence["y"]) * image.height))
                    right = min(image.width, round((float(evidence["x"]) + float(evidence["width"])) * image.width))
                    bottom = min(image.height, round((float(evidence["y"]) + float(evidence["height"])) * image.height))
                    if right <= left or bottom <= top:
                        criterion["evidence"] = None
                        if is_hit:
                            criterion["evidence_box_missing"] = True
                            criterion["needs_review"] = True
                            sub_reasons.add("有得分评分点的证据框无效")
                        continue
                    crop = image.crop((left, top, right, bottom))
                    target = source.parent / "evidence" / f"{safe_name(sub_question.get('sub_question_id', 'Q'))}_{safe_name(criterion.get('criterion_id', 'C'))}.jpg"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                    crop.save(temporary, format="JPEG", quality=90, optimize=True)
                    temporary.replace(target)
                try:
                    crop_path = str(target.relative_to(PORTABLE_ROOT)).replace("\\", "/")
                except ValueError:
                    crop_path = str(target)
                criterion["evidence"] = {
                    **evidence,
                    "source_path": photo_meta[index].get("path", ""),
                    "source_sha256": photo_meta[index].get("sha256", ""),
                    "original_sha256": photo_meta[index].get("original_sha256", ""),
                    "source_dimensions": [photo_meta[index].get("width"), photo_meta[index].get("height")],
                    "transform": photo_meta[index].get("transform", {}),
                    "crop_path": crop_path,
                }
            except (OSError, ValueError, KeyError):
                criterion["evidence"] = None
                if is_hit:
                    criterion["evidence_box_missing"] = True
                    criterion["needs_review"] = True
                    sub_reasons.add("有得分评分点的证据图片裁切失败")
            if is_hit and not isinstance(criterion.get("evidence"), dict):
                criterion["needs_review"] = True
                sub_reasons.add("有得分评分点的图片证据丢失")
        if sub_reasons:
            sub_question["block_reasons"] = sorted(sub_reasons)
            sub_question["blocked"] = True
            sub_question["needs_review"] = True
            global_reasons.update(sub_reasons)
    result["block_reasons"] = sorted(global_reasons)
    result["blocked"] = bool(global_reasons)
    if global_reasons:
        result["needs_review"] = True


def score_by_rules(
    criteria: list[dict[str, Any]],
    answer_text: str,
    max_total: float | None = None,
    scoring_mode: str = "weighted",
) -> dict[str, Any]:
    """Transparent fallback for testing the workflow without a model."""
    normalized = answer_text.lower().strip()
    items = []
    total = 0.0
    if max_total is None:
        max_total = total_score(criteria)
    for item in criteria:
        hits = [k for k in item.get("keywords", []) if k.lower() in normalized]
        claimed = float(item["score"]) if hits else 0.0
        if scoring_mode == "point_bank" and claimed > 0:
            claimed = float(item["score"])
        awarded = min(claimed, max(0.0, float(max_total) - total))
        capped = claimed > awarded
        total += awarded
        items.append(
            {
                "criterion_id": item["id"],
                "criterion": item["label"],
                "max_score": float(item["score"]),
                "claimed_score": claimed,
                "suggested_score": awarded,
                "credit_status": "capped" if capped else "credited" if awarded > 0 else "not_credited",
                "decision": "hit" if hits else "miss",
                "student_evidence": ", ".join(hits),
                "reason": (
                    "规则演示模式：该要点虽命中，但小题已达到满分上限。"
                    if capped
                    else "规则演示模式：命中预设关键词。" if hits
                    else "规则演示模式：未命中预设关键词。"
                ),
                "needs_review": True,
            }
        )
    return {
        "mode": "rules-demo",
        "suggested_total": total,
        "max_total": max_total,
        "confidence": 0.0,
        "needs_review": True,
        "blocked": True,
        "block_reasons": ["规则演示模式不可作为正式评分"],
        "summary": "当前未配置视觉模型，结果仅用于演示流程，不能作为真实历史主观题评分依据。",
        "student_identity": normalize_student_identity(None),
        "criteria": items,
    }


def score_sub_questions(sub_questions: list[dict[str, Any]], answer_text: str) -> dict[str, Any]:
    """Score each sub-question separately for the no-model demonstration mode."""
    results = []
    total = 0.0
    for sub_question in sub_questions:
        result = score_by_rules(
            sub_question["criteria"],
            answer_text,
            max_total=float(sub_question["max_score"]),
            scoring_mode=sub_question.get("scoring_mode", "weighted"),
        )
        total += float(result["suggested_total"])
        results.append(
            {
                "sub_question_id": sub_question["id"],
                "label": sub_question["label"],
                "question": sub_question["question"],
                "suggested_total": result["suggested_total"],
                "max_total": result["max_total"],
                "scoring_mode": sub_question.get("scoring_mode", "weighted"),
                "point_value": float(sub_question.get("point_value", 2)),
                "criteria": result["criteria"],
                "needs_review": True,
            }
        )
    return {
        "mode": "rules-demo",
        "suggested_total": total,
        "max_total": total_sub_questions(sub_questions),
        "confidence": 0.0,
        "needs_review": True,
        "blocked": True,
        "block_reasons": ["规则演示模式不可作为正式评分"],
        "summary": "当前未配置视觉模型，结果仅用于演示流程，不能作为真实历史主观题评分依据。",
        "student_identity": normalize_student_identity(None),
        "sub_questions": results,
    }


def model_configured() -> bool:
    return bool(
        os.getenv("HISTORY_GRADER_BASE_URL")
        and os.getenv("HISTORY_GRADER_MODEL")
        and os.getenv("HISTORY_GRADER_API_KEY")
    )


def current_model_preset_id() -> str:
    provider = os.getenv("HISTORY_GRADER_PROVIDER", "deepseek").strip() or "deepseek"
    base_url = os.getenv("HISTORY_GRADER_BASE_URL", "").rstrip("/")
    model = os.getenv("HISTORY_GRADER_MODEL", "").strip()
    for preset_id, preset in model_preset_catalog().items():
        if preset_id == "custom":
            continue
        if (
            preset["provider"] == provider
            and preset["base_url"].rstrip("/") == base_url
            and preset["model"] == model
        ):
            return preset_id
    return "custom"


def public_model_presets() -> list[dict[str, Any]]:
    return [{"id": preset_id, **preset} for preset_id, preset in model_preset_catalog().items()]


SERVER_MODEL_SELECTION_FILE = "server-model-selection.json"
SERVER_CURRENT_MODEL_ID = "server-current"


def model_configuration_is_synthetic() -> bool:
    provider = os.getenv("HISTORY_GRADER_PROVIDER", "").strip().casefold()
    model = os.getenv("HISTORY_GRADER_MODEL", "").strip().casefold()
    return provider in {"synthetic", "mock", "synthetic-local", "mock-local"} or model in {
        "synthetic-local", "mock-local", "synthetic", "mock"
    }


def runtime_environment() -> str:
    """Return the shared provenance label for local portable and hosted runs."""
    return "online_beta" if cloud_mode_enabled() else "portable_local"


def server_model_presets() -> list[dict[str, Any]]:
    """Return only models that can use the currently server-held credential."""
    provider = os.getenv("HISTORY_GRADER_PROVIDER", "").strip() or "deepseek"
    base_url = os.getenv("HISTORY_GRADER_BASE_URL", "").strip()
    model = os.getenv("HISTORY_GRADER_MODEL", "").strip()
    api_key = os.getenv("HISTORY_GRADER_API_KEY", "").strip()
    if not base_url or not model or not api_key:
        return []
    try:
        current_base_url, current_origin = validate_model_base_url(base_url)
    except ValueError:
        return []

    choices: list[dict[str, Any]] = []
    active_matched = False
    for preset_id, raw in model_preset_catalog().items():
        if preset_id == "custom" or raw.get("provider") != provider:
            continue
        try:
            preset_base_url, preset_origin = validate_model_base_url(str(raw.get("base_url") or ""))
        except ValueError:
            continue
        preset_model = str(raw.get("model") or "").strip()
        if not preset_model or preset_origin != current_origin:
            continue
        item = {"id": preset_id, **raw, "server_managed": True}
        if preset_base_url.rstrip("/") == current_base_url.rstrip("/") and preset_model == model:
            item["label"] = f"{item['label']}（当前）"
            item["server_current"] = True
            active_matched = True
        choices.append(item)

    if not active_matched:
        choices.insert(0, {
            "id": SERVER_CURRENT_MODEL_ID,
            "label": f"{model}（当前服务器配置）",
            "provider": provider,
            "provider_label": provider,
            "base_url": current_base_url,
            "model": model,
            "note": "当前服务器正在使用的模型配置。",
            "server_managed": True,
            "server_current": True,
        })
    return choices


def server_model_selection_id() -> str:
    provider = os.getenv("HISTORY_GRADER_PROVIDER", "").strip() or "deepseek"
    base_url = os.getenv("HISTORY_GRADER_BASE_URL", "").rstrip("/")
    model = os.getenv("HISTORY_GRADER_MODEL", "").strip()
    for preset in server_model_presets():
        if (
            preset.get("provider") == provider
            and str(preset.get("base_url", "")).rstrip("/") == base_url
            and preset.get("model") == model
        ):
            return str(preset["id"])
    return SERVER_CURRENT_MODEL_ID


def apply_server_model_selection() -> None:
    """Apply a persisted preset ID without storing or exposing a model Key."""
    path = CONFIG_DIR / SERVER_MODEL_SELECTION_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(document, dict) or document.get("format") != "history-grader-server-model-selection-v1":
        return
    preset_id = str(document.get("preset_id") or "")
    preset = next((item for item in server_model_presets() if item.get("id") == preset_id), None)
    if not preset or preset_id == SERVER_CURRENT_MODEL_ID:
        return
    os.environ["HISTORY_GRADER_PROVIDER"] = str(preset["provider"])
    os.environ["HISTORY_GRADER_BASE_URL"] = str(preset["base_url"])
    os.environ["HISTORY_GRADER_MODEL"] = str(preset["model"])


def persist_server_model_selection(preset_id: str) -> dict[str, Any]:
    choices = server_model_presets()
    preset = next((item for item in choices if item.get("id") == preset_id), None)
    if not preset:
        raise ValueError("该模型不在服务器已配置的可选列表中，请刷新页面后重试")
    if preset_id != SERVER_CURRENT_MODEL_ID:
        document = {
            "format": "history-grader-server-model-selection-v1",
            "preset_id": preset_id,
            "updated_at": utc_now(),
        }
        atomic_write_text(
            CONFIG_DIR / SERVER_MODEL_SELECTION_FILE,
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        )
    os.environ["HISTORY_GRADER_PROVIDER"] = str(preset["provider"])
    os.environ["HISTORY_GRADER_BASE_URL"] = str(preset["base_url"])
    os.environ["HISTORY_GRADER_MODEL"] = str(preset["model"])
    return preset


NoRedirectHandler = model_gateway.NoRedirectHandler


ModelProviderResponseError = model_gateway.ModelProviderResponseError


def open_model_request(request: Request, timeout: float):
    return model_gateway.open_model_request(request, timeout, opener_factory=build_opener, redirect_handler=NoRedirectHandler)


def elapsed_ms(started_at: float) -> int:
    """Return a bounded integer duration for non-sensitive diagnostics."""
    return max(0, round((time.perf_counter() - started_at) * 1000))


def read_model_chat_content(response, diagnostics=None, raw_capture=None) -> str:
    return model_gateway.read_model_chat_content(response, diagnostics, raw_capture)


def private_model_diagnostic_dir() -> Path:
    """Keep answer-bearing call artifacts under the current Windows user's local profile."""
    return (
        local_app_data_dir()
        / "HistoryGrader"
        / "model-call-diagnostics"
        / ensure_portable_instance_id()
    )


def preflight_private_model_diagnostic_dir() -> Path:
    directory = private_model_diagnostic_dir()
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / f".write-probe-{uuid.uuid4().hex}.tmp"
    try:
        probe.write_text("ok", encoding="ascii")
    finally:
        probe.unlink(missing_ok=True)
    return directory


def persist_private_model_call_artifact(artifact: dict[str, Any]) -> str:
    """Store raw answer-bearing call data outside the served/backup data tree."""
    directory = private_model_diagnostic_dir()
    directory.mkdir(parents=True, exist_ok=True)
    artifact_id = str(artifact.get("call_id") or uuid.uuid4().hex)
    path = directory / f"{artifact_id}.json"
    atomic_write_text(path, json.dumps(artifact, ensure_ascii=False, indent=2) + "\n")
    return artifact_id


def _response_sub_question_ids(value: Any) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get("sub_questions"), list):
        return []
    found = []
    for item in value["sub_questions"]:
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("sub_question_id") or item.get("id") or "").strip()
        if question_id and question_id not in found:
            found.append(question_id)
    return found


def _raw_text_sub_question_ids(content: str) -> list[str]:
    """Report literal question-ID fields in assistant content without rewriting it."""
    found = []
    pattern = r'"(?:sub_question_id|id)"\s*:\s*"(Q[0-9]+)"'
    for match in re.finditer(pattern, content):
        question_id = match.group(1)
        if question_id not in found:
            found.append(question_id)
    return found


def parse_vision_model_output(
    content: str, sub_questions: list[dict[str, Any]], photo_count: int | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return parser output and normalization output as distinct diagnostic stages."""
    parser_output = validate_grading_response(extract_json(content))
    normalized = normalize_sub_question_result(parser_output, sub_questions, photo_count)
    return parser_output, normalized


def classify_model_error(error: BaseException) -> dict[str, Any]:
    """Classify a provider failure without retaining response bodies or secrets."""
    status: int | None = None
    category = "unknown"
    retryable = False
    if isinstance(error, HTTPError):
        status = int(error.code)
        if status in {408, 425, 429} or 500 <= status <= 599:
            category = "provider_transient"
            retryable = True
        elif status in {401, 403}:
            category = "auth_or_permission"
        elif status == 413:
            category = "request_too_large"
        elif 400 <= status <= 499:
            category = "provider_request_rejected"
        else:
            category = "provider_http_error"
    elif isinstance(error, (TimeoutError, socket.timeout)):
        category = "timeout"
        retryable = True
    elif isinstance(error, URLError):
        reason = error.reason
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason).casefold():
            category = "timeout"
        else:
            category = "network"
        retryable = True
    elif isinstance(error, ModelProviderResponseError):
        category = "invalid_provider_response"
    elif isinstance(error, (json.JSONDecodeError, KeyError, IndexError, TypeError)):
        category = "invalid_provider_response"
    elif isinstance(error, ValueError):
        category = "request_or_response_validation"
    else:
        category = "unexpected_model_error"

    messages = {
        "provider_transient": "服务商暂时不可用或触发限流",
        "auth_or_permission": "服务商拒绝了认证或模型权限",
        "request_too_large": "发送给服务商的请求过大",
        "provider_request_rejected": "服务商拒绝了请求参数",
        "provider_http_error": "服务商返回了 HTTP 错误",
        "timeout": "模型请求超时",
        "network": "无法稳定连接模型服务商",
        "invalid_provider_response": "服务商返回内容不是可识别的批改结果",
        "request_or_response_validation": "请求参数或模型结果校验未通过",
        "unexpected_model_error": "模型调用发生未分类异常",
    }
    diagnostic: dict[str, Any] = {
        "error_category": category,
        "retryable": retryable,
        "message": messages[category],
        "exception_type": type(error).__name__,
    }
    if status is not None:
        diagnostic["http_status"] = status
    if isinstance(error, ModelProviderResponseError):
        diagnostic["response_stage"] = error.stage
    return diagnostic


RETRY_BACKOFF_SECONDS = (0, 5, 15, 30, 60)


def manual_retry_policy(
    result: dict[str, Any], state: str, has_photos: bool = True
) -> tuple[bool, str]:
    """Decide whether a teacher may manually retry without replaying unsafe failures."""
    if not has_photos:
        return False, "照片缓存已清理，请重新提交照片"
    mode = str(result.get("mode") or "")
    if mode == 'rules-demo' or (mode == 'vision-model-error' and (result.get('diagnostics') or {}).get('error_category') == 'model_not_configured'):
        return True, '该份未进入真实模型评分；请先配置模型，再核对外发图与费用后原位恢复'
    if state == "recovery_pending" and mode == "queued":
        return True, "服务曾中断，是否已确认服务商未重复计费后再重试"
    if mode == "processing-error":
        return True, "本机处理失败，可以人工确认后重试"
    if mode == "vision-model-error":
        diagnostics = result.get("diagnostics")
        if isinstance(diagnostics, dict) and "retryable" in diagnostics:
            if diagnostics.get("retryable") is True:
                return True, "服务商暂时性失败，可以人工确认后重试"
        elif result.get("retryable") is True:
            # Older records predate structured diagnostics and explicitly
            # marked model failures as manually retryable.
            return True, "旧版本记录标记为可重试，请确认服务商记录后再试"
        return False, str(
            diagnostics.get("message")
            if isinstance(diagnostics, dict) and diagnostics.get("message")
            else "该模型失败类型不建议重复调用，请先修正配置或请求"
        )
    return False, "该记录不是可安全重试的失败类型"


def manual_retry_backoff_seconds(attempt_count: int, updated_at: str) -> int:
    """Apply a short local cooldown to repeated manual retries."""
    if attempt_count <= 0:
        return 0
    try:
        updated = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        delay = RETRY_BACKOFF_SECONDS[min(attempt_count, len(RETRY_BACKOFF_SECONDS) - 1)]
        return max(0, int(round(updated.timestamp() + delay - time.time())))
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_generated_rubric(
    result: dict[str, Any], sub_questions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Normalize model-generated scoring points before showing them to the teacher."""
    incoming = result.get("sub_questions")
    if not isinstance(incoming, list):
        raise ValueError("模型草拟评分细则缺少 sub_questions 数组")
    by_id: dict[str, dict[str, Any]] = {}
    for item in incoming:
        if not isinstance(item, dict):
            raise ValueError("模型草拟评分细则包含无效小题")
        item_id = str(item.get("sub_question_id") or item.get("id") or "").strip()
        if not item_id:
            raise ValueError("模型草拟评分细则有小题缺少稳定 ID")
        if item_id not in {str(question["id"]) for question in sub_questions}:
            raise ValueError("模型草拟评分细则返回未知小题 ID")
        if item_id in by_id:
            raise ValueError("模型草拟评分细则返回重复小题 ID")
        by_id[item_id] = item
    expected_sub_ids = {str(question["id"]) for question in sub_questions}
    if set(by_id) != expected_sub_ids:
        raise ValueError("模型草拟评分细则缺少小题 ID")

    normalized_sub_questions = []
    for sub_question in sub_questions:
        source = by_id[sub_question["id"]]
        raw_criteria = source.get("criteria", [])
        if not isinstance(raw_criteria, list) or not raw_criteria:
            raise ValueError(f"模型未能为{sub_question['label']}生成评分点")
        scoring_mode = str(sub_question.get("scoring_mode", "weighted")).casefold()
        point_value = float(sub_question.get("point_value", 2) or 2)
        criteria = []
        seen_criterion_ids: set[str] = set()
        seen_labels: set[str] = set()
        for item in raw_criteria:
            if not isinstance(item, dict):
                raise ValueError(f"{sub_question['label']}的评分点格式无效")
            criterion_id = str(item.get("criterion_id") or item.get("id") or "").strip()
            if not criterion_id:
                raise ValueError(f"{sub_question['label']}有评分点缺少稳定 ID")
            if criterion_id in seen_criterion_ids:
                raise ValueError(f"{sub_question['label']}的评分点 ID 重复：{criterion_id}")
            seen_criterion_ids.add(criterion_id)
            label = str(
                item.get("label") or item.get("criterion") or item.get("description") or ""
            ).strip()
            if not label:
                raise ValueError(f"{sub_question['label']}存在空白评分点")
            label_key = re.sub(r"[\W_]+", "", label.casefold())
            if label_key in seen_labels:
                raise ValueError(f"{sub_question['label']}存在重复评分点，请合并同义表达")
            seen_labels.add(label_key)
            try:
                raw_score = item.get("score", item.get("points", item.get("max_score", 0)))
                score = float(raw_score)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{sub_question['label']}的评分点分值无效") from exc
            if not math.isfinite(score) or score <= 0:
                raise ValueError(f"{sub_question['label']}的评分点分值必须是有限正数")
            if scoring_mode == "point_bank":
                score = point_value
            keywords = item.get("keywords", [])
            if isinstance(keywords, str):
                keywords = [x.strip() for x in re.split(r"[,，、]", keywords) if x.strip()]
            if not isinstance(keywords, list):
                keywords = []
            criteria.append(
                {
                    "id": criterion_id,
                    "label": label,
                    "score": score,
                    "keywords": [str(x).strip() for x in keywords if str(x).strip()],
                    "note": str(item.get("note", "")).strip(),
                }
            )
        if not criteria:
            raise ValueError(f"模型未能为{ sub_question['label'] }生成有效评分点")
        normalized_sub_questions.append(
            {
                "id": sub_question["id"],
                "label": sub_question["label"],
                "question": sub_question["question"],
                "standard_answer": sub_question["standard_answer"],
                "scoring_mode": scoring_mode,
                "point_value": point_value,
                "max_score": sub_question.get("max_score"),
                "criteria": criteria,
            }
        )
    return {"sub_questions": normalized_sub_questions}


def rubric_generation_model_config(handler: Any | None = None) -> dict[str, str]:
    """Use the configured server model in cloud mode and AI-A on portable local runs."""
    if cloud_mode_enabled(handler):
        if not model_configured():
            raise ValueError("尚未配置模型接口，无法生成评分细则 Payload 预览")
        base_url, _ = validate_model_base_url(os.environ["HISTORY_GRADER_BASE_URL"])
        return {
            "provider": os.getenv("HISTORY_GRADER_PROVIDER", "custom").strip().casefold(),
            "provider_label": os.getenv("HISTORY_GRADER_PROVIDER", "custom").strip(),
            "model": os.environ["HISTORY_GRADER_MODEL"].strip(),
            "base_url": base_url,
            "api_key": os.getenv("HISTORY_GRADER_API_KEY", ""),
            "timeout": os.getenv("HISTORY_GRADER_TIMEOUT", "180"),
            "model_role": "服务器模型",
        }
    config = load_dual_review_model_configs(("A",))["A"]
    config["model_role"] = "AI-A"
    return config


def model_chat_completions_endpoint(model_config: dict[str, str]) -> str:
    return model_gateway.model_chat_completions_endpoint(model_config, validate_model_base_url=validate_model_base_url)


def build_rubric_model_payload(
    question: str,
    sub_questions: list[dict[str, Any]],
    model: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Build the exact rubric request body used for both preview and dispatch."""
    rubric_input = [
        {
            "id": str(item.get("id") or f"Q{index}"),
            "label": str(item.get("label") or f"（{index}）"),
            "question": str(item.get("question", "")).strip(),
            "standard_answer": str(item.get("standard_answer", "")).strip(),
            "max_score": item.get("max_score"),
            "scoring_mode": str(item.get("scoring_mode", "weighted")).strip().casefold(),
            "point_value": item.get("point_value", 2),
        }
        for index, item in enumerate(sub_questions, start=1)
    ]
    prompt = (
        "你是中学历史教师的评分细则设计助手。请根据大题材料、每道小题题目和参考答案，"
        "生成可供教师审核的评分点。评分点应是彼此不同、能独立核查的历史要点；"
        "同一要点的近义表达要放在同一个评分点的 keywords/note 中，不能拆成多个可重复计分的点；也不要原样复制整段参考答案。"
        "对于 scoring_mode=point_bank 的小题，criteria 是可接受答案要点池，不是必须全部答出的清单。"
        "根据参考答案列出其中所有有依据、彼此独立的有效要点；可以多于满分所需点数，不得为了让分值合计等于满分而删掉参考答案中其他有效点。"
        "每个 point_bank 要点的 score 使用 point_value（通常2分）；小题 max_score 是封顶分，学生答中足够数量的不同要点即可达到满分，多答不再加分；若最后一个要点超过剩余分值，只计到满分上限。"
        "point_bank 中 decision=full 表示学生确实答出了完整有效要点，即使已达到封顶，仍须保留该要点的原文和证据；suggested_score 填该点实际计入分值（封顶后的多余要点填0）。"
        "不要为了凑足备选点数而发明参考答案和材料都不支持的要点。"
        "对于 scoring_mode=weighted 的小题，评分项仍按各项 score 累加，分值合计应等于 max_score；没有 max_score 时再合理分配。"
        "关键词只用于规则演示，不是正式评分依据。只返回 JSON，不要 Markdown。"
        "格式：{\"sub_questions\":[{\"sub_question_id\":\"输入中的小题ID\","
        "\"label\":\"（1）\","
        "\"criteria\":[{\"criterion_id\":\"C1\",\"label\":\"彼此独立的有效要点\",\"score\":2,\"keywords\":[\"同义表达\"],"
        "\"note\":\"审核提示\"}]}]}"
        f"\n大题材料/总题干：{question}"
        f"\n小题与参考答案：{json.dumps(rubric_input, ensure_ascii=False, indent=2)}"
    )
    selected_model = str(model or os.environ.get("HISTORY_GRADER_MODEL", "")).strip()
    selected_provider = str(provider or os.getenv("HISTORY_GRADER_PROVIDER", "")).strip().casefold()
    if not selected_model:
        raise ValueError("尚未选择用于生成评分细则的模型")
    payload = {
        "model": selected_model,
        "temperature": 0,
        "max_tokens": 6000,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "你输出的是教师审核用草案，不是正式成绩。"},
            {"role": "user", "content": prompt},
        ],
    }
    payload.update(deepseek_json_payload_options(selected_provider, selected_model))
    return payload


def call_rubric_model(
    question: str,
    sub_questions: list[dict[str, Any]],
    approved_payload: dict[str, Any] | None = None,
    approved_endpoint: str | None = None,
    model_config: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Ask the selected model to draft atomic scoring points after teacher preview."""
    config = model_config
    if config is None:
        config = rubric_generation_model_config()
    endpoint = approved_endpoint or model_chat_completions_endpoint(config)
    payload = approved_payload if approved_payload is not None else build_rubric_model_payload(
        question, sub_questions, str(config.get("model", "")), str(config.get("provider", ""))
    )
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = str(config.get("api_key", ""))
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(endpoint, data=body, headers=headers, method="POST")
    timeout = float(config.get("timeout") or os.getenv("HISTORY_GRADER_TIMEOUT", "180"))
    with open_model_request(request, timeout=timeout) as response:
        content = read_model_chat_content(response)
    return normalize_generated_rubric(extract_json(content), sub_questions)


def normalize_rubric_generation_request(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    question = str(payload.get("question", "")).strip()
    raw_sub_questions = payload.get("sub_questions")
    if not question:
        raise ValueError("大题材料/题干不能为空")
    if not isinstance(raw_sub_questions, list) or not raw_sub_questions:
        raise ValueError("请至少填写一道小题")
    drafts = []
    for index, item in enumerate(raw_sub_questions, start=1):
        if not isinstance(item, dict):
            continue
        sub_question = str(item.get("question", "")).strip()
        standard_answer = str(item.get("standard_answer", "")).strip()
        if not sub_question or not standard_answer:
            raise ValueError(f"请先填写第{index}道小题的题目和参考答案")
        scoring_mode = str(item.get("scoring_mode", "weighted")).strip().casefold()
        if scoring_mode not in {"weighted", "point_bank"}:
            raise ValueError(f"第{index}道小题的评分方式无效")
        try:
            point_value = float(item.get("point_value", 2) or 2)
        except (TypeError, ValueError):
            point_value = 0
        if not math.isfinite(point_value) or point_value <= 0:
            raise ValueError(f"第{index}道小题的每点分值必须大于0")
        max_score = item.get("max_score")
        if scoring_mode == "point_bank":
            try:
                max_score = float(max_score)
            except (TypeError, ValueError):
                max_score = 0
            if not math.isfinite(max_score) or max_score <= 0:
                raise ValueError(f"第{index}道小题按得分点封顶时，请填写本小题满分")
        drafts.append(
            {
                "id": str(item.get("id") or f"Q{index}"),
                "label": str(item.get("label") or f"（{index}）").strip(),
                "question": sub_question,
                "standard_answer": standard_answer,
                "max_score": max_score,
                "scoring_mode": scoring_mode,
                "point_value": point_value,
            }
        )
    if not drafts:
        raise ValueError("没有可生成评分点的小题")
    return question, drafts


def _balanced_json_object(text: str):
    return model_gateway._balanced_json_object(text)


def extract_json(text: str):
    return model_gateway.extract_json(text)


def normalize_model_result(result: dict[str, Any], criteria: list[dict[str, Any]], max_total: float | None = None, scoring_mode: str = "weighted") -> dict[str, Any]:
    return grading_core.normalize_model_result(result, criteria, max_total, scoring_mode, evidence_rubric_echo_suspected=evidence_rubric_echo_suspected)


def normalize_sub_question_result(result: dict[str, Any], sub_questions: list[dict[str, Any]], photo_count: int | None = None) -> dict[str, Any]:
    return grading_core.normalize_sub_question_result(result, sub_questions, photo_count, normalize_model_result=normalize_model_result)


def build_teacher_question_mapping(
    raw_mapping: Any,
    result: dict[str, Any],
    sub_questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate teacher mapping against model-reported, located answer fragments."""
    if not isinstance(raw_mapping, list) or not raw_mapping or len(raw_mapping) > 50:
        raise ValueError("请为每个待对应答案片段指定小题")
    fragments = {
        str(item.get("answer_id")): item
        for item in result.get("unmapped_answers", [])
        if isinstance(item, dict) and item.get("answer_id")
    }
    questions = {str(item["id"]): item for item in sub_questions}
    normalized = []
    seen = set()
    for item in raw_mapping:
        if not isinstance(item, dict):
            raise ValueError("人工题号对应关系格式无效")
        answer_id = str(item.get("answer_id", "")).strip()
        target_id = str(item.get("sub_question_id", "")).strip()
        fragment = fragments.get(answer_id)
        target = questions.get(target_id)
        if not fragment or not target or answer_id in seen:
            raise ValueError("答案片段或目标小题无效，请刷新记录后重新选择")
        seen.add(answer_id)
        evidence = fragment.get("evidence")
        if not isinstance(evidence, dict):
            raise ValueError("答案片段缺少可靠图片位置，不能建立题号对应")
        normalized.append({
            "answer_id": answer_id,
            "photo_index": int(fragment["photo_index"]),
            "evidence": {
                key: round(float(evidence[key]), 6)
                for key in ("x", "y", "width", "height")
            },
            "sub_question_id": target_id,
            "label": str(target["label"]),
            "question": str(target["question"]),
        })
    if seen != set(fragments):
        raise ValueError("必须为所有待对应答案片段指定小题")
    return normalized

UNRESOLVED_ANSWER_STATUSES = {"unanswered_candidate", "question_unmatched", "image_unreadable"}

def result_has_unresolved_answer_status(result: dict[str, Any]) -> bool:
    return any(
        isinstance(item, dict) and item.get("answer_status") in UNRESOLVED_ANSWER_STATUSES
        for item in result.get("sub_questions", [])
    ) or bool(result.get("unmapped_answers"))

def build_vision_prompt_text(
    assignment_question: str,
    sub_questions: list[dict[str, Any]],
    answer_text: str = "",
    *,
    privacy_mode: bool = False,
    answer_regions: list[dict[str, Any]] | None = None,
    question_mapping: list[dict[str, Any]] | None = None,
) -> str:
    rubric = json.dumps(sub_questions, ensure_ascii=False, indent=2)
    privacy_instructions = (
        "只依据教师集中核对并确认的实际外发图片和下方题目/评分细则批改。图片可以是整页，也可以是可选裁切；"
        "不要尝试提取、推断、复述或输出姓名、学号、座号、班级、二维码或其他身份信息。"
        "只返回评分所需的答案证据。"
        if privacy_mode
        else "同时从答卷顶部的姓名、座号（或序号）区域提取学生身份；看不清或不存在时返回空字符串，绝对不要猜测。"
             "student_identity 字段格式为 {student_no, student_name, confidence, evidence}。"
    )
    answer_instruction = "" if privacy_mode else f"\n可选 OCR/教师录入文字：{answer_text or '无'}"
    region_context = ""
    if answer_regions:
        region_context = (
            "\n图片只包含教师框选并确认的答案裁切区域，不是整页。"
            "图片 photo_index 是裁切图序号；证据框必须相对于对应裁切图归一化定位。"
            "裁切区域与小题对应关系："
            + json.dumps(
                [
                    {
                        "photo_index": index,
                        "source_page": int(region["photo_index"]),
                        "sub_question_ids": region["sub_question_ids"],
                    }
                    for index, region in enumerate(answer_regions, start=1)
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "。不得从裁切区域之外推断学生答案。"
        )
    mapping_context = ""
    if question_mapping:
        mapping_context = (
            "\n教师已核对最终图片，并将此前标记的答案片段对应到以下小题；请只据此重新判断指定小题，"
            "仍须按完整评分细则独立返回每道小题，不得复用上次分数："
            + json.dumps(question_mapping, ensure_ascii=False, separators=(",", ":"))
            + "。"
        )
    return (
        "请批改这份中学历史材料题作业。学生答案可能存在 OCR 缺失；必须以图片为准。"
        "硬性证据来源规则：evidence 必须是当前答题区域中学生实际可见的手写原文或极短连续片段；不得从评分细则、标准答案、题干、印刷材料或模型自身知识中补写 evidence。若无法在学生作答区域明确看到对应文字，必须返回 uncertain/not_hit/question_unmatched，不得生成标准答案式 evidence。"
        "来源归属约束：题干、材料、地图标签、印刷文字、参考答案和rubric不是学生答案；无法确认属于学生实际手写时不得据此给分。学生确实手写抄录的材料内容仍是student_handwriting，按正常答案判断，不能仅因文字相似而拒绝给分。"
        "禁止语义补足：只判断学生已经表达的信息，不得依据历史知识、语境、rubric、参考答案或常识补上未写出的关键事实；抽象概括不能代替要求的具体事实，要求多个对应项时必须实际表达各项或真正等价的语义。"
        "严格按评分点判断，不要因为参考答案中有某个词就替学生补写。"
        "每个评分结果必须原样回传输入中的 sub_question_id 与 criterion_id，不得改名、重复、遗漏或新增 ID；遇到无法判断的点也要保留对应 ID 并将 suggested_score 设为0。"
        "每个有得分的评分点都要给出学生原文中的证据；证据不清楚或图片看不清时 needs_review=true，证据清楚时 needs_review=false。"
        "每个有得分的评分点还必须给出 evidence 对象，使用输入图片的1-based photo_index和0到1之间的归一化 x、y、width、height 框住对应手写答案；找不到可靠位置时不得给分。"
        "每道小题的评分方式、满分上限和得分点分值在评分细则中提供。若 scoring_mode=point_bank，criteria 是可接受的候选答案点池，候选点数量可能多于满分所需；"
        "判断学生实际答出的不同有效要点，每个要点只计一次，语义相同的表述不能重复计分。该模式下每个要点按配置的完整分值或0分判断；若最后一个有效要点超过剩余分值，只计剩余分值。所有要点所得分总和不得超过该小题 max_score，达到上限后多答不再加分。"
        "若 scoring_mode=weighted，则依照各评分项的分值判断，可按评分细则给部分分。"
        "学生照片可能包含多道大题或小题，也可能跨页作答。题目/材料由assignment_question提供；评分细则数组中的每个平铺小题均有独立ID、题号、题干、评分标准和满分。"
        "请逐项定位评分细则中的每个小题，只能用其自己的题干与材料判断，并分别返回独立评分记录。"
        "每项必须增加answer_status，取值只能是answered、unanswered_candidate、question_unmatched、image_unreadable。"
        "只有能清晰核实该小题在整份答卷中确实留白时，才返回unanswered_candidate；这只是AI候选，仍须教师明确确认空白后才可成为0分。"
        "题号不清或无法把一段答案可靠对应到预设小题时返回question_unmatched，并在顶层unmapped_answers数组中列出answer_id、evidence（photo_index与归一化位置框）；不得猜题号。"
        "图像内容无法读取时返回image_unreadable，不得当作未作答或0分。"
        "照片缺少某个题号不等于该题未作答；先检查跨页和无题号答案，仍不能可靠对应才标记question_unmatched。"
        "只返回 JSON，不要 Markdown。字段：confidence、summary、sub_questions、unmapped_answers。"
        "sub_questions 必须是数组；每项包含 sub_question_id、label、answer_status、criteria；criteria 每项包含 criterion_id、suggested_score、decision、student_evidence、evidence、reason、needs_review。顶层unmapped_answers必须是数组。"
        "suggested_score 是唯一用于计分的学生所得分，必须与 reason 一致；decision 只能填写 full、partial、none 或 uncertain；uncertain 时 suggested_score 必须为0并将 needs_review 设为true，不要把分数写进 decision；"
        "每个小题的 suggested_total 是该小题各项实际计分之和并受 max_score 封顶，总分是各小题得分之和。"
        "不要把question_unmatched或image_unreadable转换成0分；此类记录不能确认成绩。unanswered_candidate也不能直接作为已确认零分。"
        f"{privacy_instructions}\n材料与总体说明（assignment_question）：{assignment_question}\n评分细则（每个平铺小题独立关联题干、满分与评分点）：{rubric}{region_context}{mapping_context}{answer_instruction}"
    )


def build_scoring_prompt_text(*args, **kwargs):
    from holistic_scoring import build_prompt
    return build_prompt(build_vision_prompt_text, *args, **kwargs)


def _call_mixed_vision(assignment,questions,answer_text,paths,diagnostics,photos,privacy_confirmed,regions,anonymous_mode,mapping,model_config,diagnostic_run,invocation_scope):
    import holistic_scoring as hs
    privacy=cloud_mode_enabled() or anonymous_mode
    plan=build_scoring_prompt_text(str(assignment['question']),questions,answer_text,privacy_mode=privacy,answer_regions=regions or None,question_mapping=mapping)
    plan_hash=hashlib.sha256(plan.encode()).hexdigest()
    if privacy and (not privacy_confirmed or len(paths)!=len(photos or []) or any(p.get('approved_prompt_sha256')!=plan_hash for p in photos or [])):
        raise ValueError('混合评分计划与已确认隐私预览不一致')
    outputs=[];runs=[]
    for group in hs.groups(questions):
        selected=hs.regions_for(regions,group)
        text=build_scoring_prompt_text(str(assignment['question']),group,answer_text,privacy_mode=privacy,answer_regions=selected or None,question_mapping=mapping)
        group_hash=hashlib.sha256(text.encode()).hexdigest()
        # Derive subrequest hashes only after authenticating the complete plan.
        group_photos=[{**p,'approved_prompt_sha256':group_hash} for p in photos or []]
        group_regions=[{**r,'approved_prompt_sha256':group_hash} for r in selected]
        detail={}
        output=call_vision_model(assignment,group,answer_text,paths,detail,group_photos,privacy_confirmed,group_regions,anonymous_mode,mapping,model_config,diagnostic_run,invocation_scope)
        outputs.append(output);runs.append(dict(question_ids=[q['id'] for q in group],**detail))
    by_id={q['sub_question_id']:q for output in outputs for q in output['sub_questions']}
    combined=dict(outputs[0]);combined.update(sub_questions=[by_id[q['id']] for q in questions],
        suggested_total=sum(o['suggested_total'] for o in outputs),max_total=sum(o['max_total'] for o in outputs),
        requires_question_mapping=any(o.get('requires_question_mapping') for o in outputs),blocked=any(o.get('blocked') for o in outputs),needs_review=any(o.get('needs_review') for o in outputs),
        block_reasons=sorted({r for o in outputs for r in o.get('block_reasons',[])}),
        unmapped_answers=[r for o in outputs for r in o.get('unmapped_answers',[])])
    diagnostics.update(prompt_version='mixed-grading-v1',prompt_hash=plan_hash,input_hash=hs.digest([r.get('input_hash') for r in runs]),
        question_runs=runs,status='ok',request_count=len(runs),finish_reason=[r.get('finish_reason') for r in runs])
    return combined


def _canonical_ai_redactions(photos: list[dict[str, Any]]) -> list[list[dict[str, float]]]:
    return [normalize_ai_redactions(item.get("ai_redactions", [])) for item in photos]


def _ai_prompt_fingerprint(
    question: str,
    sub_questions: list[dict[str, Any]],
    answer_regions: list[dict[str, Any]] | None = None,
    question_mapping: list[dict[str, Any]] | None = None,
) -> str:
    prompt = build_scoring_prompt_text(
        question, sub_questions, privacy_mode=True, answer_regions=answer_regions,
        question_mapping=question_mapping,
    )
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _contains_roster_identity(conn: sqlite3.Connection, prompt: str) -> bool:
    folded = prompt.casefold()
    rows = conn.execute("SELECT student_no, student_name FROM roster_students").fetchall()
    for row in rows:
        for value in (str(row["student_name"] or "").strip(), str(row["student_no"] or "").strip()):
            if len(value) >= 2 and (not value.isdigit() or len(value) >= 4) and value.casefold() in folded:
                return True
    return False


def _contains_labeled_identity_data(prompt: str) -> bool:
    """Fail closed on explicit identity labels or QR/barcode payload fields."""
    patterns = (
        r"(?:学生姓名|姓名|student[_ -]?name|学号|student[_ -]?no|考号|准考证号|座号|班级|class[_ -]?name)\s*[:：=]\s*[^\s，,。；;]{2,}",
        r"(?:二维码|QR(?:\s*code)?|条形码)\s*(?:内容|数据|值)?\s*[:：=]\s*[^\s，,。；;]{2,}",
        r"(?<!\d)1[3-9]\d{9}(?!\d)",
        r"(?<!\d)\d{17}[\dXx](?!\d)",
    )
    return any(re.search(pattern, prompt, flags=re.IGNORECASE) for pattern in patterns)


def _sign_ai_preview(claims: dict[str, Any]) -> str:
    # Identical preview claims created in the same second must still produce
    # distinct one-time tokens. The random JTI is signed with all other claims.
    signed_claims = dict(claims)
    signed_claims["jti"] = secrets.token_urlsafe(24)
    raw = json.dumps(signed_claims, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(AI_PREVIEW_SIGNING_KEY, encoded.encode("ascii"), hashlib.sha256).digest()
    return encoded + "." + base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def _ai_preview_fingerprint(claims: dict[str, Any]) -> str:
    # The one-time JTI distinguishes tokens; it must not change the digest of
    # the teacher-reviewed content bound to the preview.
    fingerprint_claims = dict(claims)
    fingerprint_claims.pop("jti", None)
    raw = json.dumps(fingerprint_claims, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read_preview_claims(token: str) -> dict[str, Any]:
    try:
        encoded, signature_text = token.split(".", 1)
        signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        expected = hmac.new(AI_PREVIEW_SIGNING_KEY, encoded.encode("ascii"), hashlib.sha256).digest()
        if not secrets.compare_digest(signature, expected):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8"))
        if not isinstance(claims, dict):
            raise ValueError
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, base64.binascii.Error) as exc:
        raise ValueError("AI隐私预览凭证无效或已过期，请重新预览确认") from exc
    try:
        expires_at = int(claims.get("expires_at", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("AI隐私预览凭证无效或已过期，请重新预览确认") from exc
    if expires_at < int(time.time()):
        raise ValueError("AI隐私预览凭证已过期，请重新预览确认")
    return claims


def _student_crop_artifact_root(run_id: str = "") -> Path:
    """Write runtime artifacts outside immutable releases; retain legacy reads."""
    if run_id and not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("alignment_run_id 无效")
    root = (DATA_DIR / "student_auto_crop").resolve()
    if run_id and not (root / run_id / "run.json").is_file():
        legacy = (PROGRAM_DIR / "reports" / "artifacts" / "student_auto_crop_poc_2026-10-04").resolve()
        if (legacy / run_id / "run.json").is_file():
            return legacy
    return root


def _accepted_crop_answer_region_inputs(run_id: str) -> list[dict[str, Any]]:
    """Issue short-lived credentials only for crops from a passed local run."""
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("alignment_run_id 无效")
    artifact_root = _student_crop_artifact_root(run_id)
    run_dir = (artifact_root / run_id).resolve()
    if not run_dir.is_relative_to(artifact_root):
        raise ValueError("自动裁切运行路径无效")
    manifest_path = run_dir / "run.json"
    if not manifest_path.is_file():
        raise ValueError("自动裁切运行记录不存在")
    try:
        run = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("自动裁切运行记录不可读取") from exc
    padding_validation = run.get("padding_validation") or {}
    if run.get("status") != "cropped" or run.get("human_review") != "passed" or padding_validation.get("status") != "passed":
        raise ValueError("answer_region_invalid:crop_not_teacher_accepted")

    conn = get_db()
    try:
        context = _exam_layout_profile_context(conn, str(run.get("exam_id") or ""))
        if (
            not context
            or context.get("assignment_id") != str(run.get("assignment_id") or "")
            or context.get("profile_hash_mismatch")
            or context.get("profile_invalid")
            or not context.get("layout_profile_id")
            or context.get("layout_profile_id") != run.get("layout_profile_id")
            or not context.get("source_pdf_sha256")
            or context.get("source_pdf_sha256") != run.get("source_pdf_sha256")
        ):
            raise ValueError("answer_region_invalid:layout_profile_stale")
        assignment = conn.execute(
            "SELECT * FROM assignments WHERE id = ?", (context["assignment_id"],)
        ).fetchone()
        if not assignment:
            raise ValueError("answer_region_invalid:assignment_missing")
        saved = json.loads(assignment["sub_questions_json"] or "[]")
        sub_questions = clean_sub_questions(saved) if saved else clean_sub_questions(
            None,
            fallback_question=assignment["question"],
            fallback_answer=assignment["standard_answer"],
            fallback_criteria=json.loads(assignment["criteria_json"] or "[]"),
        )
        profile = context.get("layout_profile") or {}
        profile_regions = {
            str(item.get("question_id") or ""): item
            for item in profile.get("regions", [])
            if isinstance(item, dict) and item.get("region_type") == "subjective_answer"
        }
        accepted_pages = {
            int(item.get("page_number")): item
            for item in run.get("accepted_pages", [])
            if isinstance(item, dict) and str(item.get("page_number", "")).isdigit()
        }
        padding_crops = {
            str(item.get("question_id") or ""): item
            for item in padding_validation.get("crops", []) if isinstance(item, dict)
        }
        import_metadata = _json_object_or_empty(assignment["import_metadata_json"])
        result: list[dict[str, Any]] = []
        for crop in run.get("crops", []):
            if not isinstance(crop, dict):
                continue
            question_label = str(crop.get("question_id") or "")
            question = next((item for item in sub_questions if str(item.get("id") or "") == question_label or str(item.get("label") or "") == question_label), None)
            padding_crop = padding_crops.get(question_label)
            if not question or not padding_crop or padding_crop.get("visual_status") != "passed":
                continue
            question_id = str(question.get("id") or "")
            page_number = int(crop.get("page_number") or 0)
            accepted_page = accepted_pages.get(page_number)
            metrics = (accepted_page or {}).get("alignment_metrics") or {}
            reference = next((item for item in context.get("references", []) if int(item.get("page_number", -1)) == page_number), None)
            profile_region = profile_regions.get(question_label)
            bbox = crop.get("configured_bbox") or {}
            profile_bbox = (profile_region or {}).get("bbox") or {}
            bbox_matches = all(
                key in bbox and key in profile_bbox and math.isclose(float(bbox[key]), float(profile_bbox[key]), rel_tol=0, abs_tol=1e-6)
                for key in ("x1", "y1", "x2", "y2")
            )
            if (
                not accepted_page or metrics.get("quality_gate") != "passed"
                or metrics.get("alignment_status") != "aligned"
                or not reference or crop.get("reference_page_hash") != reference.get("reference_sha256")
                or int((profile_region or {}).get("page_number") or 0) != page_number
                or not bbox_matches
                or not crop.get("padding") or not crop.get("effective_crop_bbox") or not crop.get("pixel_bbox")
            ):
                continue
            padded_name = str(padding_crop.get("padded_file_name") or crop.get("padding_validation_file_name") or "")
            if not padded_name:
                continue
            crop_path = (run_dir / padded_name).resolve()
            if not crop_path.is_relative_to(run_dir) or not crop_path.is_file():
                continue
            crop_bytes = crop_path.read_bytes()
            media_type = "image/png" if crop_path.suffix.casefold() == ".png" else "image/jpeg" if crop_path.suffix.casefold() in {".jpg", ".jpeg"} else "image/webp"
            _, dimensions = validate_image_bytes(crop_bytes, media_type)
            if dimensions[0] < 1 or dimensions[1] < 1:
                continue
            _require_answer_region_rubric_ready(import_metadata, sub_questions, [question_id])
            teacher_quality = str((run.get("teacher_crop_quality") or {}).get(question_label) or "good")
            if teacher_quality not in {"good", "minor_issue"}:
                continue
            crop_sha256 = hashlib.sha256(crop_bytes).hexdigest()
            claims = {
                "kind": "accepted_answer_region",
                "assignment_id": str(assignment["id"]),
                "exam_id": str(run["exam_id"]),
                "question_id": question_id,
                "question_label": question_label,
                "page_number": page_number,
                "region_type": "subjective_answer",
                "layout_profile_id": str(context["layout_profile_id"]),
                "source_pdf_sha256": str(context["source_pdf_sha256"]),
                "reference_page_sha256": str(reference["reference_sha256"]),
                "source_student_image_sha256": str(run.get("source_student_image_hash") or ""),
                "alignment_run_id": run_id,
                "alignment_status": "aligned",
                "quality_gate": "passed",
                "crop_status": "passed",
                "teacher_crop_quality": teacher_quality,
                "configured_bbox": crop.get("configured_bbox"),
                "padding": crop.get("padding"),
                "effective_crop_bbox": crop.get("effective_crop_bbox"),
                "pixel_bbox": crop.get("pixel_bbox"),
                "crop_sha256": crop_sha256,
                "crop_width": dimensions[0],
                "crop_height": dimensions[1],
                "crop_bytes": len(crop_bytes),
                "rubric_sha256": rubric_fingerprint([question]),
                "expires_at": int(time.time()) + 30 * 60,
            }
            validate_answer_region_image(
                crop_bytes, media_type, claims,
                expected_sha256=crop_sha256, expected_dimensions=dimensions,
            )
            result.append({
                "question_id": question_id,
                "question_label": question_label,
                "page_number": page_number,
                "layout_profile_id": claims["layout_profile_id"],
                "image_role": "answer_region",
                "data_url": bytes_to_data_url(crop_bytes, media_type),
                "attestation": _sign_ai_preview(claims),
                "provenance": {key: value for key, value in claims.items() if key not in {"kind", "expires_at", "rubric_sha256"}},
            })
        if not result:
            raise ValueError("answer_region_invalid:no_accepted_crops")
        return result
    finally:
        conn.close()


def _preview_session_fingerprint(handler: Any) -> str:
    session = getattr(handler, "_web_auth_session", None)
    if session is not None and getattr(session, "csrf_token", ""):
        material = "web-session:" + str(session.csrf_token)
    elif _handler_has_valid_mobile_grading_token(handler):
        authorization = str(getattr(handler, "headers", {}).get("Authorization", ""))
        token = authorization[7:].strip()
        material = "mobile-grading-token:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
    elif cloud_mode_enabled(handler):
        raise ValueError("云端 AI 预览需要有效的教师登录会话，请重新登录后预览")
    else:
        client = getattr(handler, "client_address", ("local",))[0]
        material = "local-client:" + str(client)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _handler_has_valid_mobile_grading_token(handler: Any) -> bool:
    authorization = str(getattr(handler, "headers", {}).get("Authorization", ""))
    if not authorization.lower().startswith("bearer "):
        return False
    return valid_mobile_token(authorization[7:].strip())

def _ai_privacy_identity_fingerprint(assignment_id: str, class_name: str, student_label: str) -> str:
    material = {
        "assignment_id": str(assignment_id).strip().lower(),
        "class_name": str(class_name).strip(),
        "student_label": str(student_label).strip(),
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _consume_preview_token(fingerprint: str, expires_at: int) -> None:
    now = int(time.time())
    with PREVIEW_TOKEN_CONSUMPTIONS_LOCK:
        expired = [key for key, expiry in PREVIEW_TOKEN_CONSUMPTIONS.items() if expiry < now]
        for key in expired:
            PREVIEW_TOKEN_CONSUMPTIONS.pop(key, None)
        if fingerprint in PREVIEW_TOKEN_CONSUMPTIONS:
            raise ValueError("AI Payload 预览令牌已使用，请重新预览并确认")
        PREVIEW_TOKEN_CONSUMPTIONS[fingerprint] = max(now + 1, int(expires_at))


def _verify_ai_preview_token(
    token: str,
    assignment_id: str,
    question: str,
    sub_questions: list[dict[str, Any]],
    photos: list[dict[str, Any]],
    session_fingerprint: str = "",
    identity_fingerprint: str = "",
    answer_regions: list[dict[str, Any]] | None = None,
    question_mapping: list[dict[str, Any]] | None = None,
    require_complete_regions: bool = True,
    grading_input_mode: str = "auto_crop",
    exam_id: str = "",
) -> str:
    claims = _read_preview_claims(token)
    if (_grading_input_mode(claims) != grading_input_mode
            or (grading_input_mode == "original_photo" and str(claims.get("exam_id") or "") != exam_id)):
        raise ValueError("批改图片模式或考试与隐私预览不一致，请重新预览")
    normalized_regions = normalize_answer_regions(
        answer_regions, sub_questions, len(photos), require_complete=require_complete_regions
    ) if answer_regions is not None else []
    prompt_fingerprint = _ai_prompt_fingerprint(
        question, sub_questions, normalized_regions or None, question_mapping
    )
    sources = []
    page_images: list[bytes] = []
    for item in photos:
        raw, media_type = photo_upload_bytes(item)
        corners = normalize_photo_corners(item.get("corners"))
        normalized_corners = [[point[0], point[1]] for point in corners] if corners else None
        redactions = normalize_ai_redactions(item.get("ai_redactions", []))
        model_image = prepare_ai_preview_image_bytes(raw, media_type, redactions, corners)
        page_images.append(model_image)
        model_sha256 = hashlib.sha256(model_image).hexdigest()
        sources.append({
            "sha256": hashlib.sha256(raw).hexdigest(),
            "corners": normalized_corners,
            "redactions": redactions,
            "model_sha256": model_sha256,
        })
        item["_verified_ai_model_sha256"] = model_sha256
        item["_verified_ai_prompt_sha256"] = hashlib.sha256(
            build_scoring_prompt_text(
                question, sub_questions, privacy_mode=True,
                answer_regions=normalized_regions or None,
                question_mapping=question_mapping,
            ).encode("utf-8")
        ).hexdigest()
        item["_verified_preview_expires_at"] = int(claims["expires_at"])
    region_claims = []
    for region in normalized_regions:
        page_index = int(region["photo_index"]) - 1
        crop = crop_model_image_bytes(page_images[page_index], region)
        crop_sha256 = hashlib.sha256(crop).hexdigest()
        region["approved_model_sha256"] = crop_sha256
        region_prompt_hash = ""
        if region.get("invocation_scope") == "answer_region":
            ids = set(region["sub_question_ids"])
            selected_questions = [item for item in sub_questions if str(item.get("id")) in ids]
            if len(selected_questions) != 1:
                raise ValueError("逐小题答案区域必须且只能关联一道小题")
            region_prompt_hash = hashlib.sha256(build_scoring_prompt_text(
                str(selected_questions[0].get("question") or ""), selected_questions, privacy_mode=True, answer_regions=[region],
            ).encode("utf-8")).hexdigest()
            region["approved_prompt_sha256"] = region_prompt_hash
            item_prompt_hash = str((answer_regions or [])[len(region_claims)].get("approved_prompt_sha256") or "")
            if not item_prompt_hash or not secrets.compare_digest(item_prompt_hash, region_prompt_hash):
                raise ValueError("逐题提示词与教师预览确认不一致，请重新预览")
        region_claim = {
            key: value for key, value in region.items()
            if key not in {"approved_model_sha256", "approved_prompt_sha256"}
        }
        region_claim["model_sha256"] = crop_sha256
        if region_prompt_hash:
            region_claim["prompt_sha256"] = region_prompt_hash
        region_claims.append(region_claim)
    if (
        claims.get("kind") != "grading"
        or claims.get("assignment_id") != assignment_id
        or claims.get("prompt_sha256") != prompt_fingerprint
        or claims.get("session_sha256") != session_fingerprint
        or claims.get("identity_sha256") != identity_fingerprint
        or claims.get("images") != sources
        or claims.get("answer_regions") != region_claims
        or not sources
    ):
        raise ValueError("AI隐私预览与本次题目或图片不一致，请重新预览确认")
    # Carry the server-calculated crop fingerprints into the immutable queued
    # record so grading can prove it is sending those exact crops later.
    if isinstance(answer_regions, list):
        for index, region in enumerate(normalized_regions):
            if index < len(answer_regions) and isinstance(answer_regions[index], dict):
                answer_regions[index]["approved_model_sha256"] = region["approved_model_sha256"]
                if region.get("approved_prompt_sha256"):
                    answer_regions[index]["approved_prompt_sha256"] = region["approved_prompt_sha256"]
    for item in photos:
        item["_verified_grading_input_mode"] = grading_input_mode
    return hashlib.sha256(token.split(".", 1)[0].encode("ascii")).hexdigest()


def call_vision_model(
    assignment: sqlite3.Row,
    sub_questions: list[dict[str, Any]],
    answer_text: str,
    photo_paths: list[Path],
    diagnostics: dict[str, Any] | None = None,
    photo_meta: list[dict[str, Any]] | None = None,
    privacy_confirmed: bool = False,
    answer_regions: list[dict[str, Any]] | None = None,
    anonymous_mode: bool = False,
    question_mapping: list[dict[str, Any]] | None = None,
    model_config_override: dict[str, str] | None = None,
    diagnostic_run: bool = False,
    invocation_scope: str = "whole_paper",
) -> dict[str, Any]:
    from holistic_draft import require_supported
    require_supported(sub_questions)
    from holistic_scoring import groups
    if len(groups(sub_questions))>1:
        return _call_mixed_vision(assignment,sub_questions,answer_text,photo_paths,diagnostics if diagnostics is not None else {},photo_meta,privacy_confirmed,answer_regions,anonymous_mode,question_mapping,model_config_override,diagnostic_run,invocation_scope)
    model_call_started_at = time.perf_counter()
    call_diagnostics = diagnostics if diagnostics is not None else {}
    if diagnostic_run and cloud_mode_enabled():
        raise ValueError("含答案内容的诊断 artifact 只允许保存在本地 Beta")
    private_artifact_dir = None if cloud_mode_enabled() else preflight_private_model_diagnostic_dir()
    call_id = uuid.uuid4().hex
    privacy_mode = cloud_mode_enabled() or anonymous_mode
    if privacy_mode and (not privacy_confirmed or len(photo_paths) != len(photo_meta or [])):
        raise ValueError("云端批改缺少有效的 AI 隐私预览确认，已阻止模型请求")
    normalized_regions = answer_regions or []
    if anonymous_mode:
        normalized_regions = normalize_answer_regions(
            normalized_regions, sub_questions, len(photo_paths)
        )
    model_config = model_config_override or {}
    configured_base_url = str(model_config.get("base_url") or os.environ["HISTORY_GRADER_BASE_URL"])
    base_url, _ = validate_model_base_url(configured_base_url)
    endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    model = str(model_config.get("model") or os.environ["HISTORY_GRADER_MODEL"])
    provider = str(model_config.get("provider") or os.getenv("HISTORY_GRADER_PROVIDER", "")).strip().casefold()
    invocation_scope = str(invocation_scope or "whole_paper").strip()
    if invocation_scope not in INVOCATION_SCOPES:
        raise ValueError("模型调用范围无效")
    prompt_text = build_scoring_prompt_text(
        str(assignment["question"]),
        sub_questions,
        answer_text,
        privacy_mode=privacy_mode,
        answer_regions=normalized_regions or None,
        question_mapping=question_mapping,
    )
    if privacy_mode:
        actual_prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
        if invocation_scope == "answer_region" and len(normalized_regions) == 1:
            expected_prompt_hashes = {str(normalized_regions[0].get("approved_prompt_sha256", ""))}
        else:
            expected_prompt_hashes = {
                str(item.get("approved_prompt_sha256", "")) for item in (photo_meta or [])
            }
        if len(expected_prompt_hashes) != 1 or not secrets.compare_digest(
            actual_prompt_hash, next(iter(expected_prompt_hashes))
        ):
            raise ValueError("云端批改提示词已偏离教师确认的 AI Payload，已阻止模型请求")
    grounded = bool(sub_questions and sub_questions[0].get("scoring_mode") == "holistic")
    essay_plan = json.loads(prompt_text) if grounded else None
    user_content: list[dict[str, Any]] = [{"type": "text", "text": essay_plan["transcription_prompt"] if grounded else prompt_text}]
    model_photo_bytes = 0
    page_image_bytes: list[bytes] = []
    for index, path in enumerate(photo_paths):
        redactions = (photo_meta or [])[index].get("ai_redactions", []) if privacy_mode else []
        image_bytes = prepare_model_image(path, redactions)
        if privacy_mode:
            expected_image_hash = str((photo_meta or [])[index].get("approved_ai_page_sha256", ""))
            actual_image_hash = hashlib.sha256(image_bytes).hexdigest()
            if expected_image_hash and not secrets.compare_digest(actual_image_hash, expected_image_hash):
                raise ValueError("服务端处理页与教师确认的来源图不一致，已阻止模型请求")
        page_image_bytes.append(image_bytes)
    outbound_images: list[bytes] = []
    if anonymous_mode and normalized_regions:
        for region in normalized_regions:
            page_index = int(region["photo_index"]) - 1
            if page_index < 0 or page_index >= len(page_image_bytes):
                raise ValueError("裁切区域引用的答卷页不存在，已阻止模型请求")
            crop_bytes = crop_model_image_bytes(page_image_bytes[page_index], region)
            expected_crop_hash = str(region.get("approved_model_sha256", ""))
            actual_crop_hash = hashlib.sha256(crop_bytes).hexdigest()
            if not expected_crop_hash or not secrets.compare_digest(actual_crop_hash, expected_crop_hash):
                raise ValueError("实际裁切图与教师确认的 AI Payload 不一致，已阻止模型请求")
            outbound_images.append(crop_bytes)
    else:
        outbound_images = page_image_bytes
    for index, image_bytes in enumerate(outbound_images, start=1):
        model_photo_bytes += len(image_bytes)
        encoded = base64.b64encode(image_bytes).decode("ascii")
        user_content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}
        )
    openai_reasoning_model = provider == "openai" and model in {"gpt-5.4", "gpt-5.4-mini"}
    output_limit_type = "max_completion_tokens" if provider in {"qwen", "mimo"} or openai_reasoning_model else "max_tokens"
    output_limit_value = configured_model_max_tokens()
    payload = {
        "model": model,
        "temperature": 0,
        output_limit_type: output_limit_value,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "你是一个谨慎的中学历史教师批改助手，不能直接发布正式成绩。",
            },
            {"role": "user", "content": user_content},
        ],
    }
    payload.update(deepseek_json_payload_options(provider, model))
    if openai_reasoning_model:
        # GPT-5.4 rejects temperature when reasoning_effort is not "none".
        payload.pop("temperature", None)
    input_fingerprint = hashlib.sha256()
    input_fingerprint.update(prompt_text.encode("utf-8"))
    for image_bytes in outbound_images:
        input_fingerprint.update(len(image_bytes).to_bytes(8, "big"))
        input_fingerprint.update(hashlib.sha256(image_bytes).digest())
    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    input_hash = input_fingerprint.hexdigest()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    processed_bytes = 0
    for path in photo_paths:
        try:
            processed_bytes += path.stat().st_size
        except OSError:
            pass
    request_images = []
    for image_bytes in outbound_images:
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                width, height = image.size
        except (OSError, ValueError):
            width = height = None
        request_images.append(
            {
                "width": width,
                "height": height,
                "bytes": len(image_bytes),
                "sha256": hashlib.sha256(image_bytes).hexdigest(),
            }
        )
    timeout = float(os.getenv("HISTORY_GRADER_TIMEOUT", "180"))
    request_metadata = {
        "provider": provider,
        "model_id": model,
        "invocation_scope": invocation_scope,
        "input_mode": invocation_scope,
        "output_limit_type": output_limit_type,
        "output_limit_value": output_limit_value,
        "reasoning_effort": payload.get("reasoning_effort"),
        "max_tokens": payload.get("max_tokens"),
        "max_completion_tokens": payload.get("max_completion_tokens"),
        "preserve_thinking": payload.get("preserve_thinking"),
        "thinking": payload.get("thinking"),
        "timeout_seconds": timeout,
        "prompt_hash": prompt_hash,
        "input_hash": input_hash,
        "image_count": len(request_images),
        "image_width": request_images[0]["width"] if len(request_images) == 1 else [item["width"] for item in request_images],
        "image_height": request_images[0]["height"] if len(request_images) == 1 else [item["height"] for item in request_images],
        "image_bytes": request_images[0]["bytes"] if len(request_images) == 1 else [item["bytes"] for item in request_images],
        "images": request_images,
    }
    if invocation_scope == "answer_region" and len(normalized_regions) == 1:
        answer_region = normalized_regions[0]
        request_metadata.update({
            "answer_region_id": answer_region["region_id"],
            "request_crop_hash": request_images[0]["sha256"] if request_images else None,
            "request_crop_width": request_images[0]["width"] if request_images else None,
            "request_crop_height": request_images[0]["height"] if request_images else None,
            "profile_id": answer_region.get("profile_id"),
            # Provenance is diagnostics only, never part of the provider payload.
            "crop_provenance": [
                {key: photo.get("answer_region_provenance", {}).get(key) for key in (
                    "question_id", "page_number", "configured_bbox", "effective_crop_bbox",
                    "crop_sha256", "source_student_image_sha256", "reference_page_sha256",
                    "alignment_run_id", "alignment_status", "quality_gate", "layout_profile_id",
                )}
                for photo in (photo_meta or []) if photo.get("image_role") == "answer_region"
                and photo.get("answer_region_provenance", {}).get("question_id") in answer_region.get("sub_question_ids", [])
            ],
        })
    call_diagnostics.update({
        "provider": provider,
        "model_id": model,
        "invocation_scope": invocation_scope,
        "input_hash": input_hash,
        "prompt_version": sub_questions[0]["holistic"].get("scoring_version", "holistic-essay-grading-v1") if sub_questions and sub_questions[0].get("scoring_mode")=="holistic" else VISION_PROMPT_VERSION,
        "prompt_hash": prompt_hash,
        "diagnostic_run": bool(diagnostic_run),
        "diagnostic_artifact_id": call_id if private_artifact_dir is not None else None,
        "request_metadata": request_metadata,
        "photo_count": len(photo_paths),
        "answer_crop_count": len(normalized_regions) if anonymous_mode else 0,
        "answer_region_id": normalized_regions[0].get("region_id") if invocation_scope == "answer_region" and len(normalized_regions) == 1 else None,
        "request_crop_hash": request_metadata.get("request_crop_hash"),
        "processed_photo_bytes": processed_bytes,
        "model_photo_bytes": model_photo_bytes,
        "model_image_long_edge": MODEL_IMAGE_LONG_EDGE,
        "model_max_tokens": configured_model_max_tokens(),
        "request_body_bytes": len(body),
    })
    headers = {"Content-Type": "application/json"}
    api_key = str(model_config.get("api_key") or os.getenv("HISTORY_GRADER_API_KEY", ""))
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(endpoint, data=body, headers=headers, method="POST")
    content = None
    raw_capture: dict[str, Any] = {}
    parser_output = None
    normalized = None
    transcribed = None
    transcription_text = None
    response_stage = "request_or_response"
    stage_error = None

    def persist_artifact() -> None:
        if private_artifact_dir is None:
            return
        finished_ms = elapsed_ms(model_call_started_at)
        call_diagnostics["latency_ms"] = finished_ms
        call_diagnostics["model_call_ms"] = finished_ms
        artifact = {
            "artifact_schema_version": MODEL_CALL_DIAGNOSTIC_SCHEMA_VERSION,
            "call_id": call_id,
            "created_at": utc_now(),
            "run_id": call_diagnostics.get("run_id"),
            "diagnostic_run": bool(diagnostic_run),
            "teacher_final_available": False,
            "request": request_metadata,
            "response": {
                "provider_response_id": call_diagnostics.get("provider_response_id"),
                "http_status": call_diagnostics.get("http_status"),
                "provider_response_status": call_diagnostics.get("provider_response_status"),
                "finish_reason": call_diagnostics.get("finish_reason"),
                "usage": call_diagnostics.get("token_usage"),
                "latency_ms": finished_ms,
                "provider_error": call_diagnostics.get("provider_error") or call_diagnostics.get("provider_error_summary"),
            },
            "assistant_content_original": raw_capture.get("assistant_content_original"),
            "raw_assistant_content": content,
            "parser_input": raw_capture.get("parser_input"),
            "parser_output": parser_output,
            "normalized_result": normalized,
            "transcription": transcribed,
            "transcription_response": transcription_text,
            "stage_presence": {
                "raw_question_ids": _raw_text_sub_question_ids(content or ""),
                "parser_question_ids": _response_sub_question_ids(parser_output),
                "normalized_question_ids": [str(item.get("sub_question_id") or "") for item in (normalized or {}).get("sub_questions", []) if isinstance(item, dict)],
                "normalized_answer_statuses": {
                    str(item.get("sub_question_id") or ""): item.get("answer_status")
                    for item in (normalized or {}).get("sub_questions", []) if isinstance(item, dict)
                },
            },
            "error": stage_error,
        }
        try:
            persist_private_model_call_artifact(artifact)
        except Exception as exc:
            call_diagnostics["diagnostic_artifact_error"] = type(exc).__name__
            if diagnostic_run:
                raise

    try:
        if grounded:
            from open_response_grounding import readable_inputs, transcriptions
            recognition_diagnostics = {}
            response_stage = "transcription"
            with open_model_request(request, timeout=timeout) as response:
                transcription_text = read_model_chat_content(response, recognition_diagnostics)
            call_diagnostics['transcription_finish_reason'] = recognition_diagnostics.get('finish_reason')
            if recognition_diagnostics.get('finish_reason') in ('length', 'content_filter'):
                raise ValueError('开放题转写被截断或过滤，请核对原图')
            transcribed = transcriptions(extract_json(transcription_text), sub_questions, len(outbound_images))
            readable = readable_inputs(transcribed)
            if readable:
                response_stage = 'transcript_scoring'
                score_text = essay_plan['scoring_prompt'] + '\n本次可评分转写：' + json.dumps(readable, ensure_ascii=False)
                score_payload = {**payload, 'messages': [payload['messages'][0], {
                    'role': 'user', 'content': [{'type': 'text', 'text': score_text}]
                }]}
                request = Request(endpoint, data=json.dumps(score_payload, ensure_ascii=False).encode('utf-8'), headers=headers, method='POST')
                with open_model_request(request, timeout=timeout) as response:
                    content = read_model_chat_content(response, call_diagnostics, raw_capture)
            else:
                content = '{"sub_questions":[]}'
                call_diagnostics.update(recognition_diagnostics)
        else:
            with open_model_request(request, timeout=timeout) as response:
                content = read_model_chat_content(response, call_diagnostics, raw_capture)
    except Exception as exc:
        call_diagnostics["provider_error_summary"] = classify_model_error(exc)
        stage_error = {"stage": response_stage, **classify_model_error(exc)}
        if response_stage == "transcription" and isinstance(exc, ValueError):
            stage_error["validation_detail"] = str(exc)[:250]
        persist_artifact()
        raise

    try:
        parser_output = extract_json(content)
    except Exception as exc:
        stage_error = {"stage": "parser", "exception_type": type(exc).__name__, "message": str(exc)[:500]}
        call_diagnostics["parse_error"] = stage_error
        persist_artifact()
        raise

    try:
        if not any(q.get("scoring_mode")=="holistic" for q in sub_questions):
            parser_output = validate_grading_response(parser_output)
        call_diagnostics["schema_validation"] = "passed"
    except Exception as exc:
        stage_error = {"stage": "schema_validation", "exception_type": type(exc).__name__, "message": str(exc)[:500]}
        call_diagnostics["schema_validation_error"] = stage_error
        persist_artifact()
        raise

    try:
        if grounded:
            from open_response_grounding import normalize_grounded
            normalized = normalize_grounded(parser_output, sub_questions, transcribed, len(outbound_images))
        else:
            normalized = normalize_sub_question_result(parser_output, sub_questions, len(photo_paths))
        if anonymous_mode:
            map_crop_evidence_to_source(normalized, normalized_regions)
        if privacy_mode:
            normalized["student_identity"] = normalize_student_identity(None)
    except Exception as exc:
        stage_error = {"stage": "normalization", "exception_type": type(exc).__name__, "message": str(exc)[:500]}
        call_diagnostics["normalization_error"] = stage_error
        persist_artifact()
        raise

    persist_artifact()
    return normalized


def evidence_rubric_echo_suspected(evidence: Any, criterion: dict[str, Any]) -> bool:
    """Flag long evidence that nearly repeats a saved rubric/answer phrase."""
    def fold(value: Any) -> str:
        return re.sub(r"[\W_]+", "", str(value or "").casefold())

    evidence_key = fold(evidence)
    if len(evidence_key) < 8:
        return False
    candidates = [criterion.get(key) for key in ("label", "standard_answer", "answer", "description")]
    candidates.extend(criterion.get("keywords", []) if isinstance(criterion.get("keywords"), list) else [])
    for candidate in candidates:
        candidate_key = fold(candidate)
        if len(candidate_key) < 8:
            continue
        if evidence_key in candidate_key or candidate_key in evidence_key:
            if min(len(evidence_key), len(candidate_key)) / max(len(evidence_key), len(candidate_key)) >= 0.72:
                return True
        if difflib.SequenceMatcher(None, evidence_key, candidate_key, autojunk=False).ratio() >= 0.92:
            return True
    return False


def model_failure_result(
    sub_questions: list[dict[str, Any]], diagnostics: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return an explicit retryable state instead of inventing a score."""
    sub_results = []
    for sub_question in sub_questions:
        criteria = [
            {
                "criterion_id": item["id"],
                "criterion": item["label"],
                "max_score": float(item["score"]),
                "suggested_score": 0.0,
                "decision": "model_error",
                "student_evidence": "",
                "reason": "视觉模型调用失败，未生成有效评分。",
                "needs_review": True,
            }
            for item in sub_question["criteria"]
        ]
        sub_results.append(
            {
                "mode": "vision-model-error",
                "suggested_total": 0.0,
                "max_total": float(sub_question["max_score"]),
                "scoring_mode": sub_question.get("scoring_mode", "weighted"),
                "point_value": float(sub_question.get("point_value", 2)),
                "confidence": 0.0,
                "needs_review": True,
                "summary": "视觉模型调用失败，当前没有有效评分。",
                "sub_question_id": sub_question["id"],
                "label": sub_question["label"],
                "question": sub_question["question"],
                "criteria": criteria,
            }
        )
    return {
        "mode": "vision-model-error",
        "suggested_total": 0.0,
        "max_total": total_sub_questions(sub_questions),
        "confidence": 0.0,
        "needs_review": True,
        "blocked": True,
        "block_reasons": ["视觉模型调用失败"],
        "retryable": True,
        "diagnostics": diagnostics or {},
        "summary": "视觉模型调用失败，未生成有效分数。请检查接口配置后重新提交；此记录不能确认。",
        "student_identity": normalize_student_identity(None),
        "sub_questions": sub_results,
    }


def image_quality_failure_result(
    sub_questions: list[dict[str, Any]],
    issues: list[str],
) -> dict[str, Any]:
    """Return a retryable result without calling the vision model."""
    sub_results = []
    for sub_question in sub_questions:
        criteria = [
            {
                "criterion_id": item["id"],
                "criterion": item["label"],
                "max_score": float(item["score"]),
                "suggested_score": None,
                "decision": "unreadable",
                "student_evidence": "",
                "reason": "照片质量门禁未通过，未调用视觉模型。",
                "needs_review": True,
                "evidence_missing": True,
            }
            for item in sub_question["criteria"]
        ]
        sub_results.append(
            {
                "mode": "image-quality-error",
                "suggested_total": None,
                "max_total": float(sub_question["max_score"]),
                "scoring_mode": sub_question.get("scoring_mode", "weighted"),
                "point_value": float(sub_question.get("point_value", 2)),
                "confidence": 0.0,
                "needs_review": True,
                "blocked": True,
                "block_reasons": issues,
                "summary": "照片质量不满足判分条件。",
                "sub_question_id": sub_question["id"],
                "label": sub_question["label"],
                "question": sub_question["question"],
                "criteria": criteria,
            }
        )
    return {
        "mode": "image-quality-error",
        "suggested_total": None,
        "max_total": total_sub_questions(sub_questions),
        "confidence": 0.0,
        "needs_review": True,
        "blocked": True,
        "block_reasons": issues,
        "retryable": True,
        "quality_issues": issues,
        "diagnostics": {
            "error_category": "image_quality_gate",
            "retryable": False,
            "message": "照片质量门禁未通过，未调用视觉模型",
        },
        "summary": "照片质量门禁未通过，未调用视觉模型。请重新拍摄清晰、完整的试卷后再提交。",
        "student_identity": normalize_student_identity(None),
        "sub_questions": sub_results,
    }


def answer_region_invalid_result(
    sub_questions: list[dict[str, Any]], issues: list[str]
) -> dict[str, Any]:
    category = "answer_region_unavailable" if any("answer_region_unavailable:" in issue for issue in issues) else "answer_region_invalid"
    result = model_failure_result(sub_questions, {
        "error_category": category,
        "retryable": False,
        "issues": issues,
    })
    result.update({
        "mode": "answer-region-invalid",
        "blocked": True,
        "needs_review": True,
        "retryable": False,
        "block_reasons": issues,
        "summary": "小题答题区图片或来源凭证无效，已阻止模型请求。",
    })
    result.setdefault("diagnostics", {})["error_category"] = category
    return result


def configured_model_max_tokens() -> int:
    """Keep model responses bounded while allowing a local non-secret override."""
    try:
        value = int(os.getenv("HISTORY_GRADER_MAX_TOKENS", "6000"))
    except (TypeError, ValueError):
        value = 6000
    return max(2000, min(16000, value))


def _dual_point_rows(result: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for sub_question in result.get("sub_questions", []) if isinstance(result, dict) else []:
        if not isinstance(sub_question, dict):
            continue
        question_id = str(sub_question.get("sub_question_id") or "")
        for point in sub_question.get("criteria", []) if isinstance(sub_question.get("criteria"), list) else []:
            if isinstance(point, dict):
                point_id = str(point.get("criterion_id") or "")
                if question_id and point_id:
                    rows[(question_id, point_id)] = point
    return rows


def _point_claims_credit(point: dict[str, Any] | None) -> bool:
    if not point:
        return False
    if str(point.get("decision") or "").casefold() in {"full", "partial", "hit"}:
        return True
    for key in ("claimed_score", "suggested_score", "awarded_score"):
        try:
            if float(point.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def compare_dual_review_results(result_a: dict[str, Any], result_b: dict[str, Any]) -> dict[str, Any]:
    """Compare normalized point results; aggregate score equality never hides point conflicts."""
    points_a = _dual_point_rows(result_a)
    points_b = _dual_point_rows(result_b)
    keys = sorted(set(points_a) | set(points_b))
    point_comparisons: list[dict[str, Any]] = []
    reasons: set[str] = set()
    high = False
    medium = False
    low = False
    for question_id, point_id in keys:
        point_a = points_a.get((question_id, point_id))
        point_b = points_b.get((question_id, point_id))
        if point_a is None or point_b is None:
            high = True
            reasons.add("rubric_point_missing")
        decision_a = str((point_a or {}).get("decision") or "missing")
        decision_b = str((point_b or {}).get("decision") or "missing")
        score_a = (point_a or {}).get("suggested_score")
        score_b = (point_b or {}).get("suggested_score")
        try:
            score_diff = abs(float(score_a) - float(score_b)) > 1e-9
        except (TypeError, ValueError):
            score_diff = True
        evidence_a = str((point_a or {}).get("student_evidence") or "")
        evidence_b = str((point_b or {}).get("student_evidence") or "")
        echo_suspected = bool(
            (point_a or {}).get("rubric_echo_suspected")
            or (point_b or {}).get("rubric_echo_suspected")
        )
        if echo_suspected:
            # Similarity is a hint. It never sets high/medium/low by itself;
            # real conflicts and invalid results retain their existing gates.
            reasons.add("rubric_echo_suspected")
        evidence_key_a = re.sub(r"[\W_]+", "", evidence_a.casefold())
        evidence_key_b = re.sub(r"[\W_]+", "", evidence_b.casefold())
        evidence_missing = bool(
            (_point_claims_credit(point_a) and (point_a.get("evidence_missing") or not evidence_key_a))
            or (_point_claims_credit(point_b) and (point_b.get("evidence_missing") or not evidence_key_b))
        )
        decision_diff = decision_a != decision_b
        if decision_diff:
            high = True
            reasons.add("rubric_decision_disagreement")
        if score_diff:
            high = True
            reasons.add("score_disagreement")
        if evidence_missing:
            high = True
            reasons.add("evidence_missing")
        evidence_diff = evidence_key_a != evidence_key_b
        evidence_window_diff = bool(
            evidence_diff and evidence_key_a and evidence_key_b
            and (evidence_key_a in evidence_key_b or evidence_key_b in evidence_key_a)
        )
        if evidence_diff:
            reasons.add("evidence_difference")
            if evidence_window_diff:
                low = True
            else:
                medium = True
        point_comparisons.append({
            "question_id": question_id,
            "rubric_point_id": point_id,
            "decision_a": decision_a,
            "decision_b": decision_b,
            "score_a": score_a,
            "score_b": score_b,
            "evidence_a": evidence_a,
            "evidence_b": evidence_b,
            "reason_a": str((point_a or {}).get("reason") or ""),
            "reason_b": str((point_b or {}).get("reason") or ""),
            "decision_conflict": decision_diff,
            "score_conflict": score_diff,
            "evidence_difference": evidence_diff,
            "rubric_echo_suspected": echo_suspected,
        })
    if result_a.get("suggested_total") != result_b.get("suggested_total"):
        try:
            if abs(float(result_a.get("suggested_total")) - float(result_b.get("suggested_total"))) > 1e-9:
                high = True
                reasons.add("total_score_disagreement")
        except (TypeError, ValueError):
            high = True
            reasons.add("total_score_unavailable")
    else:
        # Equal totals can still hide different rubric decisions. Keep that
        # disagreement explicit so the teacher review queue does not treat it
        # as agreement merely because the sums happen to match.
        if any(item.get("decision_conflict") for item in point_comparisons):
            reasons.add("same_score_different_rubric")
    invalid_states = {"unanswered_candidate", "question_unmatched", "image_unreadable"}
    for result in (result_a, result_b):
        if result.get("blocked") or any(
            item.get("answer_status") in invalid_states
            for item in result.get("sub_questions", []) if isinstance(item, dict)
        ):
            high = True
            reasons.add("model_result_requires_review")
    import holistic_scoring as hs
    ha={q['sub_question_id']:q for q in result_a.get('sub_questions',[]) if q.get('scoring_mode')=='holistic'}
    hb={q['sub_question_id']:q for q in result_b.get('sub_questions',[]) if q.get('scoring_mode')=='holistic'}
    holistic_comparisons=[]
    for qid in sorted(set(ha)|set(hb)):
        issues=hs.compare(ha.get(qid,{}),hb.get(qid,{}))
        if issues:high=True;reasons.update(issues)
        holistic_comparisons.append(dict(question_id=qid,reasons=issues,level_a=(ha.get(qid,{}).get('holistic_result') or {}).get('level_id'),level_b=(hb.get(qid,{}).get('holistic_result') or {}).get('level_id')))
    risk_level = "HIGH" if high else "MEDIUM" if medium else "LOW" if low else "NO_CONFLICT"
    return {
        "holistic_comparisons": holistic_comparisons,
        "risk_level": risk_level,
        "review_reasons": sorted(reasons),
        "point_comparisons": point_comparisons,
        "score_a": result_a.get("suggested_total"),
        "score_b": result_b.get("suggested_total"),
    }


def normalize_teacher_dual_point_decisions(
    dual_review: dict[str, Any] | None,
    submitted: Any,
) -> list[dict[str, Any]]:
    """Validate and resolve point-level teacher choices against stored normalized A/B results."""
    if submitted in (None, []):
        return []
    if not isinstance(dual_review, dict) or not isinstance(submitted, list) or len(submitted) > 200:
        raise ValueError("评分点终评记录格式无效")
    results = dual_review.get("results") if isinstance(dual_review.get("results"), dict) else {}
    rows_a = _dual_point_rows(results.get("A", {}))
    rows_b = _dual_point_rows(results.get("B", {}))
    valid_choices = {
        "teacher_full": ("teacher", "full"),
        "teacher_none": ("teacher", "none"),
        "teacher_partial": ("teacher", "partial"),
        "teacher_uncertain": ("teacher", "uncertain"),
        "both_wrong_custom_hit": ("both_wrong_custom", "full"),
        "both_wrong_custom_not_hit": ("both_wrong_custom", "none"),
        "both_wrong_custom_uncertain": ("both_wrong_custom", "uncertain"),
    }
    seen: set[tuple[str, str]] = set()
    normalized: list[dict[str, Any]] = []
    for item in submitted:
        if not isinstance(item, dict):
            raise ValueError("评分点终评记录格式无效")
        question_id = str(item.get("question_id") or "")[:80]
        rubric_point_id = str(item.get("rubric_point_id") or "")[:80]
        key = (question_id, rubric_point_id)
        if not all(key) or key in seen:
            raise ValueError("评分点终评包含空编号或重复项")
        seen.add(key)
        choice = str(item.get("choice") or "")
        if choice in {"model_a", "support_A"}:
            source = rows_a.get(key)
            if source is None:
                raise ValueError("该评分点没有可采用的 AI-A 结果")
            decision = str(source.get("decision") or "uncertain")
            source_label = "AI-A"
            resolution = "support_A"
        elif choice in {"model_b", "support_B"}:
            source = rows_b.get(key)
            if source is None:
                raise ValueError("该评分点没有可采用的 AI-B 结果")
            decision = str(source.get("decision") or "uncertain")
            source_label = "AI-B"
            resolution = "support_B"
        elif choice == "support_both":
            source_a = rows_a.get(key)
            source_b = rows_b.get(key)
            if source_a is None or source_b is None:
                raise ValueError("该评分点缺少任一模型结果，不能记录为同时支持双方")
            decision_a = str(source_a.get("decision") or "uncertain")
            decision_b = str(source_b.get("decision") or "uncertain")
            try:
                score_a = float(source_a.get("suggested_score"))
                score_b = float(source_b.get("suggested_score"))
                scores_match = math.isfinite(score_a) and math.isfinite(score_b) and abs(score_a - score_b) <= 1e-9
            except (TypeError, ValueError):
                scores_match = False
            if decision_a != decision_b or not scores_match or decision_a in {"missing", "conflict"}:
                raise ValueError("只有 AI-A 与 AI-B 的评分点判断和分数都一致时，才能选择同时支持双方")
            decision = decision_a
            source_label = "AI-A+AI-B"
            resolution = "support_both"
        elif choice in valid_choices:
            source_label, decision = valid_choices[choice]
            resolution = "both_wrong_custom" if source_label == "both_wrong_custom" else "custom"
        else:
            raise ValueError("评分点终评选项无效")
        if decision not in {"full", "partial", "none", "uncertain", "missing", "conflict"}:
            raise ValueError("模型评分点判断无法用于终评记录")
        normalized.append({
            "question_id": question_id,
            "rubric_point_id": rubric_point_id,
            "decision": decision,
            "teacher_decision": {
                "full": "hit", "none": "not_hit", "uncertain": "uncertain", "partial": "partial"
            }.get(decision, "uncertain"),
            "resolution": resolution,
            "source": source_label,
        })
    return normalized


def teacher_review_completeness(
    sub_questions: list[dict[str, Any]], point_decisions: list[dict[str, Any]]
) -> str:
    """Only mark a review point-complete when every configured point was saved."""
    expected = {
        (str(question.get("id") or ""), str(point.get("id") or ""))
        for question in sub_questions
        for point in question.get("criteria", [])
        if question.get("id") and point.get("id")
    }
    reviewed = {
        (str(item.get("question_id") or ""), str(item.get("rubric_point_id") or ""))
        for item in point_decisions
        if item.get("decision", {"hit": "full", "not_hit": "none"}.get(item.get("teacher_decision"))) in {"full", "none"}
    }
    return "point_level_complete" if expected and reviewed == expected else "total_only"


def capped_point_credit(claimed: float, maximum: float, accumulated: float) -> float:
    return grading_core.capped_point_credit(claimed, maximum, accumulated)


def teacher_deterministic_score(sub_questions, point_decisions):
    return grading_core.teacher_deterministic_score(sub_questions, point_decisions)


def _grade_dual_answer_regions_for_side(
    assignment: sqlite3.Row,
    sub_questions: list[dict[str, Any]],
    photo_paths: list[Path],
    photo_meta: list[dict[str, Any]] | None,
    model_config: dict[str, str],
    answer_regions: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    *,
    diagnostic_run: bool = False,
    run_id: str,
) -> dict[str, Any]:
    """Call one model once per configured question crop, then join its points."""
    scoped_questions, scope_metadata = resolve_grading_scope(sub_questions, answer_regions)
    by_id = {str(item["id"]): item for item in scoped_questions}
    question_results: dict[str, dict[str, Any]] = {}
    question_runs: list[dict[str, Any]] = []
    question_errors: list[dict[str, Any]] = []
    for region in answer_regions:
        ids = region.get("sub_question_ids") or []
        if len(ids) != 1 or str(ids[0]) not in by_id:
            raise ValueError("逐小题裁切必须且只能关联一道已配置小题")
        question_id = str(ids[0])
        sub_question = by_id[question_id]
        call_diagnostics: dict[str, Any] = {
            "run_id": run_id,
            "question_id": question_id,
            "answer_region_id": region.get("region_id"),
            "profile_id": region.get("profile_id"),
        }
        scoped_assignment = dict(assignment)
        # Only the current sub-question prompt/context is sent with its answer crop.
        scoped_assignment["question"] = str(sub_question.get("question") or "")
        try:
            result = call_vision_model(
                scoped_assignment,
                [sub_question],
                "",
                photo_paths,
                call_diagnostics,
                photo_meta,
                True,
                [region],
                True,
                None,
                model_config,
                diagnostic_run,
                "answer_region",
            )
            question_results[question_id] = result["sub_questions"][0]
        except Exception as exc:
            error = classify_model_error(exc)
            question_errors.append({
                "question_id": question_id,
                "answer_region_id": region.get("region_id"),
                "error_category": error.get("error_category", "unknown"),
            })
            failure = normalize_sub_question_result(
                {"sub_questions": [{"sub_question_id": question_id, "answer_status": "question_unmatched"}]},
                [sub_question],
                len(photo_paths),
            )
            question_results[question_id] = failure["sub_questions"][0]
        request = call_diagnostics.get("request_metadata") or {}
        question_runs.append({
            "question_id": question_id,
            "answer_region_id": region.get("region_id"),
            "profile_id": region.get("profile_id"),
            "request_crop_hash": request.get("request_crop_hash"),
            "request_crop_width": request.get("request_crop_width"),
            "request_crop_height": request.get("request_crop_height"),
            "input_hash": call_diagnostics.get("input_hash"),
            "prompt_hash": call_diagnostics.get("prompt_hash"),
            "prompt_version": call_diagnostics.get("prompt_version"),
            "latency_ms": call_diagnostics.get("latency_ms"),
            "token_usage": call_diagnostics.get("token_usage"),
            "finish_reason": call_diagnostics.get("finish_reason"),
            "http_status": call_diagnostics.get("http_status"),
            "diagnostic_artifact_id": call_diagnostics.get("diagnostic_artifact_id"),
            "request_metadata": request,
        })

    ordered_results = [question_results.get(str(item["id"])) for item in scoped_questions]
    complete = all(item is not None for item in ordered_results)
    all_answered = complete and all(
        item.get("answer_status") == "answered" and item.get("suggested_total") is not None
        for item in ordered_results
    )
    block_reasons = sorted({
        reason
        for item in ordered_results if item
        for reason in item.get("block_reasons", [])
    })
    if question_errors:
        block_reasons.append("至少一个小题的独立模型调用失败")
    token_keys = ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "prompt_tokens", "completion_tokens")
    token_usage = {
        key: sum(int((item.get("token_usage") or {}).get(key, 0) or 0) for item in question_runs)
        for key in token_keys
        if any(key in (item.get("token_usage") or {}) for item in question_runs)
    }
    usable_hashes = [item.get("input_hash") for item in question_runs]
    usable_prompts = [item.get("prompt_hash") for item in question_runs]
    diagnostics.update({
        "provider": model_config.get("provider"),
        "model_id": model_config.get("model"),
        "invocation_scope": "answer_region",
        "diagnostic_run": bool(diagnostic_run),
        "question_runs": question_runs,
        "question_errors": question_errors,
        "input_hash": hashlib.sha256(json.dumps(usable_hashes, separators=(",", ":")).encode()).hexdigest(),
        "prompt_hash": hashlib.sha256(json.dumps(usable_prompts, separators=(",", ":")).encode()).hexdigest(),
        "prompt_version": ('mixed-grading-v1' if len({r.get('prompt_version') for r in question_runs})>1 else question_runs[0].get('prompt_version')) if question_runs else VISION_PROMPT_VERSION,
        "token_usage": token_usage,
        "request_metadata": {
            "invocation_scope": "answer_region",
            "output_limit_type": (question_runs[0].get("request_metadata") or {}).get("output_limit_type") if question_runs else None,
            "output_limit_value": (question_runs[0].get("request_metadata") or {}).get("output_limit_value") if question_runs else None,
            "timeout_seconds": (question_runs[0].get("request_metadata") or {}).get("timeout_seconds") if question_runs else None,
            "answer_region_calls": [
                {key: run.get(key) for key in ("question_id", "answer_region_id", "profile_id", "request_crop_hash", "request_crop_width", "request_crop_height", "input_hash", "prompt_hash", "latency_ms")}
                for run in question_runs
            ],
        },
        "diagnostic_artifact_id": [item.get("diagnostic_artifact_id") for item in question_runs],
        "finish_reason": [item.get("finish_reason") for item in question_runs],
        "http_status": [item.get("http_status") for item in question_runs],
    })
    diagnostics["question_errors"] = question_errors
    return {
        "mode": "vision-model",
        "grading_scope": scope_metadata,
        "suggested_total": sum(float(item["suggested_total"]) for item in ordered_results if item and item.get("suggested_total") is not None) if all_answered else None,
        "max_total": scope_metadata["scoped_max_score"],
        "confidence": min((float(item.get("confidence") or 0) for item in ordered_results if item), default=0.0),
        "needs_review": True,
        "blocked": bool(block_reasons) or not all_answered,
        "block_reasons": block_reasons,
        "requires_question_mapping": any(item and item.get("answer_status") in UNRESOLVED_ANSWER_STATUSES for item in ordered_results),
        "unmapped_answers": [entry for item in ordered_results if item for entry in item.get("unmapped_answers", [])],
        "summary": "已按每道小题的固定答题区独立评分；请逐点对照原图确认。",
        "student_identity": normalize_student_identity(None),
        "sub_questions": [item for item in ordered_results if item],
        "diagnostics": {"invocation_scope": "answer_region", "question_runs": question_runs},
    }


def grade_submission_dual(
    assignment: sqlite3.Row,
    sub_questions: list[dict[str, Any]],
    answer_text: str,
    photo_paths: list[Path],
    photo_meta: list[dict[str, Any]] | None,
    privacy_confirmed: bool,
    answer_regions: list[dict[str, Any]] | None,
    anonymous_mode: bool,
    question_mapping: list[dict[str, Any]] | None,
    diagnostic_run: bool = False,
) -> dict[str, Any]:
    """Run the selected DeepSeek/MiMo pair independently on identical canonical input."""
    scoped_questions, scope_metadata = resolve_grading_scope(sub_questions, answer_regions)
    from holistic_draft import require_supported
    require_supported(scoped_questions)
    configs = load_dual_review_model_configs()
    side_results: dict[str, dict[str, Any]] = {}
    side_diagnostics: dict[str, dict[str, Any]] = {side: {} for side in configs}
    errors: dict[str, dict[str, Any]] = {}

    def call_side(side: str) -> tuple[str, dict[str, Any]]:
        spec = configs[side]
        started_at = time.perf_counter()
        try:
            if answer_regions and all(item.get("invocation_scope") == "answer_region" for item in answer_regions):
                result = _grade_dual_answer_regions_for_side(
                    assignment, scoped_questions, photo_paths, photo_meta, spec,
                    answer_regions, side_diagnostics[side], diagnostic_run=diagnostic_run,
                    run_id=str(uuid.uuid4()),
                )
            else:
                result = call_vision_model(
                    assignment, scoped_questions, answer_text, photo_paths, side_diagnostics[side],
                    photo_meta, privacy_confirmed, answer_regions, anonymous_mode,
                    question_mapping, spec, diagnostic_run,
                )
        finally:
            side_diagnostics[side]["latency_ms"] = elapsed_ms(started_at)
        return side, result

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="dual-grade") as executor:
        future_to_side = {executor.submit(call_side, side): side for side in configs}
        for future in as_completed(future_to_side):
            side = future_to_side[future]
            try:
                _, normalized = future.result()
                side_results[side] = normalized
            except Exception as exc:
                error = classify_model_error(exc)
                error.update({
                    "provider": configs[side]["provider"],
                    "model_id": configs[side]["model"],
                    "latency_ms": side_diagnostics[side].get("latency_ms"),
                })
                errors[side] = error

    for side, diagnostics in side_diagnostics.items():
        diagnostics.setdefault("latency_ms", diagnostics.get("model_call_ms"))
        diagnostics.pop("api_key", None)
        if diagnostics.get("question_errors"):
            errors[side] = {
                "error_category": "answer_region_incomplete",
                "failed_question_count": len(diagnostics["question_errors"]),
            }
    side_metadata = {}
    for side, spec in configs.items():
        side_metadata[side] = {
            "provider": spec["provider"],
            "model_id": spec["model"],
            "status": "error" if side in errors else "ok",
            "latency_ms": side_diagnostics[side].get("latency_ms"),
            "token_usage": side_diagnostics[side].get("token_usage"),
            "input_hash": side_diagnostics[side].get("input_hash"),
            "prompt_version": side_diagnostics[side].get("prompt_version"),
            "prompt_hash": side_diagnostics[side].get("prompt_hash"),
            "invocation_scope": (side_diagnostics[side].get("request_metadata") or {}).get("invocation_scope", "whole_paper"),
            "output_limit_type": (side_diagnostics[side].get("request_metadata") or {}).get("output_limit_type"),
            "output_limit_value": (side_diagnostics[side].get("request_metadata") or {}).get("output_limit_value"),
            "diagnostic_run": side_diagnostics[side].get("diagnostic_run", False),
            "diagnostic_artifact_id": side_diagnostics[side].get("diagnostic_artifact_id"),
            "request_metadata": side_diagnostics[side].get("request_metadata"),
            "question_runs": side_diagnostics[side].get("question_runs", []),
            "provider_response_id": side_diagnostics[side].get("provider_response_id"),
            "http_status": side_diagnostics[side].get("http_status"),
            "provider_response_status": side_diagnostics[side].get("provider_response_status"),
            "finish_reason": side_diagnostics[side].get("finish_reason"),
            "error_category": errors.get(side, {}).get("error_category"),
        }
    hashes = {meta.get("input_hash") for meta in side_metadata.values() if meta.get("status") == "ok"}
    input_match = len(hashes) == 1 and all(meta.get("input_hash") for meta in side_metadata.values() if meta.get("status") == "ok")
    complete = len(side_results) == 2 and input_match
    import blank_consensus
    raw_blank_results = copy.deepcopy(side_results)
    side_results, blank_receipt = blank_consensus.resolve(side_results, side_metadata, errors, scoped_questions, photo_meta)
    if len(side_results) == 2:
        comparison = compare_dual_review_results(side_results["A"], side_results["B"])
    else:
        comparison = {
            "risk_level": "HIGH",
            "review_reasons": ["dual_review_degraded"],
            "point_comparisons": [],
            "score_a": side_results.get("A", {}).get("suggested_total"),
            "score_b": side_results.get("B", {}).get("suggested_total"),
        }
    if len(side_results) == 2 and not input_match:
        comparison["risk_level"] = "HIGH"
        comparison["review_reasons"] = sorted(set(comparison["review_reasons"]) | {"canonical_input_mismatch"})
    primary = side_results.get("A") or side_results.get("B")
    if primary is None:
        primary = model_failure_result(scoped_questions, {"error_category": "dual_model_calls_failed"})
    primary = dict(primary)
    side_problems = sorted(
        side for side, result in side_results.items()
        if result.get("blocked") or result_has_unresolved_answer_status(result)
    )
    if not complete or side_problems:
        primary["blocked"] = True
        primary["needs_review"] = True
        block_reasons = set(primary.get("block_reasons", []))
        if not complete:
            block_reasons.add("异模型双评未完整成功；请检查两个模型结果后再确认")
        if side_problems:
            block_reasons.add(f"双评中 {'、'.join(side_problems)} 侧存在未解决异常；请核对后再确认")
        primary["block_reasons"] = sorted(block_reasons)
    primary["dual_review"] = {
        "status": "complete" if complete else "degraded",
        "base_side": "A" if "A" in side_results else "B" if "B" in side_results else None,
        "run_id": uuid.uuid4().hex,
        "environment": runtime_environment(),
        "provenance": {
            "environment": runtime_environment(),
            "app_version": APP_VERSION,
            "git_commit": GIT_COMMIT,
            "data_schema_version": RUN_SCHEMA_VERSION,
        },
        "rubric_hash": rubric_fingerprint(scoped_questions),
        "invocation_scope": "answer_region" if answer_regions and all(item.get("invocation_scope") == "answer_region" for item in answer_regions) else "whole_paper",
        "grading_scope": scope_metadata,
        "assignment_id": str(assignment["id"]) if hasattr(assignment, "keys") and "id" in assignment.keys() else "unknown",
        "question_ids": list(scope_metadata["graded_question_ids"]),
        "input_hash": side_metadata["A"].get("input_hash") if input_match and "A" in side_metadata else None,
        "prompt_version": side_metadata["A"].get("prompt_version") if "A" in side_metadata else None,
        "prompt_hash": side_metadata["A"].get("prompt_hash") if "A" in side_metadata else None,
        "models": side_metadata,
        "results": side_results,
        "errors": errors,
        "comparison": comparison,
        "teacher_final": None,
    }
    if blank_receipt:
        primary["dual_review"].update(raw_results=raw_blank_results, blank_resolution=blank_receipt,
                                    raw_comparison=compare_dual_review_results(raw_blank_results["A"], raw_blank_results["B"]))
    primary.setdefault("diagnostics", {})["dual_review_input_match"] = bool(input_match)
    if scope_metadata.get("source") == "answer_regions":
        apply_grading_scope_to_result(primary, scoped_questions, scope_metadata)
    else:
        primary["grading_scope"] = scope_metadata
    if blank_receipt:
        blank_receipt["effective_results_sha256"] = blank_consensus.digest(primary["dual_review"]["results"])
    return withhold_incomplete_dual_score(primary)


def _encode_model_image(image: Image.Image, redactions: Any = None) -> bytes:
    scale = min(1.0, MODEL_IMAGE_LONG_EDGE / max(image.width, image.height))
    if scale < 1:
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )
    redactions = normalize_ai_redactions(redactions or [])
    if redactions:
        draw = ImageDraw.Draw(image)
        for item in redactions:
            left = math.floor(item["x"] * image.width)
            top = math.floor(item["y"] * image.height)
            right = max(left + 1, math.ceil((item["x"] + item["width"]) * image.width))
            bottom = max(top + 1, math.ceil((item["y"] + item["height"]) * image.height))
            draw.rectangle((left, top, min(image.width, right), min(image.height, bottom)), fill=(0, 0, 0))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=MODEL_IMAGE_JPEG_QUALITY, optimize=True)
    return output.getvalue()


def prepare_model_image(path: Path, redactions: Any = None) -> bytes:
    """Create a temporary in-memory vision image without changing stored photos."""
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    return _encode_model_image(image, redactions)


def prepare_ai_preview_image(data_url: str, redactions: Any, corners: Any = None) -> bytes:
    """Decode a data URL once and return the exact server-side model image."""
    return prepare_ai_preview_image_bytes(
        data_url_bytes(data_url, require_image=True), data_url_media_type(data_url), redactions, corners
    )


def prepare_ai_preview_image_bytes(raw: bytes, media_type: str, redactions: Any, corners: Any = None) -> bytes:
    """Create the exact stored-page/model image from already-decoded upload bytes."""
    with IMAGE_DECODE_SEMAPHORE:
        image, _ = decode_image_for_processing(raw, media_type)
        image, _ = rectify_image(image, normalize_photo_corners(corners))
        scale = min(1.0, MAX_IMAGE_EDGE / max(image.width, image.height))
        if scale < 1:
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
        processed = io.BytesIO()
        image.save(processed, format="JPEG", quality=86, optimize=True)
        with Image.open(io.BytesIO(processed.getvalue())) as saved:
            canonical = ImageOps.exif_transpose(saved).convert("RGB")
    return _encode_model_image(canonical, redactions)


def grade_submission(
    assignment: sqlite3.Row,
    sub_questions: list[dict[str, Any]],
    answer_text: str,
    photo_paths: list[Path],
    photo_meta: list[dict[str, Any]] | None = None,
    privacy_confirmed: bool = False,
    answer_regions: list[dict[str, Any]] | None = None,
    anonymous_mode: bool = False,
    question_mapping: list[dict[str, Any]] | None = None,
    dual_review_enabled: bool = False,
) -> dict[str, Any]:
    from holistic_draft import require_supported
    require_supported(sub_questions)
    started_at = time.perf_counter()
    if dual_review_enabled:
        model_diagnostics: dict[str, Any] = {}
        try:
            result = grade_submission_dual(
                assignment, sub_questions, answer_text, photo_paths, photo_meta,
                privacy_confirmed, answer_regions, anonymous_mode, question_mapping,
            )
            result.setdefault("diagnostics", {}).update(model_diagnostics)
            result["diagnostics"]["model_call_ms"] = elapsed_ms(started_at)
            result["diagnostics"]["grade_ms"] = elapsed_ms(started_at)
            return result
        except Exception as exc:
            diagnostics = classify_model_error(exc)
            diagnostics["model_call_ms"] = elapsed_ms(started_at)
            diagnostics["grade_ms"] = elapsed_ms(started_at)
            print(
                f"[{utc_now()}] WARNING: dual vision grading unavailable "
                f"(category={diagnostics['error_category']}, type={diagnostics['exception_type']})"
            )
            failed = model_failure_result(sub_questions, diagnostics)
            failed["dual_review"] = {
                "status": "degraded",
                "base_side": None,
                "run_id": uuid.uuid4().hex,
                "environment": runtime_environment(),
                "provenance": {
                    "environment": runtime_environment(),
                    "app_version": APP_VERSION,
                    "git_commit": GIT_COMMIT,
                    "data_schema_version": RUN_SCHEMA_VERSION,
                },
                "models": {
                    side: {
                        "provider": spec["provider"],
                        "model_id": spec["model"],
                        "status": "error",
                        "error_category": diagnostics.get("error_category"),
                    }
                    for side, spec in dual_review_model_specs().items()
                },
                "results": {},
                "errors": {"configuration": {"error_category": diagnostics.get("error_category")}},
                "comparison": {"risk_level": "HIGH", "review_reasons": ["dual_review_degraded"], "point_comparisons": []},
                "teacher_final": None,
            }
            return failed
    if model_configured() and photo_paths:
        model_diagnostics: dict[str, Any] = {}
        try:
            result = call_vision_model(
                assignment,
                sub_questions,
                answer_text,
                photo_paths,
                model_diagnostics,
                photo_meta,
                privacy_confirmed,
                answer_regions,
                anonymous_mode,
                question_mapping,
            )
            result.setdefault("diagnostics", {}).update(model_diagnostics)
            result["diagnostics"]["model_call_ms"] = elapsed_ms(started_at)
            result["diagnostics"]["grade_ms"] = elapsed_ms(started_at)
            return result
        except Exception as exc:
            diagnostics = classify_model_error(exc)
            diagnostics.update(model_diagnostics)
            diagnostics["model_call_ms"] = elapsed_ms(started_at)
            diagnostics["grade_ms"] = elapsed_ms(started_at)
            print(
                f"[{utc_now()}] WARNING: vision model grading failed "
                f"(category={diagnostics['error_category']}, "
                f"status={diagnostics.get('http_status', '-')}, "
                f"type={diagnostics['exception_type']})"
            )
            return model_failure_result(sub_questions, diagnostics)
    if photo_paths and hasattr(assignment, 'keys') and 'batch_id' in assignment.keys() and assignment['batch_id']:
        failed = model_failure_result(sub_questions, {'error_category':'model_not_configured','retryable':False})
        failed['diagnostics'] = {'error_category':'model_not_configured','retryable':False,'model_call_ms':0}
        failed['summary'] = '模型未配置，本批已暂停；请配置模型并明确确认后原位恢复，未使用规则演示代替AI成绩。'
        return failed
    result = score_sub_questions(sub_questions, answer_text)
    result.setdefault("diagnostics", {})["grade_ms"] = elapsed_ms(started_at)
    result["diagnostics"]["model_call_ms"] = 0
    return result


CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)


def write_http_response(
    handler: BaseHTTPRequestHandler,
    body: bytes,
    content_type: str,
    status: int = 200,
    extra_headers: dict[str, str] | None = None,
) -> bool:
    """Write a response without treating a closed client connection as a server error."""
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(body)
    except CLIENT_DISCONNECT_ERRORS:
        # Browsers may cancel a request while navigating, refreshing, or replacing
        # a large upload. The request is already gone, so there is no response to
        # send and no actionable server failure to report.
        return False
    return True


def json_response(handler: BaseHTTPRequestHandler, value: Any, status: int = 200) -> None:
    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    write_http_response(handler, body, "application/json; charset=utf-8", status)


class PortableHTTPServer(ThreadingHTTPServer):
    """Bind exclusively so a second program cannot silently share the port."""

    allow_reuse_address = False
    daemon_threads = True
    block_on_close = False
    request_queue_size = 64

    def __init__(self, server_address: Any, RequestHandlerClass: type, bind_and_activate: bool = True):
        self._request_slots = threading.BoundedSemaphore(MAX_HTTP_CONNECTIONS)
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(timeout=60):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


EXAM_IMPORT_DOCX_MAX_BYTES = 10 * 1024 * 1024
EXAM_IMPORT_PDF_MAX_BYTES = 26 * 1024 * 1024


def _decode_exam_upload(payload: dict[str, Any], filename_key: str, data_key: str, suffix: str, max_bytes: int) -> tuple[str, bytes] | None:
    encoded = str(payload.get(data_key) or "")
    filename = str(payload.get(filename_key) or "").strip()
    if not encoded and not filename:
        return None
    if not filename.lower().endswith(suffix):
        raise ValueError(f"请选择 {suffix} 文件")
    if not encoded or len(encoded) > ((max_bytes + 2) // 3) * 4 + 8:
        raise ValueError(f"{suffix} 文件为空或超过大小限制")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError(f"{suffix} 文件编码无效") from exc
    if not raw or len(raw) > max_bytes:
        raise ValueError(f"{suffix} 文件为空或超过大小限制")
    return filename, raw


def _render_exam_import_references(pdf_path: Path, draft_id: str, draft: dict[str, Any]) -> list[dict[str, Any]]:
    layout = draft.get("source", {}).get("layout_source") or {}
    # Exam question pages plus the blank answer sheet are valid layout
    # references; scoring-reference/answer pages stay excluded.
    page_numbers = layout_reference_page_numbers(layout)
    if not page_numbers:
        draft.setdefault("layout_reference", {})["render_status"] = "no_exam_pages"
        return []
    from pdf_renderer import new_reference_method, render_pdf_page
    render_method = new_reference_method()
    if render_method == "unavailable":
        draft.setdefault("layout_reference", {})["render_status"] = "unavailable"
        draft.setdefault("warning_codes", []).append("layout_reference_unavailable")
        draft.setdefault("warnings", []).append("当前电脑缺少 PDF 页面渲染组件；PDF 已作为版式来源保存，但页面 reference 尚未生成。")
        return []
    reference_dir = DATA_DIR / "exam_import_drafts" / draft_id / "layout_reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    references: list[dict[str, Any]] = []
    for page_number in page_numbers:
        try:
            page_number = int(page_number)
        except (TypeError, ValueError):
            continue
        if page_number < 1 or page_number > int(layout.get("page_count", 0) or 0):
            continue
        destination = reference_dir / f"page-{page_number}.png"
        render_pdf_page(pdf_path, page_number, 150, destination, method=render_method)
        with Image.open(destination) as image:
            width, height = image.size
        raw = destination.read_bytes()
        references.append({
            "page_number": page_number,
            "reference_width": width,
            "reference_height": height,
            "reference_sha256": hashlib.sha256(raw).hexdigest(),
            "render_dpi": 150,
            "image_file": f"data/exam_import_drafts/{draft_id}/layout_reference/{destination.name}",
            "render_method": render_method,
        })
    draft.setdefault("layout_reference", {})["pages"] = references
    draft["layout_reference"]["render_status"] = "generated" if references else "no_exam_pages"
    return references


def _exam_import_draft_load(draft_id: str) -> tuple[Path, dict[str, Any], Path]:
    if not re.fullmatch(r"[0-9a-f]{32}", draft_id):
        raise ValueError("考试草稿编号无效，请重新解析文件")
    draft_dir = DATA_DIR / "exam_import_drafts" / draft_id
    draft_file = draft_dir / "exam_import_draft.json"
    if not draft_file.is_file() or draft_dir.resolve().parent != (DATA_DIR / "exam_import_drafts").resolve():
        raise ValueError("考试草稿已失效，请重新上传并解析")
    try:
        draft = json.loads(draft_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("无法读取考试草稿，请重新解析文件") from exc
    if not isinstance(draft, dict):
        raise ValueError("考试草稿结构无效")
    return draft_dir, draft, draft_file


def _exam_import_draft_response(draft: dict[str, Any], draft_id: str) -> dict[str, Any]:
    response = copy.deepcopy(draft)
    for page in response.get("layout_reference", {}).get("pages", []) or []:
        if isinstance(page, dict):
            page["preview_url"] = f"/api/exam-import-reference?draft_id={draft_id}&page_number={page['page_number']}"
    return response


def _exam_import_ai_source(draft_dir: Path, draft: dict[str, Any]) -> dict[str, Any]:
    """Extract Word/PDF text only; page images are deliberately never sent."""
    docx_text = ""
    docx_path = draft_dir / "content.docx"
    if docx_path.is_file():
        docx_text = extract_docx_plain_text(docx_path.read_bytes())
    pdf_pages: list[str] = []
    pdf_path = draft_dir / "layout.pdf"
    if pdf_path.is_file():
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(pdf_path.read_bytes()), strict=False)
            pdf_pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:
            raise ValueError("无法读取 PDF 文字，请重新生成草稿") from exc
    return build_exam_import_ai_source(docx_text, pdf_pages, str(draft.get("title") or ""))


def _exam_import_ai_fingerprint(draft_id: str, source: dict[str, Any], model_config: dict[str, str]) -> str:
    claims = {
        "draft_id": draft_id,
        "source_sha256": hashlib.sha256(source["text"].encode("utf-8")).hexdigest(),
        "model": str(model_config.get("model") or ""),
        "provider": str(model_config.get("provider") or ""),
        "prompt_version": AI_PARSE_PROMPT_VERSION,
    }
    return hashlib.sha256(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _check_exam_import_ai_privacy(source: dict[str, Any]) -> None:
    """Apply existing identity checks; blank form placeholders are not values."""
    text = str(source.get("text") or "")
    labels = r"(?:学生姓名|姓名|student[_ -]?name|学号|student[_ -]?no|考号|准考证号|座号|班级|class[_ -]?name)"
    # Ignore only explicit empty placeholders during scanning. Actual source
    # text is unchanged, and names/numbers must still be blocked before sending.
    scan_text = re.sub(
        rf"({labels}\s*[:：=]\s*)[_＿﹏—–\-□☐·…⋯]+(?=$|[\s，,。；;]|{labels}\s*[:：=])",
        "", text, flags=re.IGNORECASE,
    )
    conn = get_db()
    try:
        if _contains_labeled_identity_data(scan_text) or _contains_roster_identity(conn, scan_text):
            raise ValueError("试卷文字可能包含姓名、学号等身份信息，已阻止发送；请先脱敏并重新上传")
    finally:
        conn.close()


def call_exam_import_ai_model(source_text: str, model_config: dict[str, str]) -> dict[str, Any]:
    """Send extracted Word/PDF text (never images) and return the JSON result."""
    payload = build_exam_import_rubric_payload(source_text, model_config["model"], model_config.get("provider", ""))
    payload.update(deepseek_json_payload_options(model_config.get("provider", ""), model_config["model"]))
    endpoint = model_chat_completions_endpoint(model_config)
    headers = {"Content-Type": "application/json"}
    api_key = str(model_config.get("api_key", ""))
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(endpoint, data=body, headers=headers, method="POST")
    timeout = float(model_config.get("timeout") or os.getenv("HISTORY_GRADER_TIMEOUT", "180"))
    try:
        with open_model_request(request, timeout=timeout) as response:
            content = read_model_chat_content(response)
        return extract_json(content)
    except Exception as exc:
        raise ValueError("AI 解析模型调用或结果解析失败，请检查模型配置后重试") from exc


def _confirmed_import_payload(draft: dict[str, Any], confirmation: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    if not isinstance(confirmation, dict):
        raise ValueError("确认信息格式无效")
    source = draft.get("source") or {}
    pair_status = str(source.get("source_pair_status", "not_applicable"))
    if pair_status == "mismatch":
        raise ValueError("Word 与 PDF 存在明显不一致；请移除其中一个来源后重新生成草稿")
    if pair_status == "needs_teacher_confirmation" and confirmation.get("source_pair_confirmed") is not True:
        raise ValueError("请先确认 Word 与 PDF 属于同一份试卷")
    if confirmation.get("teacher_confirmed") is not True:
        raise ValueError("请先勾选已核对题目、答案、参考答案和评分点")

    exam_name = str(confirmation.get("exam_name") or draft.get("title") or "").strip()
    if not exam_name:
        raise ValueError("请填写考试名称")
    objective_questions = draft.get("objective_questions", [])
    if not isinstance(objective_questions, list):
        raise ValueError("客观题草稿结构无效")
    submitted_key = confirmation.get("objective_answer_key", {})
    if not isinstance(submitted_key, dict):
        raise ValueError("客观题答案格式无效")
    final_key: dict[str, str] = {}
    for question in objective_questions:
        question_id = str(question.get("question_id", "")).strip()
        if question.get("needs_teacher_review") or not str(question.get("stem", "")).strip() or set((question.get("options") or {}).keys()) != {"A", "B", "C", "D"}:
            raise ValueError(f"客观题 {question_id} 结构未完整识别；请重新整理 Word 后导入")
        answer = str(submitted_key.get(question_id, "")).strip().upper()
        if answer not in {"A", "B", "C", "D"}:
            raise ValueError(f"客观题 {question_id} 尚未确认正确答案")
        final_key[question_id] = answer
    if (draft.get("answer_key_crosscheck") or {}).get("status") == "conflict":
        resolutions = confirmation.get("answer_conflict_resolutions", {})
        if not isinstance(resolutions, dict):
            raise ValueError("答案冲突确认格式无效")
        for conflict in (draft.get("source", {}).get("source_conflicts") or []):
            if conflict.get("type") != "answer_key_conflict":
                continue
            question_id = str(conflict.get("question_id", ""))
            if resolutions.get(question_id) != final_key.get(question_id):
                raise ValueError(f"客观题 {question_id} 的 Word/PDF 答案冲突尚未由教师明确裁决")

    original_subs = [
        sub for major in draft.get("material_questions", []) for sub in major.get("sub_questions", [])
    ]
    submitted_majors = confirmation.get("material_questions", [])
    if not isinstance(submitted_majors, list):
        raise ValueError("主观题确认内容格式无效")
    if any(not isinstance(major, dict) for major in submitted_majors):
        raise ValueError("主观题确认内容格式无效")
    from draft_structure import normalize_majors
    submitted_majors = normalize_majors(submitted_majors, draft.get("material_questions", []))
    submitted_subs = [sub for major in submitted_majors for sub in major['sub_questions']]
    submitted_by_id = {sub['question_id']: sub for sub in submitted_subs}

    from unified_exam import confirmed_objectives
    objective_rows = confirmed_objectives(objective_questions, final_key, confirmation.get("objective_points", {}))
    confirmed_draft = copy.deepcopy(draft)
    confirmed_draft["material_questions"] = copy.deepcopy(submitted_majors)
    confirmed_draft["title"] = exam_name
    confirmed_draft["objective_answer_key"] = final_key
    confirmed_draft["objective_points"] = {r["original_question_id"]: r["points"] for r in objective_rows}
    confirmed_draft["objective_answer_key_status"] = "confirmed_by_teacher"
    confirmed_draft["objective_answer_key_confirmation_status"] = "confirmed_by_teacher"
    confirmed_draft["teacher_confirmation_status"] = "confirmed_by_teacher"
    confirmed_draft["status"] = "confirmed"
    for question in confirmed_draft.get("objective_questions", []):
        question_id = str(question.get("question_id", ""))
        question["correct_answer_draft"] = final_key[question_id]
        question["answer_found"] = True

    official_sub_questions: list[dict[str, Any]] = []
    point_warnings: list[str] = []
    for major in confirmed_draft.get("material_questions", []):
        major_id = str(major.get("label", "")).strip()
        edited_major = next((item for item in submitted_majors if str(item.get("label", "")).strip() == major_id), None)
        if not edited_major:
            raise ValueError(f"第 {major_id} 题确认信息缺失")
        major["prompt"] = str(edited_major.get("prompt", major.get("prompt", ""))).strip()
        original_major_sub_questions = list(major.get("sub_questions", []))
        major["sub_questions"] = []
        for original in original_major_sub_questions:
            question_id = str(original.get("question_id", ""))
            edited = submitted_by_id[question_id]
            question_text = str(edited.get("question", "")).strip()
            reference_answer = str(edited.get("reference_answer", "")).strip()
            if not question_text or (not reference_answer and edited.get("scoring_mode") != "holistic"):
                raise ValueError(f"{question_id} 的设问和参考答案不能为空")
            try:
                max_score = float(edited.get("max_score"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{question_id} 未填写有效满分") from exc
            if not math.isfinite(max_score) or max_score <= 0:
                raise ValueError(f"{question_id} 未填写有效满分")
            if edited.get('scoring_mode') == 'holistic':
                from holistic_draft import validate
                holistic = validate(edited.get('holistic'), max_score)
                preserved = {**original, 'reference_answer': reference_answer, 'standard_answer': reference_answer,
                    'max_score': max_score, 'scoring_mode': 'holistic', 'holistic': holistic,
                    'rubric_draft': [], 'needs_teacher_confirmation': False}
                major['sub_questions'].append(preserved)
                context = major.get('prompt', '')
                official_sub_questions.append(dict(id='Q'+question_id, label=question_id,
                    question=context+'\n'+question_text, standard_answer=reference_answer, criteria=[],
                    max_score=max_score, scoring_mode='holistic', holistic=holistic))
                point_warnings.append(question_id+'：开放题评价标准已保存，实际评分按教师确认的计分方式执行')
                continue
            raw_points = edited.get("rubric_draft", [])
            if not isinstance(raw_points, list) or not raw_points:
                raise ValueError(f"{question_id} 至少需要一个评分点")
            criteria_input = []
            for index, point in enumerate(raw_points, start=1):
                if not isinstance(point, dict) or not str(point.get("label", "")).strip():
                    raise ValueError(f"{question_id} 的第 {index} 个评分点缺少文字")
                try:
                    score = float(point.get("score"))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{question_id} 的第 {index} 个评分点未填写分值") from exc
                if not math.isfinite(score) or score <= 0:
                    raise ValueError(f"{question_id} 的第 {index} 个评分点未填写有效分值")
                criteria_input.append({"id": f"C{index}", "label": str(point["label"]).strip(), "score": score})
            criteria = clean_criteria(criteria_input)
            scoring_mode = str(edited.get("scoring_mode") or "weighted").strip().casefold()
            if scoring_mode not in {"weighted", "point_bank"}:
                raise ValueError(f"{question_id} 的评分方式无效")
            try:
                point_value = float(edited.get("point_value", 2))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{question_id} 的每点分值必须是有效正数") from exc
            if not math.isfinite(point_value) or point_value <= 0:
                raise ValueError(f"{question_id} 的每点分值必须是有效正数")
            if scoring_mode == "point_bank":
                from auto_crop_double_review_poc import require_consistent_point_bank_values
                require_consistent_point_bank_values({"id": question_id, "scoring_mode": scoring_mode,
                                                      "point_value": point_value, "criteria": criteria})
            score_sum = sum(float(item["score"]) for item in criteria)
            if scoring_mode == "point_bank" and score_sum < max_score - 1e-6:
                raise ValueError(f"{question_id} 的候选评分点不足以达到满分，请补充评分点或核对满分")
            if scoring_mode == "weighted" and not math.isclose(score_sum, max_score, rel_tol=0, abs_tol=1e-6):
                point_warnings.append(f"{question_id}：评分点分值合计 {score_sum:g}，本题满分 {max_score:g}，请确认。")
            normalized_sub = {
                **original,
                "question": question_text,
                "reference_answer": reference_answer,
                "standard_answer": reference_answer,
                "max_score": max_score,
                "scoring_mode": scoring_mode,
                "point_value": point_value,
                "rubric_draft": [{**point, "score": float(criteria[index]["score"])} for index, point in enumerate(raw_points)],
                "needs_teacher_confirmation": False,
            }
            major["sub_questions"].append(normalized_sub)
            shared_context = f"所属大题材料（第{major_id}题）：\n{major['prompt']}\n\n" if major.get("prompt") else ""
            official_sub_questions.append({
                "id": f"Q{question_id}",
                "label": question_id,
                "question": f"{shared_context}小题题干（{question_id}）：\n{question_text}",
                "standard_answer": reference_answer,
                "criteria": criteria,
                "scoring_mode": scoring_mode,
                "point_value": point_value,
                "max_score": max_score,
            })
    if not objective_questions and not official_sub_questions:
        raise ValueError("草稿中没有可创建的题目")
    # Persist the teacher's explicit mode and point weights; never infer a bank
    # from a sum mismatch or silently rewrite unequal criterion values.
    official_sub_questions = clean_sub_questions(official_sub_questions) if official_sub_questions else []

    answer_conflicts = (draft.get("answer_key_crosscheck") or {}).get("status") == "conflict"
    if answer_conflicts:
        confirmed_draft["answer_key_crosscheck"]["status"] = "resolved_by_teacher"
        confirmed_draft["answer_key_crosscheck"]["teacher_final_answer_key"] = final_key
    return confirmed_draft, official_sub_questions, point_warnings


def export_scope_maximum(questions: list[dict[str, Any]], criteria: list[dict[str, Any]], result: dict[str, Any]) -> float:
    """Use the saved run's scope, rejecting inconsistent metadata instead of widening it."""
    scope = result.get("grading_scope")
    if scope is None:
        return total_sub_questions(questions) if questions else total_score(criteria)
    if not isinstance(scope, dict) or not isinstance(scope.get("graded_question_ids"), list):
        raise ValueError("invalid_export_grading_scope")
    _, resolved = resolve_grading_scope(questions, graded_question_ids=scope["graded_question_ids"])
    maximum = float(resolved["scoped_max_score"])
    declared = float(scope.get("scoped_max_score"))
    if not math.isfinite(declared) or not math.isclose(maximum, declared, rel_tol=0, abs_tol=1e-9):
        raise ValueError("inconsistent_export_grading_scope")
    return maximum


class AppHandler(BaseHTTPRequestHandler):
    server_version = "HistoryGrader/0.1"
    MOBILE_READ_PATHS = {
        "/api/status",
        "/api/assignments",
        "/api/rosters",
        "/api/roster-students",
        "/api/submission",
        "/api/submission-photo", "/api/total-confirmation",
        "/api/submission-evidence",
        "/api/mobile/dual-review-readiness",
        "/api/recovery-submissions",
    }
    # The only non-persistent preview endpoint a mobile upload needs. It does
    # not call the model provider or expose stored answers or settings.
    MOBILE_PREVIEW_PATHS = {"/api/ai-payload-preview", "/api/question-remap-preview"}
    MOBILE_WRITE_PATHS = {
        "/api/submissions", "/api/batches", "/api/batches/upload-failed",
        "/api/batches/skip",
        "/api/delete-submission-photos", "/api/confirm-total", "/api/confirm", "/api/review", "/api/confirm-blank-subquestion",
        "/api/reprocess-question-mapping"
    }

    def log_message(self, fmt: str, *args: Any) -> None:
        status = "protocol"
        if len(args) >= 2 and str(args[1]).isdigit() and len(str(args[1])) == 3:
            status = str(args[1])
        print(f"[{utc_now()}] HTTP event status={status}")

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError) as exc:
            raise ValueError("请求体长度无效") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("请求体为空或超过40MiB；请逐张上传图片")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("请求体未完整接收，请重试")
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求不是有效的 UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("请求 JSON 必须是对象")
        return value

    def read_submission_payload(self) -> dict[str, Any]:
        """Accept legacy JSON and the lower-overhead multipart upload format."""
        content_type = self.headers.get("Content-Type", "")
        if not content_type.casefold().startswith("multipart/form-data"):
            return self.read_json()
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError) as exc:
            raise ValueError("请求体长度无效") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("请求体为空或超过40MiB；请逐张上传图片")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("请求体未完整接收，请重试")
        return parse_multipart_submission(body, content_type)

    def read_student_auto_crop_upload(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.casefold().startswith("multipart/form-data"):
            raise ValueError("自动裁切测试需要通过本地图片上传")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError) as exc:
            raise ValueError("请求体长度无效") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("上传请求为空或超过40MiB")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("学生答卷照片未完整接收，请重试")
        return parse_student_auto_crop_upload(body, content_type)

    def is_local_client(self) -> bool:
        return self.client_address[0] in {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}

    def dispatch_objective_request(self, parsed) -> bool:
        prefix = "/objective"
        if parsed.path != prefix and not parsed.path.startswith(prefix + "/"):
            return False
        if parsed.path == prefix + "/login" and self.command in {"GET", "HEAD"}:
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        is_api = parsed.path.startswith(prefix + "/api/")
        if is_api and not self.require_api_access(parsed.path):
            return True
        if is_api and (
            RESTORE_IN_PROGRESS
            or getattr(self.server, "stopping", False)
            or getattr(self.server, "restarting", False)
        ):
            json_response(self, {"error": "服务正在等待当前任务完成，请稍候"}, 409)
            return True
        application = getattr(self.server, "objective_wsgi_app", None)
        if application is None:
            json_response(self, {"error": "客观题模块尚未初始化"}, 503)
            return True
        from objective.integration import dispatch_wsgi_request
        return dispatch_wsgi_request(self, application, mount_prefix=prefix)

    @track_active_task
    def create_exam_report(self, *, ai: bool = False) -> None:
        if not self.require_local_client():
            return
        payload = self.read_json()
        assignment_id = str(payload.get("assignment_id") or "").strip().lower()
        exam_id = str(payload.get("exam_id") or "").strip().lower()
        batch_id = str(payload.get("batch_id") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{12}", assignment_id):
            raise ValueError("考试编号无效")
        if exam_id and not re.fullmatch(r"[0-9a-f]{32}", exam_id):
            raise ValueError("正式考试编号无效")
        if batch_id and not re.fullmatch(r"[0-9a-f]{12}", batch_id):
            raise ValueError("批次编号无效")
        objectives = str(payload.get("objectives") or "").strip()
        if len(objectives) > 4000:
            raise ValueError("学习目标内容过长，请精简到4000字以内")
        if ai and payload.get("confirm_send") is not True:
            raise ValueError("请先确认将聚合统计和学习目标发送给AI分析")
        conn = get_db()
        try:
            if exam_id and not conn.execute(
                "SELECT 1 FROM grading_exams WHERE id=? AND assignment_id=?", (exam_id, assignment_id)
            ).fetchone():
                raise ValueError("考试与作业不匹配")
            if batch_id:
                batch = conn.execute("SELECT assignment_id,exam_id FROM grading_batches WHERE id=?", (batch_id,)).fetchone()
                if not batch or batch["assignment_id"] != assignment_id or (exam_id and batch["exam_id"] != exam_id):
                    raise ValueError("批次与考试不匹配")
            data = exam_report.build_exam_report_data(conn, assignment_id, exam_id, batch_id)
            if not ai:
                json_response(self, {"report": data})
                return
            if not data.get("student_count"):
                raise ValueError("当前范围内没有可统计成绩，无法生成AI分析")
            messages = exam_report.build_ai_report_prompt(data, objectives)
            content = "\n".join(str(item.get("content") or "") for item in messages)
            if _contains_roster_identity(conn, content) or _contains_labeled_identity_data(content):
                raise ValueError("报告标题、评分点或学习目标含身份信息，请排除后再生成AI分析")
        finally:
            conn.close()
        config = (load_dual_review_model_configs(("A",))["A"] if dual_review_key_is_configured("A")
                  else rubric_generation_model_config(self))
        model_payload = {"model": config["model"], "temperature": 0.2, "max_tokens": 5000, "messages": messages}
        model_payload.update(deepseek_json_payload_options(str(config.get("provider", "")), str(config["model"])))
        headers = {"Content-Type": "application/json"}
        if config.get("api_key"):
            headers["Authorization"] = "Bearer " + config["api_key"]
        request = Request(model_chat_completions_endpoint(config),
                          data=json.dumps(model_payload, ensure_ascii=False).encode("utf8"), headers=headers, method="POST")
        try:
            with open_model_request(request, timeout=float(config.get("timeout") or config.get("timeout_seconds") or 180)) as response:
                markdown = read_model_chat_content(response).strip()
        except Exception as exc:
            raise ValueError("AI分析未完成，请检查模型配置或稍后手动重试；本地统计仍可导出") from exc
        if not markdown or len(markdown) > 120000:
            raise ValueError("AI报告为空或过长；本地统计仍可导出")
        json_response(self, {"report": data, "markdown": markdown, "model": config["model"],
                             "provider": config.get("provider_label", config.get("provider", "")), "objectives": objectives})

    def create_ai_payload_preview(self) -> None:
        payload = self.read_json()
        grading_input_mode = _grading_input_mode(payload)
        assignment_id = str(payload.get("assignment_id", "")).strip().lower()
        class_name = str(payload.get("class_name", "")).strip()
        student_label = str(payload.get("student_label", "")).strip() or "未命名学生"
        photos = payload.get("photos")
        if not re.fullmatch(r"[0-9a-f]{12}", assignment_id):
            raise ValueError("作业编号无效")
        if not isinstance(photos, list) or not 1 <= len(photos) <= MAX_IMAGES_PER_SUBMISSION:
            raise ValueError(f"隐私预览需要1—{MAX_IMAGES_PER_SUBMISSION}张图片")
        answer_regions = payload.get("answer_regions")
        session_fingerprint = _preview_session_fingerprint(self)
        identity_fingerprint = _ai_privacy_identity_fingerprint(assignment_id, class_name, student_label)
        conn = get_db()
        try:
            assignment = conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
            if not assignment:
                raise ValueError("作业不存在")
            saved = json.loads(assignment["sub_questions_json"] or "[]")
            if saved:
                sub_questions = clean_sub_questions(saved)
            else:
                sub_questions = clean_sub_questions(
                    None,
                    fallback_question=assignment["question"],
                    fallback_answer=assignment["standard_answer"],
                    fallback_criteria=json.loads(assignment["criteria_json"]),
                )
            if payload.get("preview_stage") == "prepare":
                # Return the exact server-side rectified/model-ready page for
                # the teacher's crop selection. This stage does not create an
                # approval token and never contacts the model provider.
                prepared_pages: list[dict[str, Any]] = []
                for index, item in enumerate(photos, start=1):
                    if not isinstance(item, dict):
                        raise ValueError("隐私预览图片格式无效")
                    data_url = str(item.get("data_url", ""))
                    corners = normalize_photo_corners(item.get("corners"))
                    image_bytes = prepare_ai_preview_image(data_url, [], corners)
                    prepared_pages.append({
                        "page": index,
                        "sha256": hashlib.sha256(image_bytes).hexdigest(),
                        "data_url": bytes_to_data_url(image_bytes, "image/jpeg"),
                    })
                json_response(self, {
                    "stage": "prepared_pages",
                    "prepared_pages": prepared_pages,
                    "image_count": len(prepared_pages),
                    "model_request_sent": False,
                })
                return
            has_answer_region_attestation = any(
                isinstance(item, dict) and str(item.get("answer_region_attestation") or "")
                for item in photos
            )
            normalized_regions = normalize_answer_regions(
                answer_regions, sub_questions, len(photos),
                require_complete=not has_answer_region_attestation,
            )
            verified_answer_regions = _validate_formal_answer_region_attestations(
                photos, normalized_regions, sub_questions, assignment, conn, payload
            )
            if verified_answer_regions:
                _require_answer_region_rubric_ready(
                    assignment["import_metadata_json"], sub_questions,
                    [str(item["question_id"]) for item in verified_answer_regions.values()],
                )
            prompt = build_scoring_prompt_text(
                str(assignment["question"]), sub_questions, privacy_mode=True,
                answer_regions=normalized_regions,
            )
            region_prompts = []
            for region in normalized_regions:
                if region.get("invocation_scope") != "answer_region":
                    continue
                region_ids = set(region["sub_question_ids"])
                region_questions = [item for item in sub_questions if str(item.get("id")) in region_ids]
                if len(region_questions) != 1:
                    raise ValueError("逐小题答案区域必须且只能关联一道小题")
                region_prompt = build_scoring_prompt_text(
                    str(region_questions[0].get("question") or ""), region_questions, privacy_mode=True,
                    answer_regions=[region],
                )
                region_prompt_hash = hashlib.sha256(region_prompt.encode("utf-8")).hexdigest()
                region["approved_prompt_sha256"] = region_prompt_hash
                region_prompts.append({
                    "region_id": region["region_id"],
                    "sub_question_id": region_questions[0]["id"],
                    "label": region_questions[0]["label"],
                    "prompt": region_prompt,
                    "prompt_sha256": region_prompt_hash,
                })
            if (
                _contains_roster_identity(conn, prompt)
                or _contains_labeled_identity_data(prompt)
                or any(_contains_roster_identity(conn, item["prompt"]) or _contains_labeled_identity_data(item["prompt"]) for item in region_prompts)
            ):
                json_response(self, {
                    "blocked": True,
                    "error": "题目或评分细则包含本地花名册身份字段。请先修改题目内容，再重新预览。",
                }, 409)
                return
        finally:
            conn.close()
        sources: list[dict[str, Any]] = []
        preview_images: list[dict[str, Any]] = []
        processed_pages: list[dict[str, Any]] = []
        page_image_bytes: list[bytes] = []
        for index, item in enumerate(photos, start=1):
            if not isinstance(item, dict):
                raise ValueError("隐私预览图片格式无效")
            data_url = str(item.get("data_url", ""))
            raw = data_url_bytes(data_url, require_image=True)
            redactions = normalize_ai_redactions(item.get("ai_redactions", []))
            corners = normalize_photo_corners(item.get("corners"))
            normalized_corners = [[point[0], point[1]] for point in corners] if corners else None
            image_bytes = prepare_ai_preview_image_bytes(
                raw, data_url_media_type(data_url), redactions, corners
            )
            page_image_bytes.append(image_bytes)
            processed_pages.append({
                "page": index,
                "sha256": hashlib.sha256(image_bytes).hexdigest(),
                "data_url": bytes_to_data_url(image_bytes, "image/jpeg"),
            })
            model_sha256 = hashlib.sha256(image_bytes).hexdigest()
            sources.append({
                "sha256": hashlib.sha256(raw).hexdigest(),
                "corners": normalized_corners,
                "redactions": redactions,
                "model_sha256": model_sha256,
            })
        region_claims = []
        for crop_index, region in enumerate(normalized_regions, start=1):
            source_page = int(region["photo_index"])
            page_bytes = page_image_bytes[source_page - 1]
            crop_bytes = crop_model_image_bytes(page_bytes, region)
            crop_sha256 = hashlib.sha256(crop_bytes).hexdigest()
            region_claim = {
                key: value for key, value in region.items()
                if key not in {"approved_model_sha256", "approved_prompt_sha256"}
            }
            region_claim["model_sha256"] = crop_sha256
            if region.get("approved_prompt_sha256"):
                region_claim["prompt_sha256"] = region["approved_prompt_sha256"]
            region_claims.append(region_claim)
            preview_images.append({
                "page": crop_index,
                "source_page": source_page,
                "region_id": region["region_id"],
                "sub_question_ids": region["sub_question_ids"],
                "sha256": crop_sha256,
                "data_url": bytes_to_data_url(crop_bytes, "image/jpeg"),
            })
        if not normalized_regions:
            for page_index, image_bytes in enumerate(page_image_bytes, start=1):
                image_sha256 = hashlib.sha256(image_bytes).hexdigest()
                preview_images.append({
                    "page": page_index,
                    "source_page": page_index,
                    "sub_question_ids": [],
                    "image_kind": "full_page",
                    "sha256": image_sha256,
                    "data_url": bytes_to_data_url(image_bytes, "image/jpeg"),
                })
        claims = {
            "kind": "grading",
            "assignment_id": assignment_id,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "session_sha256": session_fingerprint,
            "identity_sha256": identity_fingerprint,
            "images": sources,
            "answer_regions": region_claims,
            "expires_at": int(time.time()) + 2 * 60 * 60,
        }
        if grading_input_mode == "original_photo":
            claims.update(grading_input_mode=grading_input_mode, exam_id=str(payload.get("exam_id") or ""))
        fingerprint = _ai_preview_fingerprint(claims)
        preview_provider = os.getenv("HISTORY_GRADER_PROVIDER", "未配置")
        preview_model = os.getenv("HISTORY_GRADER_MODEL", "未配置")
        if payload.get("dual_review_enabled") is True:
            preview_specs = dual_review_model_specs()
            preview_provider = (
                f"{preview_specs['A']['provider_label']} + {preview_specs['B']['provider_label']}"
            )
            preview_model = f"{preview_specs['A']['model']} + {preview_specs['B']['model']}"
        json_response(self, {
            "blocked": False,
            "model": preview_model,
            "provider": preview_provider,
            "prompt": prompt,
            "estimated_model_requests": (2 if payload.get("dual_review_enabled") is True else 1) * __import__("holistic_scoring").request_count(sub_questions, region_prompts),
            "region_prompts": region_prompts,
            "answer_regions": normalized_regions,
            "images": preview_images,
            "processed_pages": processed_pages,
            "image_count": len(preview_images),
            "included_fields": ["题目/材料", "评分细则", "教师确认过的去身份化图片"],
            "excluded_fields": ["姓名/学号/班级/学生标签", "花名册", "OCR/教师录入答案文字", "原始文件名", "原图EXIF"],
            "authorization_header": "仅由服务端附加；值不会出现在预览或日志中",
            "preview_fingerprint": fingerprint,
            "preview_token": _sign_ai_preview(claims),
            "expires_at": claims["expires_at"],
        })

    def _question_remap_record(
        self, conn: sqlite3.Connection, submission_id: str
    ) -> tuple[sqlite3.Row, list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], list[Path]]:
        if not re.fullmatch(r"[0-9a-f]{12}", submission_id):
            raise ValueError("批改记录编号无效")
        row = conn.execute(
            "SELECT submissions.*, assignments.question, assignments.sub_questions_json, "
            "assignments.standard_answer, assignments.criteria_json "
            "FROM submissions JOIN assignments ON assignments.id = submissions.assignment_id "
            "WHERE submissions.id = ?",
            (submission_id,),
        ).fetchone()
        if not row:
            raise ValueError("批改记录不存在")
        if self.is_mobile_token_client() and row["source_channel"] != "mobile":
            raise ValueError("手机访问码只能处理手机提交的本份答卷")
        if row["state"] != "completed" or row["confirmed_at"]:
            raise ValueError("只有未确认的已完成记录可以重新对应题号")
        result = json.loads(row["result_json"] or "{}")
        if result.get("mode") != "vision-model" or result.get("simulation_model"):
            raise ValueError("只有真实视觉评分结果可以进入题号对应复核")
        if not result.get("requires_question_mapping") or not result.get("unmapped_answers"):
            raise ValueError("当前记录没有带可靠图片位置的未匹配答案片段，不能猜测题号")
        if result.get("photos_deleted"):
            raise ValueError("原图缓存已删除，无法安全重新预览和处理")
        saved = json.loads(row["sub_questions_json"] or "[]")
        sub_questions = clean_sub_questions(saved) if saved else clean_sub_questions(
            None,
            fallback_question=row["question"],
            fallback_answer=row["standard_answer"],
            fallback_criteria=json.loads(row["criteria_json"] or "[]"),
        )
        photo_meta = json.loads(row["photos_json"] or "[]")
        photo_paths = _submission_photo_paths(photo_meta)
        if not photo_paths or len(photo_paths) != len(photo_meta):
            raise ValueError("答卷页清单不完整，无法重新处理")
        return row, sub_questions, result, photo_meta, photo_paths

    def _question_remap_images(
        self, photo_meta: list[dict[str, Any]], photo_paths: list[Path]
    ) -> tuple[list[bytes], list[dict[str, str]]]:
        images: list[bytes] = []
        claims: list[dict[str, str]] = []
        for item, path in zip(photo_meta, photo_paths):
            image_bytes = prepare_model_image(path, item.get("ai_redactions", []))
            image_hash = hashlib.sha256(image_bytes).hexdigest()
            approved_hash = str(item.get("approved_ai_page_sha256", ""))
            if not approved_hash or not secrets.compare_digest(image_hash, approved_hash):
                raise ValueError("最终外发图片与原教师确认指纹不一致，已阻止重新处理")
            images.append(image_bytes)
            claims.append({"model_sha256": image_hash})
        return images, claims

    def create_question_remap_preview(self) -> None:
        payload = self.read_json()
        submission_id = str(payload.get("submission_id", "")).strip().lower()
        conn = get_db()
        try:
            row, sub_questions, result, photo_meta, photo_paths = self._question_remap_record(conn, submission_id)
            mapping = build_teacher_question_mapping(payload.get("question_mapping"), result, sub_questions)
            prompt = build_scoring_prompt_text(
                str(row["question"]), sub_questions, privacy_mode=True,
                answer_regions=[], question_mapping=mapping,
            )
            if _contains_roster_identity(conn, prompt) or _contains_labeled_identity_data(prompt):
                json_response(self, {"blocked": True, "error": "请求内容触发身份信息保护，已阻止重新预览。"}, 409)
                return
            images, image_claims = self._question_remap_images(photo_meta, photo_paths)
            identity_fingerprint = _ai_privacy_identity_fingerprint(
                str(row["assignment_id"]),
                "" if row["marking_code_id"] else str(row["class_name"] or ""),
                str(row["marking_code"] or row["student_label"] or ""),
            )
            claims = {
                "kind": "grading-question-remap",
                "submission_id": submission_id,
                "assignment_id": str(row["assignment_id"]),
                "question_mapping": mapping,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "session_sha256": _preview_session_fingerprint(self),
                "identity_sha256": identity_fingerprint,
                "images": image_claims,
                "expires_at": int(time.time()) + 10 * 60,
            }
        finally:
            conn.close()
        fingerprint = _ai_preview_fingerprint(claims)
        json_response(self, {
            "blocked": False,
            "submission_id": submission_id,
            "provider": os.getenv("HISTORY_GRADER_PROVIDER", "未配置"),
            "model": os.getenv("HISTORY_GRADER_MODEL", "未配置"),
            "prompt": prompt,
            "images": [
                {
                    "page": index,
                    "source_page": index,
                    "image_kind": "full_page",
                    "sha256": hashlib.sha256(image).hexdigest(),
                    "data_url": bytes_to_data_url(image, "image/jpeg"),
                }
                for index, image in enumerate(images, start=1)
            ],
            "included_fields": ["教师指定的答案片段题号", "原评分细则", "教师已确认且哈希一致的最终整页图片"],
            "excluded_fields": ["姓名/学号/班级/学生标签", "花名册", "阅卷码", "上传文件名", "原图EXIF"],
            "preview_fingerprint": fingerprint,
            "preview_token": _sign_ai_preview(claims),
            "expires_at": claims["expires_at"],
        })

    def reprocess_question_mapping(self) -> None:
        payload = self.read_json()
        submission_id = str(payload.get("submission_id", "")).strip().lower()
        token = str(payload.get("ai_privacy_preview_token", ""))
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row, sub_questions, result, photo_meta, photo_paths = self._question_remap_record(conn, submission_id)
            mapping = build_teacher_question_mapping(payload.get("question_mapping"), result, sub_questions)
            prompt = build_scoring_prompt_text(
                str(row["question"]), sub_questions, privacy_mode=True,
                answer_regions=[], question_mapping=mapping,
            )
            images, image_claims = self._question_remap_images(photo_meta, photo_paths)
            claims = _read_preview_claims(token)
            identity_fingerprint = _ai_privacy_identity_fingerprint(
                str(row["assignment_id"]),
                "" if row["marking_code_id"] else str(row["class_name"] or ""),
                str(row["marking_code"] or row["student_label"] or ""),
            )
            prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            if (
                claims.get("kind") != "grading-question-remap"
                or claims.get("submission_id") != submission_id
                or claims.get("assignment_id") != str(row["assignment_id"])
                or claims.get("question_mapping") != mapping
                or claims.get("prompt_sha256") != prompt_hash
                or claims.get("session_sha256") != _preview_session_fingerprint(self)
                or claims.get("identity_sha256") != identity_fingerprint
                or claims.get("images") != image_claims
                or payload.get("ai_privacy_fingerprint") != _ai_preview_fingerprint(claims)
                or payload.get("ai_privacy_confirmed") is not True
            ):
                raise ValueError("人工题号选择或最终图片与教师实际预览不一致，请重新预览确认")
            batch_id = str(row["batch_id"] or "")
            if batch_id:
                batch = conn.execute("SELECT * FROM grading_batches WHERE id = ?", (batch_id,)).fetchone()
                from batch_grading_control import stopped
                if stopped(batch):
                    raise ValueError('本批已停止，请先明确继续剩余批改')
                sequence = int(row["batch_sequence"] or 0)
                if (
                    not batch or batch["status"] != "paused"
                    or int(batch["next_sequence"]) != sequence
                    or int(batch["pause_sequence"] or 0) != sequence
                ):
                    raise ValueError("批次仅允许对当前暂停序号原位重新处理")
            queue = conn.execute("SELECT attempt_count FROM task_queue WHERE submission_id = ?", (submission_id,)).fetchone()
            if not queue:
                raise ValueError("任务队列记录不存在，无法安全重新处理")
            token_fingerprint = hashlib.sha256(token.split(".", 1)[0].encode("ascii")).hexdigest()
            _consume_preview_token(token_fingerprint, int(claims["expires_at"]))
            for index, item in enumerate(photo_meta):
                item["approved_prompt_sha256"] = prompt_hash
                item["approved_ai_page_sha256"] = image_claims[index]["model_sha256"]
            queued = queued_result(total_sub_questions(sub_questions))
            queued["summary"] = "教师已指定未匹配答案题号，等待在原批次位置重新处理。"
            queued["teacher_question_mapping"] = mapping
            if result.get("dual_review_requested") is True or isinstance(result.get("dual_review"), dict):
                queued["dual_review_requested"] = True
            now = utc_now()
            changed = conn.execute(
                "UPDATE submissions SET state='queued', result_json=?, photos_json=?, uncertain_model_call=0, "
                "manual_reviewed=0, reviewed_at=NULL, review_note='' WHERE id=? AND state='completed' AND confirmed_at IS NULL",
                (json.dumps(queued, ensure_ascii=False), json.dumps(photo_meta, ensure_ascii=False), submission_id),
            )
            if changed.rowcount != 1:
                raise ValueError("批改记录状态已变化，拒绝重复处理")
            conn.execute(
                "UPDATE task_queue SET state='queued', attempt_count=attempt_count+1, available_at=?, last_error='', updated_at=? WHERE submission_id=?",
                (now, now, submission_id),
            )
            if batch_id:
                conn.execute(
                    "UPDATE grading_batches SET status='active', pause_sequence=NULL, pause_reason='', updated_at=? WHERE id=? AND next_sequence=?",
                    (now, batch_id, int(row["batch_sequence"])),
                )
            append_audit_event(
                conn, "submission_question_mapping_requested", "submission", submission_id,
                {"mapped_fragment_count": len(mapping), "attempt": int(queue["attempt_count"] or 0) + 1},
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        enqueue_submission(submission_id)
        json_response(self, {"ok": True, "submission_id": submission_id, "message": "已按教师指定题号在原记录中重新处理"}, 202)

    def confirm_blank_subquestion(self) -> None:
        payload = self.read_json()
        submission_id = str(payload.get("submission_id", "")).strip().lower()
        sub_question_id = str(payload.get("sub_question_id", "")).strip()
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT submissions.*, assignments.sub_questions_json, assignments.criteria_json, assignments.question, assignments.standard_answer "
                "FROM submissions JOIN assignments ON assignments.id=submissions.assignment_id WHERE submissions.id=?",
                (submission_id,),
            ).fetchone()
            if not row:
                raise ValueError("批改记录不存在")
            if self.is_mobile_token_client() and row["source_channel"] != "mobile":
                raise ValueError("手机访问码只能确认手机提交的本份答卷")
            if row["state"] != "completed" or row["confirmed_at"]:
                raise ValueError("只有尚未确认的已完成记录可以确认空白小题")
            result = json.loads(row["result_json"] or "{}")
            if result.get("mode") != "vision-model" or result.get("simulation_model"):
                raise ValueError("当前记录不是可确认的真实视觉评分结果")
            sub_results = result.get("sub_questions", [])
            selected = next((item for item in sub_results if item.get("sub_question_id") == sub_question_id), None)
            if not selected or selected.get("answer_status") != "unanswered_candidate":
                raise ValueError("该小题并非等待教师确认的空白候选")
            saved = json.loads(row["sub_questions_json"] or "[]")
            sub_questions = clean_sub_questions(saved) if saved else clean_sub_questions(
                None, fallback_question=row["question"], fallback_answer=row["standard_answer"],
                fallback_criteria=json.loads(row["criteria_json"] or "[]"),
            )
            rubric = next((item for item in sub_questions if item["id"] == sub_question_id), None)
            if not rubric:
                raise ValueError("题目评分细则已变化，请重新载入记录")
            selected.update({
                "answer_status": "blank_confirmed",
                "suggested_total": 0.0,
                "blocked": False,
                "needs_review": False,
                "block_reasons": [],
                "summary": "教师已核对原图并确认本题未作答，按0分计。",
                "criteria": [
                    {
                        "criterion_id": criterion["id"], "criterion": criterion["label"],
                        "max_score": float(criterion["score"]), "claimed_score": 0.0,
                        "suggested_score": 0.0, "credit_status": "not_credited", "decision": "none",
                        "student_evidence": "", "reason": "教师确认本题未作答。", "needs_review": False,
                        "evidence_missing": False, "evidence_box_missing": False, "evidence": None,
                    }
                    for criterion in rubric["criteria"]
                ],
            })
            result["suggested_total"] = sum(
                float(item["suggested_total"])
                for item in sub_results
                if isinstance(item, dict) and item.get("suggested_total") is not None
            )
            result["requires_question_mapping"] = bool(result.get("unmapped_answers")) or any(
                item.get("answer_status") == "question_unmatched" for item in sub_results
            )
            result["block_reasons"] = sorted({
                reason for item in sub_results for reason in item.get("block_reasons", [])
            })
            result["blocked"] = bool(result["block_reasons"]) or result_has_unresolved_answer_status(result)
            result["needs_review"] = result["blocked"] or any(
                item.get("needs_review") for item in sub_results
            )
            conn.execute(
                "UPDATE submissions SET result_json=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), submission_id),
            )
            append_audit_event(
                conn, "subquestion_blank_confirmed", "submission", submission_id,
                {"sub_question_id": sub_question_id, "final_zero_requires_submission_confirmation": True},
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        json_response(self, {"ok": True, "submission_id": submission_id, "sub_question_id": sub_question_id})

    def privacy_template_assignment_fingerprint(self, assignment: sqlite3.Row) -> str:
        material = {
            "id": str(assignment["id"]),
            "question": str(assignment["question"] or ""),
            "standard_answer": str(assignment["standard_answer"] or ""),
            "criteria_json": str(assignment["criteria_json"] or "[]"),
            "sub_questions_json": str(assignment["sub_questions_json"] or "[]"),
        }
        return hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def privacy_template_sub_questions(self, assignment: sqlite3.Row) -> list[dict[str, Any]]:
        saved = json.loads(assignment["sub_questions_json"] or "[]")
        if saved:
            return clean_sub_questions(saved)
        return clean_sub_questions(
            None,
            fallback_question=assignment["question"],
            fallback_answer=assignment["standard_answer"],
            fallback_criteria=json.loads(assignment["criteria_json"] or "[]"),
        )

    def get_privacy_templates(self) -> None:
        assignment_id = parse_qs(urlparse(self.path).query).get("assignment_id", [""])[0].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{12}", assignment_id):
            raise ValueError("作业编号无效")
        conn = get_db()
        try:
            assignment = conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
            if not assignment:
                json_response(self, {"error": "作业不存在"}, 404)
                return
            fingerprint = self.privacy_template_assignment_fingerprint(assignment)
            rows = conn.execute(
                "SELECT page_count, template_json, updated_at FROM privacy_templates "
                "WHERE assignment_id = ? AND assignment_fingerprint = ? ORDER BY page_count",
                (assignment_id, fingerprint),
            ).fetchall()
            templates = {}
            for row in rows:
                try:
                    value = json.loads(row["template_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict):
                    templates[str(int(row["page_count"]))] = value
            json_response(self, {"assignment_fingerprint": fingerprint, "templates": templates})
        finally:
            conn.close()

    def save_privacy_template(self) -> None:
        payload = self.read_json()
        assignment_id = str(payload.get("assignment_id", "")).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{12}", assignment_id):
            raise ValueError("作业编号无效")
        try:
            page_count = int(payload.get("page_count", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("模板页数无效") from exc
        if not 1 <= page_count <= MAX_IMAGES_PER_SUBMISSION:
            raise ValueError("模板页数无效")
        conn = get_db()
        try:
            assignment = conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
            if not assignment:
                raise ValueError("作业不存在")
            sub_questions = self.privacy_template_sub_questions(assignment)
            answer_regions = normalize_answer_regions(
                payload.get("answer_regions"), sub_questions, page_count
            )
            if answer_regions:
                raise ValueError("身份排除模板只保存遮挡区域；答案裁切请在单份预览中可选设置")
            redactions = payload.get("redactions")
            if not isinstance(redactions, list) or len(redactions) != page_count:
                raise ValueError("身份排除模板页数不匹配")
            normalized_redactions = [normalize_ai_redactions(item) for item in redactions]
            version = payload.get("version")
            coordinate_system = str(payload.get("coordinate_system", ""))
            if version != 2 or coordinate_system != "perspective_corrected_page_normalized":
                raise ValueError("身份模板必须登记在透视校正后的标准页面坐标中")
            value = {
                "version": 2,
                "coordinate_system": coordinate_system,
                "page_count": page_count,
                "answer_regions": answer_regions,
                "redactions": normalized_redactions,
            }
            fingerprint = self.privacy_template_assignment_fingerprint(assignment)
            conn.execute(
                "INSERT INTO privacy_templates "
                "(assignment_id, page_count, assignment_fingerprint, template_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(assignment_id, page_count, assignment_fingerprint) DO UPDATE SET "
                "template_json=excluded.template_json, updated_at=excluded.updated_at",
                (assignment_id, page_count, fingerprint, json.dumps(value, ensure_ascii=False), utc_now()),
            )
            conn.commit()
            json_response(self, {"ok": True, "page_count": page_count, "assignment_fingerprint": fingerprint})
        finally:
            conn.close()

    def require_local_client(self) -> bool:
        if self.is_local_client():
            return True
        json_response(self, {"error": "此操作只允许在运行程序的电脑本机执行"}, 403)
        return False

    def require_identity_mapping_access(self) -> bool:
        """Require the authenticated single teacher for roster/code crosswalks."""
        if cloud_mode_enabled(self):
            session = getattr(self, "_web_auth_session", None)
            if session is not None and getattr(session, "csrf_token", ""):
                return True
            json_response(self, {"error": "身份映射仅允许已登录教师访问"}, 401)
            return False
        return self.require_local_client()

    def mobile_page_token(self) -> str:
        """Return a valid temporary token supplied for the photo entry page."""
        token = parse_qs(urlparse(self.path).query).get("access_token", [""])[0].strip()
        if valid_mobile_token(token):
            return token
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get("history_grader_mobile")
        token = morsel.value.strip() if morsel else ""
        return token if valid_mobile_token(token) else ""

    def is_mobile_token_client(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        return valid_mobile_token(token)

    def require_api_access(self, path: str = "") -> bool:
        if self.is_local_client():
            return True
        mobile_paths = self.MOBILE_READ_PATHS | self.MOBILE_WRITE_PATHS | self.MOBILE_PREVIEW_PATHS
        if self.is_mobile_token_client() and path in mobile_paths:
            return True
        if self.is_mobile_token_client():
            json_response(self, {"error": "手机访问码仅允许读取必要信息、上传答卷、核对并复核手机提交的答卷、确认手机提交的成绩、跳过手机批次当前暂停项和清理手机提交的照片缓存；设置、删整题、备份、恢复、重启和退出只允许电脑本机执行"}, 403)
            return False
        json_response(
            self,
            {"error": "手机访问需要有效的临时授权码，请从电脑本机重新生成访问码", "mobile_auth_required": True},
            401,
        )
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/diagnostics-export":
            self.export_diagnostics()
            return
        if parsed.path in {"/objective", "/objective/"} and parse_qs(parsed.query).get("embedded") != ["1"] and parse_qs(parsed.query).get("legacy") != ["1"]:
            query = parse_qs(parsed.query, keep_blank_values=True)
            context = {target: query[source][0] for source, target in (("exam_id", "exam"), ("assessment_id", "assessment")) if source in query}
            self.send_response(302)
            self.send_header("Location", "/next/#/objective" + ("?" + urlencode(context) if context else ""))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.dispatch_objective_request(parsed):
            return
        # Mobile token access retains its existing restricted photo page.
        if parsed.path in {"/legacy", "/legacy/", "/legacy/index.html"} or (
            parsed.path in {"/", "/index.html"} and self.mobile_page_token()
        ):
            self.serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8", no_store=True)
            return
        if serve_preview(self, parsed.path, STATIC_DIR):
            return
        if parsed.path.startswith("/api/") and not self.require_api_access(parsed.path):
            return
        if parsed.path in {'/api/class-upload-preview','/api/assignment-markings','/api/exam-mapping','/api/exam-marking-codes','/api/bind-student','/api/attach-anonymous-submission'}:
            json_response(self, {'error':'身份码功能已移除；答卷可匿名批改、导出，通过答卷关联合并成绩'},410)
            return
        if parsed.path.startswith("/api/") and RESTORE_IN_PROGRESS:
            json_response(self, {"error": "数据恢复正在进行，请稍候"}, 409)
            return
        if parsed.path in {"/", "/index.html"}:
            self.serve_file(STATIC_DIR / "index.html", "text/html; charset=utf-8", no_store=True)
            return
        if parsed.path in {"/help", "/help.html"}:
            self.serve_file(STATIC_DIR / "help.html", "text/html; charset=utf-8", no_store=True)
            return
        if parsed.path.startswith("/static/"):
            filename = Path(parsed.path.removeprefix("/static/")).name
            content_types = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
            self.serve_file(STATIC_DIR / filename, content_types.get(Path(filename).suffix, "text/plain"))
            return
        if parsed.path == "/api/exam-import-reference":
            if not self.require_local_client():
                return
            query = parse_qs(parsed.query)
            draft_id = query.get("draft_id", [""])[0].strip().lower()
            page_text = query.get("page_number", [""])[0].strip()
            if not re.fullmatch(r"[0-9a-f]{32}", draft_id) or not page_text.isdigit():
                json_response(self, {"error": "页面 reference 参数无效"}, 400)
                return
            candidate = (DATA_DIR / "exam_import_drafts" / draft_id / "layout_reference" / f"page-{int(page_text)}.png").resolve()
            if not candidate.is_relative_to((DATA_DIR / "exam_import_drafts" / draft_id).resolve()):
                json_response(self, {"error": "页面 reference 路径无效"}, 400)
                return
            self.serve_file(candidate, "image/png", no_store=True)
            return
        if parsed.path == "/api/exam-layout-profile":
            if not self.require_local_client():
                return
            exam_id = parse_qs(parsed.query).get("exam_id", [""])[0].strip().lower()
            if not re.fullmatch(r"[0-9a-f]{32}", exam_id):
                json_response(self, {"error": "正式考试编号无效"}, 400)
                return
            conn = get_db()
            try:
                context = _exam_layout_profile_context(conn, exam_id)
            finally:
                conn.close()
            if context is None:
                json_response(self, {"error": "正式考试不存在"}, 404)
                return
            public_pages = []
            for page in context["references"]:
                public_pages.append({
                    key: value for key, value in page.items() if key != "image_file"
                } | {
                    "preview_url": f"/api/exam-layout-reference?exam_id={exam_id}&page_number={page['page_number']}"
                })
            json_response(self, {**context, "references": public_pages})
            return
        if parsed.path == "/api/exam-layout-reference":
            if not self.require_local_client():
                return
            query = parse_qs(parsed.query)
            exam_id = query.get("exam_id", [""])[0].strip().lower()
            page_text = query.get("page_number", [""])[0].strip()
            if not re.fullmatch(r"[0-9a-f]{32}", exam_id) or not page_text.isdigit():
                json_response(self, {"error": "考试 reference 参数无效"}, 400)
                return
            conn = get_db()
            try:
                context = _exam_layout_profile_context(conn, exam_id)
                if context is not None:
                    _resolve_exam_reference_assets(conn, context, page_numbers={int(page_text)})
            finally:
                conn.close()
            if context is None:
                json_response(self, {"error": "正式考试不存在"}, 404)
                return
            page_number = int(page_text)
            page = next((item for item in context["references"] if item["page_number"] == page_number), None)
            if page is None:
                json_response(self, {"error": "该页不是正式试卷的可用 PDF reference"}, 404)
                return
            if page.get("resolved_asset_status") not in {"reference_asset_ready", "reference_rebuilt"}:
                json_response(self, {
                    "error": str(page.get("resolved_asset_status") or "reference_asset_missing"),
                    "status": page.get("resolved_asset_status") or "reference_asset_missing",
                    "reason": page.get("asset_reason"),
                }, 404)
                return
            candidate = Path(str(page.get("resolved_image_path") or ""))
            self.serve_file(candidate, "image/png", no_store=True)
            return
        if parsed.path == "/api/student-auto-crop-artifact":
            if not self.require_local_client():
                return
            query = parse_qs(parsed.query)
            run_id = query.get("run_id", [""])[0].strip().lower()
            file_name = query.get("file", [""])[0].strip()
            is_root_artifact = bool(file_name) and Path(file_name).name == file_name
            is_padding_artifact = bool(re.fullmatch(r"padding_validation/[A-Za-z0-9._-]+", file_name))
            if not re.fullmatch(r"[0-9a-f]{32}", run_id) or not (is_root_artifact or is_padding_artifact) or "\\" in file_name:
                json_response(self, {"error": "自动裁切产物参数无效"}, 400)
                return
            artifact_root = _student_crop_artifact_root(run_id)
            run_dir = (artifact_root / run_id).resolve()
            if not run_dir.is_relative_to(artifact_root):
                json_response(self, {"error": "自动裁切产物路径无效"}, 400)
                return
            manifest_path = run_dir / "run.json"
            if not manifest_path.is_file():
                json_response(self, {"error": "自动裁切运行记录不存在"}, 404)
                return
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                json_response(self, {"error": "自动裁切运行记录不可读取"}, 404)
                return
            allowed_files = set(manifest.get("artifacts", [])) | {"run.json", "AUTO_CROP_REPORT.md"}
            if file_name not in allowed_files:
                json_response(self, {"error": "该文件不属于本次自动裁切产物"}, 404)
                return
            candidate = (run_dir / file_name).resolve()
            if not candidate.is_relative_to(run_dir) or not candidate.is_file():
                json_response(self, {"error": "自动裁切产物不存在"}, 404)
                return
            media_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".json": "application/json; charset=utf-8",
                ".md": "text/markdown; charset=utf-8",
            }.get(candidate.suffix.casefold(), "application/octet-stream")
            self.serve_file(candidate, media_type, no_store=True)
            return
        if parsed.path == "/api/status":
            force_network_refresh = parse_qs(parsed.query).get("refresh") == ["1"]
            cloud_mode = cloud_mode_enabled(self)
            network_interfaces = (
                [] if cloud_mode else cached_local_network_interfaces(force_network_refresh)
            )
            cloud_admin_session = cloud_mode and getattr(self, "_web_auth_session", None) is not None
            available_model_presets = (
                server_model_presets()
                if cloud_admin_session
                else [] if cloud_mode_enabled(self) else public_model_presets()
            )
            json_response(
                self,
                {
                    "ok": True,
                    "model_configured": model_configured(),
                    "model_is_simulation": model_configuration_is_synthetic(),
                    "provider": os.getenv("HISTORY_GRADER_PROVIDER", "deepseek"),
                    "model_preset": server_model_selection_id() if cloud_admin_session else current_model_preset_id(),
                    "base_url": os.getenv("HISTORY_GRADER_BASE_URL", ""),
                    "model": os.getenv("HISTORY_GRADER_MODEL", ""),
                    "api_key_configured": bool(os.getenv("HISTORY_GRADER_API_KEY")),
                    "api_key_persisted": local_api_key_exists(
                        os.getenv("HISTORY_GRADER_PROVIDER", "deepseek"),
                        expected_origin=current_api_origin(),
                    ),
                    "api_key_session_only": os.getenv("HISTORY_GRADER_PROVIDER", "deepseek")
                    in SESSION_ONLY_KEY_PROVIDERS,
                    "api_key_error": KEY_STORE_ERROR,
                    "model_presets": available_model_presets,
                    "server_model_selection_enabled": bool(cloud_admin_session and available_model_presets),
                    "data_dir": str(DATA_DIR),
                    "portable_instance_id": ensure_portable_instance_id(),
                    "service_pid": os.getpid(),
                    "service_port": None if cloud_mode else self.server.server_address[1],
                    # Keep the original string list for older clients. The
                    # richer records let the page distinguish USB tethering
                    # from ordinary Wi-Fi or Ethernet addresses.
                    "network_addresses": [] if cloud_mode else [item["address"] for item in network_interfaces],
                    "network_interfaces": network_interfaces,
                    "usb_network_available": False if cloud_mode else any(
                        item.get("connection_type") == "usb" for item in network_interfaces
                    ),
                    "mobile_token_active": mobile_token_active(),
                    "active_task_count": active_task_count(),
                    "grade_worker_count": configured_grade_worker_count(),
                    "active_grade_workers": sum(thread.is_alive() for thread in QUEUE_THREADS),
                    "queued_in_memory": GRADE_QUEUE.qsize(),
                },
            )
            return
        if parsed.path == "/api/dual-review/status":
            if not self.require_local_client():
                return
            specs = dual_review_model_specs()
            catalog = model_preset_catalog()
            pair_distinct = (
                specs["A"]["provider"], specs["A"]["model"], specs["A"]["api_origin"]
            ) != (
                specs["B"]["provider"], specs["B"]["model"], specs["B"]["api_origin"]
            )
            json_response(self, {
                "enabled": True,
                "provider_a": specs["A"]["provider"],
                "provider_label_a": specs["A"]["provider_label"],
                "model_a": specs["A"]["model"],
                "preset_a": specs["A"]["preset_id"],
                "label_a": specs["A"]["label"],
                "configured_a": dual_review_key_is_configured("A"),
                "provider_b": specs["B"]["provider"],
                "provider_label_b": specs["B"]["provider_label"],
                "model_b": specs["B"]["model"],
                "preset_b": specs["B"]["preset_id"],
                "label_b": specs["B"]["label"],
                "configured_b": dual_review_key_is_configured("B"),
                "primary_model_configured": model_configured(),
                "pair_distinct": pair_distinct,
                "available_models": [
                    {"id": preset_id, "label": catalog[preset_id]["label"],
                     "provider": catalog[preset_id]["provider"],
                     "provider_label": catalog[preset_id]["provider_label"],
                     "model": catalog[preset_id]["model"],
                     "note": catalog[preset_id].get("note", "")}
                    for preset_id in catalog if dual_review_preset_allowed(preset_id)
                ],
            })
            return
        if parsed.path == "/api/mobile/dual-review-readiness":
            # A phone token may read only the already-selected pair and whether
            # its credentials are present. It cannot inspect the model catalog,
            # provider endpoints, or change either model configuration.
            specs = dual_review_model_specs()
            pair_distinct = (
                specs["A"]["provider"], specs["A"]["model"], specs["A"]["api_origin"]
            ) != (
                specs["B"]["provider"], specs["B"]["model"], specs["B"]["api_origin"]
            )
            selected = [
                {
                    "id": specs[side]["preset_id"],
                    "label": specs[side]["label"],
                    "provider_label": specs[side]["provider_label"],
                    "model": specs[side]["model"],
                }
                for side in ("A", "B")
            ]
            json_response(self, {
                "enabled": True,
                "mobile_read_only": True,
                "provider_label_a": specs["A"]["provider_label"],
                "model_a": specs["A"]["model"],
                "preset_a": specs["A"]["preset_id"],
                "label_a": specs["A"]["label"],
                "configured_a": dual_review_key_is_configured("A"),
                "provider_label_b": specs["B"]["provider_label"],
                "model_b": specs["B"]["model"],
                "preset_b": specs["B"]["preset_id"],
                "label_b": specs["B"]["label"],
                "configured_b": dual_review_key_is_configured("B"),
                "pair_distinct": pair_distinct,
                "available_models": selected,
            })
            return
        if parsed.path == "/api/custom-models":
            if not self.require_local_client():
                return
            json_response(self, {"presets": read_custom_model_presets()})
            return
        if parsed.path == "/api/storage":
            if not self.require_local_client():
                return
            json_response(self, storage_snapshot())
            return
        if parsed.path == "/api/retention":
            if not self.require_local_client():
                return
            json_response(self, retention_report())
            return
        if parsed.path == "/api/evaluation/summary":
            if not self.require_local_client():
                return
            try:
                json_response(self, evaluation_summary())
            except ValueError as exc:
                json_response(self, {"error": str(exc)}, 400)
            return
        if parsed.path == "/api/audit/summary":
            if not self.require_local_client():
                return
            json_response(self, audit_chain_snapshot())
            return
        if parsed.path == "/api/backups":
            if not self.require_local_client():
                return
            json_response(self, {"backups": list_backups()})
            return
        if parsed.path == "/api/rosters":
            if self.is_mobile_token_client():
                json_response(self, {"classes": []})
                return
            conn = get_db()
            rows = storage.roster_class_rows(conn)
            conn.close()
            json_response(self, {"classes": [dict(row) for row in rows]})
            return
        if parsed.path == "/api/roster-students":
            if self.is_mobile_token_client():
                json_response(self, {"students": []})
                return
            class_name = parse_qs(parsed.query).get("class_name", [""])[0].strip()
            conn = get_db()
            rows = storage.roster_student_rows(conn, class_name)
            conn.close()
            json_response(self, {"students": [dict(row) for row in rows]})
            return
        if parsed.path == "/api/class-exams":
            if not self.require_identity_mapping_access():
                return
            query = parse_qs(parsed.query)
            conn = get_db()
            try:
                rows = conn.execute("SELECT e.id AS exam_id,e.created_at FROM grading_exams e LEFT JOIN roster_classes c ON c.id=e.class_id WHERE e.assignment_id=? AND (c.class_name=? OR (e.class_id IS NULL AND NOT EXISTS (SELECT 1 FROM exam_marking_codes m WHERE m.exam_id=e.id))) ORDER BY e.created_at DESC", (query.get("assignment_id", [""])[0], query.get("class_name", [""])[0])).fetchall()
                json_response(self, {"exams": [dict(r) for r in rows]})
            finally:
                conn.close()
            return
        if parsed.path == "/api/submission":
            submission_id = parse_qs(parsed.query).get("submission_id", [""])[0].strip()
            if not re.fullmatch(r"[0-9a-f]{12}", submission_id):
                json_response(self, {"error": "批改记录编号无效"}, 400)
                return
            conn = get_db()
            try:
                row = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
                if row and not self.is_local_client() and row["source_channel"] != "mobile":
                    row = None
                submission = submission_public(row) if row else None
            finally:
                conn.close()
            if submission is None:
                json_response(self, {"error": "批改记录不存在"}, 404)
                return
            json_response(self, {"submission": submission})
            return
        if parsed.path == '/api/batch-adoption':
            if not self.require_local_client():
                return
            from batch_adoption import listing
            params = parse_qs(parsed.query)
            conn = get_db()
            try:
                result = listing(conn, params.get('assignment_id',[''])[0], params.get('exam_id',[''])[0], getattr(getattr(self,'_web_auth_session',None),'username','') or 'local', sys.modules[__name__],batch_id=params.get('batch_id',[''])[0])
                json_response(self, result)
            except ValueError:
                json_response(self, {'error':'考试范围无效，请重新选择考试'}, 400)
            finally:
                conn.close()
            return
        if parsed.path == "/api/batches":
            if not self.require_local_client():
                return
            conn = get_db()
            try:
                params = parse_qs(parsed.query, keep_blank_values=True)
                include_completed = params.get('include_completed', [''])[0] == '1'
                summary_only = include_completed and params.get('summary', [''])[0] == '1'
                filters, values = [], []
                for key, column, length in [('assignment_id','assignment_id',12),('exam_id','exam_id',32),('batch_id','id',12)]:
                    value = params.get(key, [''])[0]
                    if key in params:
                        if not re.fullmatch('[0-9a-f]{'+str(length)+'}', value):
                            json_response(self, {'error':'批次查询范围无效'}, 400)
                            return
                        filters.append(column+'=?');values.append(value)
                if not include_completed:
                    filters.append("status IN ('active', 'paused')")
                rows = conn.execute(
                    "SELECT id, exam_id, assignment_id, class_name, total_sequences, next_sequence, status, pause_sequence, pause_reason, manifest_json, created_at, updated_at "
                    "FROM grading_batches" + (' WHERE '+' AND '.join(filters) if filters else '') + " ORDER BY created_at DESC LIMIT 100", values
                ).fetchall()
                batches = []
                for row in rows:
                    manifest = json.loads(row["manifest_json"] or "{}")
                    for item in ([] if summary_only else manifest.get("items", [])):
                        submitted = conn.execute(
                            "SELECT state FROM submissions WHERE id = ?", (item.get("submission_id"),)
                        ).fetchone()
                        item["submission_state"] = submitted["state"] if submitted else None
                    batches.append({
                        "batch_id": row["id"],
                        "exam_id": str(row["exam_id"] or ""),
                        "assignment_id": row["assignment_id"],
                        "class_name": row["class_name"],
                        "grading_input_mode": _grading_input_mode(manifest),
                        "preview_exam_id": str(manifest.get("preview_exam_id", "") or ""),
                        "total_sequences": row["total_sequences"],
                        "next_sequence": row["next_sequence"],
                        "status": row["status"],
                        "pause_sequence": row["pause_sequence"],
                        "pause_reason": row["pause_reason"],
                        "grading_stopped": bool(manifest.get('grading_stop_requested')),
                        "items": [] if summary_only else manifest.get("items", []),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    })
                    if include_completed:
                        counts = conn.execute("SELECT COUNT(*),COALESCE(SUM(state='completed'),0) FROM submissions WHERE batch_id=?",(row['id'],)).fetchone()
                        batches[-1].update(uploaded_count=counts[0], processed_count=counts[1], assignment_name=(conn.execute('SELECT name FROM assignments WHERE id=?',(row['assignment_id'],)).fetchone() or ['考试来源不可用'])[0])
                json_response(self, {"batches": batches})
            finally:
                conn.close()
            return
        if parsed.path == "/api/assignment-submissions":
            if not self.require_local_client():
                return
            assignment_id = parse_qs(parsed.query).get("assignment_id", [""])[0].strip().lower()
            if not re.fullmatch(r"[0-9a-f]{12}", assignment_id):
                json_response(self, {"error": "作业记录编号无效"}, 400)
                return
            conn = get_db()
            try:
                rows = storage.assignment_submission_rows(conn, assignment_id)
                from batch_grading_control import stopped
                stopped_batches = {r['id'] for r in conn.execute('SELECT id,manifest_json FROM grading_batches WHERE assignment_id=?', (assignment_id,)) if stopped(r)}
                json_response(self, {"submissions": [{**submission_list_public(row), 'grading_stopped':row['state'] == 'queued' and row['batch_id'] in stopped_batches} for row in rows]})
            finally:
                conn.close()
            return
        if parsed.path == "/api/recovery-submissions":
            mobile_client = self.is_mobile_token_client()
            if not self.is_local_client() and not mobile_client and not self.require_local_client():
                return
            conn = get_db()
            try:
                rows = conn.execute(
                    """
                    SELECT submissions.*, assignments.name AS assignment_name
                    FROM submissions
                    JOIN assignments ON assignments.id = submissions.assignment_id
                    LEFT JOIN grading_batches ON grading_batches.id = submissions.batch_id
                    WHERE (submissions.state = 'recovery_pending'
                       OR submissions.state = 'failed'
                       OR (submissions.state = 'completed' AND submissions.uncertain_model_call = 1)
                       OR (grading_batches.status = 'paused' AND grading_batches.pause_sequence = submissions.batch_sequence
                           AND submissions.state = 'completed'))
                      AND (? = 0 OR (submissions.source_channel = 'mobile'
                           AND grading_batches.status = 'paused'
                           AND grading_batches.pause_sequence = submissions.batch_sequence))
                    ORDER BY COALESCE(grading_batches.created_at, submissions.created_at) DESC,
                             CASE WHEN submissions.batch_id IS NULL THEN 1 ELSE 0 END,
                             CASE WHEN submissions.batch_id IS NOT NULL THEN submissions.batch_sequence END ASC,
                             submissions.created_at DESC
                    LIMIT 100
                    """,
                    (1 if mobile_client else 0,),
                ).fetchall()
                submissions = []
                for row in rows:
                    item = submission_public(row)
                    paused_unmatched = (
                        item["state"] == "completed"
                        and not row["confirmed_at"]
                        and item["result"].get("mode") == "vision-model"
                        and item["result"].get("requires_question_mapping") is True
                        and (
                            bool(item["result"].get("unmapped_answers"))
                            or any(
                                point.get("answer_status") == "question_unmatched"
                                for point in item["result"].get("sub_questions", [])
                                if isinstance(point, dict)
                            )
                        )
                    )
                    retryable, retry_reason = manual_retry_policy(
                        item["result"], item["state"], bool(item["photos"])
                    )
                    if item["state"] in {"recovery_pending", "failed"} or item["result"].get("mode") in {
                        "vision-model-error",
                        "processing-error",
                        "rules-demo",
                    }:
                        item["assignment_name"] = row["assignment_name"]
                        item["retryable"] = retryable
                        item["retry_reason"] = retry_reason
                        queue_row = conn.execute(
                            "SELECT attempt_count, updated_at FROM task_queue WHERE submission_id = ?",
                            (item["id"],),
                        ).fetchone()
                        if queue_row:
                            item["retry_wait_seconds"] = manual_retry_backoff_seconds(
                                int(queue_row["attempt_count"] or 0), str(queue_row["updated_at"] or "")
                            )
                        submissions.append(item)
                    elif paused_unmatched:
                        item["assignment_name"] = row["assignment_name"]
                        item["retryable"] = False
                        item["retry_reason"] = "题号无法可靠对应；请确认跳过后再继续批次"
                        item["recovery_action"] = "skip_unmatched_batch_sequence"
                        submissions.append(item)
                mobile_batches = []
                if mobile_client:
                    paused_uploads = conn.execute(
                        "SELECT grading_batches.*, assignments.name AS assignment_name "
                        "FROM grading_batches JOIN assignments ON assignments.id = grading_batches.assignment_id "
                        "WHERE grading_batches.source_channel = 'mobile' AND grading_batches.status = 'paused' "
                        "AND grading_batches.pause_sequence = grading_batches.next_sequence "
                        "ORDER BY grading_batches.created_at DESC LIMIT 20"
                    ).fetchall()
                    for batch in paused_uploads:
                        try:
                            manifest = json.loads(batch["manifest_json"] or "{}")
                        except (TypeError, json.JSONDecodeError):
                            manifest = {}
                        current = next((
                            value for value in manifest.get("items", [])
                            if int(value.get("sequence", 0)) == int(batch["pause_sequence"] or 0)
                        ), None)
                        if current and current.get("upload_status") == "failed":
                            exists = conn.execute(
                                "SELECT 1 FROM submissions WHERE id = ?",
                                (current.get("submission_id"),),
                            ).fetchone()
                            if not exists:
                                submissions.append({
                                    "id": str(current.get("submission_id") or ""),
                                    "batch_id": str(batch["id"]),
                                    "sequence": int(current.get("sequence", 0)),
                                    "state": "failed",
                                    "student_label": f"第 {int(current.get('sequence', 0))} 份答卷",
                                    "assignment_name": batch["assignment_name"],
                                    "photos": [],
                                    "result": {"summary": "上传失败，答卷未保存；教师可在手机确认跳过并继续"},
                                    "retryable": False,
                                    "retry_reason": "照片未完整保存；请重新选择照片或确认跳过",
                                    "recovery_action": "skip_failed_upload",
                                })
                    active = conn.execute(
                        "SELECT id, total_sequences, next_sequence, status, manifest_json "
                        "FROM grading_batches WHERE source_channel = 'mobile' AND status = 'active' "
                        "ORDER BY created_at DESC LIMIT 20"
                    ).fetchall()
                    for batch in active:
                        try:
                            manifest = json.loads(batch["manifest_json"] or "{}")
                        except (TypeError, json.JSONDecodeError):
                            manifest = {}
                        mobile_batches.append({
                            "batch_id": str(batch["id"]),
                            "total_sequences": int(batch["total_sequences"]),
                            "next_sequence": int(batch["next_sequence"]),
                            "status": str(batch["status"]),
                            "items": [
                                {"sequence": int(value.get("sequence", 0))}
                                for value in manifest.get("items", [])
                                if isinstance(value, dict)
                            ],
                        })
            finally:
                conn.close()
            response = {"submissions": submissions}
            if mobile_client:
                response["batches"] = mobile_batches
            json_response(self, response)
            return
        if parsed.path == "/api/total-confirmation":
            import teacher_total
            local_client = self.is_local_client()
            conn = get_db()
            try:
                conn.execute('BEGIN')
                row = teacher_total.read(conn, parse_qs(parsed.query).get('submission_id', [''])[0])
                if not local_client and row['source_channel'] != 'mobile':
                    raise ValueError('手机访问码不能查看本答卷')
                prepared = teacher_total.prepare(conn, row, sys.modules[__name__])
            except ValueError as exc:
                json_response(self, {"error":str(exc)}, 400)
                return
            finally:
                conn.close()
            json_response(self, prepared, 200)
            return
        if parsed.path == "/api/submission-photo":
            query = parse_qs(parsed.query)
            submission_id = query.get("submission_id", [""])[0].strip()
            try:
                photo_index = int(query.get("photo_index", ["0"])[0])
            except ValueError:
                photo_index = 0
            if not re.fullmatch(r"[0-9a-f]{12}", submission_id) or photo_index < 1:
                json_response(self, {"error": "照片参数无效"}, 400)
                return
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT assignment_id, source_channel, photos_json FROM submissions WHERE id = ?",
                    (submission_id,),
                ).fetchone()
            finally:
                conn.close()
            if not row or (not self.is_local_client() and row["source_channel"] != "mobile"):
                json_response(self, {"error": "批改记录不存在"}, 404)
                return
            try:
                photos = json.loads(row["photos_json"] or "[]")
            except json.JSONDecodeError:
                photos = []
            if not isinstance(photos, list):
                photos = []
            if photo_index > len(photos):
                json_response(self, {"error": "照片不存在"}, 404)
                return
            photo = photos[photo_index - 1]
            preview_mode = query.get("preview", [""])[0]
            crop_preview = preview_mode == "answer_region"
            if crop_preview and photo.get("image_role") != "answer_region":
                json_response(self, {"error": "该照片不是已保存的答案图"}, 404)
                return
            relative = str(photo.get("original_path") or photo.get("path") or "")
            try:
                candidate = resolve_data_relative_path(relative) if relative else None
            except ValueError:
                candidate = None
            try:
                student_dir = submission_photo_directory(row["assignment_id"], submission_id, photos)
            except ValueError:
                student_dir = None
            if (
                not relative
                or student_dir is None
                or candidate is None
                or candidate.parent != student_dir.resolve()
                or not candidate.is_file()
            ):
                json_response(self, {"error": "原图文件不存在"}, 404)
                return
            content_type = str(photo.get("original_content_type") or "image/jpeg").split(";", 1)[0].strip().casefold()
            if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                json_response(self, {"error": "原图格式不支持查看"}, 415)
                return
            # Viewing the original is the human-review gate for both local and
            # authenticated mobile submissions. A mobile token is already
            # restricted to its own mobile-origin record above.
            if preview_mode not in {"answer_region", "readonly"} and (self.is_local_client() or self.is_mobile_token_client()):
                mark_submission_photo_viewed(submission_id)
            self.serve_file(candidate, content_type, no_store=True)
            return
        if parsed.path == "/api/submission-evidence":
            query = parse_qs(parsed.query)
            submission_id = query.get("submission_id", [""])[0].strip()
            sub_question_id = query.get("sub_question_id", [""])[0].strip()
            criterion_id = query.get("criterion_id", [""])[0].strip()
            if not re.fullmatch(r"[0-9a-f]{12}", submission_id) or not sub_question_id or not criterion_id:
                json_response(self, {"error": "证据参数无效"}, 400)
                return
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT assignment_id, source_channel, photos_json, result_json FROM submissions WHERE id = ?",
                    (submission_id,),
                ).fetchone()
            finally:
                conn.close()
            if not row or (not self.is_local_client() and row["source_channel"] != "mobile"):
                json_response(self, {"error": "批改记录不存在"}, 404)
                return
            try:
                photos = json.loads(row["photos_json"] or "[]")
                result = json.loads(row["result_json"] or "{}")
            except json.JSONDecodeError:
                photos, result = [], {}
            if not isinstance(photos, list) or not isinstance(result, dict):
                json_response(self, {"error": "证据记录无效"}, 404)
                return
            evidence = None
            for sub_question in result.get("sub_questions", []):
                if not isinstance(sub_question, dict) or str(sub_question.get("sub_question_id")) != sub_question_id:
                    continue
                for criterion in sub_question.get("criteria", []):
                    if isinstance(criterion, dict) and str(criterion.get("criterion_id")) == criterion_id:
                        evidence = criterion.get("evidence")
                        break
            if not isinstance(evidence, dict) or not evidence.get("crop_path"):
                json_response(self, {"error": "该评分点没有可用证据图片"}, 404)
                return
            try:
                student_dir = submission_photo_directory(row["assignment_id"], submission_id, photos)
                relative = str(evidence["crop_path"])
                candidate = resolve_data_relative_path(relative)
            except (ValueError, OSError):
                student_dir = None
                candidate = PORTABLE_ROOT / "__invalid_evidence__"
            evidence_dir = (student_dir / "evidence").resolve() if student_dir else None
            if (
                evidence_dir is None
                or not candidate.is_relative_to(evidence_dir)
                or candidate.suffix.casefold() != ".jpg"
                or not candidate.is_file()
            ):
                json_response(self, {"error": "证据图片不存在"}, 404)
                return
            self.serve_file(candidate, "image/jpeg", no_store=True)
            return
        if parsed.path == "/api/answer-management":
            try:
                self.manage_answer_data(listing=True)
            except ValueError as exc:
                json_response(self, {"error": str(exc)}, 400)
            return
        if parsed.path == "/api/batch-delete-preview":
            try:
                self.batch_delete_preview()
            except ValueError:
                json_response(self, {"error": "批次不存在、归属不符或无法预览删除范围"}, 400)
            return
        if parsed.path == "/api/assignment-delete-preview":
            try:
                self.assignment_delete_preview()
            except ValueError:
                json_response(self, {"error": "考试不存在或无法预览删除范围"}, 400)
            return
        if parsed.path == "/api/assignments":
            conn = get_db()
            objective_tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            objective_source_available={'objective_assessments','objective_submissions'} <= objective_tables and 'source_exam_id' in {r[1] for r in conn.execute('PRAGMA table_info(objective_assessments)')}
            rows = conn.execute(
                "SELECT id, name, question, created_at FROM assignments ORDER BY created_at DESC"
            ).fetchall()
            result = []
            for row in rows:
                count = conn.execute(
                    "SELECT COUNT(*) FROM submissions WHERE assignment_id = ?", (row["id"],)
                ).fetchone()[0]
                assignment = conn.execute(
                    "SELECT sub_questions_json, criteria_json, standard_answer, import_metadata_json FROM assignments WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                formal_exam = conn.execute(
                    "SELECT id, layout_profile_id FROM grading_exams WHERE assignment_id = ? "
                    "ORDER BY (layout_profile_id IS NOT NULL) DESC, created_at DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                saved_questions = json.loads(assignment["sub_questions_json"] or "[]")
                if saved_questions:
                    crop_units = clean_sub_questions(saved_questions)
                elif _json_object_or_empty(assignment["import_metadata_json"]).get("created_from_import") and not json.loads(assignment["criteria_json"] or "[]"):
                    crop_units = []
                else:
                    crop_units = clean_sub_questions(
                        None,
                        fallback_question=row["question"],
                        fallback_answer=assignment["standard_answer"],
                        fallback_criteria=json.loads(assignment["criteria_json"] or "[]"),
                    )
                result.append({
                    **dict(row),
                    "submission_count": count,
                    "objective_submission_count": conn.execute(
                        'SELECT COUNT(*) FROM objective_submissions s JOIN objective_assessments a ON a.assessment_id=s.assessment_id JOIN grading_exams e ON e.id=a.source_exam_id WHERE e.assignment_id=?',
                        (row['id'],)).fetchone()[0] if objective_source_available else 0,
                    "answer_region_profile": answer_region_profile(rubric_fingerprint(crop_units), 1),
                    "exam_id": str(formal_exam["id"]) if formal_exam else "",
                    "formal_layout_required": bool(
                        _json_object_or_empty(_json_object_or_empty(conn.execute(
                            "SELECT import_metadata_json FROM assignments WHERE id=?", (row["id"],)
                        ).fetchone()[0]).get("source")).get("layout_source")
                        or (formal_exam and formal_exam["layout_profile_id"])
                    ),
                    "layout_exams": [dict(exam) for exam in conn.execute(
                        "SELECT id AS exam_id, created_at FROM grading_exams WHERE assignment_id=? "
                        "AND class_id IS NULL AND layout_profile_id IS NOT NULL ORDER BY created_at DESC",
                        (row["id"],),
                    )],
                    "original_photo_exams": [dict(exam) for exam in conn.execute(
                        "SELECT id AS exam_id, created_at FROM grading_exams WHERE assignment_id=? "
                        "AND class_id IS NULL ORDER BY created_at DESC",
                        (row["id"],),
                    )],
                    "sub_questions": [
                        {"id": item["id"], "label": item["label"]} for item in crop_units
                    ],
                })
            conn.close()
            json_response(self, {"assignments": result})
            return
        if parsed.path == "/api/privacy-templates":
            self.get_privacy_templates()
            return
        if parsed.path == "/api/export.csv":
            assignment_id = parse_qs(parsed.query).get("assignment_id", [""])[0]
            self.export_csv(assignment_id)
            return
        if parsed.path == "/api/export-anonymous.csv":
            assignment_id = parse_qs(parsed.query).get("assignment_id", [""])[0]
            self.export_anonymous_csv(assignment_id)
            return
        json_response(self, {"error": "Not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if self.dispatch_objective_request(parsed):
            return
        if parsed.path.startswith("/api/") and not self.require_api_access(parsed.path):
            return
        if parsed.path in {'/api/class-upload-preview','/api/assignment-markings','/api/exam-mapping','/api/exam-marking-codes','/api/bind-student','/api/attach-anonymous-submission'}:
            json_response(self, {'error':'身份码功能已移除；答卷可匿名批改、导出，通过答卷关联合并成绩'},410)
            return
        if (
            parsed.path.startswith("/api/")
            and (getattr(self.server, "stopping", False) or getattr(self.server, "restarting", False))
            and parsed.path not in {"/api/shutdown", "/api/restart"}
        ):
            json_response(self, {"error": "服务正在等待当前任务完成，请稍候"}, 409)
            return
        if RESTORE_IN_PROGRESS and parsed.path != "/api/backup/restore":
            json_response(self, {"error": "数据恢复正在进行，请稍候"}, 409)
            return
        try:
            if parsed.path == "/api/copy-exam":
                self.copy_exam()
                return
            if parsed.path == "/api/assignments":
                self.create_assignment()
                return
            if parsed.path in {"/api/answer-delete-preview", "/api/delete-answers", "/api/clear-grading-preview", "/api/clear-grading-data"}:
                self.manage_answer_data()
                return
            if parsed.path == "/api/delete-exam":
                self.delete_assignment(strict=True)
                return
            if parsed.path == "/api/import-exam-draft":
                self.import_exam_draft()
                return
            if parsed.path == "/api/exam-report":
                self.create_exam_report()
                return
            if parsed.path == "/api/exam-report/ai":
                self.create_exam_report(ai=True)
                return
            if parsed.path == "/api/exam-import-ai-preview":
                self.exam_import_ai_preview()
                return
            if parsed.path == "/api/exam-import-ai-parse":
                self.exam_import_ai_parse()
                return
            if parsed.path == "/api/create-imported-exam":
                self.create_imported_exam()
                return
            if parsed.path == "/api/exam-layout-profile":
                self.save_exam_layout_profile()
                return
            if parsed.path == "/api/student-auto-crop":
                self.run_student_auto_crop()
                return
            if parsed.path == "/api/student-auto-crop-review":
                self.save_student_auto_crop_review()
                return
            if parsed.path == "/api/student-auto-crop-answer-regions":
                self.create_student_auto_crop_answer_regions()
                return
            if parsed.path == "/api/import-docx":
                self.import_docx_questions()
                return
            if parsed.path == "/api/import-roster":
                self.import_roster()
                return
            if parsed.path == "/api/roster-student":
                self.save_roster_student()
                return
            if parsed.path == "/api/delete-assignment":
                self.delete_assignment()
                return
            if parsed.path == "/api/delete-submission-photos":
                self.delete_submission_photos()
                return
            if parsed.path == "/api/rubric-payload-preview":
                self.create_rubric_payload_preview()
                return
            if parsed.path == "/api/generate-rubric":
                self.generate_rubric()
                return
            if parsed.path == "/api/custom-models/save":
                self.save_custom_model_preset()
                return
            if parsed.path == "/api/custom-models/delete":
                self.delete_custom_model_preset()
                return
            if parsed.path == "/api/custom-models/list":
                self.fetch_custom_model_list()
                return
            if parsed.path == "/api/settings":
                self.save_settings()
                return
            if parsed.path == "/api/dual-review/configuration":
                self.save_dual_review_configuration()
                return
            if parsed.path == "/api/restart":
                self.restart_service()
                return
            if parsed.path == "/api/shutdown":
                self.shutdown_service()
                return
            if parsed.path == "/api/mobile-token":
                self.create_mobile_token()
                return
            if parsed.path == "/api/mobile-token/revoke":
                self.revoke_mobile_access()
                return
            if parsed.path == "/api/backup/create":
                self.create_backup()
                return
            if parsed.path == "/api/backup/restore":
                self.restore_backup()
                return
            if parsed.path == "/api/ai-payload-preview":
                if not UPLOAD_SEMAPHORE.acquire(timeout=60):
                    json_response(self, {"error": "上传和图片处理队列已满，请稍后重试"}, 503)
                    return
                try:
                    self.create_ai_payload_preview()
                finally:
                    release_image_memory()
                    UPLOAD_SEMAPHORE.release()
                return
            if parsed.path == "/api/question-remap-preview":
                if not UPLOAD_SEMAPHORE.acquire(timeout=60):
                    json_response(self, {"error": "上传和图片处理队列已满，请稍后重试"}, 503)
                    return
                try:
                    self.create_question_remap_preview()
                finally:
                    release_image_memory()
                    UPLOAD_SEMAPHORE.release()
                return
            if parsed.path == "/api/reprocess-question-mapping":
                if not UPLOAD_SEMAPHORE.acquire(timeout=60):
                    json_response(self, {"error": "上传和图片处理队列已满，请稍后重试"}, 503)
                    return
                try:
                    self.reprocess_question_mapping()
                finally:
                    release_image_memory()
                    UPLOAD_SEMAPHORE.release()
                return
            if parsed.path == "/api/confirm-blank-subquestion":
                self.confirm_blank_subquestion()
                return
            if parsed.path == "/api/privacy-templates":
                self.save_privacy_template()
                return
            if parsed.path == "/api/retention/preview":
                payload = self.read_json()
                days = payload.get("retention_days", configured_retention_days())
                try:
                    days = int(days)
                except (TypeError, ValueError) as exc:
                    raise ValueError("保留天数必须是整数") from exc
                if days < 0 or days > 3650:
                    raise ValueError("保留天数必须在0至3650天之间")
                json_response(self, retention_report(days))
                return
            if parsed.path == "/api/submissions":
                if not UPLOAD_SEMAPHORE.acquire(timeout=60):
                    json_response(self, {"error": "上传和图片处理队列已满，请稍后重试"}, 503)
                    return
                try:
                    self.create_submission()
                finally:
                    release_image_memory()
                    UPLOAD_SEMAPHORE.release()
                return
            if parsed.path == "/api/batches":
                self.create_batch()
                return
            if parsed.path == "/api/batches/upload-failed":
                self.mark_batch_upload_failed()
                return
            if parsed.path == "/api/delete-batch":
                self.delete_batch()
                return
            if parsed.path == "/api/batches/skip":
                self.skip_batch_sequence()
                return
            if parsed.path == "/api/batches/grading-control":
                self.control_batch_grading()
                return
            if parsed.path == "/api/confirm-total":
                self.confirm_total_submission()
                return
            if parsed.path == "/api/confirm":
                self.confirm_submission()
                return
            if parsed.path == '/api/batch-adoption':
                self.adopt_batch_consensus()
                return
            if parsed.path == "/api/review":
                self.review_submission()
                return
            if parsed.path == "/api/retry":
                self.retry_submission()
                return
            json_response(self, {"error": "Not found"}, 404)
        except ServiceDrainingError as exc:
            json_response(self, {"error": str(exc)}, 409)
        except ValueError as exc:
            json_response(self, {"error": str(exc)}, 400)
        except Exception as exc:
            print(f"[{utc_now()}] ERROR: request failed (type={type(exc).__name__})")
            json_response(self, {"error": "服务器内部错误，请查看电脑终端日志"}, 500)

    @track_active_task
    def save_exam_layout_profile(self) -> None:
        if not self.require_local_client():
            return
        payload = self.read_json()
        exam_id = str(payload.get("exam_id") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", exam_id):
            raise ValueError("正式考试编号无效")
        regions = payload.get("regions")
        if not isinstance(regions, list):
            raise ValueError("答题区列表格式无效")

        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            context = _exam_layout_profile_context(conn, exam_id)
            if context is None:
                raise ValueError("正式考试不存在")
            if not context["has_pdf_reference"]:
                raise ValueError("当前考试没有可用的打印 PDF 试卷页 reference，无法保存答题区")
            if context["pdf_hash_mismatch"]:
                raise ValueError("考试保存的 PDF 与当前 PDF 来源 hash 不一致，请检查 PDF 来源后再设置答题区")

            if not regions:
                conn.execute(
                    "UPDATE grading_exams SET layout_profile_id = NULL, layout_profile_json = '{}' WHERE id = ?",
                    (exam_id,),
                )
                append_audit_event(conn, "exam_layout_profile_cleared", "grading_exam", exam_id, {
                    "assignment_id": context["assignment_id"],
                    "source_pdf_sha256": context["source_pdf_sha256"],
                })
            else:
                previous = context.get("layout_profile") or {}
                profile_id = context.get("layout_profile_id") or uuid.uuid4().hex
                now = utc_now()
                profile = build_exam_layout_profile(
                    exam_id=exam_id,
                    assignment_id=context["assignment_id"],
                    source_pdf_sha256=context["source_pdf_sha256"],
                    references=context["references"],
                    required_question_ids=context["subjective_question_ids"],
                    objective_question_ids=context.get("objective_question_ids") or [],
                    regions=regions,
                    layout_profile_id=profile_id,
                    created_at=str(previous.get("created_at") or now),
                    updated_at=now,
                )
                progress = layout_profile_status(
                    context["subjective_question_ids"], profile,
                    context.get("objective_question_ids") or [],
                )
                conn.execute(
                    "UPDATE grading_exams SET layout_profile_id = ?, layout_profile_json = ? WHERE id = ?",
                    (profile_id, json.dumps(profile, ensure_ascii=False), exam_id),
                )
                append_audit_event(conn, "exam_layout_profile_saved", "grading_exam", exam_id, {
                    "assignment_id": context["assignment_id"],
                    "layout_profile_id": profile_id,
                    "source_pdf_sha256": context["source_pdf_sha256"],
                    "status": progress["status"],
                    "configured_count": progress["configured_count"],
                    "required_count": progress["required_count"],
                    "objective_status": progress["objective_status"],
                    "objective_configured_count": progress["objective_configured_count"],
                    "objective_required_count": progress["objective_required_count"],
                    "region_question_ids": [
                        region.get("question_id") or ",".join(region.get("question_mapping") or [])
                        for region in profile["regions"]
                    ],
                })
            conn.commit()
            saved_context = _exam_layout_profile_context(conn, exam_id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if saved_context is None:
            json_response(self, {"error": "保存后无法读取考试答题区"}, 500)
            return
        public_pages = [
            {key: value for key, value in page.items() if key != "image_file"}
            | {"preview_url": f"/api/exam-layout-reference?exam_id={exam_id}&page_number={page['page_number']}"}
            for page in saved_context["references"]
        ]
        json_response(self, {**saved_context, "references": public_pages})

    @track_active_task
    def run_student_auto_crop(self) -> None:
        if not self.require_local_client():
            return
        if not UPLOAD_SEMAPHORE.acquire(timeout=60):
            json_response(self, {"error": "上传和图片处理队列已满，请稍后重试"}, 503)
            return
        try:
            payload = self.read_student_auto_crop_upload()
            exam_id = str(payload.get("exam_id") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{32}", exam_id):
                raise ValueError("正式考试编号无效")
            conn = get_db()
            try:
                context = _exam_layout_profile_context(conn, exam_id)
                if context is not None and context.get("layout_profile") and not context.get("profile_hash_mismatch"):
                    _resolve_exam_reference_assets(conn, context)
            finally:
                conn.close()
            if context is None:
                json_response(self, {"error": "正式考试不存在"}, 404)
                return

            from student_auto_crop_service import run_student_auto_crop

            run_id = uuid.uuid4().hex
            artifact_root = _student_crop_artifact_root()

            def load_reference(page: dict[str, Any]) -> bytes | None:
                stored = str(page.get("resolved_image_path") or "")
                if not stored:
                    return None
                try:
                    path = Path(stored)
                    return path.read_bytes() if path.is_file() else None
                except OSError:
                    return None

            result = run_student_auto_crop(
                exam_context=context,
                student_image_bytes=payload["student_image_bytes"],
                input_mode=str(payload.get("input_mode") or ""),
                reference_loader=load_reference,
                output_dir=artifact_root / run_id,
                source_extension=str(payload.get("source_extension") or ".jpg"),
                run_id=run_id,
            )
            if result.get("alignment_run_id"):
                result["artifact_urls"] = {
                    file_name: f"/api/student-auto-crop-artifact?run_id={run_id}&file={file_name}"
                    for file_name in result.get("artifacts", [])
                }
                result["artifact_urls"]["AUTO_CROP_REPORT.md"] = f"/api/student-auto-crop-artifact?run_id={run_id}&file=AUTO_CROP_REPORT.md"
                result["artifact_urls"]["run.json"] = f"/api/student-auto-crop-artifact?run_id={run_id}&file=run.json"
            json_response(self, result)
        finally:
            release_image_memory()
            UPLOAD_SEMAPHORE.release()

    def save_student_auto_crop_review(self) -> None:
        if not self.require_local_client():
            return
        payload = self.read_json()
        run_id = str(payload.get("alignment_run_id") or "").strip().lower()
        verdict = str(payload.get("verdict") or "").strip()
        from student_auto_crop_service import record_manual_review

        artifact_root = _student_crop_artifact_root(run_id)
        result = record_manual_review(artifact_root, run_id, verdict)
        json_response(self, result)

    def create_student_auto_crop_answer_regions(self) -> None:
        if not self.require_local_client():
            return
        payload = self.read_json()
        run_id = str(payload.get("alignment_run_id") or "").strip().lower()
        answer_regions = _accepted_crop_answer_region_inputs(run_id)
        json_response(self, {
            "alignment_run_id": run_id,
            "image_role": "answer_region",
            "answer_regions": answer_regions,
            "model_request_sent": False,
        })

    def serve_file(self, path: Path, content_type: str, *, no_store: bool = False) -> None:
        if not path.exists() or not path.is_file():
            json_response(self, {"error": "Not found"}, 404)
            return
        body = path.read_bytes()
        headers = {"Cache-Control": "no-store, private"} if no_store else None
        write_http_response(self, body, content_type, extra_headers=headers)

    @track_active_task
    def import_docx_questions(self) -> None:
        """Parse a local DOCX into an editable draft; never create scoring rules."""
        if not self.require_local_client():
            return
        payload = self.read_json()
        filename = str(payload.get("filename") or "").strip().lower()
        if not filename.endswith(".docx"):
            raise ValueError("请选择 .docx 文件；旧式 .doc 不支持")
        encoded = str(payload.get("data_base64") or "")
        if not encoded or len(encoded) > 15 * 1024 * 1024:
            raise ValueError("Word 文件为空或超过10MiB")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("Word 文件编码无效") from exc
        imported = parse_docx_question_import(data)
        json_response(self, {"imported": imported, "draft_only": True})

    @track_active_task
    def import_exam_draft(self) -> None:
        if not self.require_local_client():
            return
        payload = self.read_json()
        docx_file = _decode_exam_upload(payload, "docx_filename", "docx_data_base64", ".docx", EXAM_IMPORT_DOCX_MAX_BYTES)
        pdf_file = _decode_exam_upload(payload, "pdf_filename", "pdf_data_base64", ".pdf", EXAM_IMPORT_PDF_MAX_BYTES)
        if docx_file is None and pdf_file is None:
            raise ValueError("请至少上传 Word / DOCX 或 PDF")
        docx_name, docx_data = docx_file if docx_file else (None, None)
        pdf_name, pdf_data = pdf_file if pdf_file else (None, None)
        draft_id = uuid.uuid4().hex
        draft_dir = DATA_DIR / "exam_import_drafts" / draft_id
        draft_dir.mkdir(parents=True, exist_ok=False)
        try:
            if docx_data is not None:
                (draft_dir / "content.docx").write_bytes(docx_data)
            if pdf_data is not None:
                pdf_path = draft_dir / "layout.pdf"
                pdf_path.write_bytes(pdf_data)
            else:
                pdf_path = None
            draft = build_exam_import_draft(
                docx_bytes=docx_data, docx_file_name=docx_name,
                pdf_bytes=pdf_data, pdf_file_name=pdf_name,
                project_dir=str(ROOT),
            )
            if draft.get("status") != "draft":
                error_code = draft.get("error_code") or "exam_import_failed"
                raise ValueError("PDF 没有可用文本层，无法安全生成草稿（pdf_text_unavailable）" if error_code == "pdf_text_unavailable" else f"未能生成考试草稿：{error_code}")
            if pdf_path is not None:
                _render_exam_import_references(pdf_path, draft_id, draft)
            staged_json = draft_dir / "exam_import_draft.json"
            staged_json.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            json_response(self, {"draft_id": draft_id, "draft": _exam_import_draft_response(draft, draft_id), "draft_only": True})
        except Exception:
            shutil.rmtree(draft_dir, ignore_errors=True)
            raise

    @track_active_task
    def exam_import_ai_preview(self) -> None:
        """Preview text-only AI parsing; page images are never sent."""
        if not self.require_local_client():
            return
        payload = self.read_json()
        draft_id = str(payload.get("draft_id", "")).strip().lower()
        draft_dir, draft, _draft_file = _exam_import_draft_load(draft_id)
        source = _exam_import_ai_source(draft_dir, draft)
        _check_exam_import_ai_privacy(source)
        model_config = rubric_generation_model_config(self)
        endpoint = model_chat_completions_endpoint(model_config)
        endpoint_host = urlparse(endpoint).hostname or "未配置"
        json_response(self, {
            "ok": True,
            "draft_id": draft_id,
            "source_label": source["source_label"],
            "text_chars": source["text_chars"],
            "docx_chars": source["docx_chars"],
            "pdf_chars": source["pdf_chars"],
            "pdf_page_count": source["pdf_page_count"],
            "provider": model_config.get("provider_label", model_config.get("provider", "")),
            "model": model_config["model"],
            "model_role": model_config.get("model_role", "模型"),
            "destination_host": endpoint_host,
            "preview_sha256": _exam_import_ai_fingerprint(draft_id, source, model_config),
            "prompt_version": AI_PARSE_PROMPT_VERSION,
            "note": "仅发送 Word/PDF 提取文字，不发送页面图片；已执行身份风险检查，请仍确认原文不含个人信息。",
        })

    @track_active_task
    def exam_import_ai_parse(self) -> None:
        if not self.require_local_client():
            return
        payload = self.read_json()
        draft_id = str(payload.get("draft_id", "")).strip().lower()
        if payload.get("confirm_send_text") is not True:
            raise ValueError("请先确认发送 Word/PDF 提取的文字")
        draft_dir, draft, draft_file = _exam_import_draft_load(draft_id)
        source = _exam_import_ai_source(draft_dir, draft)
        _check_exam_import_ai_privacy(source)
        model_config = rubric_generation_model_config(self)
        expected_fingerprint = _exam_import_ai_fingerprint(draft_id, source, model_config)
        if str(payload.get("preview_sha256") or "") != expected_fingerprint:
            raise ValueError("试卷文字或模型配置已变化，请重新预览后再解析")
        raw_result = call_exam_import_ai_model(source["text"], model_config)
        parsed = normalize_exam_import_ai_result(raw_result, source_text=source["text"])
        merged, summary = merge_exam_import_ai_result(draft, parsed)
        atomic_write_text(draft_file, json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
        conn = get_db()
        try:
            append_audit_event(conn, "exam_import_ai_parse", "exam_import_draft", draft_id, {
                "provider": model_config.get("provider", ""),
                "model": model_config["model"],
                "text_chars": source["text_chars"],
                "docx_chars": source["docx_chars"],
                "pdf_page_count": source["pdf_page_count"],
                "objective_ai_count": summary["objective"]["ai_count"],
                "subjective_ai_count": summary["subjective"]["ai_count"],
                "added_objective_count": len(summary["objective"]["added"]),
                "added_subjective_count": len(summary["subjective"]["added"]),
                "answer_conflict_count": len(summary["objective"]["answer_conflicts"]) + len(summary["subjective"]["answer_conflicts"]),
                "prompt_version": AI_PARSE_PROMPT_VERSION,
            })
            conn.commit()
        finally:
            conn.close()
        json_response(self, {
            "ok": True,
            "draft": _exam_import_draft_response(merged, draft_id),
            "summary": summary,
            "source_label": source["source_label"],
            "text_chars": source["text_chars"],
        })

    @track_active_task
    def create_imported_exam(self) -> None:
        if not self.require_local_client():
            return
        payload = self.read_json()
        draft_id = str(payload.get("draft_id", "")).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", draft_id):
            raise ValueError("考试草稿编号无效，请重新解析文件")
        draft_dir = DATA_DIR / "exam_import_drafts" / draft_id
        draft_file = draft_dir / "exam_import_draft.json"
        if not draft_file.is_file() or draft_dir.resolve().parent != (DATA_DIR / "exam_import_drafts").resolve():
            raise ValueError("考试草稿已失效，请重新上传并解析")
        try:
            original_draft = json.loads(draft_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("无法读取考试草稿，请重新解析文件") from exc
        confirmed_draft, official_sub_questions, point_warnings = _confirmed_import_payload(
            original_draft, payload.get("confirmation", {})
        )
        assignment_id = uuid.uuid4().hex[:12]
        exam_id = uuid.uuid4().hex
        assignment_dir = DATA_DIR / assignment_id
        created_at = utc_now()
        warnings = list(point_warnings)
        official_asset_dir: Path | None = None
        try:
            assignment_dir.mkdir(parents=True, exist_ok=False)
            imports_dir = assignment_dir / "imports"
            imports_dir.mkdir()
            if (draft_dir / "content.docx").is_file():
                shutil.copy2(draft_dir / "content.docx", imports_dir / "content.docx")
            content_source = confirmed_draft.get("source", {}).get("content_source")
            layout_source = confirmed_draft.get("source", {}).get("layout_source")
            answer_source = confirmed_draft.get("source", {}).get("answer_source")
            if isinstance(content_source, dict) and (imports_dir / "content.docx").is_file():
                content_source["stored_path"] = f"data/{assignment_id}/imports/content.docx"
            pdf_path = draft_dir / "layout.pdf"
            pdf_stored_path = ""
            if isinstance(layout_source, dict):
                if not pdf_path.is_file():
                    raise ValueError("正式考试 PDF source 丢失，未创建考试")
                asset_result = persist_exam_pdf_assets(
                    portable_root=PORTABLE_ROOT,
                    exam_id=exam_id,
                    source_pdf_path=pdf_path,
                    layout_source=layout_source,
                    layout_reference=confirmed_draft.get("layout_reference", {}),
                    reference_path_resolver=resolve_data_relative_path,
                )
                official_asset_dir = asset_result["asset_dir"]
                confirmed_draft["source"]["layout_source"] = asset_result["layout_source"]
                confirmed_draft.setdefault("layout_reference", {})["pages"] = asset_result["references"]
                layout_source = confirmed_draft["source"]["layout_source"]
                pdf_stored_path = str(layout_source["stored_path"])
            elif pdf_path.is_file():
                raise ValueError("PDF 存在但导入草稿缺少版式来源 metadata，未创建考试")

            if isinstance(answer_source, dict):
                if answer_source.get("type") == "docx" and (imports_dir / "content.docx").is_file():
                    answer_source["stored_path"] = f"data/{assignment_id}/imports/content.docx"
                elif answer_source.get("type") in {"pdf", "pdf_semantic_fallback"} and pdf_stored_path:
                    answer_source["stored_path"] = pdf_stored_path
                elif answer_source.get("type") == "mixed":
                    for source in answer_source.get("sources", []):
                        if source.get("type") == "docx" and (imports_dir / "content.docx").is_file():
                            source["stored_path"] = f"data/{assignment_id}/imports/content.docx"
                        elif source.get("type") in {"pdf", "pdf_semantic_fallback"} and pdf_stored_path:
                            source["stored_path"] = pdf_stored_path

            question_source = confirmed_draft.get("source", {}).get("question_source")
            if isinstance(question_source, dict) and question_source.get("type") in {"pdf", "pdf_semantic_fallback"} and pdf_stored_path:
                question_source["stored_path"] = pdf_stored_path
            if isinstance(content_source, dict) and content_source.get("type") in {"pdf", "pdf_semantic_fallback"} and pdf_stored_path:
                content_source["stored_path"] = pdf_stored_path

            confirmed_draft["created_from_import"] = True
            confirmed_draft["import_draft_id"] = draft_id
            confirmed_draft["created_at"] = created_at
            confirmed_draft["teacher_confirmed_at"] = created_at
            confirmed_draft["import_confirmation_warnings"] = warnings

            objectives = confirmed_draft.get("objective_questions", [])
            subjective_groups = confirmed_draft.get("material_questions", [])
            question_parts = []
            for major in subjective_groups:
                major_id = str(major.get("label", "")).strip()
                if major.get("prompt"):
                    question_parts.append(f"【第{major_id}题材料】\n{major['prompt']}")
                for sub in major.get("sub_questions", []):
                    question_parts.append(f"【{sub.get('question_id')}】\n{sub.get('question', '')}")
            if objectives:
                question_parts.insert(0, f"客观题共 {len(objectives)} 题；完整题目、选项和答案键保存在导入考试配置中。")
            assignment_question = "\n\n".join(part for part in question_parts if part.strip()) or "已导入考试题目"
            answer_parts = [f"{qid} {answer}" for qid, answer in confirmed_draft.get("objective_answer_key", {}).items()]
            answer_parts.extend(
                f"{sub.get('question_id')}：{sub.get('reference_answer', '')}"
                for major in subjective_groups for sub in major.get("sub_questions", [])
            )
            standard_answer = "\n".join(answer_parts)
            criteria = flatten_criteria(official_sub_questions) if official_sub_questions else []
            total_score = total_sub_questions(official_sub_questions) if official_sub_questions else 0.0
            objective_total = sum(confirmed_draft["objective_points"].values())
            confirmed_draft["score_totals"] = {"objective": objective_total, "subjective": total_score, "paper": objective_total + total_score}
            metadata = json.dumps(confirmed_draft, ensure_ascii=False)
            (assignment_dir / "confirmed_import_draft.json").write_text(json.dumps(confirmed_draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            conn = get_db()
            try:
                conn.execute(
                    "INSERT INTO assignments (id, name, question, standard_answer, criteria_json, sub_questions_json, created_at, import_metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (assignment_id, confirmed_draft["title"], assignment_question, standard_answer,
                     json.dumps(criteria, ensure_ascii=False), json.dumps(official_sub_questions, ensure_ascii=False), created_at, metadata),
                )
                conn.execute(
                    "INSERT INTO grading_exams (id, assignment_id, created_at) VALUES (?, ?, ?)",
                    (exam_id, assignment_id, created_at),
                )
                append_audit_event(conn, "exam_created_from_import", "grading_exam", exam_id, {
                    "assignment_id": assignment_id, "import_draft_id": draft_id,
                    "source_pair_status": confirmed_draft.get("source", {}).get("source_pair_status"),
                    "content_sha256": (confirmed_draft.get("source", {}).get("content_source") or {}).get("sha256"),
                    "layout_sha256": (confirmed_draft.get("source", {}).get("layout_source") or {}).get("source_pdf_sha256"),
                })
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            json_response(self, {
                "exam_id": exam_id, "assignment": {
                    "id": assignment_id, "name": confirmed_draft["title"],
                    "max_score": total_score, "score_totals": confirmed_draft["score_totals"], "objective_count": len(objectives),
                    "subjective_count": len(official_sub_questions),
                    "created_from_import": True,
                },
                "provenance": {
                    "content_source": confirmed_draft.get("source", {}).get("content_source"),
                    "layout_source": confirmed_draft.get("source", {}).get("layout_source"),
                    "answer_source": confirmed_draft.get("source", {}).get("answer_source"),
                    "created_from_import": True, "import_draft_id": draft_id,
                },
                "warnings": warnings,
            }, 201)
        except Exception:
            shutil.rmtree(assignment_dir, ignore_errors=True)
            if official_asset_dir is not None:
                shutil.rmtree(official_asset_dir, ignore_errors=True)
            raise

    @track_active_task
    def copy_exam(self) -> None:
        if not self.require_local_client():
            return
        from exam_copy import duplicate
        body = self.read_json()
        result = duplicate(sys.modules[__name__], body.get('assignment_id'), body.get('name'))
        json_response(self, result, 201)

    @track_active_task
    def create_assignment(self) -> None:
        if not self.require_local_client():
            return
        payload = self.read_json()
        name = str(payload.get("name", "")).strip()
        question = str(payload.get("question", "")).strip()
        standard_answer = str(payload.get("standard_answer", "")).strip()
        raw_sub_questions = payload.get("sub_questions")
        if not name or not question:
            raise ValueError("题目名称和大题材料/题干不能为空")
        if raw_sub_questions is None and not standard_answer:
            raise ValueError("参考答案不能为空")
        sub_questions = clean_sub_questions(
            raw_sub_questions,
            fallback_question=question,
            fallback_answer=standard_answer,
            fallback_criteria=payload.get("criteria"),
        )
        if not standard_answer:
            standard_answer = "\n".join(
                f"{item['label']} {item['standard_answer']}" for item in sub_questions if item["standard_answer"]
            )
        criteria = flatten_criteria(sub_questions)
        assignment_id = uuid.uuid4().hex[:12]
        conn = get_db()
        conn.execute(
            "INSERT INTO assignments (id, name, question, standard_answer, criteria_json, sub_questions_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                assignment_id,
                name,
                question,
                standard_answer,
                json.dumps(criteria, ensure_ascii=False),
                json.dumps(sub_questions, ensure_ascii=False),
                utc_now(),
            ),
        )
        warnings = scoring_warnings(sub_questions)
        append_audit_event(
            conn,
            "assignment_created",
            "assignment",
            assignment_id,
            {"criteria_count": len(criteria), "scoring_warning_count": len(warnings)},
        )
        conn.commit()
        conn.close()
        json_response(
            self,
            {
                "assignment": {
                    "id": assignment_id,
                    "name": name,
                    "question": question,
                    "standard_answer": standard_answer,
                    "criteria": criteria,
                    "sub_questions": sub_questions,
                    "max_score": total_sub_questions(sub_questions),
                    "scoring_warnings": warnings,
                }
            },
            201,
        )

    @track_active_task
    def import_roster(self) -> None:
        if not self.require_identity_mapping_access():
            return
        payload = self.read_json()
        filename = str(payload.get("filename", "")).strip()
        data_url = str(payload.get("data_url", "")).strip()
        default_class_name = str(payload.get("default_class_name", "")).strip()
        records = parse_roster_upload(filename, data_url, default_class_name)
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            preview = roster_identity.import_preview(conn, records)
            classes = sorted({item["class_name"] for item in records})
            if payload.get("preview_only"):
                json_response(self, {"preview": {k: v for k, v in preview.items() if k != "records"}, "classes": classes})
                return
            for item in preview["records"]:
                roster_identity.save_student(conn, {"class_name": item["class_name"], "student_no": item["student_no"], "display_name": item["student_name"]}, utc_now())
            append_audit_event(conn, "roster_imported", "roster", uuid.uuid4().hex[:12],
                               {k: v for k, v in preview.items() if k != "records"})
            conn.commit()
            json_response(self, {"ok": True, "classes": classes, "student_count": preview["new_count"],
                "preview": {k: v for k, v in preview.items() if k != "records"},
                "message": f"新增 {preview['new_count']} 名学生；已有 {preview['existing_count']}，重复码 {preview['duplicate_count']}，缺失或无效 {preview['missing_count']}，姓名冲突 {preview['conflict_count']}。已有学生ID和历史绑定保留。"})
        finally:
            conn.close()

    @track_active_task
    def save_roster_student(self) -> None:
        if not self.require_identity_mapping_access():
            return
        payload = self.read_json()
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            student_id = roster_identity.save_student(conn, payload, utc_now())
            append_audit_event(conn, "roster_student_saved", "roster_student", student_id, {})
            conn.commit()
            json_response(self, {"student_id": student_id, "ok": True})
        finally:
            conn.close()

    def manage_answer_data(self, *, listing=False) -> None:
        if not self.require_local_client():return
        import grading_data_deletion as deletion
        from objective import app as objective_engine
        if listing:
            query=parse_qs(urlparse(self.path).query,keep_blank_values=True)
            with closing(get_db()) as conn, objective_engine._JOB_LOCK:
                live={k for k,w in objective_engine._JOB_BY_ID.items() if w and w.is_alive()}
                result=deletion.groups(conn,query.get('assignment_id',[''])[0],query.get('exam_id',[''])[0],live)
            json_response(self,{'groups':result});return
        body=self.read_json()
        path=urlparse(self.path).path
        body['clear']=path in {'/api/clear-grading-preview','/api/clear-grading-data'}
        with ACTIVE_TASKS_CONDITION, objective_engine._JOB_LOCK:
            live={k for k,w in objective_engine._JOB_BY_ID.items() if w and w.is_alive()}
            if path.endswith('-preview'):
                with closing(get_db()) as conn:result=deletion.plan(conn,body,live)['preview']
            else:
                if ACTIVE_TASKS:raise ValueError('有上传或评分正在执行，请等待当前操作结束后删除')
                objective_app=getattr(self.server,'objective_wsgi_app',None)
                result=deletion.remove(sys.modules[__name__],body,active_workers=live,
                    upload_root=objective_app.config['OBJECTIVE_UPLOAD_DIR'] if objective_app else None,
                    actor=getattr(getattr(self,'_web_auth_session',None),'username','') or 'local')
        json_response(self,result)

    def batch_delete_preview(self) -> None:
        if not self.require_local_client():return
        from batch_deletion import plan
        query=parse_qs(urlparse(self.path).query,keep_blank_values=True)
        with closing(get_db()) as conn:
            preview,_,_=plan(conn,query.get('batch_id',[''])[0],query.get('assignment_id',[''])[0],query.get('exam_id',[''])[0])
        json_response(self,preview)

    @track_active_task
    def delete_batch(self) -> None:
        if not self.require_local_client():return
        from batch_deletion import delete
        body=self.read_json()
        with ACTIVE_TASKS_CONDITION:
            if ACTIVE_TASKS>1:raise ValueError('有上传或评分操作正在进行，请结束后再删除批次')
            result=delete(get_db,DATA_DIR,body,audit=append_audit_event,
                          actor=getattr(getattr(self,'_web_auth_session',None),'username','') or 'local',
                          resolve_path=resolve_data_relative_path)
        json_response(self,result)

    def assignment_delete_preview(self) -> None:
        if not self.require_local_client():
            return
        from exam_deletion import deletion_plan
        aid = parse_qs(urlparse(self.path).query).get("assignment_id", [""])[0]
        with closing(get_db()) as conn:
            plan = deletion_plan(conn, aid)
        json_response(self, plan["preview"])

    @track_active_task
    def delete_assignment(self, *, strict: bool = False) -> None:
        if not self.require_local_client():
            return
        from exam_deletion import delete_exam
        payload = self.read_json()
        # Refuse to race another in-flight main-workspace operation. SQLite
        # locks and persistent objective states cover the separate controller.
        with ACTIVE_TASKS_CONDITION:
            if ACTIVE_TASKS > 1:
                raise ValueError("有操作正在进行，请结束后再删除考试")
            result = delete_exam(
                get_db, DATA_DIR, payload, strict=strict,
                audit=append_audit_event,
                actor=getattr(getattr(self, "_web_auth_session", None), "username", "") or "local",
            )
        json_response(self, result)

    @track_active_task
    def delete_submission_photos(self) -> None:
        """Delete one student's generated photo cache while retaining the record."""
        mobile_client = not self.is_local_client()
        payload = self.read_json()
        submission_id = str(payload.get("submission_id", "")).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{12}", submission_id):
            raise ValueError("批改记录编号无效")
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if not row:
                raise ValueError("批改记录不存在")
            if ("expected_assignment_id" in payload and payload["expected_assignment_id"] != row["assignment_id"]) or (
                "expected_exam_id" in payload and payload["expected_exam_id"] != (row["exam_id"] or "")
            ):
                raise ValueError("答卷不属于当前考试，已拒绝清理")
            if mobile_client and row["source_channel"] != "mobile":
                raise ValueError("手机只能清理通过手机提交的本份照片缓存")
            if row["state"] in {"queued", "grading", "recovery_pending"}:
                raise ValueError("该任务尚未完成，暂不能清理照片；请等待任务结束")
            unconfirmed = not row["confirmed_at"] or submission_list_public(row).get("teacher_reconfirmation_required")
            if unconfirmed and payload.get("discard_unconfirmed") is not True:
                raise ValueError("请明确确认弃用未终评答卷照片后再清理")
            photo_meta = json.loads(row["photos_json"] or "[]")
            if not isinstance(photo_meta, list):
                raise ValueError("批改记录中的照片清单无效，已拒绝清理")
            if not photo_meta:
                conn.commit()
                json_response(
                    self,
                    {
                        "ok": True,
                        "submission": submission_public(row),
                        "message": "本份学生记录没有可清理的照片缓存",
                    },
                )
                return
            student_dir = submission_photo_directory(row["assignment_id"], submission_id, photo_meta)
            removed_bytes = 0
            removed_files = 0
            if student_dir is not None and student_dir.exists():
                if not student_dir.is_dir():
                    raise ValueError("学生照片缓存目录无效，已拒绝清理")
                for item in student_dir.rglob("*"):
                    if item.is_file():
                        try:
                            removed_bytes += item.stat().st_size
                        except OSError:
                            pass
                        removed_files += 1
                # This directory is created by create_submission and contains
                # only the submitted, processed, and derived evidence images.
                shutil.rmtree(student_dir)

            try:
                result = json.loads(row["result_json"] or "{}")
            except json.JSONDecodeError:
                result = {}
            if not isinstance(result, dict):
                result = {}
            for sub_question in result.get("sub_questions", []):
                if not isinstance(sub_question, dict):
                    continue
                for criterion in sub_question.get("criteria", []):
                    if isinstance(criterion, dict):
                        # Keep the textual evidence and score, but remove the
                        # now-unavailable image coordinates and file references.
                        criterion["evidence"] = None
            deleted_at = utc_now()
            result["photos_deleted"] = True
            result["photos_deleted_at"] = deleted_at
            if unconfirmed:
                result["photos_discarded_unconfirmed"] = True
            conn.execute(
                "UPDATE submissions SET photos_json = '[]', evidence_json = '[]', result_json = ? WHERE id = ?",
                (json.dumps(result, ensure_ascii=False), submission_id),
            )
            append_audit_event(
                conn,
                "submission_photos_deleted",
                "submission",
                submission_id,
                {"removed_files": removed_files, "removed_bytes": removed_bytes, "discarded_unconfirmed": bool(unconfirmed)},
            )
            conn.commit()
            clear_submission_photo_viewed(submission_id)
            updated = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            json_response(
                self,
                {
                    "ok": True,
                    "submission": submission_public(updated),
                    "message": "已清理本份学生的照片缓存；题目、成绩、评语和学生记录均已保留",
                },
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @track_active_task
    def create_mobile_token(self) -> None:
        if not self.require_local_client():
            return
        token, expires_at = issue_mobile_token()
        json_response(
            self,
            {
                "ok": True,
                "token": token,
                "expires_at": expires_at,
                "expires_in_seconds": MOBILE_TOKEN_TTL_SECONDS,
                "message": "访问码仅保存在当前服务进程内，45分钟后或重启服务后失效",
            },
        )

    @track_active_task
    def revoke_mobile_access(self) -> None:
        if not self.require_local_client():
            return
        revoke_mobile_token()
        json_response(self, {"ok": True, "message": "手机临时访问码已撤销"})

    @track_active_task
    def create_backup(self) -> None:
        if not self.require_local_client():
            return
        with RESTORE_LOCK:
            if RESTORE_IN_PROGRESS:
                raise ValueError("数据恢复正在进行，请稍候")
            archive_path = create_backup_archive()
        json_response(
            self,
            {
                "ok": True,
                "filename": archive_path.name,
                "message": "备份完成；备份不包含 API Key、DPAPI 文件或服务控制文件",
                "storage": storage_snapshot(),
            },
            201,
        )

    @track_active_task
    def restore_backup(self) -> None:
        if not self.require_local_client():
            return
        payload = self.read_json()
        filename = str(payload.get("filename", "")).strip()
        archive_path, safety_backup = restore_backup_archive(filename)
        json_response(
            self,
            {
                "ok": True,
                "filename": archive_path.name,
                "safety_backup": safety_backup.name,
                "message": "恢复完成；当前数据的恢复前安全备份已生成，请重启服务使数据状态完全刷新",
                "storage": storage_snapshot(),
            },
        )

    @track_active_task
    def create_rubric_payload_preview(self) -> None:
        if not self.require_local_client():
            return
        question, drafts = normalize_rubric_generation_request(self.read_json())
        model_config = rubric_generation_model_config(self)
        request_payload = build_rubric_model_payload(
            question, drafts, model_config["model"], model_config["provider"]
        )
        prompt = str(request_payload["messages"][1]["content"])
        conn = get_db()
        try:
            if _contains_roster_identity(conn, prompt) or _contains_labeled_identity_data(prompt):
                json_response(self, {
                    "blocked": True,
                    "error": "评分细则草稿包含花名册身份字段，已阻止预览和模型发送。请移除身份信息后重试。",
                }, 409)
                return
        finally:
            conn.close()
        endpoint = model_chat_completions_endpoint(model_config)
        endpoint_host = urlparse(endpoint).hostname or "未配置"
        endpoint_sha256 = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
        provider = model_config["provider"]
        payload_sha256 = hashlib.sha256(
            json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        claims = {
            "kind": "rubric",
            "payload_sha256": payload_sha256,
            "destination_sha256": endpoint_sha256,
            "provider": provider,
            "model": model_config["model"],
            "session_sha256": _preview_session_fingerprint(self),
            "expires_at": int(time.time()) + 10 * 60,
        }
        fingerprint = _ai_preview_fingerprint(claims)
        json_response(self, {
            "blocked": False,
            "provider": model_config.get("provider_label", provider),
            "model": request_payload.get("model", "未配置"),
            "model_role": model_config.get("model_role", "模型"),
            "destination_host": endpoint_host,
            "payload": request_payload,
            "payload_sha256": payload_sha256,
            "preview_fingerprint": fingerprint,
            "preview_token": _sign_ai_preview(claims),
            "expires_at": claims["expires_at"],
        })

    @track_active_task
    def generate_rubric(self) -> None:
        if not self.require_local_client():
            return
        payload = self.read_json()
        question, drafts = normalize_rubric_generation_request(payload)
        model_config = rubric_generation_model_config(self)
        request_payload = build_rubric_model_payload(
            question, drafts, model_config["model"], model_config["provider"]
        )
        endpoint = model_chat_completions_endpoint(model_config)
        endpoint_sha256 = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
        provider = model_config["provider"]
        payload_sha256 = hashlib.sha256(
            json.dumps(request_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        claims = _read_preview_claims(str(payload.get("rubric_preview_token", "")))
        session_fingerprint = _preview_session_fingerprint(self)
        if (
            claims.get("kind") != "rubric"
            or claims.get("payload_sha256") != payload_sha256
            or claims.get("destination_sha256") != endpoint_sha256
            or claims.get("provider") != provider
            or claims.get("model") != model_config["model"]
            or claims.get("session_sha256") != session_fingerprint
        ):
            raise ValueError("评分细则 Payload 与已确认预览不一致，请重新预览")
        conn = get_db()
        try:
            if _contains_roster_identity(conn, str(request_payload["messages"][1]["content"])) or _contains_labeled_identity_data(
                str(request_payload["messages"][1]["content"])
            ):
                raise ValueError("评分细则草稿包含花名册身份字段，已阻止模型发送")
        finally:
            conn.close()
        token_fingerprint = hashlib.sha256(str(payload.get("rubric_preview_token", "")).split(".", 1)[0].encode("ascii")).hexdigest()
        _consume_preview_token(token_fingerprint, int(claims["expires_at"]))
        json_response(self, call_rubric_model(question, drafts, request_payload, endpoint, model_config))

    def export_diagnostics(self) -> None:
        if not self.require_local_client():
            return
        conn = get_db()
        try:
            schema = conn.execute("SELECT schema_version FROM schema_meta WHERE id=1").fetchone()[0]
            objective_schema = conn.execute("PRAGMA user_version").fetchone()[0]
            counts = {"assignments": conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0],
                      "audit_events": conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0],
                      "states": dict(conn.execute("SELECT state, COUNT(*) FROM submissions GROUP BY state").fetchall())}
        finally:
            conn.close()
        body = support_bundle.build_bundle(version=APP_VERSION, commit=GIT_COMMIT, schema=schema,
            objective_schema=objective_schema, counts=counts, audit=audit_chain_snapshot(limit=8),
            python=sys.version.split()[0], platform=sys.platform)
        write_http_response(self, body, "application/zip", extra_headers={
            "Content-Disposition": 'attachment; filename="history-grader-diagnostics.zip"', "Cache-Control": "no-store"})

    @track_active_task
    def fetch_custom_model_list(self) -> None:
        if not self.require_local_client():
            return
        if cloud_mode_enabled(self) or web_auth_required():
            json_response(self, {"error": "模型目录查询只允许在桌面本机模式执行"}, 403)
            return
        payload = self.read_json()
        if payload.get("confirm_send") is not True:
            raise ValueError("请先确认向所填接口查询模型列表")
        base, _ = validate_model_base_url(str(payload.get("base_url") or ""))
        parsed = urlparse(base)
        if parsed.query or parsed.fragment:
            raise ValueError("模型接口地址不能包含查询参数或片段")
        key = str(payload.get("api_key") or "").strip()
        if len(key) > 500 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError("API Key 格式无效")
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        # Redirect rejection and explicit supplied Key; never reuse another endpoint's Key.
        catalog_base = base.removesuffix('/chat/completions')
        request = Request(catalog_base.rstrip('/') + '/models', headers=headers, method='GET')
        try:
            with open_model_request(request, timeout=15) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError()
            data = json.loads(raw.decode('utf-8-sig'))
            entries = data.get('data') if isinstance(data, dict) else None
            if not isinstance(entries, list) or len(entries) > 10000:
                raise ValueError()
            models = []
            for item in entries:
                value = item.get('id') if isinstance(item, dict) else None
                if isinstance(value, str) and 0 < len(value) <= 200 and not any(ord(c) < 32 or ord(c) == 127 for c in value):
                    if value not in models:
                        models.append(value)
                if len(models) >= 1000:
                    break
        except Exception:
            raise ValueError("无法读取模型列表，请检查接口地址与Key；不会自动重试，也未调用评分模型") from None
        json_response(self, {"models": models, "model_calls": 0})

    @track_active_task
    def save_custom_model_preset(self) -> None:
        if not self.require_local_client():
            return
        payload = self.read_json()
        if str(payload.get("api_key", "")).strip():
            raise ValueError("自定义预设不能保存 API Key；Key 只能在模型设置中保存到本机")
        preset_id = str(payload.get("preset_id", "")).strip().lower()
        existing = read_custom_model_presets()
        if preset_id:
            if preset_id not in {item["id"] for item in existing}:
                raise ValueError("要修改的自定义模型预设不存在")
            preset = normalize_custom_model_preset(payload, preset_id)
            updated = [preset if item["id"] == preset_id else item for item in existing]
            status = 200
            event_type = "custom_model_preset_updated"
        else:
            preset = normalize_custom_model_preset(payload)
            updated = [*existing, preset]
            status = 201
            event_type = "custom_model_preset_created"
        write_custom_model_presets(updated)
        _, api_origin = validate_model_base_url(preset["base_url"])
        conn = get_db()
        try:
            append_audit_event(
                conn,
                event_type,
                "custom_model_preset",
                preset["id"],
                {"label": preset["label"], "api_origin": api_origin, "model": preset["model"]},
            )
            conn.commit()
        finally:
            conn.close()
        json_response(
            self,
            {
                "ok": True,
                "preset": preset,
                "presets": read_custom_model_presets(),
                "model_presets": public_model_presets(),
                "message": "自定义模型预设已保存；如需启用，请选择它后再点击“保存模型配置”。",
            },
            status,
        )

    @track_active_task
    def delete_custom_model_preset(self) -> None:
        if not self.require_local_client():
            return
        payload = self.read_json()
        preset_id = str(payload.get("preset_id", "")).strip().lower()
        existing = read_custom_model_presets()
        if preset_id not in {item["id"] for item in existing}:
            raise ValueError("要删除的自定义模型预设不存在")
        if preset_id in read_dual_review_preset_ids().values():
            raise ValueError("此预设正用于双评，请先更换 AI-A/AI-B 的模型再删除")
        updated = [item for item in existing if item["id"] != preset_id]
        write_custom_model_presets(updated)
        conn = get_db()
        try:
            append_audit_event(conn, "custom_model_preset_deleted", "custom_model_preset", preset_id, {})
            conn.commit()
        finally:
            conn.close()
        json_response(
            self,
            {
                "ok": True,
                "presets": read_custom_model_presets(),
                "model_presets": public_model_presets(),
                "message": "自定义模型预设已删除；不会删除本机 Key，也不会删除题目或批改数据。",
            },
        )

    @track_active_task
    def save_dual_review_configuration(self) -> None:
        global KEY_STORE_ERROR
        if not self.require_local_client():
            return
        cloud = cloud_mode_enabled(self)
        if cloud:
            session = getattr(self, "_web_auth_session", None)
            if session is None or getattr(session, "role", "") != "admin":
                json_response(self, {"error": "此操作仅限管理员"}, 403)
                return
        payload = self.read_json()
        side = str(payload.get("side", "")).strip().upper()
        if side not in DUAL_REVIEW_MODEL_SPECS:
            raise ValueError("双评模型侧别无效，请选择 AI-A 或 AI-B")
        if set(payload) - {"side", "preset_id", "api_key"}:
            raise ValueError("双评配置请求字段无效")
        preset_id = str(payload.get("preset_id", "")).strip()
        if not preset_id:
            raise ValueError("请选择此侧的视觉模型")
        api_key = str(payload.get("api_key", "")).strip()
        if len(api_key) > 500:
            raise ValueError("API Key 长度异常")
        if cloud:
            if any(ord(c) < 33 or ord(c) > 126 for c in api_key):
                raise ValueError("API Key 不能包含空白、换行或非ASCII字符")
            with server_dual_config.LOCK:
                selected = read_dual_review_preset_ids()
                candidate = {**selected, side: preset_id}
                if not dual_review_preset_allowed(preset_id):
                    raise ValueError("双评模型预设无效，请刷新页面后重试")
                spec = dual_review_model_specs(candidate)[side]
                document = server_dual_config.read(CONFIG_DIR) or {"credentials": {}}
                if not api_key:
                    # The target provider/origin must already have a usable credential.
                    old_key = server_dual_config.key_for(document, side, spec)
                    for name in dual_review_provider_env_names(spec["provider"]):
                        old_key = old_key or os.getenv(name, "").strip()
                    if not old_key and os.getenv("HISTORY_GRADER_PROVIDER", "").strip().casefold() == spec["provider"]:
                        try:
                            _, origin = validate_model_base_url(os.getenv("HISTORY_GRADER_BASE_URL", ""))
                            if origin == spec["api_origin"]:
                                old_key = os.getenv("HISTORY_GRADER_API_KEY", "").strip()
                        except ValueError:
                            pass
                    if not old_key:
                        raise ValueError("所选模型尚无可沿用的服务器Key，请输入此服务商的Key。")
                else:
                    document["credentials"].setdefault(side, {})[spec["provider"]] = {"api_origin": spec["api_origin"], "api_key": api_key}
                document.update(models=candidate, updated_at=utc_now())
                server_dual_config.write(CONFIG_DIR, document)
            conn = get_db()
            try:
                append_audit_event(conn, "dual_review_model_configured", "configuration", "server", {"side": side, "preset_id": preset_id, "provider": spec["provider"], "model": spec["model"], "key_updated": bool(api_key)})
                conn.commit()
            finally:
                conn.close()
            json_response(self, {"ok": True, "side": side, "preset_id": preset_id, "configured": dual_review_key_is_configured(side), "message": "服务器双评配置已保存；Key不会回显，未调用模型。"})
            return
        if api_key:
            selected_catalog = model_preset_catalog()
            selected_model = selected_catalog.get(preset_id)
            if not dual_review_preset_allowed(preset_id) or not selected_model:
                raise ValueError("双评模型预设无效，请刷新页面后重试")
            _, origin = validate_model_base_url(str(selected_model["base_url"]))
            scoped_provider = dual_review_key_store_provider(side, str(selected_model["provider"]))
            try:
                save_dpapi_api_key(api_key, scoped_provider, api_origin=origin)
            except (ApiKeyStoreError, ValueError) as exc:
                KEY_STORE_ERROR = str(exc)
                raise ValueError(str(exc)) from exc
            SESSION_ONLY_KEY_PROVIDERS.discard(scoped_provider)
        KEY_STORE_ERROR = ""
        selected = write_dual_review_preset_ids(side, preset_id)
        spec = dual_review_model_specs(selected)[side]
        conn = get_db()
        try:
            append_audit_event(
                conn,
                "dual_review_model_configured",
                "configuration",
                ensure_portable_instance_id(),
                {
                    "side": side,
                    "preset_id": spec["preset_id"],
                    "provider": spec["provider"],
                    "api_origin": spec["api_origin"],
                    "model": spec["model"],
                    "key_updated": bool(api_key),
                },
            )
            conn.commit()
        finally:
            conn.close()
        json_response(self, {
            "ok": True,
            "side": side,
            "configured": dual_review_key_is_configured(side),
            "preset_id": spec["preset_id"],
            "provider": spec["provider"],
            "provider_label": spec["provider_label"],
            "model": spec["model"],
            "message": (
                f"AI-{side} 已选择 {spec['label']}；"
                + ("Key 已使用 Windows 本机加密保存，不会回显或写入便携目录。" if api_key else
                   ("本机已存在此模型 Key。" if dual_review_key_is_configured(side) else "尚未配置此服务商 Key。"))
            ),
        })

    @track_active_task
    def save_settings(self) -> None:
        global KEY_STORE_ERROR
        if not self.require_local_client():
            return
        payload = self.read_json()
        if cloud_mode_enabled(self):
            if set(payload) - {"preset_id"}:
                raise ValueError("服务器模型配置只接受预设编号；接口地址和 API Key 由服务器安全保存")
            preset_id = str(payload.get("preset_id", "")).strip()
            preset = persist_server_model_selection(preset_id)
            _, api_origin = validate_model_base_url(str(preset["base_url"]))
            audit_conn = get_db()
            try:
                append_audit_event(
                    audit_conn,
                    "server_model_selection_changed",
                    "configuration",
                    ensure_portable_instance_id(),
                    {"provider": preset["provider"], "api_origin": api_origin, "model": preset["model"]},
                )
                audit_conn.commit()
            finally:
                audit_conn.close()
            json_response(
                self,
                {
                    "ok": True,
                    "message": "服务器模型已切换；API Key 仍保存在服务器，网页不会接触 Key。",
                    "model": preset["model"],
                    "model_is_simulation": model_configuration_is_synthetic(),
                },
            )
            return
        preset_id = str(payload.get("preset_id", "")).strip()
        preset_catalog = model_preset_catalog()
        if preset_id in preset_catalog and preset_id != "custom":
            preset = preset_catalog[preset_id]
            provider = preset["provider"]
            base_url = preset["base_url"]
            model = preset["model"]
            if preset.get("user_defined") and str(payload.get("base_url", "")).strip():
                base_url = str(payload["base_url"]).strip()
            if (preset.get("experimental") or preset.get("user_defined")) and str(payload.get("model", "")).strip():
                model = str(payload["model"]).strip()
        elif preset_id == "custom":
            provider = "custom"
            base_url = str(payload.get("base_url", "")).strip()
            model = str(payload.get("model", "")).strip()
        elif not preset_id:
            # Keep compatibility with the earlier page, which only posted api_key.
            provider = os.getenv("HISTORY_GRADER_PROVIDER", "deepseek").strip() or "deepseek"
            base_url = os.getenv("HISTORY_GRADER_BASE_URL", "").strip()
            model = os.getenv("HISTORY_GRADER_MODEL", "").strip()
        else:
            raise ValueError("模型预设不存在，请刷新页面后重试")
        if len(base_url) > 500 or len(model) > 200:
            raise ValueError("接口地址或模型 ID 长度异常")
        base_url, api_origin = validate_model_base_url(base_url)
        if not model:
            raise ValueError("请填写模型 ID")
        provided_key = str(payload.get("api_key", "")).strip()
        remember_key = payload.get("remember_key", True) is not False
        api_key = provided_key
        if not api_key:
            try:
                api_key = read_dpapi_api_key(provider, expected_origin=api_origin)
            except ApiKeyStoreError as exc:
                KEY_STORE_ERROR = str(exc)
                raise ValueError(str(exc)) from exc
        if (
            not api_key
            and provider == (os.getenv("HISTORY_GRADER_PROVIDER", "deepseek").strip() or "deepseek")
            and api_origin == current_api_origin()
        ):
            api_key = os.getenv("HISTORY_GRADER_API_KEY", "").strip()
        if not api_key:
            raise ValueError("本机尚未保存当前服务商的 Key，请输入 Key；如只使用本次可取消“记住本机”")
        if len(api_key) > 500:
            raise ValueError("API Key 长度异常")
        if remember_key:
            save_dpapi_api_key(api_key, provider, api_origin=api_origin)
            SESSION_ONLY_KEY_PROVIDERS.discard(provider)
        else:
            SESSION_ONLY_KEY_PROVIDERS.add(provider)
        KEY_STORE_ERROR = ""
        write_env_values(
            {
                "HISTORY_GRADER_PROVIDER": provider,
                "HISTORY_GRADER_BASE_URL": base_url,
                "HISTORY_GRADER_MODEL": model,
            }
        )
        os.environ["HISTORY_GRADER_PROVIDER"] = provider
        os.environ["HISTORY_GRADER_BASE_URL"] = base_url
        os.environ["HISTORY_GRADER_MODEL"] = model
        os.environ["HISTORY_GRADER_API_KEY"] = api_key
        audit_conn = get_db()
        try:
            append_audit_event(
                audit_conn,
                "model_settings_changed",
                "configuration",
                ensure_portable_instance_id(),
                {"provider": provider, "api_origin": api_origin, "model": model, "remembered": remember_key},
            )
            audit_conn.commit()
        finally:
            audit_conn.close()
        message = (
            "模型配置和 Key 已保存到本机，不会写入 U 盘"
            if remember_key
            else "模型配置已保存；Key 仅在本次运行中使用，未写入磁盘"
        )
        json_response(self, {"ok": True, "remembered": remember_key, "message": message})

    def restart_service(self) -> None:
        self.shutdown_service(restart=True)

    def shutdown_service(self, restart: bool = False) -> None:
        global TASK_DRAINING
        if not self.require_local_client():
            return
        state_name = "restarting" if restart else "stopping"
        if getattr(self.server, state_name, False):
            message = "服务正在重启，请稍候刷新页面" if restart else "服务正在退出，请稍候"
            raise ValueError(message)
        if getattr(self.server, "stopping", False) or getattr(self.server, "restarting", False):
            raise ValueError("服务正在关闭，请稍候")
        with ACTIVE_TASKS_CONDITION:
            TASK_DRAINING = True
        if restart:
            self.server.restarting = True
            message = "服务将在当前任务完成后重启"
        else:
            self.server.stopping = True
            message = "服务将在当前任务完成后安全退出"
        json_response(self, {"ok": True, "message": message}, 202)

        def stop_server() -> None:
            global TASK_DRAINING
            if not wait_for_active_tasks():
                self.server.stopping = False
                self.server.restarting = False
                with ACTIVE_TASKS_CONDITION:
                    TASK_DRAINING = False
                    ACTIVE_TASKS_CONDITION.notify_all()
                print(f"[{utc_now()}] WARNING: 当前任务超过5分钟，已取消安全退出/重启")
                return
            if restart:
                try:
                    environment = build_restart_environment()
                    helper_command = restart_helper_command(sys.argv[1:], os.getpid())
                    subprocess.Popen(
                        helper_command,
                        cwd=str(PORTABLE_ROOT),
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except Exception as exc:
                    self.server.restarting = False
                    with ACTIVE_TASKS_CONDITION:
                        TASK_DRAINING = False
                        ACTIVE_TASKS_CONDITION.notify_all()
                    print(f"[{utc_now()}] WARNING: 无法启动重启助手（{type(exc).__name__}），已取消重启")
                    return
            time.sleep(0.2)
            self.server.shutdown()

        threading.Thread(target=stop_server, name="history-grader-stop", daemon=True).start()

    @track_active_task
    def create_batch(self) -> None:
        payload = self.read_json()
        grading_input_mode = _grading_input_mode(payload)
        assignment_id = str(payload.get("assignment_id", "")).strip().lower()
        request_id = str(payload.get("batch_request_id", "")).strip()[:128]
        formal_class = bool(str(payload.get("class_name") or "").strip())
        class_name = str(payload.get("class_name") or "").strip() if formal_class else ""
        requested_exam_id = str(payload.get("exam_id") or "").strip().lower()
        if formal_class and not self.require_identity_mapping_access():
            return
        raw_items = payload.get("items")
        if not re.fullmatch(r"[0-9a-f]{12}", assignment_id):
            raise ValueError("作业编号无效")
        if not request_id or not isinstance(raw_items, list) or not raw_items:
            raise ValueError("批次标识或学生清单无效")
        limit = 100 if self.is_local_client() else 20
        if len(raw_items) > limit:
            raise ValueError(f"单批最多处理{limit}名学生")
        items = []
        for sequence, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, dict):
                raise ValueError("批次学生清单格式无效")
            try:
                page_count = int(raw.get("page_count", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"第{sequence}份的页数无效") from exc
            if page_count < 1 or page_count > MAX_IMAGES_PER_SUBMISSION:
                raise ValueError(f"第{sequence}份答卷页数必须为1—{MAX_IMAGES_PER_SUBMISSION}页")
            items.append({
                "sequence": sequence,
                "submission_id": uuid.uuid4().hex[:12],
                "page_count": page_count,
                "upload_status": "reserved",
            })
        # A repeated request ID is an idempotent replay of the original
        # anonymous exam and its mappings.
        stable_material = {
            "assignment_id": assignment_id,
            "items": [{"sequence": item["sequence"], "page_count": item["page_count"]} for item in items],
        }
        if grading_input_mode == "original_photo":
            stable_material["grading_input_mode"] = grading_input_mode
        if formal_class:
            stable_material.update(class_name=class_name, exam_id=requested_exam_id)
        elif requested_exam_id:
            stable_material["exam_id"] = requested_exam_id
        request_hash = hashlib.sha256(
            json.dumps(stable_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            assignment_row = conn.execute("SELECT import_metadata_json FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
            if not assignment_row:
                raise ValueError("作业不存在")
            layout_required = bool(_json_object_or_empty(
                _json_object_or_empty(assignment_row["import_metadata_json"]).get("source")
            ).get("layout_source"))
            existing = conn.execute("SELECT * FROM grading_batches WHERE request_id = ?", (request_id,)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise ValueError("批次幂等标识已用于不同清单，请重新生成")
                manifest = json.loads(existing["manifest_json"] or "{}")
                json_response(self, {"batch_id": existing["id"], "exam_id": existing["exam_id"], "items": manifest.get("items", []), "idempotent_replay": True})
                return
            batch_id = uuid.uuid4().hex[:12]
            class_id = None
            if not formal_class:
                if not requested_exam_id and grading_input_mode == "auto_crop":
                    candidates = conn.execute(
                        "SELECT id FROM grading_exams WHERE assignment_id=? AND class_id IS NULL "
                        "AND layout_profile_id IS NOT NULL", (assignment_id,),
                    ).fetchall()
                    if len(candidates) > 1:
                        raise ValueError("请选择本批答卷对应的正式考试及答题区")
                    if candidates:
                        requested_exam_id = candidates[0][0]
                if requested_exam_id and not conn.execute(
                    "SELECT 1 FROM grading_exams WHERE id=? AND assignment_id=? AND class_id IS NULL",
                    (requested_exam_id, assignment_id),
                ).fetchone():
                    raise ValueError("考试与作业或匿名模式不匹配")
            if formal_class:
                class_row = roster_identity.find_class(conn, class_name)
                if not class_row:
                    raise ValueError("请选择已建立花名册的班级")
                # Only this explicit batch write registers an existing legacy
                # class. Student rows, IDs and historical bindings stay intact.
                class_id = class_row["id"] or roster_identity.ensure_class(conn, class_row["class_name"], utc_now())
                if not requested_exam_id and layout_required and grading_input_mode == "auto_crop":
                    candidates = conn.execute(
                        "SELECT e.id FROM grading_exams e WHERE assignment_id=? AND layout_profile_id IS NOT NULL "
                        "AND (class_id=? OR (class_id IS NULL AND NOT EXISTS (SELECT 1 FROM exam_marking_codes m WHERE m.exam_id=e.id)))",
                        (assignment_id, class_id),
                    ).fetchall()
                    if len(candidates) == 1:
                        requested_exam_id = candidates[0][0]
                    else:
                        raise ValueError("请选择已配置答题区且属于本班的正式考试")
                if requested_exam_id and not conn.execute("SELECT 1 FROM grading_exams e WHERE id=? AND assignment_id=? AND (class_id=? OR (class_id IS NULL AND NOT EXISTS (SELECT 1 FROM exam_marking_codes m WHERE m.exam_id=e.id)))", (requested_exam_id, assignment_id, class_id)).fetchone():
                    raise ValueError("考试与班级或作业不匹配")
                if requested_exam_id:
                    conn.execute("UPDATE grading_exams SET class_id=? WHERE id=? AND class_id IS NULL", (class_id, requested_exam_id))
            context = _exam_layout_profile_context(conn, requested_exam_id) if requested_exam_id else None
            if grading_input_mode == "auto_crop" and (layout_required or (context and context.get("layout_profile_id"))):
                if not context or not context.get("layout_profile") or context.get("profile_invalid") or context.get("profile_hash_mismatch") or context.get("pdf_hash_mismatch"):
                    raise ValueError("answer_region_unavailable:layout_profile_missing_or_stale；请先选择并保存正式考试答题区")
            exam_id = requested_exam_id or uuid.uuid4().hex
            source_channel = "local" if self.is_local_client() else "mobile"
            now = utc_now()
            if not requested_exam_id:
                conn.execute("INSERT INTO grading_exams (id, assignment_id, created_at, class_id) VALUES (?, ?, ?, ?)",
                             (exam_id, assignment_id, now, class_id))
            manifest_items = []
            for item in items:
                manifest_items.append(dict(item))
            conn.execute(
                "INSERT INTO grading_batches (id, request_id, assignment_id, class_name, source_channel, total_sequences, next_sequence, status, manifest_json, request_hash, created_at, updated_at, exam_id) VALUES (?, ?, ?, ?, ?, ?, 1, 'active', ?, ?, ?, ?, ?)",
                (
                    batch_id, request_id, assignment_id, class_name, source_channel, len(items),
                    json.dumps({"items": manifest_items, "identity_mode": "anonymous", "grading_input_mode": grading_input_mode, "preview_exam_id": requested_exam_id}, ensure_ascii=False), request_hash, now, now, exam_id,
                ),
            )
            append_audit_event(
                conn, "grading_batch_created", "grading_batch", batch_id,
                {"assignment_id": assignment_id, "total_sequences": len(items), "source_channel": source_channel, "request_hash": request_hash},
            )
            conn.commit()
            json_response(self, {"batch_id": batch_id, "exam_id": exam_id, "items": manifest_items}, 201)
        finally:
            conn.close()

    @track_active_task
    def control_batch_grading(self) -> None:
        if not self.require_local_client():
            return
        from batch_grading_control import change
        payload = self.read_json()
        if payload.get('confirmed') is not True:
            raise ValueError('请明确确认停止或继续本批剩余批改')
        conn = get_db()
        try:
            result = change(conn, str(payload.get('batch_id') or ''), str(payload.get('assignment_id') or ''), str(payload.get('exam_id') or ''), payload.get('action'), utc_now, append_audit_event, actor=getattr(getattr(self, '_web_auth_session', None), 'username', '') or 'local')
            next_id = _enqueue_current_batch_submission(conn, result['batch_id']) if payload.get('action') == 'resume' else None
            conn.commit()
        finally:
            conn.close()
        if next_id:
            enqueue_submission(next_id)
        json_response(self, result)

    @track_active_task
    def mark_batch_upload_failed(self) -> None:
        payload = self.read_json()
        batch_id = str(payload.get("batch_id", "")).strip().lower()
        try:
            sequence = int(payload.get("sequence", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("批次序号无效") from exc
        # Browser/network exceptions may include an uploaded filename or
        # student identity. Persist only a categorical failure description.
        error = "答卷照片上传失败；等待教师原位重试或确认跳过"
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute("SELECT * FROM grading_batches WHERE id = ?", (batch_id,)).fetchone()
            if not batch or sequence < 1 or sequence > int(batch["total_sequences"]):
                raise ValueError("批次或序号不存在")
            if not self.is_local_client() and batch["source_channel"] != "mobile":
                raise ValueError("手机访问码只能操作手机创建的批次")
            already_saved = conn.execute(
                "SELECT id FROM submissions WHERE batch_id = ? AND batch_sequence = ?", (batch_id, sequence)
            ).fetchone()
            if already_saved:
                json_response(self, {"ok": True, "batch_id": batch_id, "sequence": sequence, "upload_already_saved": True})
                return
            item = _mark_batch_item_upload_status(conn, batch_id, sequence, "failed", error)
            if int(batch["next_sequence"]) == sequence and batch["status"] == "active":
                conn.execute(
                    "UPDATE grading_batches SET status = 'paused', pause_sequence = ?, pause_reason = ?, updated_at = ? WHERE id = ?",
                    (sequence, error or "答卷上传失败", utc_now(), batch_id),
                )
            append_audit_event(
                conn, "grading_batch_upload_failed", "grading_batch", batch_id,
                {"sequence": sequence, "submission_id": item["submission_id"], "error": error},
            )
            conn.commit()
            json_response(self, {"ok": True, "batch_id": batch_id, "sequence": sequence})
        finally:
            conn.close()

    @track_active_task
    def skip_batch_sequence(self) -> None:
        local_client = self.is_local_client()
        mobile_client = not local_client and self.is_mobile_token_client()
        if not local_client and not mobile_client:
            if not self.require_local_client():
                return
        payload = self.read_json()
        batch_id = str(payload.get("batch_id", "")).strip().lower()
        submission_id = str(payload.get("submission_id", "")).strip().lower()
        try:
            sequence = int(payload.get("sequence", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("批次序号无效") from exc
        reason = str(payload.get("reason", "教师确认跳过失败项")).strip()[:500]
        if payload.get("confirm_skip") is not True:
            raise ValueError("必须明确确认后才能跳过批次序号")
        conn = get_db()
        next_id = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute("SELECT * FROM grading_batches WHERE id = ?", (batch_id,)).fetchone()
            if not batch or batch["status"] != "paused" or int(batch["pause_sequence"] or 0) != sequence or int(batch["next_sequence"]) != sequence:
                raise ValueError("只能跳过当前暂停的原位序号")
            if batch["exam_id"]:
                reason = "教师确认跳过匿名答卷"
            manifest = json.loads(batch["manifest_json"] or "{}")
            if manifest.get('grading_stop_requested'):
                raise ValueError('本批已由教师停止，请先明确继续剩余批改，再处理当前序号')
            item = next((value for value in manifest.get("items", []) if int(value.get("sequence", 0)) == sequence), None)
            if not item or item.get("submission_id") != submission_id:
                raise ValueError("批次序号与答卷编号不匹配")
            existing = conn.execute(
                "SELECT state, result_json, confirmed_at, source_channel FROM submissions WHERE id = ?",
                (submission_id,),
            ).fetchone()
            upload_failed = item.get("upload_status") == "failed"
            if existing:
                result = json.loads(existing["result_json"] or "{}")
                if existing["state"] not in {"failed", "recovery_pending", "completed"}:
                    raise ValueError("当前答卷尚未进入可跳过的失败状态")
                unmatched_mapping = (
                    existing["state"] == "completed"
                    and existing["confirmed_at"] is None
                    and result.get("mode") == "vision-model"
                    and result.get("requires_question_mapping") is True
                    and (
                        bool(result.get("unmapped_answers"))
                        or any(
                            point.get("answer_status") == "question_unmatched"
                            for point in result.get("sub_questions", [])
                            if isinstance(point, dict)
                        )
                    )
                )
                mobile_skip_allowed = (
                    batch["source_channel"] == "mobile"
                    and existing["source_channel"] == "mobile"
                    and existing["confirmed_at"] is None
                    and (
                        existing["state"] in {"failed", "recovery_pending"}
                        or unmatched_mapping
                        or (
                            existing["state"] == "completed"
                            and result.get("mode") in {"vision-model-error", "image-quality-error", "processing-error"}
                        )
                    )
                )
                if mobile_client and not mobile_skip_allowed:
                    conn.rollback()
                    json_response(self, {"error": "手机端只能跳过自己创建的批次中当前暂停且尚未确认成绩的失败答卷"}, 403)
                    return
                if existing["state"] == "completed":
                    allowed_completed_skip = result.get("mode") in {
                        "vision-model-error", "image-quality-error", "processing-error", "rules-demo"
                    } or unmatched_mapping
                    if existing["confirmed_at"] is not None or not allowed_completed_skip:
                        raise ValueError("已完成且可正常复核的答卷不能跳过")
                conn.execute("UPDATE submissions SET state = 'skipped' WHERE id = ?", (submission_id,))
                conn.execute("UPDATE task_queue SET state = 'skipped', updated_at = ? WHERE submission_id = ?", (utc_now(), submission_id))
            elif mobile_client and not (batch["source_channel"] == "mobile" and upload_failed):
                conn.rollback()
                json_response(self, {"error": "手机端只能跳过自己创建的批次中已记录上传失败的当前答卷"}, 403)
                return
            item["upload_status"] = "skipped"
            item["skip_reason"] = reason
            next_sequence = sequence + 1
            status = "completed" if next_sequence > int(batch["total_sequences"]) else "active"
            conn.execute(
                "UPDATE grading_batches SET manifest_json = ?, next_sequence = ?, status = ?, pause_sequence = NULL, pause_reason = '', updated_at = ? WHERE id = ?",
                (json.dumps(manifest, ensure_ascii=False), next_sequence, status, utc_now(), batch_id),
            )
            append_audit_event(
                conn, "grading_batch_sequence_skipped", "grading_batch", batch_id,
                {
                    "sequence": sequence,
                    "submission_id": submission_id,
                    "reason": reason,
                    "operation_source": "mobile-confirmed" if mobile_client else "computer-local",
                },
            )
            if status == "active":
                next_id = _enqueue_current_batch_submission(conn, batch_id)
            conn.commit()
        finally:
            conn.close()
        if next_id:
            enqueue_submission(next_id)
        json_response(self, {"ok": True, "batch_id": batch_id, "skipped_sequence": sequence, "next_submission_id": next_id})

    @track_active_task
    def create_submission(self) -> None:
        payload = self.read_submission_payload()
        batch_id = str(payload.get("batch_id", "")).strip().lower()
        if batch_id:
            with _batch_upload_lock(batch_id):
                self._create_submission_from_payload(payload)
            return
        self._create_submission_from_payload(payload)

    def _create_submission_from_payload(self, payload: dict[str, Any]) -> None:
        submission_started_at = time.perf_counter()
        grading_input_mode = _grading_input_mode(payload)
        assignment_id = str(payload.get("assignment_id", "")).strip()
        class_name = str(payload.get("class_name", "")).strip()
        student_label = str(payload.get("student_label", "")).strip() or "未命名学生"
        answer_text = str(payload.get("answer_text", "")).strip()
        dual_review_requested = payload.get("dual_review_enabled") is True
        photos = payload.get("photos", [])
        batch_id = str(payload.get("batch_id", "")).strip().lower()
        batch_sequence = None
        batch = None
        batch_item = None
        if batch_id:
            submission_id = str(payload.get("submission_id", "")).strip().lower()
            try:
                batch_sequence = int(payload.get("sequence", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("批次序号无效") from exc
            if not re.fullmatch(r"[0-9a-f]{12}", batch_id) or not re.fullmatch(r"[0-9a-f]{12}", submission_id):
                raise ValueError("批次或答卷编号无效")
        else:
            submission_id = uuid.uuid4().hex[:12]
        if not assignment_id:
            raise ValueError("请选择作业")
        if dual_review_requested:
            if not self.is_local_client() and not self.is_mobile_token_client():
                raise ValueError("异模型双评需要有效的电脑本机或手机临时访问码")
            if not dual_review_key_is_configured("A") or not dual_review_key_is_configured("B"):
                raise ValueError("异模型双评需要先在电脑本机配置 AI-A 和 AI-B 的模型与 Key")
            specs = dual_review_model_specs()
            if (
                specs["A"]["provider"], specs["A"]["model"], specs["A"]["api_origin"]
            ) == (
                specs["B"]["provider"], specs["B"]["model"], specs["B"]["api_origin"]
            ):
                raise ValueError("AI-A 和 AI-B 必须是两个不同模型")
        if not isinstance(photos, list) or len(photos) > MAX_IMAGES_PER_SUBMISSION:
            raise ValueError(f"单份作业最多上传{MAX_IMAGES_PER_SUBMISSION}张照片，请逐份处理")
        conn = get_db()
        assignment = conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
        if not assignment:
            conn.close()
            raise ValueError("作业不存在")
        saved_sub_questions = json.loads(assignment["sub_questions_json"] or "[]")
        if saved_sub_questions:
            sub_questions = clean_sub_questions(saved_sub_questions)
        else:
            sub_questions = clean_sub_questions(
                None,
                fallback_question=assignment["question"],
                fallback_answer=assignment["standard_answer"],
                fallback_criteria=json.loads(assignment["criteria_json"]),
            )
        has_answer_region_attestation = any(
            isinstance(item, dict) and str(item.get("answer_region_attestation") or "")
            for item in photos
        )
        if not has_answer_region_attestation and any(
            isinstance(item, dict) and str(item.get("image_role") or "") == "answer_region"
            for item in photos
        ):
            conn.close()
            raise ValueError("answer_region_invalid:attestation_missing")
        cloud_privacy_mode = cloud_mode_enabled(self)
        privacy_fingerprint = ""
        if batch_id:
            batch = conn.execute("SELECT * FROM grading_batches WHERE id = ?", (batch_id,)).fetchone()
            if not batch or batch["assignment_id"] != assignment_id:
                conn.close()
                raise ValueError("批次与作业不匹配")
            if not self.is_local_client() and batch["source_channel"] != "mobile":
                conn.close()
                raise ValueError("手机访问码只能上传到手机创建的批次")
            manifest = json.loads(batch["manifest_json"] or "{}")
            if grading_input_mode != _grading_input_mode(manifest):
                conn.close()
                raise ValueError("批次图片模式不能更改，请重新创建批次")
            batch_item = next((item for item in manifest.get("items", []) if int(item.get("sequence", 0)) == batch_sequence), None)
            if not batch_item or batch_item["submission_id"] != submission_id:
                conn.close()
                raise ValueError("批次编号与原始序号不匹配")
            if int(batch_item["page_count"]) != len(photos):
                conn.close()
                raise ValueError("上传页数与教师确认的答卷页数不一致")
            if batch_sequence < int(batch["next_sequence"]):
                already = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
                if already:
                    conn.close()
                    json_response(self, {"submission": submission_public(already), "idempotent_replay": True}, 200)
                    return
                conn.close()
                raise ValueError("该批次序号已处理，不能覆盖旧记录")
            retrying_upload_failure = (
                batch["status"] == "paused"
                and int(batch["pause_sequence"] or 0) == batch_sequence
                and batch_item.get("upload_status") == "failed"
            )
        else:
            retrying_upload_failure = False
        source_channel = "local" if self.is_local_client() else "mobile"
        supplied_idempotency = str(payload.get("idempotency_key", "")).strip()
        raw_photo_fingerprints = []
        for index, item in enumerate(photos, start=1):
            if not isinstance(item, dict):
                conn.close()
                raise ValueError("照片元数据格式无效")
            original_bytes, _ = photo_upload_bytes(item, original=True)
            try:
                page_sequence = int(item.get("page_sequence", index))
            except (TypeError, ValueError) as exc:
                conn.close()
                raise ValueError("答卷页序无效") from exc
            item["page_sequence"] = page_sequence
            raw_photo_fingerprints.append({
                "original_sha256": hashlib.sha256(original_bytes).hexdigest(),
                "original_bytes": len(original_bytes),
                "page_sequence": page_sequence,
            })
            del original_bytes
        if sorted(item["page_sequence"] for item in raw_photo_fingerprints) != list(range(1, len(photos) + 1)):
            conn.close()
            raise ValueError("多页答卷页序必须从1开始且不可重复")
        photos = sorted(photos, key=lambda item: int(item.get("page_sequence", 1)))
        raw_photo_fingerprints.sort(key=lambda item: item["page_sequence"])
        answer_regions: list[dict[str, Any]] = []
        verified_answer_regions: dict[int, dict[str, Any]] = {}
        approval_required = bool(photos and (model_configured() or dual_review_requested or grading_input_mode == "original_photo"))
        privacy_status = str(payload.get("privacy_status") or "unchecked").strip()
        if privacy_status not in (PRIVACY_STATUS_VALUES - {"legacy_unknown"}):
            conn.close()
            raise ValueError("答卷隐私状态无效，请重新完成隐私预览")
        if approval_required and privacy_status not in PRIVACY_STATUS_APPROVED:
            conn.close()
            raise ValueError("请先确认本份答卷已预先匿名、已遮挡，或确认图片中没有可辨认身份信息；未确认时不会发送云模型")
        if photos:
            for item in photos:
                if isinstance(item, dict):
                    item["ai_redactions"] = normalize_ai_redactions(item.get("ai_redactions", []))
            if payload.get("answer_regions") is not None or approval_required or has_answer_region_attestation:
                answer_regions = normalize_answer_regions(
                    payload.get("answer_regions"), sub_questions, len(photos),
                    require_complete=not has_answer_region_attestation,
                )
                verified_answer_regions = _validate_formal_answer_region_attestations(
                    photos, answer_regions, sub_questions, assignment, conn, payload
                )
                if verified_answer_regions:
                    _require_answer_region_rubric_ready(
                        assignment["import_metadata_json"], sub_questions,
                        [str(item["question_id"]) for item in verified_answer_regions.values()],
                    )
            else:
                verified_answer_regions = {}
        if approval_required:
            try:
                _require_formal_answer_regions(
                    _request_formal_exam_context(conn, assignment_id, payload, verified_answer_regions),
                    sub_questions, answer_regions, verified_answer_regions,
                )
            except ValueError:
                conn.close()
                raise
            if payload.get("ai_privacy_confirmed") is not True:
                conn.close()
                raise ValueError("视觉模型请求必须先完成最终图片 AI Payload 预览和教师确认")
            try:
                preview_token = str(payload.get("ai_privacy_preview_token", ""))
                prompt = build_scoring_prompt_text(
                    str(assignment["question"]), sub_questions, privacy_mode=True,
                    answer_regions=answer_regions,
                )
                if _contains_roster_identity(conn, prompt) or _contains_labeled_identity_data(prompt):
                    conn.close()
                    raise ValueError("题目或评分细则包含花名册身份字段，请修改后重新预览")
                privacy_fingerprint = _verify_ai_preview_token(
                    preview_token,
                    assignment_id,
                    str(assignment["question"]),
                    sub_questions,
                    photos,
                    _preview_session_fingerprint(self),
                    _ai_privacy_identity_fingerprint(assignment_id, class_name, student_label),
                    answer_regions,
                    require_complete_regions=not bool(verified_answer_regions),
                    grading_input_mode=grading_input_mode,
                    exam_id=str(payload.get("exam_id") or ""),
                )
            except Exception:
                if conn:
                    conn.close()
                raise
        request_material = {
            "assignment_id": assignment_id,
            "anonymous_marking": True,
            "privacy_status": privacy_status,
            # Preserve the legacy idempotency fingerprint for existing
            # submissions without retaining identity labels or answer text.
            "photos": raw_photo_fingerprints if batch_id else [
                {key: item[key] for key in ("original_sha256", "original_bytes")}
                for item in raw_photo_fingerprints
            ],
        }
        if dual_review_requested:
            request_material["dual_review_enabled"] = True
        if grading_input_mode == "original_photo":
            request_material["grading_input_mode"] = grading_input_mode
        if batch_id:
            request_material["batch_id"] = batch_id
            request_material["sequence"] = batch_sequence
        request_material["answer_regions"] = [
            {key: region[key] for key in ("region_id", "profile_id", "invocation_scope", "coordinate_system", "rotation_ccw", "photo_index", "x", "y", "width", "height", "sub_question_ids")}
            for region in answer_regions
        ]
        request_hash = hashlib.sha256(
            json.dumps(request_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        idempotency_key = supplied_idempotency[:128] if supplied_idempotency else request_hash
        if batch_id:
            reserved = conn.execute('SELECT manifest_json FROM grading_batches WHERE id=?',(batch_id,)).fetchone()
            if reserved and any(i.get('submission_id') == submission_id and i.get('upload_status') == 'deleted' for i in json.loads(reserved['manifest_json']).get('items', [])):
                conn.close()
                raise ValueError('该份答卷已永久删除，请新建上传批次')
        existing = conn.execute(
            "SELECT * FROM submissions WHERE idempotency_key = ? OR id = ? ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END LIMIT 1",
            (idempotency_key, submission_id, submission_id),
        ).fetchone()
        if existing:
            conn.close()
            if str(existing["request_hash"] or "") != request_hash:
                raise ValueError("答卷序号或幂等键已被不同内容占用，拒绝覆盖")
            json_response(self, {"submission": submission_public(existing), "idempotent_replay": True}, 200)
            return
        if batch and batch["status"] in {"completed", "cancelled"}:
            conn.close()
            raise ValueError("该批次已结束，不能继续上传")
        if batch_id:
            # Serialize with teacher stop and worker claims before accepting a new item.
            if not conn.in_transaction:
                conn.execute('BEGIN IMMEDIATE')
            from batch_grading_control import stopped
            batch = conn.execute('SELECT * FROM grading_batches WHERE id=?', (batch_id,)).fetchone()
            if stopped(batch):
                conn.close()
                raise ValueError('本批已停止，请先明确继续剩余批改后再上传')
        if approval_required:
            expires_at = int(photos[0].get("_verified_preview_expires_at", 0))
            try:
                _consume_preview_token(privacy_fingerprint, expires_at)
            except Exception:
                conn.close()
                raise
        student_folder = submission_id
        student_dir = DATA_DIR / assignment_id / "students" / student_folder
        photo_meta = []
        photo_paths = []
        total_original_bytes = 0
        try:
            for index, item in enumerate(photos, start=1):
                if not isinstance(item, dict):
                    continue
                path = student_dir / f"page_{index:02d}.jpg"
                original_path = student_dir / f"page_{index:02d}.original"
                verified_provenance = item.get("_verified_answer_region_provenance")
                verified_role = str(item.get("_verified_image_role") or "source_page")
                meta = save_photo_item(
                    item, path, original_path, item.get("corners"),
                    image_role=verified_role,
                    answer_region_provenance=verified_provenance if verified_role == "answer_region" else None,
                )
                if grading_input_mode == "original_photo" and approval_required and item.get("_verified_grading_input_mode") == "original_photo":
                    meta["grading_input_mode"] = "original_photo"
                total_original_bytes += int(meta.get("original_bytes", 0))
                if total_original_bytes > MAX_SUBMISSION_BYTES:
                    raise ValueError("单份作业原图总量不能超过250MiB，请分成多份提交")
                # Never persist uploaded filenames on anonymous records.
                meta["name"] = f"page_{index:02d}.jpg"
                meta["page_sequence"] = int(item.get("page_sequence", len(photo_meta) + 1))
                meta["ai_redactions"] = normalize_ai_redactions(item.get("ai_redactions", []))
                if approval_required:
                    meta["approved_ai_page_sha256"] = str(item.get("_verified_ai_model_sha256", ""))
                    meta["approved_prompt_sha256"] = str(item.get("_verified_ai_prompt_sha256", ""))
                    if not meta["approved_ai_page_sha256"] or not meta["approved_prompt_sha256"]:
                        raise ValueError("缺少教师确认的 AI Payload 指纹，已阻止上传")
                photo_meta.append(meta)
                photo_paths.append(path)
            if not photo_paths and not answer_text:
                raise ValueError("请至少上传一张照片，或填写学生答案文字用于演示")
        except Exception:
            if student_dir.exists():
                shutil.rmtree(student_dir, ignore_errors=True)
            conn.close()
            raise
        result = queued_result(total_sub_questions(sub_questions))
        if dual_review_requested:
            result["dual_review_requested"] = True
        result["diagnostics"] = {
            "submit_ms": elapsed_ms(submission_started_at),
            "upload_photo_count": len(photo_paths),
            "uploaded_original_bytes": total_original_bytes,
        }
        if batch_id:
            code_row = conn.execute(
                "SELECT * FROM exam_marking_codes WHERE batch_id = ? AND batch_sequence = ?",
                (batch_id, batch_sequence),
            ).fetchone()
            exam_id=str(code_row['exam_id']) if code_row else str(batch['exam_id'] or '')
            if not exam_id:
                exam_id=uuid.uuid4().hex
                conn.execute('INSERT INTO grading_exams(id,assignment_id,created_at) VALUES(?,?,?)',(exam_id,assignment_id,utc_now()))
                conn.execute('UPDATE grading_batches SET exam_id=? WHERE id=?',(exam_id,batch_id))
            marking_code=str(code_row['code']) if code_row else submission_id
            marking_code_id=str(code_row['id']) if code_row else None
            marking_status=str(code_row['binding_status']) if code_row else 'anonymous'
        else:
            exam_id=str(payload.get('exam_id') or (next(iter(verified_answer_regions.values()))['exam_id'] if verified_answer_regions else ''))
            if exam_id and not conn.execute('SELECT 1 FROM grading_exams WHERE id=? AND assignment_id=?',(exam_id,assignment_id)).fetchone():raise ValueError('答卷与考试来源不一致')
            if not exam_id:
                exam_id=uuid.uuid4().hex
                conn.execute('INSERT INTO grading_exams(id,assignment_id,created_at) VALUES(?,?,?)',(exam_id,assignment_id,utc_now()))
            marking_code_id=None
            marking_code=submission_id
            marking_status='anonymous'
        answer_text = ""
        student_label = submission_id
        class_name = ""
        identity_status = "anonymous"
        identity_method = "anonymous_submission"
        batch_task_state = "queued"
        if batch_id and (
            (batch["status"] != "active" and not retrying_upload_failure)
            or batch_sequence != int(batch["next_sequence"])
        ):
            batch_task_state = "waiting_batch"
        now = utc_now()
        conn.execute(
            "INSERT INTO submissions (id, assignment_id, student_label, answer_text, photos_json, result_json, created_at, class_name, student_no, student_name, identity_status, identity_method, final_total, teacher_note, confirmed_at, idempotency_key, request_hash, state, source_channel, uncertain_model_call, evidence_json, batch_id, batch_sequence, ai_privacy_confirmed, ai_privacy_reviewed_at, ai_privacy_fingerprint, exam_id, marking_code_id, marking_code, answer_regions_json, marking_status, privacy_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" ,
            (
                submission_id,
                assignment_id,
                student_label,
                answer_text,
                json.dumps(photo_meta, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                now,
                class_name,
                "",
                "",
                "anonymous",
                "marking_code",
                None,
                "",
                None,
                idempotency_key,
                request_hash,
                "queued",
                source_channel,
                0,
                "[]",
                batch_id or None,
                batch_sequence,
                int(bool(approval_required and photos and privacy_fingerprint)),
                now if approval_required and photos and privacy_fingerprint else None,
                privacy_fingerprint,
                exam_id,
                marking_code_id,
                marking_code,
                json.dumps(answer_regions, ensure_ascii=False),
                marking_status,
                privacy_status,
            ),
        )
        conn.execute(
            "INSERT INTO task_queue (id, submission_id, state, attempt_count, available_at, last_error, created_at, updated_at) VALUES (?, ?, ?, 0, ?, '', ?, ?)",
            (uuid.uuid4().hex, submission_id, batch_task_state, now, now, now),
        )
        if batch_id:
            _mark_batch_item_upload_status(conn, batch_id, batch_sequence, "uploaded")
            if retrying_upload_failure and batch_task_state == "queued":
                conn.execute(
                    "UPDATE grading_batches SET status = 'active', pause_sequence = NULL, pause_reason = '', updated_at = ? WHERE id = ? AND next_sequence = ?",
                    (now, batch_id, batch_sequence),
                )
        append_audit_event(
            conn,
            "submission_queued",
            "submission",
            submission_id,
            {"request_hash": request_hash, "source_channel": source_channel, "batch_id": batch_id or None, "sequence": batch_sequence},
        )
        if approval_required and photos:
            append_audit_event(
                conn,
                "ai_privacy_preview_confirmed",
                "submission",
                submission_id,
                {"preview_fingerprint": privacy_fingerprint, "image_count": len(photo_meta), "redacted_regions": sum(len(item.get("ai_redactions", [])) for item in photo_meta), "privacy_status": privacy_status},
            )
        conn.commit()
        submission_to_enqueue = None
        try:
            if batch_id:
                # The batch cursor may advance while this upload is being
                # decoded and persisted. Recheck after commit so a submission
                # that was initially marked waiting_batch cannot be stranded
                # when it becomes the current sequence during that window.
                submission_to_enqueue = _enqueue_current_batch_submission(conn, batch_id)
                conn.commit()
            elif batch_task_state == "queued":
                submission_to_enqueue = submission_id
        finally:
            conn.close()
        if submission_to_enqueue:
            enqueue_submission(submission_to_enqueue)
        public = load_submission_public(submission_id)
        json_response(self, {"submission": public, "message": "已加入本机批改队列，请等待处理完成"}, 202)

    @track_active_task
    def adopt_batch_consensus(self) -> None:
        if not self.require_local_client():
            return
        from batch_adoption import adopt
        body = self.read_json()
        conn = get_db()
        try:
            result = adopt(conn, body, getattr(getattr(self,'_web_auth_session',None),'username','') or 'local', sys.modules[__name__])
            conn.commit()
            json_response(self, result)
        finally:
            conn.close()

    @track_active_task
    def confirm_total_submission(self) -> None:
        import teacher_total
        import sys
        payload = self.read_json()
        local_client = self.is_local_client()
        with closing(get_db()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = teacher_total.read(conn, str(payload.get('submission_id', '')))
            if not local_client and row['source_channel'] != 'mobile':
                raise ValueError('手机访问码只能操作本机采集的答卷')
            actor = getattr(getattr(self, '_web_auth_session', None), 'username', None)
            record = teacher_total.confirm(conn, row, payload, sys.modules[__name__], actor)
        clear_submission_photo_viewed(row['id'])
        json_response(self, {'confirmation': record}, 200)

    def confirm_submission(self) -> None:
        mobile_client = not self.is_local_client()
        payload = self.read_json()
        submission_id = str(payload.get("submission_id", "")).strip()
        try:
            final_total = float(payload["final_total"]) if "final_total" in payload else None
        except (TypeError, ValueError):
            raise ValueError("最终确认分数必须是数字")
        if final_total is not None and not math.isfinite(final_total):
            raise ValueError("最终确认分数必须是有限数字，不能使用 NaN 或无穷大")
        teacher_note = str(payload.get("teacher_note", "")).strip()[:2000]
        conn = get_db()
        # Keep content validation and the teacher-final write in one write
        # transaction so a concurrent assignment edit cannot slip between them.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT submissions.*, assignments.question, assignments.standard_answer, "
            "assignments.criteria_json, assignments.sub_questions_json "
            "FROM submissions JOIN assignments ON assignments.id = submissions.assignment_id "
            "WHERE submissions.id = ?",
            (submission_id,),
        ).fetchone()
        if not row:
            conn.close()
            raise ValueError("批改记录不存在")
        if "exam_id" in payload and str(payload["exam_id"]) != str(row["exam_id"] or ""):
            conn.close()
            raise ValueError("答卷不属于指定考试，不能确认成绩")
        if row["state"] != "completed":
            conn.close()
            raise ValueError("只有已完成的批改记录可以确认成绩")
        if mobile_client and row["source_channel"] != "mobile":
            conn.close()
            raise ValueError("手机只能确认通过手机提交的本份成绩")
        result = json.loads(row["result_json"] or "{}")
        if result.get("photos_discarded_unconfirmed"):
            conn.close()
            raise ValueError("未终评答卷的照片已弃用清理，请重新上传后再确认成绩")
        try:
            teacher_point_decisions = normalize_teacher_dual_point_decisions(
                result.get("dual_review") or {"results": {"A": result}},
                payload.get("teacher_point_decisions"),
            )
        except ValueError:
            conn.close()
            raise
        if result.get("simulation_model"):
            conn.close()
            raise ValueError("模拟模型结果仅用于流程验收，不能确认或导出为正式成绩")
        if result_has_unresolved_answer_status(result):
            conn.close()
            raise ValueError("存在未确认空白、题号未对应或图像无法读取的小题，不能确认为成绩")
        review_override = bool(row["manual_reviewed"]) and result.get("mode") == "vision-model"
        if (result.get("blocked") and not review_override) or result.get("mode") in {"vision-model-error", "image-quality-error", "rules-demo", "processing-error", "queued"}:
            conn.close()
            raise ValueError("该记录未通过安全判分门禁，不能确认；请重新拍摄清晰试卷或核对模型证据")
        saved_sub_questions = json.loads(row["sub_questions_json"] or "[]")
        scope_value = result.get("grading_scope") if isinstance(result, dict) else None
        scoped_sub_questions = saved_sub_questions
        if saved_sub_questions and isinstance(scope_value, dict):
            if not isinstance(scope_value.get("graded_question_ids"), list) or not scope_value["graded_question_ids"]:
                conn.close()
                raise ValueError("本次评分范围缺少有效题号，已拒绝确认")
            scoped_sub_questions, calculated_scope = resolve_grading_scope(
                saved_sub_questions,
                graded_question_ids=scope_value.get("graded_question_ids"),
            )
            maximum = calculated_scope["scoped_max_score"]
            recorded_maximum = scope_value.get("scoped_max_score")
            if recorded_maximum is not None:
                try:
                    recorded_maximum = float(recorded_maximum)
                except (TypeError, ValueError):
                    conn.close()
                    raise ValueError("本次评分范围满分无效，已拒绝确认")
                if not math.isfinite(recorded_maximum) or abs(recorded_maximum - maximum) > 1e-9:
                    conn.close()
                    raise ValueError("本次评分范围满分与正式评分细则不一致，已拒绝确认")
            calculated_scope["source"] = str(scope_value.get("source") or "explicit")
            result["grading_scope"] = calculated_scope
            result["max_total"] = maximum
        else:
            maximum = (
                total_sub_questions(saved_sub_questions)
                if saved_sub_questions
                else total_score(json.loads(row["criteria_json"]))
            )
        if not math.isfinite(maximum) or maximum <= 0:
            conn.close()
            raise ValueError("题目满分数据无效，已拒绝确认")
        # Rebuild the current grading projection with the same clean/scope steps
        # used before grading, then verify the recorded evidence against it.
        # Legacy records created before version evidence existed keep their prior
        # confirmation boundary; they are not backfilled and remain ineligible
        # for a complete unified total.
        current_version_questions = []
        current_version_scope = None
        try:
            if saved_sub_questions:
                current_cleaned = clean_sub_questions(saved_sub_questions)
                current_version_questions, current_version_scope = resolve_grading_scope(
                    current_cleaned,
                    graded_question_ids=(
                        scope_value.get("graded_question_ids")
                        if isinstance(scope_value, dict) and isinstance(scope_value.get("graded_question_ids"), list)
                        else None
                    ),
                )
        except (AttributeError, KeyError, TypeError, ValueError):
            current_version_questions = []
            current_version_scope = None
        if "subjective_version_evidence" in result:
            version_error = grading_core.subjective_version_state(
                result.get("subjective_version_evidence"),
                assignment_question=row["question"],
                assignment_standard_answer=row["standard_answer"],
                source_questions=saved_sub_questions,
                scoring_questions=current_version_questions,
                graded_question_ids=(current_version_scope or {}).get("graded_question_ids"),
                scoped_max_score=(current_version_scope or {}).get("scoped_max_score"),
                grading_context=result.get("grading_context"),
                dual_review=result.get("dual_review"),
            )
            if version_error:
                conn.close()
                if version_error == "subjective_content_version_mismatch":
                    raise ValueError(
                        "subjective_content_version_mismatch：当前试卷题干、材料、参考答案或评分标准与该答卷评分时版本不一致，不能保存教师终评；请恢复原内容或重新评分后再试"
                    )
                raise ValueError(
                    "subjective_version_unverifiable：该答卷缺少或无法核实评分时的内容版本证据，不能保存教师终评；请重新评分后再试"
                )
        # Every new/reconfirmed subjective final is derived from teacher points.
        # Old stored finals are not rewritten by reads, startup, or exports.
        try:
            teacher_score = teacher_deterministic_score(scoped_sub_questions, teacher_point_decisions)
            if final_total is not None and abs(final_total - teacher_score['final_total']) > 1e-9:
                raise ValueError('teacher_final_total_mismatch：提交总分与教师逐点计算结果不一致')
            final_total = teacher_score['final_total']
        except (ValueError, TypeError, KeyError):
            conn.close()
            raise
        if final_total < 0 or final_total > maximum:
            conn.close()
            raise ValueError(f"最终分数必须在0—{maximum}之间")
        try:
            ai_suggested_total = float(result.get("suggested_total"))
        except (TypeError, ValueError):
            ai_suggested_total = None
        ai_suggested_total_valid = (
            ai_suggested_total is not None
            and math.isfinite(ai_suggested_total)
            and 0 <= ai_suggested_total <= maximum
        )
        if not ai_suggested_total_valid:
            ai_suggested_total = None
        confirmed_at = utc_now()
        # A later explicit point review supersedes batch adoption, without
        # claiming the prior adoption was a point-by-point teacher review.
        previous_confirmation_method = result.get('confirmation_method')
        if previous_confirmation_method == 'teacher_batch_adoption':
            result['previous_batch_adoption'] = result.pop('batch_adoption', None)
        result['confirmation_method'] = 'teacher_point_review'
        if result.get("teacher_reconfirmation_required"):
            result["teacher_reconfirmation_required"] = False
            result["scope_reconfirmed_at"] = confirmed_at
        dual_review = result.get("dual_review")
        if isinstance(dual_review, dict):
            review_completeness = teacher_review_completeness(scoped_sub_questions, teacher_point_decisions)
            dual_review["teacher_final"] = {
                **teacher_score,
                "final_total": final_total,
                "max_total": maximum,
                "grading_scope": result.get("grading_scope"),
                "teacher_note": teacher_note,
                "point_decisions": teacher_point_decisions,
                "teacher_review_completeness": review_completeness,
                "manual_score_override": (
                    not ai_suggested_total_valid
                    or abs(final_total - ai_suggested_total) > 1e-9
                ),
                "ai_suggested_total_valid": ai_suggested_total_valid,
                "expanded_full_answer": payload.get("expanded_full_answer") is True,
                "confirmed_at": confirmed_at,
                "review_provenance": {
                    "environment": runtime_environment(),
                    "app_version": APP_VERSION,
                    "git_commit": GIT_COMMIT,
                    "data_schema_version": TEACHER_REVIEW_SCHEMA_VERSION,
                },
            }
            conn.execute(
                "UPDATE submissions SET final_total = ?, teacher_note = ?, confirmed_at = ?, result_json = ? WHERE id = ?",
                (final_total, teacher_note, confirmed_at, json.dumps(result, ensure_ascii=False), submission_id),
            )
        else:
            result['teacher_final'] = {**teacher_score, 'point_decisions': teacher_point_decisions,
                                       'teacher_review_completeness': 'point_level_complete', 'confirmed_at': confirmed_at}
            conn.execute(
                "UPDATE submissions SET final_total = ?, teacher_note = ?, confirmed_at = ?, result_json = ? WHERE id = ?",
                (final_total, teacher_note, confirmed_at, json.dumps(result, ensure_ascii=False), submission_id),
            )
        append_audit_event(
            conn,
            "submission_confirmed",
            "submission",
            submission_id,
            {
                "previous_final_total": row["final_total"],
                "previous_confirmation_method": previous_confirmation_method,
                "ai_suggested_total": ai_suggested_total,
                "final_total": final_total,
                "max_total": maximum,
                "graded_question_ids": (result.get("grading_scope") or {}).get("graded_question_ids"),
                "previous_confirmed_at": row["confirmed_at"],
                "manual_reviewed": bool(row["manual_reviewed"]),
                "operation_source": "mobile" if mobile_client else "computer-local",
            },
        )
        conn.commit()
        conn.close()
        clear_submission_photo_viewed(submission_id)
        json_response(self, {"ok": True, "submission_id": submission_id, "final_total": final_total, "max_total": maximum})

    @track_active_task
    def review_submission(self) -> None:
        mobile_client = self.is_mobile_token_client()
        if not self.is_local_client() and not mobile_client:
            json_response(self, {"error": "教师原图复核需要电脑本机或有效手机访问码"}, 403)
            return
        payload = self.read_json()
        submission_id = str(payload.get("submission_id", "")).strip()
        review_note = str(payload.get("review_note", "")).strip()[:2000]
        if payload.get("confirm_original") is not True:
            raise ValueError("请先确认已打开原图并核对证据")
        conn = get_db()
        row = conn.execute(
            "SELECT id, result_json, state, manual_reviewed, source_channel FROM submissions WHERE id = ?",
            (submission_id,),
        ).fetchone()
        if not row:
            conn.close()
            raise ValueError("批改记录不存在")
        if mobile_client and row["source_channel"] != "mobile":
            conn.close()
            raise ValueError("手机访问码只能复核通过手机提交的本份答卷")
        result = json.loads(row["result_json"] or "{}")
        if result.get("simulation_model"):
            conn.close()
            raise ValueError("模拟模型结果仅用于流程验收，不能解锁教师复核或确认为正式成绩")
        if row["state"] != "completed" or result.get("mode") != "vision-model":
            conn.close()
            raise ValueError("只有已完成的视觉模型结果可以进入教师人工复核；规则演示、照片不合格和模型失败结果不能解锁")
        if not submission_photo_was_viewed(submission_id):
            conn.close()
            raise ValueError("请先在当前设备打开至少一张原图并核对证据，再进行人工复核")
        conn.execute(
            "UPDATE submissions SET manual_reviewed = 1, reviewed_at = ?, review_note = ? WHERE id = ?",
            (utc_now(), review_note, submission_id),
        )
        append_audit_event(
            conn,
            "submission_reviewed",
            "submission",
            submission_id,
            {
                "previous_manual_reviewed": bool(row["manual_reviewed"]),
                "manual_reviewed": True,
                "has_note": bool(review_note),
                "operation_source": "mobile" if mobile_client else "computer-local",
            },
        )
        conn.commit()
        conn.close()
        json_response(self, {"ok": True, "submission_id": submission_id, "message": "已记录教师原图复核，可继续修改并确认分数"})

    @track_active_task
    def retry_submission(self) -> None:
        if not self.require_local_client():
            return
        payload = self.read_json()
        submission_id = str(payload.get("submission_id", "")).strip()
        conn = get_db()
        # Serialize retry decisions with BEGIN IMMEDIATE. Without re-reading
        # under the write lock, two simultaneous retry requests can both pass
        # the stale-state checks and enqueue the same failed batch item.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT submissions.id, submissions.result_json, submissions.photos_json, submissions.state,
                   submissions.batch_id, submissions.batch_sequence, submissions.confirmed_at,
                   submissions.ai_privacy_confirmed, submissions.privacy_status,
                   task_queue.attempt_count AS retry_attempt_count, task_queue.updated_at AS queue_updated_at
            FROM submissions
            LEFT JOIN task_queue ON task_queue.submission_id = submissions.id
            WHERE submissions.id = ?
            """,
            (submission_id,),
        ).fetchone()
        if not row:
            conn.close()
            raise ValueError("批改记录不存在")
        result = json.loads(row["result_json"] or "{}")
        configuration_recovery = result.get('mode')=='rules-demo' or (result.get('diagnostics') or {}).get('error_category')=='model_not_configured'
        if configuration_recovery:
            if payload.get('confirm_model_retry') is not True:
                conn.close()
                raise ValueError('请明确确认恢复真实模型评分、外发图隐私与可能产生的费用')
            if row['confirmed_at'] or not row['ai_privacy_confirmed'] or row['privacy_status'] not in PRIVACY_STATUS_APPROVED:
                conn.close()
                raise ValueError('该份缺少有效隐私确认或已有正式成绩，不能直接恢复')
            try:
                if result.get('dual_review_requested') or isinstance(result.get('dual_review'),dict):
                    load_dual_review_model_configs()
                elif not model_configured():
                    raise ValueError('请先到系统管理配置主模型；尚未发起模型请求')
            except Exception:
                conn.close()
                raise
        try:
            has_photos = bool(json.loads(row["photos_json"] or "[]"))
        except json.JSONDecodeError:
            has_photos = False
        retryable, retry_reason = manual_retry_policy(result, row["state"], has_photos)
        if not retryable:
            conn.close()
            raise ValueError(f"当前记录不允许重试：{retry_reason}")
        if row["state"] not in {"completed", "failed", "recovery_pending"}:
            conn.close()
            raise ValueError("该记录当前不允许重试")
        if row["retry_attempt_count"] is None:
            conn.close()
            raise ValueError("任务队列记录不存在，无法安全重试；请重新提交照片")
        wait_seconds = manual_retry_backoff_seconds(
            int(row["retry_attempt_count"] or 0), str(row["queue_updated_at"] or "")
        )
        if wait_seconds > 0:
            conn.close()
            raise ValueError(f"为避免重复请求，请等待约 {wait_seconds} 秒后再重试")
        batch_id = str(row["batch_id"] or "")
        if batch_id:
            batch = conn.execute("SELECT * FROM grading_batches WHERE id = ?", (batch_id,)).fetchone()
            from batch_grading_control import stopped
            if stopped(batch):
                conn.close()
                raise ValueError('本批已停止，请先明确继续剩余批改')
            if (
                not batch
                or batch["status"] != "paused"
                or int(batch["next_sequence"]) != int(row["batch_sequence"] or 0)
                or int(batch["pause_sequence"] or 0) != int(row["batch_sequence"] or 0)
            ):
                conn.close()
                raise ValueError("批次只能在当前暂停序号原位重试")
        try:
            previous_result = json.loads(row["result_json"] or "{}")
        except (TypeError, ValueError):
            previous_result = {}
        retry_result = {"mode": "queued", "blocked": True, "needs_review": True, "summary": "已重新加入本机批改队列。"}
        if isinstance(previous_result, dict) and (previous_result.get("dual_review_requested") is True or isinstance(previous_result.get('dual_review'),dict)):
            retry_result["dual_review_requested"] = True
        if configuration_recovery:
            retry_result['model_required'] = True
        now = utc_now()
        conn.execute(
            "UPDATE submissions SET state = 'queued', result_json = ?, uncertain_model_call = 0, manual_reviewed = 0, reviewed_at = NULL, review_note = '' WHERE id = ?",
            (json.dumps(retry_result, ensure_ascii=False), submission_id),
        )
        conn.execute(
            "UPDATE task_queue SET state = 'queued', attempt_count = attempt_count + 1, available_at = ?, last_error = '', updated_at = ? WHERE submission_id = ?",
            (now, now, submission_id),
        )
        if batch_id:
            conn.execute(
                "UPDATE grading_batches SET status = 'active', pause_sequence = NULL, pause_reason = '', updated_at = ? WHERE id = ? AND next_sequence = ?",
                (now, batch_id, int(row["batch_sequence"])),
            )
        append_audit_event(
            conn,
            "submission_retry_requested",
            "submission",
            submission_id,
            {"previous_mode": result.get("mode"), "retry_reason": retry_reason},
        )
        conn.commit()
        conn.close()
        enqueue_submission(submission_id)
        json_response(self, {"ok": True, "submission_id": submission_id, "message": "已重新加入本机批改队列"})

    def export_csv(self, assignment_id: str) -> None:
        if not self.require_local_client():
            return
        conn = get_db()
        assignment = conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
        if not assignment:
            conn.close()
            json_response(self, {"error": "作业不存在"}, 404)
            return
        rows = conn.execute(
            "SELECT submissions.*, codes.binding_status AS code_binding_status, "
            "codes.roster_student_id AS code_roster_student_id, "
            "students.class_name AS mapped_class_name, students.student_no AS mapped_student_no, "
            "students.student_name AS mapped_student_name, "
            "(SELECT COUNT(*) FROM exam_marking_codes AS duplicates "
            " WHERE duplicates.exam_id = codes.exam_id AND duplicates.roster_student_id = codes.roster_student_id) AS code_binding_count "
            "FROM submissions LEFT JOIN grading_batches ON grading_batches.id = submissions.batch_id "
            "LEFT JOIN exam_marking_codes AS codes ON codes.id = submissions.marking_code_id "
            "LEFT JOIN roster_students AS students ON students.id = codes.roster_student_id "
            "WHERE submissions.assignment_id = ? "
            "ORDER BY COALESCE(grading_batches.created_at, submissions.created_at) ASC, "
            "CASE WHEN submissions.batch_id IS NULL THEN 0 ELSE 1 END, "
            "CASE WHEN submissions.batch_id IS NOT NULL THEN submissions.batch_sequence END ASC, "
            "submissions.created_at ASC",
            (assignment_id,),
        ).fetchall()
        conn.close()
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        saved_sub_questions = json.loads(assignment["sub_questions_json"] or "[]")
        sub_headers = [f"{item['label']} AI建议分" for item in saved_sub_questions]
        writer.writerow(["班级", "座号", "学生", "最终确认分", "AI建议总分", "满分", *sub_headers, "批改模式", "是否已确认", "评语摘要", "提交时间", "批次编号", "批次原始序号", "答卷页序列", "确认方式"])
        for row in rows:
            try:
                result = json.loads(row["result_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(result, dict):
                continue
            from batch_adoption import current_adoption
            if not current_adoption(result, assignment):
                continue
            # The formal gradebook contains only explicitly confirmed, completed
            # vision-model records. A blocked result is exportable only after a
            # traceable local manual review and confirmation.
            from teacher_total import current_confirmation
            total_confirmed = current_confirmation(row, assignment, sys.modules[__name__])
            if (
                not row["confirmed_at"]
                or row["state"] != "completed"
                or (result.get("confirmation_method") in ("teacher_total_confirmation", "automatic_blank_zero") and not total_confirmed)
                or (not total_confirmed and (result.get("mode") != "vision-model" or (result.get("blocked") and not row["manual_reviewed"])))
            ):
                continue
            anonymous_record = bool(row["marking_code_id"])
            try:
                final_total = float(row["final_total"])
                assignment_max = export_scope_maximum(saved_sub_questions, json.loads(assignment["criteria_json"] or "[]"), result)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            try:
                ai_total = (float(result["suggested_total"]) if result.get("suggested_total") is not None else None) if total_confirmed else float(result.get("suggested_total"))
            except (TypeError, ValueError):
                continue
            if (
                not math.isfinite(final_total)
                or not math.isfinite(assignment_max)
                or assignment_max <= 0
                or not (0 <= final_total <= assignment_max)
                or (ai_total is not None and (not math.isfinite(ai_total) or not (0 <= ai_total <= assignment_max)))
            ):
                continue
            raw_sub_results = result.get("sub_questions", [])
            if not isinstance(raw_sub_results, list):
                raw_sub_results = []
            sub_results = {
                item.get("sub_question_id"): item
                for item in raw_sub_results
                if isinstance(item, dict) and item.get("sub_question_id")
            }
            sub_scores = []
            for sub_question in saved_sub_questions:
                sub_result = sub_results.get(sub_question["id"], {})
                try:
                    sub_total = float(sub_result.get("suggested_total"))
                    sub_maximum = float(sub_question.get("max_score"))
                except (TypeError, ValueError):
                    sub_scores.append("")
                    continue
                sub_scores.append(
                    sub_total
                    if math.isfinite(sub_total)
                    and math.isfinite(sub_maximum)
                    and sub_maximum > 0
                    and 0 <= sub_total <= sub_maximum
                    else ""
                )

            def csv_text(value: Any) -> str:
                text = str(value or "")
                if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
                    return "'" + text
                return text

            writer.writerow(
                [
                    csv_text(row["mapped_class_name"] if anonymous_record else row["class_name"]),
                    csv_text(row["mapped_student_no"] if anonymous_record else row["student_no"]),
                    csv_text(row["mapped_student_name"] if anonymous_record else row["student_label"]),
                    final_total,
                    ai_total,
                    assignment_max,
                    *sub_scores,
                    result.get("mode", ""),
                    "是" if row["confirmed_at"] else "否",
                    csv_text(result.get("summary", "")),
                    row["created_at"],
                    row["batch_id"] or "",
                    row["batch_sequence"] if row["batch_sequence"] is not None else "",
                    ",".join(
                        str(item.get("page_sequence"))
                        for item in json.loads(row["photos_json"] or "[]")
                        if isinstance(item, dict) and item.get("page_sequence") is not None
                    ),
                    "批量采纳 AI 共识（未逐点评阅）" if result.get('confirmation_method') == 'teacher_batch_adoption' else "教师总分终评（未逐点确认）" if result.get("confirmation_method")=="teacher_total_confirmation" else "双模型明确空白，自动0分" if result.get("confirmation_method")=="automatic_blank_zero" else "教师终评",
                ]
            )
        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        # Header values should stay ASCII-compatible across browsers and PowerShell.
        filename = f"history_grading_{assignment_id}.csv"
        write_http_response(
            self,
            body,
            "text/csv; charset=utf-8",
            extra_headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def export_anonymous_csv(self, assignment_id: str) -> None:
        if not self.require_local_client():
            return
        if not re.fullmatch(r"[0-9a-f]{12}", str(assignment_id or "")):
            json_response(self, {"error": "作业记录编号无效"}, 400)
            return
        conn = get_db()
        assignment = conn.execute(
            "SELECT id, question, standard_answer, sub_questions_json, criteria_json FROM assignments WHERE id = ?",
            (assignment_id,),
        ).fetchone()
        if not assignment:
            conn.close()
            json_response(self, {"error": "作业不存在"}, 404)
            return
        rows = conn.execute(
            "SELECT submissions.*, COALESCE(codes.code,submissions.id) AS anonymous_code "
            "FROM submissions "
            "LEFT JOIN exam_marking_codes AS codes ON codes.id = submissions.marking_code_id "
            "LEFT JOIN grading_batches ON grading_batches.id = submissions.batch_id "
            "WHERE submissions.assignment_id = ? "
            "ORDER BY COALESCE(grading_batches.created_at, submissions.created_at) ASC, "
            "CASE WHEN submissions.batch_id IS NULL THEN 0 ELSE 1 END, "
            "CASE WHEN submissions.batch_id IS NOT NULL THEN submissions.batch_sequence END ASC, "
            "submissions.created_at ASC",
            (assignment_id,),
        ).fetchall()
        conn.close()

        output = io.StringIO(newline="")
        writer = csv.writer(output)
        saved_sub_questions = json.loads(assignment["sub_questions_json"] or "[]")
        def csv_text(value: Any) -> str:
            text = str(value or "")
            if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
                return "'" + text
            return text

        sub_headers = [f"{csv_text(item['label'])} AI建议分" for item in saved_sub_questions]
        writer.writerow(["随机阅卷码", "最终确认分", "AI建议总分", "满分", *sub_headers, "确认方式"])
        for row in rows:
            try:
                result = json.loads(row["result_json"] or "{}")
            except json.JSONDecodeError:
                continue
            from batch_adoption import current_adoption
            if not isinstance(result, dict) or not current_adoption(result, assignment):
                continue
            from teacher_total import current_confirmation
            total_confirmed = current_confirmation(row, assignment, sys.modules[__name__])
            if (
                not isinstance(result, dict)
                or not row["confirmed_at"]
                or row["state"] != "completed"
                or (result.get("confirmation_method") in ("teacher_total_confirmation", "automatic_blank_zero") and not total_confirmed)
                or (not total_confirmed and (result.get("mode") != "vision-model" or (result.get("blocked") and not row["manual_reviewed"])))
            ):
                continue
            try:
                final_total = float(row["final_total"])
                ai_total = (float(result["suggested_total"]) if result.get("suggested_total") is not None else None) if total_confirmed else float(result.get("suggested_total"))
                maximum = export_scope_maximum(saved_sub_questions, json.loads(assignment["criteria_json"] or "[]"), result)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                not all(math.isfinite(value) for value in (final_total, maximum))
                or (ai_total is not None and not math.isfinite(ai_total))
                or maximum <= 0
                or not (0 <= final_total <= maximum)
                or (ai_total is not None and not (0 <= ai_total <= maximum))
                or not row["anonymous_code"]
            ):
                continue
            raw_sub_results = result.get("sub_questions", [])
            if not isinstance(raw_sub_results, list):
                raw_sub_results = []
            sub_results = {
                item.get("sub_question_id"): item
                for item in raw_sub_results
                if isinstance(item, dict) and item.get("sub_question_id")
            }
            sub_scores = []
            for sub_question in saved_sub_questions:
                sub_result = sub_results.get(sub_question["id"], {})
                try:
                    score = float(sub_result.get("suggested_total"))
                    sub_maximum = float(sub_question.get("max_score"))
                except (TypeError, ValueError):
                    sub_scores.append("")
                    continue
                sub_scores.append(
                    score
                    if math.isfinite(score)
                    and math.isfinite(sub_maximum)
                    and sub_maximum > 0
                    and 0 <= score <= sub_maximum
                    else ""
                )
            writer.writerow([row["anonymous_code"], final_total, ai_total, maximum, *sub_scores, "批量采纳 AI 共识（未逐点评阅）" if result.get("confirmation_method") == "teacher_batch_adoption" else "教师总分终评（未逐点确认）" if result.get("confirmation_method")=="teacher_total_confirmation" else "双模型明确空白，自动0分" if result.get("confirmation_method")=="automatic_blank_zero" else "教师终评"])

        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        filename = f"history_grading_anonymous_{assignment_id}.csv"
        write_http_response(
            self,
            body,
            "text/csv; charset=utf-8",
            extra_headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


from web_auth import apply_single_teacher_auth

AppHandler = apply_single_teacher_auth(AppHandler)


def _objective_vision_prompt(answer_key: list[dict[str, Any]], image_count: int) -> str:
    key = [
        {
            "question_no": int(row["question_no"]),
            "correct_option": str(row["correct_option"]).upper(),
            "points": float(row["points"]),
        }
        for row in answer_key
    ]
    return (
        "你是客观题答卷的选项识别助手。请只读取学生答卷照片中学生实际选择的选项，不要替学生解题，不要根据标准答案推测学生选择。\n"
        f"图片按上传顺序编号为第1至第{image_count}页。请按题号识别每题的作答。\n"
        "标准答案仅供最后核对；即使能看出标准答案，也必须单独判断照片上学生实际标记了什么。忽略姓名、学校、班级、学号等身份信息，不要抄录或返回身份信息。\n"
        "每题 status 只能是 answered（清楚选择一个选项）、blank（清楚未作答）、uncertain（有涂改/多选/标记不清）、question_unmatched（照片中找不到该题）。"
        "有多个选项均被标记时用 uncertain。不得把未看清当作 blank。\n"
        "只返回 JSON 对象，不要 Markdown。格式："
        '{"answers":[{"question_no":1,"status":"answered","selected_option":"A","page_no":1,"confidence":0.95,"mark_evidence":"第1页第1题A选项有明显勾选"}]}\n'
        "blank/uncertain/question_unmatched 时 selected_option 返回 null。confidence 为0到1之间的估计值；无法估计则 null。mark_evidence 只简短描述选项标记，不转录题目、学生姓名或其他文字。\n"
        "本次题号与标准答案如下：\n"
        + json.dumps(key, ensure_ascii=False, separators=(",", ":"))
    )


def _normalize_objective_vision_answers(payload: dict[str, Any], answer_key: list[dict[str, Any]], image_count: int) -> dict[str, dict[str, Any]]:
    expected = {int(row["question_no"]) for row in answer_key}
    result: dict[str, dict[str, Any]] = {}
    duplicates: set[int] = set()
    rows = payload.get("answers")
    if not isinstance(rows, list):
        raise ValueError("客观题模型结果缺少 answers 数组")
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            qno = int(row.get("question_no"))
        except (TypeError, ValueError):
            continue
        if qno not in expected:
            continue
        if qno in result:
            duplicates.add(qno)
            continue
        status = str(row.get("status") or "").strip().casefold()
        option = str(row.get("selected_option") or "").strip().upper()
        if status not in {"answered", "blank", "uncertain", "question_unmatched"}:
            status = "answered" if option in {"A", "B", "C", "D"} else "uncertain"
        if status == "answered" and option not in {"A", "B", "C", "D"}:
            status, option = "uncertain", ""
        if status != "answered":
            option = ""
        confidence = row.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not math.isfinite(float(confidence)):
            confidence = None
        else:
            confidence = max(0.0, min(1.0, float(confidence)))
        page_no = row.get("page_no")
        if not isinstance(page_no, int) or isinstance(page_no, bool) or not 1 <= page_no <= image_count:
            page_no = None
        result[qno] = {
            "status": status,
            "option": option or None,
            "page_no": page_no,
            "confidence": confidence,
            "mark_evidence": str(row.get("mark_evidence") or "")[:160],
        }
    for qno in expected:
        if qno in duplicates:
            result[qno] = {"status": "uncertain", "option": None, "page_no": None, "confidence": None, "mark_evidence": "模型重复返回该题，需人工复核"}
        elif qno not in result:
            result[qno] = {"status": "question_unmatched", "option": None, "page_no": None, "confidence": None, "mark_evidence": "模型未返回该题"}
    return {str(qno): result[qno] for qno in sorted(result)}


def _objective_vision_model_call(
    side: str,
    model_config: dict[str, str],
    prompt: str,
    model_images: list[bytes],
    image_metadata: list[dict[str, Any]],
    answer_key: list[dict[str, Any]],
    prompt_hash: str,
    input_hash: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    provider = str(model_config["provider"]).strip().casefold()
    model = str(model_config["model"])
    base_url, _origin = validate_model_base_url(str(model_config["base_url"]))
    endpoint = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for image_bytes in model_images:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
    output_limit_type = "max_completion_tokens" if provider in {"qwen", "mimo"} else "max_tokens"
    output_limit_value = configured_model_max_tokens()
    request_payload: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        output_limit_type: output_limit_value,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "只按输出结构读取客观题学生选项；禁止推测未显示答案。"},
            {"role": "user", "content": content},
        ],
    }
    request_payload.update(deepseek_json_payload_options(provider, model))
    diagnostics: dict[str, Any] = {}
    timeout = float(os.getenv("HISTORY_GRADER_TIMEOUT", "180"))
    metadata = {
        "side": side,
        "provider": provider,
        "model_id": model,
        "invocation_scope": "whole_paper",
        "output_limit_type": output_limit_type,
        "output_limit_value": output_limit_value,
        "timeout_seconds": timeout,
        "prompt_hash": prompt_hash,
        "input_hash": input_hash,
        "image_count": len(image_metadata),
        "images": image_metadata,
    }
    request_body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {model_config['api_key']}"}
    model_request = Request(endpoint, data=request_body, headers=headers, method="POST")
    try:
        with open_model_request(model_request, timeout=timeout) as response:
            text = read_model_chat_content(response, diagnostics)
        parsed = extract_json(text)
        answers = _normalize_objective_vision_answers(parsed, answer_key, len(model_images))
        status = "ok"
        error = None
    except Exception as exc:
        error = classify_model_error(exc)
        status = "failed"
        answers = {
            str(int(row["question_no"])): {
                "status": "question_unmatched", "option": None, "page_no": None,
                "confidence": None, "mark_evidence": "模型调用或结果校验失败",
            }
            for row in answer_key
        }
    return {
        **metadata,
        "run_status": status,
        "latency_ms": elapsed_ms(started),
        "http_status": diagnostics.get("http_status") or (error or {}).get("http_status"),
        "provider_response_id": diagnostics.get("provider_response_id"),
        "finish_reason": diagnostics.get("finish_reason"),
        "token_usage": diagnostics.get("token_usage"),
        "error": error,
        "answers": answers,
    }


def grade_objective_submission_dual(image_paths: list[Path], answer_key: list[dict[str, Any]]) -> dict[str, Any]:
    """Read objective responses with two independent vision calls, then let the local code score."""
    if not image_paths:
        raise ValueError("客观题答卷照片为空")
    configs = load_dual_review_model_configs(required_sides=("A", "B"))
    model_images = [prepare_model_image(path) for path in image_paths]
    image_metadata: list[dict[str, Any]] = []
    input_fingerprint = hashlib.sha256()
    for image_bytes in model_images:
        with Image.open(io.BytesIO(image_bytes)) as image:
            width, height = image.size
        digest = hashlib.sha256(image_bytes).hexdigest()
        image_metadata.append({"width": width, "height": height, "bytes": len(image_bytes), "sha256": digest})
        input_fingerprint.update(len(image_bytes).to_bytes(8, "big"))
        input_fingerprint.update(bytes.fromhex(digest))
    prompt = _objective_vision_prompt(answer_key, len(model_images))
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    input_fingerprint.update(prompt.encode("utf-8"))
    input_hash = input_fingerprint.hexdigest()
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="objective-vision") as executor:
        futures = {
            executor.submit(
                _objective_vision_model_call, side, configs[side], prompt,
                model_images, image_metadata, answer_key, prompt_hash, input_hash,
            ): side
            for side in ("A", "B")
        }
        for future in as_completed(futures):
            side = futures[future]
            try:
                results[side] = future.result()
            except Exception as exc:
                results[side] = {
                    "side": side, "provider": configs[side]["provider"], "model_id": configs[side]["model"],
                    "run_status": "failed", "latency_ms": None, "error": classify_model_error(exc),
                    "answers": {}, "prompt_hash": prompt_hash, "input_hash": input_hash,
                }
    return {"models": {side: results.get(side, {}) for side in ("A", "B")}}


def objective_vision_status() -> dict[str, Any]:
    specs = dual_review_model_specs()
    models = []
    for side in ("A", "B"):
        spec = specs[side]
        models.append({
            "side": side,
            "provider": spec["provider_label"],
            "model": spec["model"],
            "configured": dual_review_key_is_configured(side),
        })
    return {"enabled": True, "available": all(row["configured"] for row in models), "models": models}


def create_integrated_objective_wsgi_app():
    """Initialize namespaced objective tables and return its embedded WSGI app."""
    from objective.integration import create_integrated_app, initialize_shared_database

    objective_data_dir = DATA_DIR / "objective"
    initialize_shared_database(DB_PATH, set_journal_mode=False)
    from unified_exam import read_source_exam
    return create_integrated_app(
        str(objective_data_dir), str(DB_PATH),
        source_exam_provider=lambda exam_id=None: read_source_exam(DB_PATH, exam_id),
        vision_grader=grade_objective_submission_dual,
        vision_status=objective_vision_status,
    )


def _is_loopback_bind_host(host: str) -> bool:
    normalized = str(host or "").strip().strip("[]").split("%", 1)[0].casefold()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def main() -> int:
    global TASK_DRAINING
    parser = argparse.ArgumentParser(description="历史学科 AI 双评阅卷系统")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址；手机访问时使用 0.0.0.0")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument(
        "--cloud-mode",
        action="store_true",
        help="即使只监听本机，也强制启用网页身份认证（例如由反向代理提供 HTTPS）",
    )
    parser.add_argument(
        "--portable-root",
        default=None,
        help="便携数据和普通配置根目录；未指定时使用程序目录",
    )
    args = parser.parse_args()
    if args.cloud_mode:
        os.environ["HISTORY_GRADER_CLOUD_MODE"] = "1"
    configure_paths(args.portable_root)
    portable_instance_id = ensure_portable_instance_id()
    service_lock = ServiceLock(SERVICE_LOCK_PATH)
    try:
        service_lock.acquire()
    except SingleInstanceError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2
    server: ThreadingHTTPServer | None = None
    try:
        load_runtime_configuration()
        get_db().close()
        objective_wsgi_app = create_integrated_objective_wsgi_app()
        try:
            server = PortableHTTPServer((args.host, args.port), AppHandler)
        except OSError as exc:
            print(
                f"启动失败：端口 {args.port} 无法监听，可能已被其他程序占用；请关闭占用端口的程序后重试。",
                file=sys.stderr,
            )
            return 3
        server.restarting = False
        server.stopping = False
        server.objective_wsgi_app = objective_wsgi_app
        start_grade_worker()
        write_service_info(args.host, args.port, portable_instance_id)
        print(f"历史学科 AI 双评阅卷系统已启动: http://{args.host}:{args.port}")
        print(f"程序资源目录: {PROGRAM_DIR}")
        print(f"便携根目录: {PORTABLE_ROOT}")
        print(f"数据目录: {DATA_DIR}")
        print(f"配置目录: {CONFIG_DIR}")
        print(f"便携实例: {portable_instance_id}")
        print(f"视觉模型: {'已配置' if model_configured() else '未配置（规则演示模式）'}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n正在退出")
        return 0
    finally:
        stop_grade_worker()
        if server is not None:
            server.server_close()
        remove_service_info(os.getpid())
        service_lock.release()
        with ACTIVE_TASKS_CONDITION:
            TASK_DRAINING = False
            ACTIVE_TASKS_CONDITION.notify_all()


if __name__ == "__main__":
    if any(arg.startswith("--restart-helper-pid=") for arg in sys.argv[1:]):
        raise SystemExit(run_restart_helper())
    else:
        raise SystemExit(main())
