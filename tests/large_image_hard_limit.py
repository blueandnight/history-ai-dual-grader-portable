"""Run the large-image pipeline inside a hard 384 MiB process limit.

This is a local Windows acceptance probe for the ECS cgroup budget.  The
child is placed in a Job Object with both per-process and per-job commit
limits, so exceeding the budget fails inside the child instead of OOM-killing
the whole service.  Peak working set is reported only when the child survives;
a non-zero exit or allocation failure is itself a test result.

Usage:
    python -B tests/large_image_hard_limit.py --cases multipart-40mp,direct-40mp
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import ctypes
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

MIB = 1024 * 1024
DEFAULT_LIMIT_MIB = 384
CORNERS = [[0.02, 0.02], [0.98, 0.02], [0.98, 0.98], [0.02, 0.98]]
REDACTIONS = [{"x": 0.04, "y": 0.03, "width": 0.24, "height": 0.08}]


class ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class JobIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JobBasicLimitInformation),
        ("IoInfo", JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def cgroup_memory_peak_bytes() -> int:
    """Read this process's cgroup v2 peak when running under a Linux scope."""
    try:
        path = ""
        for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "0":
                path = parts[2]
                break
        for name in ("memory.peak", "memory.max_usage_in_bytes"):
            candidate = Path("/sys/fs/cgroup") / path.lstrip("/") / name
            if candidate.exists():
                return int(candidate.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    return 0


def working_set_bytes() -> int:
    if os.name != "nt":
        return 0
    from ctypes import wintypes

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise OSError("GetProcessMemoryInfo failed")
    return int(counters.PeakWorkingSetSize)


def run_with_job_limit(command: list[str], limit_bytes: int) -> subprocess.CompletedProcess:
    if os.name != "nt":
        if not shutil.which("systemd-run"):
            return subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        unit = f"history-grader-hardlimit-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        wrapped = [
            "systemd-run",
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={unit}",
            "-p",
            f"MemoryMax={limit_bytes}",
            "-p",
            "MemorySwapMax=0",
            *command,
        ]
        completed = subprocess.run(
            wrapped, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        return subprocess.CompletedProcess(command, completed.returncode, completed.stdout, completed.stderr)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    try:
        info = JobExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            0x0100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | 0x0200  # JOB_OBJECT_LIMIT_JOB_MEMORY
            | 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        info.ProcessMemoryLimit = limit_bytes
        info.JobMemoryLimit = limit_bytes
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        child = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if not kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(child._handle)):  # type: ignore[attr-defined]
            error = ctypes.get_last_error()
            child.kill()
            stdout, stderr = child.communicate()
            raise OSError(error, f"AssignProcessToJobObject failed; stdout={stdout!r} stderr={stderr!r}")
        stdout, stderr = child.communicate()
        return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
    finally:
        kernel32.CloseHandle(job)


def make_noise_jpeg(path: Path, width: int, height: int, quality: int = 70) -> None:
    """Create a deliberately hard-to-compress JPEG near the upload limit."""
    raw = os.urandom(width * height)
    image = Image.frombytes("L", (width, height), raw).convert("RGB")
    image.save(path, format="JPEG", quality=quality, optimize=True)
    image.close()




def make_40mp_png(path: Path, width: int = 8000, height: int = 5000) -> None:
    image = Image.new("L", (width, height), 255)
    image.save(path, format="PNG")
    image.close()


def build_multipart_body(
    original_bytes: bytes,
    processed_bytes: bytes,
    original_name: str = "page_01.jpg",
    processed_name: str = "page_01.jpg.processed.jpg",
    processed_media_type: str = "image/jpeg",
) -> tuple[bytes, str]:
    boundary = "----history-grader-hard-limit"
    metadata = json.dumps(
        {
            "assignment_id": "a1",
            "photos": [{"name": original_name, "page_sequence": 1, "corners": CORNERS}],
        },
        ensure_ascii=False,
    ).encode("utf-8")

    def part(name: str, filename: str, media_type: str, payload: bytes) -> bytes:
        return (
            f"--{boundary}\r\n".encode()
            + f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
            + f"Content-Type: {media_type}\r\n\r\n".encode()
            + payload
            + b"\r\n"
        )

    body = b"".join(
        [
            (
                f"--{boundary}\r\n".encode()
                + b'Content-Disposition: form-data; name="metadata"\r\n'
                + b"Content-Type: application/json\r\n\r\n"
                + metadata
                + b"\r\n"
            ),
            part("processed_0", processed_name, processed_media_type, processed_bytes),
            part("original_0", original_name, "image/jpeg", original_bytes),
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return body, f'multipart/form-data; boundary="{boundary}"'


def worker(case: str, fixture_dir: Path, output_dir: Path) -> int:
    import app

    started = time.perf_counter()
    baseline = working_set_bytes()
    original_bytes = b""
    processed_bytes = b""
    if case in {
        "multipart-40mp",
        "concurrent-multipart-40mp",
        "multipart-20mp-png",
        "direct-40mp",
    }:
        original_bytes = (fixture_dir / "original.jpg").read_bytes()
        processed_bytes = (fixture_dir / "processed.jpg").read_bytes()
    if case == "multipart-16mp-same":
        original_bytes = (fixture_dir / "original-16mp.jpg").read_bytes()
        processed_bytes = original_bytes
    if case == "multipart-20mp-png":
        processed_bytes = (fixture_dir / "processed-20mp.png").read_bytes()
    target = output_dir / "stored_page.jpg"
    original_target = output_dir / "stored_page.original"

    if case == "concurrent-multipart-40mp":
        body, content_type = build_multipart_body(original_bytes, processed_bytes)

        def process_one(index: int) -> None:
            print(f"[concurrent] start {index}", flush=True)
            with app.UPLOAD_SEMAPHORE:
                print(f"[concurrent] acquired {index}", flush=True)

                def handle_request() -> None:
                    photo = app.parse_multipart_submission(body, content_type)["photos"][0]
                    raw, media_type = app.photo_upload_bytes(photo)
                    preview = app.prepare_ai_preview_image_bytes(
                        raw, media_type, REDACTIONS, photo.get("corners")
                    )
                    page_target = output_dir / f"stored_page_{index}.jpg"
                    page_original = output_dir / f"stored_page_{index}.original"
                    meta = app.save_photo_item(photo, page_target, page_original, photo.get("corners"))
                    model = app.prepare_model_image(page_target, REDACTIONS)
                    if preview != model:
                        raise AssertionError("concurrent preview and model image differ")
                    if page_original.read_bytes() != original_bytes:
                        raise AssertionError("concurrent stored original differs from upload")
                    if int(meta["width"]) < 1 or int(meta["height"]) < 1:
                        raise AssertionError("concurrent stored page has invalid dimensions")

                handle_request()
                app.release_image_memory()
                print(
                    f"[concurrent] finished {index} peak={cgroup_memory_peak_bytes() / MIB:.2f}MiB",
                    flush=True,
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(process_one, (1, 2)))
        result = {
            "case": case,
            "result": "passed",
            "concurrent_requests": 2,
            "cgroup_peak_mib": round(cgroup_memory_peak_bytes() / MIB, 2),
            "multipart_body_bytes": len(body),
            "seconds": round(time.perf_counter() - started, 3),
        }
        print("HARD_LIMIT_JSON=" + json.dumps(result, ensure_ascii=False), flush=True)
        return 0

    if case in {"multipart-20mp-png", "json-20mp-png"}:
        if case == "multipart-20mp-png":
            body, content_type = build_multipart_body(
                original_bytes,
                processed_bytes,
                "page_01.jpg",
                "page_01.png",
                "image/png",
            )
            photo = app.parse_multipart_submission(body, content_type)["photos"][0]
            action = lambda: app.save_photo_item(photo, target, original_target, photo.get("corners"))
        else:
            source_bytes = (fixture_dir / "processed-20mp.png").read_bytes()
            data_url = app.bytes_to_data_url(source_bytes, "image/png")
            action = lambda: app.save_photo(data_url, target, original_target, CORNERS, data_url)
        try:
            action()
        except ValueError as exc:
            if "1500万像素" not in str(exc):
                raise
            result = {
                "case": case,
                "result": "passed",
                "action": "rejected_before_full_decode",
                "error": str(exc),
                "cgroup_peak_mib": round(cgroup_memory_peak_bytes() / MIB, 2),
                "seconds": round(time.perf_counter() - started, 3),
            }
            print("HARD_LIMIT_JSON=" + json.dumps(result, ensure_ascii=False), flush=True)
            return 0
        raise AssertionError("20MP PNG was not rejected")

    if case in {"multipart-40mp", "multipart-16mp-same"}:
        body, content_type = build_multipart_body(original_bytes, processed_bytes)
        payload = app.parse_multipart_submission(body, content_type)
        photo = payload["photos"][0]
        if "data_url" in photo or "original_data_url" in photo:
            raise AssertionError("multipart parser still materialized Base64 data URLs")
        raw, media_type = app.photo_upload_bytes(photo)
        original_raw, original_media_type = app.photo_upload_bytes(photo, original=True)
        if raw != processed_bytes or original_raw != original_bytes:
            raise AssertionError("multipart parser changed image bytes")
        meta = app.save_photo_item(photo, target, original_target, photo.get("corners"))
        preview = app.prepare_ai_preview_image_bytes(raw, media_type, REDACTIONS, photo.get("corners"))
        model = app.prepare_model_image(target, REDACTIONS)
    elif case == "direct-40mp":
        original_url = app.bytes_to_data_url(original_bytes, "image/jpeg")
        processed_url = app.bytes_to_data_url(processed_bytes, "image/jpeg")
        meta = app.save_photo(processed_url, target, original_target, CORNERS, original_url)
        preview = app.prepare_ai_preview_image(processed_url, REDACTIONS, CORNERS)
        model = app.prepare_model_image(target, REDACTIONS)
    elif case == "json-40mp-decode":
        original_bytes = (fixture_dir / "original.jpg").read_bytes()
        processed_bytes = original_bytes
        data_url = app.bytes_to_data_url(original_bytes, "image/jpeg")
        meta = app.save_photo(data_url, target, original_target, CORNERS, data_url)
        preview = app.prepare_ai_preview_image(data_url, REDACTIONS, CORNERS)
        model = app.prepare_model_image(target, REDACTIONS)
    elif case in {
        "json-12mp-jpeg",
        "json-16mp-jpeg",
        "json-20mp-jpeg",
        "json-24mp-jpeg",
        "json-30mp-jpeg",
    }:
        if case == "json-12mp-jpeg":
            source_bytes = (fixture_dir / "original-12mp.jpg").read_bytes()
            source_media = "image/jpeg"
        elif case == "json-20mp-jpeg":
            source_bytes = (fixture_dir / "original-20mp.jpg").read_bytes()
            source_media = "image/jpeg"
        elif case == "json-16mp-jpeg":
            source_bytes = (fixture_dir / "original-16mp.jpg").read_bytes()
            source_media = "image/jpeg"
        elif case == "json-24mp-jpeg":
            source_bytes = (fixture_dir / "original-24mp.jpg").read_bytes()
            source_media = "image/jpeg"
        elif case == "json-30mp-jpeg":
            source_bytes = (fixture_dir / "original-30mp.jpg").read_bytes()
            source_media = "image/jpeg"
        else:
            source_bytes = (fixture_dir / "processed-20mp.png").read_bytes()
            source_media = "image/png"
        data_url = app.bytes_to_data_url(source_bytes, source_media)
        meta = app.save_photo(data_url, target, original_target, CORNERS, data_url)
        preview = app.prepare_ai_preview_image(data_url, REDACTIONS, CORNERS)
        model = app.prepare_model_image(target, REDACTIONS)
        processed_bytes = source_bytes
        original_bytes = source_bytes
    elif case == "reject-40mp-png":
        png_bytes = (fixture_dir / "original.png").read_bytes()
        try:
            app.decode_image_for_processing(png_bytes, "image/png")
        except ValueError as exc:
            if "1500万像素" not in str(exc):
                raise
            result = {
                "case": case,
                "result": "passed",
                "action": "rejected_before_full_decode",
                "error": str(exc),
                "baseline_peak_mib": round(baseline / MIB, 2),
                "peak_working_set_mib": round(working_set_bytes() / MIB, 2),
                "peak_delta_mib": round((working_set_bytes() - baseline) / MIB, 2),
                "cgroup_peak_mib": round(cgroup_memory_peak_bytes() / MIB, 2),
                "seconds": round(time.perf_counter() - started, 3),
            }
            print("HARD_LIMIT_JSON=" + json.dumps(result, ensure_ascii=False), flush=True)
            return 0
        raise AssertionError("40MP PNG was not rejected")
    else:
        raise SystemExit(f"unknown case: {case}")

    if preview != model:
        raise AssertionError("privacy preview and later model image differ")
    if not target.exists() or not original_target.exists():
        raise AssertionError("stored page or original is missing")
    if original_target.read_bytes() != original_bytes:
        raise AssertionError("stored original differs from upload")
    result = {
        "case": case,
        "result": "passed",
        "baseline_peak_mib": round(baseline / MIB, 2),
        "peak_working_set_mib": round(working_set_bytes() / MIB, 2),
        "peak_delta_mib": round((working_set_bytes() - baseline) / MIB, 2),
        "cgroup_peak_mib": round(cgroup_memory_peak_bytes() / MIB, 2),
        "input_bytes": len(original_bytes),
        "processed_bytes": len(processed_bytes),
        "multipart_body_bytes": len(body) if case.startswith("multipart-") else 0,
        "stored_page": [meta["width"], meta["height"]],
        "preview_matches_model": True,
        "seconds": round(time.perf_counter() - started, 3),
    }
    print("HARD_LIMIT_JSON=" + json.dumps(result, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    parser.add_argument("--fixture-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--cases",
        default=(
            "multipart-40mp,concurrent-multipart-40mp,multipart-16mp-same,"
            "multipart-20mp-png,direct-40mp,json-40mp-decode,"
            "reject-40mp-png,"
            "json-12mp-jpeg,json-16mp-jpeg,json-20mp-jpeg,json-24mp-jpeg,"
            "json-30mp-jpeg,json-20mp-png"
        ),
    )
    parser.add_argument("--limit-mib", type=int, default=DEFAULT_LIMIT_MIB)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker, args.fixture_dir, args.output_dir)

    limit_bytes = args.limit_mib * MIB
    results = []
    with tempfile.TemporaryDirectory(prefix="history-grader-hard-limit-") as temp:
        temp_root = Path(temp)
        fixture_dir = temp_root / "fixtures"
        fixture_dir.mkdir()
        print("[hard-limit] generating 8000x5000 noise JPEG", flush=True)
        make_noise_jpeg(fixture_dir / "original.jpg", 8000, 5000, quality=70)
        make_noise_jpeg(fixture_dir / "original-12mp.jpg", 4000, 3000, quality=70)
        make_noise_jpeg(fixture_dir / "original-20mp.jpg", 5472, 3648, quality=70)
        make_noise_jpeg(fixture_dir / "original-16mp.jpg", 4500, 3500, quality=70)
        make_noise_jpeg(fixture_dir / "original-24mp.jpg", 6000, 4000, quality=70)
        make_noise_jpeg(fixture_dir / "original-30mp.jpg", 7000, 4286, quality=70)
        make_noise_jpeg(fixture_dir / "processed.jpg", 3000, 1875, quality=95)
        Image.new("RGB", (5000, 4000), "white").save(fixture_dir / "processed-20mp.png", format="PNG")
        make_40mp_png(fixture_dir / "original.png")
        original_size = (fixture_dir / "original.jpg").stat().st_size
        if original_size > 25 * MIB:
            raise SystemExit(f"fixture exceeds upload limit: {original_size} bytes")
        for case in [item.strip() for item in args.cases.split(",") if item.strip()]:
            output_dir = temp_root / f"{case}-out"
            output_dir.mkdir()
            command = [
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                "--worker",
                case,
                "--fixture-dir",
                str(fixture_dir),
                "--output-dir",
                str(output_dir),
            ]
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            started = time.perf_counter()
            completed = run_with_job_limit(command, limit_bytes)
            elapsed = round(time.perf_counter() - started, 3)
            lines = [line for line in completed.stdout.splitlines() if line.startswith("HARD_LIMIT_JSON=")]
            if completed.returncode == 0 and lines:
                result = json.loads(lines[-1].split("=", 1)[1])
                result["wall_seconds"] = elapsed
                result["limit_mib"] = args.limit_mib
            else:
                result = {
                    "case": case,
                    "result": "failed",
                    "returncode": completed.returncode,
                    "limit_mib": args.limit_mib,
                    "wall_seconds": elapsed,
                    "stdout_tail": completed.stdout[-2000:],
                    "stderr_tail": completed.stderr[-2000:],
                }
            results.append(result)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        if args.keep:
            keep_dir = ROOT / "review-artifacts" / f"large-image-hard-limit-{int(time.time())}"
            keep_dir.mkdir(parents=True, exist_ok=True)
            for case in results:
                source = temp_root / f"{case['case']}-out"
                if source.exists():
                    for path in source.iterdir():
                        path.replace(keep_dir / f"{case['case']}-{path.name}")
            print(f"kept artifacts in {keep_dir}")
    return 0 if all(item.get("result") == "passed" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
