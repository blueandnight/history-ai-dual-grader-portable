import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from io import BytesIO

from exam_reference_assets import persist_exam_pdf_assets, resolve_exam_reference_page


def png_bytes(color="white", size=(80, 120)):
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class ExamReferenceAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.exam_id = "1" * 32
        self.pdf = b"%PDF-source-for-reference-asset-test"
        self.pdf_hash = hashlib.sha256(self.pdf).hexdigest()
        self.pdf_path = self.data / "drafts" / "layout.pdf"
        self.pdf_path.parent.mkdir(parents=True)
        self.pdf_path.write_bytes(self.pdf)
        self.source_ref = self.data / "drafts" / "page-3.png"
        self.reference = png_bytes()
        self.source_ref.write_bytes(self.reference)
        self.page = {
            "page_number": 3,
            "page_role": "exam",
            "reference_width": 80,
            "reference_height": 120,
            "reference_sha256": hashlib.sha256(self.reference).hexdigest(),
            "render_dpi": 150,
            "render_method": "pdftoppm_150dpi",
            "image_file": "data/drafts/page-3.png",
        }
        self.layout_source = {
            "source_pdf_sha256": self.pdf_hash,
            "page_count": 4,
            "exam_pages": [3],
            "answer_pages": [4],
            "file_size": len(self.pdf),
        }

    def tearDown(self):
        self.temp.cleanup()

    def resolve_relative(self, stored):
        normalized = str(stored).replace("\\", "/")
        if not normalized.startswith("data/") or ".." in Path(normalized).parts or Path(normalized).is_absolute():
            raise ValueError("unsafe relative path")
        result = (self.root / normalized).resolve()
        if not result.is_relative_to(self.data.resolve()):
            raise ValueError("outside data")
        return result

    def persist(self):
        return persist_exam_pdf_assets(
            portable_root=self.root,
            exam_id=self.exam_id,
            source_pdf_path=self.pdf_path,
            layout_source=self.layout_source,
            layout_reference={"pages": [self.page]},
            reference_path_resolver=self.resolve_relative,
        )

    def resolve(self, page, *, pdf_path=None):
        return resolve_exam_reference_page(
            portable_root=self.root,
            exam_id=self.exam_id,
            source_pdf_sha256=self.pdf_hash,
            page=page,
            source_pdf_path=pdf_path,
            path_resolver=self.resolve_relative,
            renderer="pdftoppm-test",
        )

    def test_persistence_stores_source_and_exam_reference_with_relative_paths(self):
        result = self.persist()
        self.assertEqual(result["layout_source"]["asset_status"], "ready")
        self.assertEqual(result["layout_source"]["file_size"], len(self.pdf))
        self.assertEqual((self.root / result["layout_source"]["stored_path"]).read_bytes(), self.pdf)
        page = result["references"][0]
        self.assertEqual(page["image_file"], f"data/exam_assets/{self.exam_id}/{self.pdf_hash}/references/page_003.png")
        self.assertFalse(Path(page["image_file"]).is_absolute())
        self.assertEqual((self.root / page["image_file"]).read_bytes(), self.reference)
        self.assertTrue((result["asset_dir"] / "manifest.json").is_file())

    def test_valid_reference_returns_ready_after_data_directory_copy(self):
        persisted = self.persist()
        clone = self.root / "portable-copy"
        clone.mkdir()
        import shutil
        shutil.copytree(self.data, clone / "data")
        page = persisted["references"][0]
        resolved = resolve_exam_reference_page(
            portable_root=clone,
            exam_id=self.exam_id,
            source_pdf_sha256=self.pdf_hash,
            page=page,
            source_pdf_path=clone / persisted["layout_source"]["stored_path"],
            path_resolver=lambda rel: (clone / rel).resolve(),
        )
        self.assertEqual(resolved["status"], "reference_asset_ready")
        self.assertEqual(Path(resolved["path"]).read_bytes(), self.reference)
        self.assertTrue(Path(resolved["path"]).is_relative_to(clone / "data"))

    def test_missing_reference_and_source_returns_explicit_missing(self):
        page = dict(self.page, image_file="data/missing/page-3.png")
        result = self.resolve(page)
        self.assertEqual(result["status"], "reference_asset_missing")

    def test_existing_but_corrupt_reference_is_not_silently_accepted(self):
        self.source_ref.write_bytes(png_bytes("black"))
        result = self.resolve(self.page, pdf_path=self.pdf_path)
        self.assertEqual(result["status"], "reference_asset_corrupt")
        self.assertEqual(result["reason"], "reference_metadata_mismatch")

    def test_source_pdf_hash_mismatch_blocks_rebuild(self):
        page = dict(self.page, image_file="data/missing/page-3.png")
        self.pdf_path.write_bytes(b"changed PDF")
        with patch("exam_reference_assets.subprocess.run") as renderer:
            result = self.resolve(page, pdf_path=self.pdf_path)
        self.assertEqual(result["status"], "reference_asset_corrupt")
        self.assertEqual(result["reason"], "source_pdf_hash_mismatch")
        renderer.assert_not_called()

    def test_rebuild_hash_or_dimension_mismatch_does_not_create_reference(self):
        page = dict(self.page, image_file="data/missing/page-3.png")
        destination = self.data / "exam_assets" / self.exam_id / self.pdf_hash / "references" / "page_003.png"

        def fake_renderer(command, **_kwargs):
            Path(command[-1]).with_suffix(".png").write_bytes(png_bytes("black"))
            return SimpleNamespace(returncode=0)

        with patch("exam_reference_assets.shutil.which", return_value="synthetic-pdftoppm"), patch("exam_reference_assets.subprocess.run", side_effect=fake_renderer):
            result = self.resolve(page, pdf_path=self.pdf_path)
        self.assertEqual(result["status"], "reference_rebuild_mismatch")
        self.assertFalse(destination.exists())

    def test_reference_metadata_keeps_page_identity_and_exact_dimensions(self):
        persisted = self.persist()
        page = dict(persisted["references"][0], page_number=2)
        resolved = self.resolve(page, pdf_path=self.pdf_path)
        self.assertEqual(resolved["status"], "reference_asset_corrupt")
        self.assertEqual(resolved["reason"], "reference_page_number_mismatch")

    def test_blank_answer_sheet_page_is_a_layout_reference_but_answer_page_is_not(self):
        answer_ref = png_bytes("gray")
        answer_page = dict(
            self.page,
            page_number=4,
            page_role="answer_sheet",
            reference_sha256=hashlib.sha256(answer_ref).hexdigest(),
            image_file="data/drafts/page-4.png",
        )
        (self.data / "drafts" / "page-4.png").write_bytes(answer_ref)
        self.layout_source.update({"answer_sheet_pages": [4], "reference_pages": [3, 4], "answer_pages": [5]})
        result = persist_exam_pdf_assets(
            portable_root=self.root,
            exam_id=self.exam_id,
            source_pdf_path=self.pdf_path,
            layout_source=self.layout_source,
            layout_reference={"pages": [self.page, answer_page]},
            reference_path_resolver=self.resolve_relative,
        )
        self.assertEqual([page["page_number"] for page in result["references"]], [3, 4])
        self.assertEqual([page["page_role"] for page in result["references"]], ["exam", "answer_sheet"])
        self.assertEqual(result["layout_source"]["reference_pages"], [3, 4])
        self.assertEqual(result["layout_source"]["asset_status"], "ready")
        manifest = json.loads((result["asset_dir"] / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["exam_pages"], [3])
        self.assertEqual(manifest["reference_pages"], [3, 4])
        self.assertEqual([page["page_number"] for page in manifest["references"]], [3, 4])

    def test_missing_blank_answer_sheet_reference_blocks_readiness(self):
        self.layout_source.update({"answer_sheet_pages": [4], "reference_pages": [3, 4]})
        with self.assertRaisesRegex(ValueError, "reference 未完整生成"):
            self.persist()


if __name__ == "__main__":
    unittest.main()
