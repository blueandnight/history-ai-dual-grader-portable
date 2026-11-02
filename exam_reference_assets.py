"""Durable PDF and rendered-page assets for confirmed exams.

All paths stored in exam metadata are relative to the portable application
root.  This module deliberately does not search reports, temporary folders, or
the original upload location.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image


PathResolver = Callable[[str], Path]


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def exam_asset_relative_dir(exam_id: str, source_pdf_sha256: str) -> str:
    if len(exam_id) != 32 or any(ch not in "0123456789abcdef" for ch in exam_id.lower()):
        raise ValueError("exam_id is invalid")
    digest = source_pdf_sha256.lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("source PDF SHA-256 is invalid")
    return f"data/exam_assets/{exam_id.lower()}/{digest}"


def _png_dimensions(raw: bytes) -> tuple[int, int]:
    from io import BytesIO

    with Image.open(BytesIO(raw)) as image:
        if image.format != "PNG":
            raise ValueError("reference image is not PNG")
        image.verify()
    with Image.open(BytesIO(raw)) as image:
        width, height = image.size
    if width < 1 or height < 1:
        raise ValueError("reference image dimensions are invalid")
    return int(width), int(height)


def persist_exam_pdf_assets(
    *,
    portable_root: str | Path,
    exam_id: str,
    source_pdf_path: str | Path,
    layout_source: dict[str, Any],
    layout_reference: dict[str, Any],
    reference_path_resolver: PathResolver,
) -> dict[str, Any]:
    """Atomically copy a confirmed PDF and all layout-reference pages.

    Layout references cover the exam pages plus the blank answer sheet, where
    teachers draw formal answer regions. Scoring-reference/answer pages are never
    copied. The caller must invoke this before inserting the formal exam rows. A
    PDF with recognized reference pages is not marked ready unless every
    corresponding rendered reference is copied and validated.
    """
    root = Path(portable_root).resolve()
    source_path = Path(source_pdf_path).resolve()
    if not source_path.is_file():
        raise ValueError("正式考试 PDF source is missing")
    source_bytes = source_path.read_bytes()
    actual_pdf_hash = sha256_bytes(source_bytes)
    expected_pdf_hash = str(layout_source.get("source_pdf_sha256") or layout_source.get("sha256") or "").lower()
    if expected_pdf_hash and actual_pdf_hash != expected_pdf_hash:
        raise ValueError("正式考试 PDF hash 不匹配")

    from exam_import_draft import layout_reference_page_numbers

    exam_pages = sorted({int(number) for number in layout_source.get("exam_pages", []) if str(number).isdigit()})
    reference_pages = layout_reference_page_numbers(layout_source)
    refs = layout_reference.get("pages", []) if isinstance(layout_reference, dict) else []
    refs_by_page = {
        int(item["page_number"]): item for item in refs
        if isinstance(item, dict) and str(item.get("page_number", "")).isdigit()
    }
    missing = sorted(set(reference_pages) - set(refs_by_page))
    if reference_pages and missing:
        raise ValueError(f"正式试卷页 reference 未完整生成：{missing}")

    relative_dir = exam_asset_relative_dir(exam_id, actual_pdf_hash)
    final_dir = (root / relative_dir).resolve()
    if not final_dir.is_relative_to((root / "data" / "exam_assets").resolve()):
        raise ValueError("正式 PDF asset path escaped the data directory")
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(tempfile.mkdtemp(prefix=f".{actual_pdf_hash[:10]}-", dir=final_dir.parent))
    final_refs: list[dict[str, Any]] = []
    try:
        staged_source = stage_dir / "source.pdf"
        staged_source.write_bytes(source_bytes)
        for page_number in reference_pages:
            page = refs_by_page[page_number]
            expected_ref_hash = str(page.get("reference_sha256") or "").lower()
            if len(expected_ref_hash) != 64:
                raise ValueError(f"PDF page {page_number} reference SHA-256 is missing")
            source_ref = reference_path_resolver(str(page.get("image_file") or ""))
            if not source_ref.is_file():
                raise ValueError(f"PDF page {page_number} reference image is missing")
            raw = source_ref.read_bytes()
            if sha256_bytes(raw) != expected_ref_hash:
                raise ValueError(f"PDF page {page_number} reference hash mismatch")
            width, height = _png_dimensions(raw)
            if width != int(page.get("reference_width") or 0) or height != int(page.get("reference_height") or 0):
                raise ValueError(f"PDF page {page_number} reference dimensions mismatch")
            name = f"page_{page_number:03d}.png"
            destination = stage_dir / "references" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            relative_path = f"{relative_dir}/references/{name}"
            final_page = {key: value for key, value in page.items() if key != "preview_url"}
            final_page.update({
                "page_number": page_number,
                "image_file": relative_path,
                "reference_sha256": expected_ref_hash,
                "reference_width": width,
                "reference_height": height,
                "asset_status": "ready",
            })
            final_refs.append(final_page)

        final_source = dict(layout_source)
        final_source.update({
            "source_pdf_sha256": actual_pdf_hash,
            "stored_path": f"{relative_dir}/source.pdf",
            "asset_relative_dir": relative_dir,
            "file_size": len(source_bytes),
            "reference_pages": reference_pages,
            "asset_status": "ready" if reference_pages and len(final_refs) == len(reference_pages) else "reference_unavailable",
        })
        manifest = {
            "manifest_version": 1,
            "exam_id": exam_id.lower(),
            "source_pdf_sha256": actual_pdf_hash,
            "source_pdf_path": final_source["stored_path"],
            "source_pdf_file_size": len(source_bytes),
            "page_count": int(layout_source.get("page_count") or 0),
            "exam_pages": exam_pages,
            "reference_pages": reference_pages,
            "references": final_refs,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (stage_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if final_dir.exists():
            shutil.rmtree(final_dir)
        os.replace(stage_dir, final_dir)
        return {
            "layout_source": final_source,
            "references": final_refs,
            "asset_dir": final_dir,
            "asset_relative_dir": relative_dir,
        }
    except Exception:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise


def resolve_exam_reference_page(
    *,
    portable_root: str | Path,
    exam_id: str,
    source_pdf_sha256: str,
    page: dict[str, Any],
    source_pdf_path: str | Path | None,
    path_resolver: PathResolver,
    renderer: str | None = None,
) -> dict[str, Any]:
    """Validate a saved reference or rebuild a missing page from its PDF.

    Existing reference files are accepted only when page number, PNG hash, and
    dimensions match metadata. Rebuilding is permitted only from the explicitly
    recorded source PDF after verifying its SHA-256.
    """
    try:
        page_number = int(page["page_number"])
        width = int(page["reference_width"])
        height = int(page["reference_height"])
        expected_ref_hash = str(page["reference_sha256"]).lower()
        expected_pdf_hash = str(source_pdf_sha256).lower()
        if page_number < 1 or width < 1 or height < 1 or len(expected_ref_hash) != 64 or len(expected_pdf_hash) != 64:
            raise ValueError("reference metadata is incomplete")
    except (KeyError, TypeError, ValueError):
        return {"status": "reference_asset_corrupt", "reason": "reference_metadata_invalid", "page": page}

    recorded_path = str(page.get("image_file") or page.get("relative_path") or "")
    if recorded_path:
        path_stem = Path(recorded_path.replace("\\", "/")).stem
        named_page = re.search(r"page[_-]?0*(\d+)$", path_stem, flags=re.IGNORECASE)
        if named_page and int(named_page.group(1)) != page_number:
            return {"status": "reference_asset_corrupt", "reason": "reference_page_number_mismatch", "page": page}
        try:
            candidate = path_resolver(recorded_path)
        except (OSError, ValueError):
            candidate = None
        if candidate is not None and candidate.is_file():
            try:
                raw = candidate.read_bytes()
                found_width, found_height = _png_dimensions(raw)
            except (OSError, ValueError):
                return {"status": "reference_asset_corrupt", "reason": "reference_png_invalid", "page": page}
            if sha256_bytes(raw) != expected_ref_hash or (found_width, found_height) != (width, height):
                return {"status": "reference_asset_corrupt", "reason": "reference_metadata_mismatch", "page": page}
            return {"status": "reference_asset_ready", "path": candidate, "relative_path": recorded_path, "page": page}

    if source_pdf_path is None or not Path(source_pdf_path).is_file():
        return {"status": "reference_asset_missing", "reason": "reference_and_source_pdf_missing", "page": page}
    pdf_path = Path(source_pdf_path).resolve()
    try:
        pdf_hash = sha256_bytes(pdf_path.read_bytes())
    except OSError:
        return {"status": "reference_asset_missing", "reason": "source_pdf_unreadable", "page": page}
    if pdf_hash != expected_pdf_hash:
        return {"status": "reference_asset_corrupt", "reason": "source_pdf_hash_mismatch", "page": page}

    dpi = page.get("render_dpi")
    try:
        dpi = int(dpi)
    except (TypeError, ValueError):
        return {"status": "reference_asset_corrupt", "reason": "render_dpi_missing", "page": page}
    if not 36 <= dpi <= 1200:
        return {"status": "reference_asset_corrupt", "reason": "render_dpi_invalid", "page": page}
    method = str(page.get("render_method") or "pdftoppm_150dpi")
    executable = renderer or shutil.which("pdftoppm")
    if not method.startswith("pdfium-") and not executable:
        return {"status": "reference_asset_missing", "reason": "pdf_renderer_unavailable", "page": page}

    root = Path(portable_root).resolve()
    relative_dir = exam_asset_relative_dir(exam_id, expected_pdf_hash)
    asset_dir = (root / relative_dir).resolve()
    if not asset_dir.is_relative_to((root / "data" / "exam_assets").resolve()):
        return {"status": "reference_asset_corrupt", "reason": "asset_path_invalid", "page": page}
    destination = asset_dir / "references" / f"page_{page_number:03d}.png"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reference-rebuild-") as temp_name:
        temp_dir = Path(temp_name)
        prefix = temp_dir / f"page_{page_number:03d}"
        try:
            if method.startswith("pdfium-"):
                from pdf_renderer import render_pdf_page
                render_pdf_page(pdf_path, page_number, dpi, prefix.with_suffix(".png"), method=method)
            else:
                subprocess.run(
                    [str(executable), "-f", str(page_number), "-l", str(page_number), "-r", str(dpi), "-png", "-singlefile", str(pdf_path), str(prefix)],
                    check=True, capture_output=True, timeout=90,
                )
            rendered = prefix.with_suffix(".png")
            raw = rendered.read_bytes()
            found_dimensions = _png_dimensions(raw)
        except (OSError, subprocess.SubprocessError, ValueError, ImportError):
            return {"status": "reference_asset_missing", "reason": "reference_rebuild_failed", "page": page}
        if sha256_bytes(raw) != expected_ref_hash or found_dimensions != (width, height):
            return {
                "status": "reference_rebuild_mismatch",
                "reason": "rendered_reference_does_not_match_metadata",
                "page": page,
                "rebuilt_sha256": sha256_bytes(raw),
                "rebuilt_width": found_dimensions[0],
                "rebuilt_height": found_dimensions[1],
                "expected_sha256": expected_ref_hash,
                "expected_width": width,
                "expected_height": height,
            }
        staged = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        staged.write_bytes(raw)
        os.replace(staged, destination)

    relative_path = f"{relative_dir}/references/page_{page_number:03d}.png"
    manifest_path = asset_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
    else:
        manifest = {}
    manifest.update({"manifest_version": 1, "exam_id": exam_id, "source_pdf_sha256": expected_pdf_hash})
    manifest.setdefault("references", [])
    rebuilt_meta = {key: value for key, value in page.items() if key not in {"image_file", "preview_url"}}
    rebuilt_meta.update({
        "page_number": page_number, "image_file": relative_path,
        "reference_sha256": expected_ref_hash, "reference_width": width,
        "reference_height": height, "asset_status": "ready",
        "reference_rebuilt": True, "reference_hash_match": True,
        "rebuilt_at": datetime.now(timezone.utc).isoformat(),
    })
    pages = [item for item in manifest["references"] if not isinstance(item, dict) or int(item.get("page_number", -1)) != page_number]
    pages.append(rebuilt_meta)
    manifest["references"] = sorted(pages, key=lambda item: int(item.get("page_number", 0)) if isinstance(item, dict) else 0)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "reference_rebuilt", "path": destination, "relative_path": relative_path, "page": rebuilt_meta,
            "reference_hash_match": True, "rebuilt_dimensions": {"width": width, "height": height}}
