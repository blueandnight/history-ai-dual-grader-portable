"""Targeted large-image memory and fidelity profiling.

Run from the app root:
    python -B tests/large_image_profile.py

Each case is generated in a separate parent process step and processed in a
fresh child process, so the child's peak working set measures the pipeline
rather than fixture generation.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

import app  # noqa: E402

CASES = {
    "12mp-jpeg": (4000, 3000, "JPEG"),
    "20mp-jpeg": (5472, 3648, "JPEG"),
    "20mp-png": (5000, 4000, "PNG"),
    "40mp-jpeg": (8000, 5000, "JPEG"),
    "over20mp-png": (5001, 4000, "PNG"),
}
CORNERS = [[0.02, 0.02], [0.98, 0.02], [0.98, 0.98], [0.02, 0.98]]
REDACTIONS = [{"x": 0.04, "y": 0.03, "width": 0.24, "height": 0.08}]
MIB = 1024 * 1024


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
        wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    process = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return int(counters.PeakWorkingSetSize)


def make_fixture(path: Path, width: int, height: int, image_format: str) -> None:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    line_width = max(2, width // 1800)
    for index in range(1, 16):
        y = round(height * index / 16)
        draw.line((0, y, width, y), fill=(0, 0, 0), width=line_width)
        x = round(width * index / 16)
        draw.line((x, 0, x, height), fill=(30, 30, 30), width=line_width)
    marker = (round(width * 0.44), round(height * 0.44), round(width * 0.56), round(height * 0.56))
    draw.rectangle(marker, outline=(0, 0, 0), width=max(4, width // 900))
    draw.line((marker[0], marker[1], marker[2], marker[3]), fill=(0, 0, 0), width=max(3, width // 1200))
    draw.line((marker[2], marker[1], marker[0], marker[3]), fill=(0, 0, 0), width=max(3, width // 1200))
    save_options = {"quality": 90} if image_format == "JPEG" else {}
    image.save(path, format=image_format, **save_options)
    image.close()


def worker(case: str, fixture: Path, output_dir: Path) -> int:
    width, height, image_format = CASES[case]
    raw = fixture.read_bytes()
    media_type = "image/png" if image_format == "PNG" else "image/jpeg"
    data_url = app.bytes_to_data_url(raw, media_type)
    baseline = working_set_bytes()
    started = time.perf_counter()
    if case in {"20mp-png", "over20mp-png"}:
        try:
            app.decode_image_for_processing(raw, media_type)
        except ValueError as exc:
            result = {
                "case": case,
                "result": "passed",
                "action": "rejected_before_full_decode",
                "error": str(exc),
                "input_pixels": width * height,
                "baseline_peak_mib": round(baseline / MIB, 2),
                "peak_working_set_mib": round(working_set_bytes() / MIB, 2),
                "seconds": round(time.perf_counter() - started, 3),
            }
            print("PROFILE_JSON=" + json.dumps(result, ensure_ascii=False), flush=True)
            return 0
        raise AssertionError("over-budget PNG was not rejected")

    target = output_dir / f"{case}.processed.jpg"
    original = output_dir / f"{case}.original"
    meta = app.save_photo(data_url, target, original, CORNERS)
    preview = app.prepare_ai_preview_image_bytes(raw, media_type, REDACTIONS, CORNERS)
    model = app.prepare_model_image(target, REDACTIONS)
    if preview != model:
        raise AssertionError("privacy preview and later model image hashes differ")

    with Image.open(target) as processed:
        processed.load()
        page_size = processed.size
        center = processed.crop((
            round(processed.width * 0.44), round(processed.height * 0.44),
            round(processed.width * 0.56), round(processed.height * 0.56),
        )).convert("L")
        histogram = center.histogram()
        dark_ratio = sum(histogram[:80]) / max(1, sum(histogram))

    region = {"photo_index": 1, "x": 0.44, "y": 0.44, "width": 0.12, "height": 0.12}
    crop_bytes = app.crop_model_image_bytes(model, region)
    with Image.open(io.BytesIO(crop_bytes)) as crop:
        crop.load()
        crop_size = crop.size

    mapped = {"sub_questions": [{"criteria": [{"evidence": {
        "photo_index": 1, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.2,
    }}]}]}
    app.map_crop_evidence_to_source(mapped, [region])
    mapped_evidence = mapped["sub_questions"][0]["criteria"][0]["evidence"]
    expected_x = region["x"] + 0.1 * region["width"]
    expected_y = region["y"] + 0.2 * region["height"]
    if abs(mapped_evidence["x"] - expected_x) > 1e-6 or abs(mapped_evidence["y"] - expected_y) > 1e-6:
        raise AssertionError("evidence coordinate mapping changed")

    result = {
        "case": case,
        "result": "passed",
        "input_pixels": width * height,
        "input_bytes": len(raw),
        "decoded_long_edge_limit": app.PROCESSING_IMAGE_LONG_EDGE,
        "stored_page": list(page_size),
        "model_image_bytes": len(model),
        "crop": list(crop_size),
        "dark_pixel_ratio_center": round(dark_ratio, 5),
        "quality": meta["quality"]["metrics"],
        "preview_hash_matches_model": True,
        "evidence_mapping_matches": True,
        "baseline_peak_mib": round(baseline / MIB, 2),
        "peak_working_set_mib": round(working_set_bytes() / MIB, 2),
        "peak_delta_mib": round((working_set_bytes() - baseline) / MIB, 2),
        "seconds": round(time.perf_counter() - started, 3),
    }
    print("PROFILE_JSON=" + json.dumps(result, ensure_ascii=False), flush=True)
    return 0


def run_case(case: str, temp_root: Path, keep: bool) -> dict:
    width, height, image_format = CASES[case]
    fixture = temp_root / f"{case}.{'png' if image_format == 'PNG' else 'jpg'}"
    print(f"[profile] generating {case} {width}x{height} {image_format}", flush=True)
    make_fixture(fixture, width, height, image_format)
    output_dir = temp_root / f"{case}-out"
    output_dir.mkdir()
    command = [
        sys.executable, "-B", str(Path(__file__).resolve()),
        "--worker", case, "--fixture", str(fixture), "--output-dir", str(output_dir),
        "--temp-root", str(temp_root),
    ]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
    )
    if completed.returncode != 0:
        print(completed.stdout)
        print(completed.stderr)
        raise RuntimeError(f"{case} worker failed with {completed.returncode}")
    lines = [line for line in completed.stdout.splitlines() if line.startswith("PROFILE_JSON=")]
    if not lines:
        raise RuntimeError(f"{case} worker did not emit profile JSON")
    result = json.loads(lines[-1].split("=", 1)[1])
    result["fixture_bytes"] = fixture.stat().st_size if fixture.exists() else 0
    if not keep:
        for path in sorted(output_dir.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        output_dir.rmdir()
        fixture.unlink()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--temp-root", type=Path)
    parser.add_argument("--cases", default="12mp-jpeg,20mp-jpeg,20mp-png,40mp-jpeg,over20mp-png")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.worker:
        with tempfile.TemporaryDirectory(dir=args.temp_root):
            return worker(args.worker, args.fixture, args.output_dir)

    selected = [item.strip() for item in args.cases.split(",") if item.strip()]
    unknown = [item for item in selected if item not in CASES]
    if unknown:
        raise SystemExit(f"unknown cases: {', '.join(unknown)}")
    results = []
    with tempfile.TemporaryDirectory(prefix="history-grader-large-") as temp:
        temp_root = Path(temp)
        for case in selected:
            results.append(run_case(case, temp_root, args.keep))
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
