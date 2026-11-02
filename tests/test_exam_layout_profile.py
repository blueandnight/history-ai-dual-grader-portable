import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import app
from exam_reference_assets import persist_exam_pdf_assets
from exam_layout_profile_service import build_exam_layout_profile, layout_profile_status


class ExamLayoutProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.assignment_id = "a" * 12
        self.exam_id = "b" * 32
        self.pdf_bytes = b"%PDF-test-source"
        self.pdf_hash = hashlib.sha256(self.pdf_bytes).hexdigest()
        self.old_values = {name: getattr(app, name) for name in ("PORTABLE_ROOT", "DATA_DIR", "DB_PATH", "BACKUP_DIR")}
        app.PORTABLE_ROOT = self.root
        app.DATA_DIR = self.data
        app.DB_PATH = self.data / "history_grading.sqlite3"
        app.BACKUP_DIR = self.root / "backups"
        self.data.mkdir(parents=True)
        self.refs = []
        for page_number in (3, 4):
            rel = f"data/{self.assignment_id}/layout_reference/page-{page_number}.png"
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1241, 1755), "white").save(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.refs.append({
                "page_number": page_number,
                "reference_width": 1241,
                "reference_height": 1755,
                "reference_sha256": digest,
                "render_dpi": 150,
                "render_method": "pdftoppm_150dpi",
                "image_file": rel,
            })
        imports = self.root / "data" / self.assignment_id / "imports"
        imports.mkdir(parents=True, exist_ok=True)
        (imports / "layout.pdf").write_bytes(self.pdf_bytes)
        self.layout_meta = {
            "source": {
                "layout_source": {
                    "type": "pdf",
                    "source_pdf_file_name": "test.pdf",
                    "source_pdf_sha256": self.pdf_hash,
                    "stored_path": f"data/{self.assignment_id}/imports/layout.pdf",
                    "page_count": 5,
                    "exam_pages": [3],
                    "answer_sheet_pages": [4],
                    "reference_pages": [3, 4],
                    "answer_pages": [5],
                },
            },
            "layout_reference": {
                "pages": self.refs,
                "page_roles": [
                    {"page_number": 3, "page_role": "exam"},
                    {"page_number": 4, "page_role": "answer_sheet"},
                    {"page_number": 5, "page_role": "answer"},
                ],
            },
            "objective_questions": [
                {"question_id": str(number), "stem": f"客观题{number}", "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"}}
                for number in range(1, 17)
            ],
            "objective_answer_key": {str(number): "A" for number in range(1, 17)},
        }
        persisted = persist_exam_pdf_assets(
            portable_root=self.root,
            exam_id=self.exam_id,
            source_pdf_path=imports / "layout.pdf",
            layout_source=self.layout_meta["source"]["layout_source"],
            layout_reference=self.layout_meta["layout_reference"],
            reference_path_resolver=app.resolve_data_relative_path,
        )
        self.refs = persisted["references"]
        self.layout_meta["source"]["layout_source"] = persisted["layout_source"]
        self.layout_meta["layout_reference"]["pages"] = self.refs
        conn = app.get_db()
        conn.execute(
            "INSERT INTO assignments (id,name,question,standard_answer,criteria_json,sub_questions_json,created_at,import_metadata_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                self.assignment_id, "测试政治制度考试", "材料", "答案", "[]",
                json.dumps([
                    {"id": "Q17.1", "label": "17.1", "question_type": "subjective"},
                    {"id": "Q17.2", "label": "17.2", "question_type": "subjective"},
                    {"id": "Q17.3", "label": "17.3", "question_type": "subjective"},
                    {"id": "Q18.1", "label": "18.1", "question_type": "subjective"},
                    {"id": "Q18.2", "label": "18.2", "question_type": "subjective"},
                ], ensure_ascii=False),
                "2026-10-04T00:00:00Z", json.dumps(self.layout_meta, ensure_ascii=False),
            ),
        )
        conn.execute(
            "INSERT INTO grading_exams (id,assignment_id,created_at) VALUES (?,?,?)",
            (self.exam_id, self.assignment_id, "2026-10-04T00:00:00Z"),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        for name, value in self.old_values.items():
            setattr(app, name, value)
        self.temp.cleanup()

    def _two_regions(self):
        return [
            {"question_id": "17.1", "page_number": 3, "region_type": "subjective_answer", "bbox": {"x1": .08, "y1": .67, "x2": .92, "y2": .81}},
            # Slight overlap is intentional: the adjacent handwritten first line must not be clipped.
            {"question_id": "17.2", "page_number": 3, "region_type": "subjective_answer", "bbox": {"x1": .08, "y1": .79, "x2": .92, "y2": .97}},
        ]

    def _post_regions(self, regions):
        handler = SimpleNamespace(require_local_client=lambda: True, read_json=lambda: {
            "exam_id": self.exam_id,
            "regions": regions,
        })
        replies = []
        with patch.object(app, "json_response", side_effect=lambda h, body, status=200: replies.append((body, status))):
            app.AppHandler.save_exam_layout_profile.__wrapped__(handler)
        return replies[0]

    def _context(self):
        conn = app.get_db()
        try:
            return app._exam_layout_profile_context(conn, self.exam_id)
        finally:
            conn.close()

    def test_formal_exam_creates_shared_profile_with_normalized_page_bindings(self):
        body, status = self._post_regions(self._two_regions())
        self.assertEqual(status, 200)
        self.assertTrue(body["layout_profile_id"])
        self.assertEqual(body["status"], "partial")
        self.assertEqual(body["configured_count"], 2)
        profile = body["layout_profile"]
        self.assertEqual(profile["schema_version"], "layout-profile-v1")
        self.assertEqual(profile["exam_id"], self.exam_id)
        self.assertEqual(profile["assignment_id"], self.assignment_id)
        self.assertEqual(profile["source_pdf_sha256"], self.pdf_hash)
        self.assertEqual([(r["question_id"], r["page_number"]) for r in profile["regions"]], [("17.1", 3), ("17.2", 3)])
        self.assertEqual(profile["regions"][0]["bbox"], {"x1": .08, "y1": .67, "x2": .92, "y2": .81})

    def test_existing_shared_layout_profile_allows_overlap(self):
        body, status = self._post_regions(self._two_regions())
        self.assertEqual(status, 200)
        first, second = body["layout_profile"]["regions"]
        self.assertGreater(first["bbox"]["y2"], second["bbox"]["y1"])

    def test_blank_answer_sheet_page_can_be_bound_and_scoring_answer_page_is_rejected(self):
        body, status = self._post_regions([
            {"question_id": "17.3", "page_number": 4, "region_type": "subjective_answer",
             "bbox": {"x1": .1, "y1": .6, "x2": .9, "y2": .8}},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["layout_profile"]["regions"][0]["page_number"], 4)
        with self.assertRaisesRegex(ValueError, "只能绑定到 PDF 试卷页"):
            self._post_regions([
                {"question_id": "17.3", "page_number": 5, "region_type": "subjective_answer",
                 "bbox": {"x1": .1, "y1": .6, "x2": .9, "y2": .8}},
            ])

    def _objective_regions(self):
        return [
            {"region_type": "handwritten_abcd", "page_number": 4,
             "bbox": {"x1": .18, "y1": .10, "x2": .92, "y2": .16},
             "question_mapping": [str(number) for number in range(1, 11)]},
            {"region_type": "handwritten_abcd", "page_number": 4,
             "bbox": {"x1": .18, "y1": .17, "x2": .67, "y2": .23},
             "question_mapping": [str(number) for number in range(11, 17)]},
        ]

    def test_objective_answer_rows_and_subjective_regions_share_one_profile(self):
        body, status = self._post_regions(self._two_regions() + self._objective_regions())
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "partial")
        self.assertEqual(body["objective_status"], "complete")
        self.assertEqual(body["objective_configured_count"], 16)
        self.assertEqual(body["objective_required_count"], 16)
        profile = body["layout_profile"]
        self.assertEqual(
            [region["region_type"] for region in profile["regions"]],
            ["subjective_answer", "subjective_answer", "handwritten_abcd", "handwritten_abcd"],
        )
        objective = [region for region in profile["regions"] if region["region_type"] == "handwritten_abcd"]
        self.assertEqual(objective[0]["question_mapping"], [f"Q{number}" for number in range(1, 11)])
        self.assertEqual((objective[0]["rows"], objective[0]["cols"]), (1, 10))
        self.assertEqual(objective[0]["page_number"], 4)
        self.assertEqual(objective[1]["question_mapping"], [f"Q{number}" for number in range(11, 17)])
        from layout_profile import validate_layout_profile
        validate_layout_profile(profile)
        context = self._context()
        self.assertEqual(context["objective_question_ids"], [str(number) for number in range(1, 17)])
        self.assertEqual(context["objective_configured_count"], 16)
        self.assertEqual(context["configured_count"], 2)

    def test_objective_region_must_reference_imported_objective_questions(self):
        bad = dict(self._objective_regions()[0])
        bad["question_mapping"] = ["1", "2", "99"]
        bad["cols"] = 3
        with self.assertRaisesRegex(ValueError, "不属于本场考试的客观题"):
            self._post_regions(self._two_regions()[:1] + [bad])

    def test_objective_question_cannot_appear_in_two_regions(self):
        first = self._objective_regions()[0]
        overlap = dict(self._objective_regions()[1])
        overlap["question_mapping"] = ["10", "11", "12"]
        overlap["cols"] = 3
        with self.assertRaisesRegex(ValueError, "重复出现在多个区域"):
            self._post_regions(self._two_regions()[:1] + [first, overlap])

    def test_objective_region_rows_cols_must_match_mapping(self):
        bad = dict(self._objective_regions()[0])
        bad["rows"] = 2
        with self.assertRaisesRegex(ValueError, "行列数必须与题号数量一致"):
            self._post_regions(self._two_regions()[:1] + [bad])

    def test_objective_region_rejects_answer_page_binding(self):
        bad = dict(self._objective_regions()[0])
        bad["page_number"] = 5
        with self.assertRaisesRegex(ValueError, "只能绑定到 PDF 试卷页"):
            self._post_regions(self._two_regions()[:1] + [bad])

    def test_objective_regions_are_ignored_by_subjective_crop_path(self):
        body, status = self._post_regions(self._two_regions() + self._objective_regions())
        self.assertEqual(status, 200)
        import numpy as np
        from layout_profile import crop_subjective_regions_from_aligned_page
        image = np.full((1755, 1241, 3), 255, dtype=np.uint8)
        crops = crop_subjective_regions_from_aligned_page(image, body["layout_profile"]["regions"])
        self.assertEqual([crop["question_id"] for crop in crops], ["17.1", "17.2"])

    def test_objective_regions_are_cleared_with_the_shared_profile(self):
        self.assertEqual(self._post_regions(self._two_regions() + self._objective_regions())[1], 200)
        body, status = self._post_regions([])
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "missing")
        self.assertEqual(body["objective_configured_count"], 0)
        self.assertIsNone(body["layout_profile"])

    def test_save_reload_delete_and_readd_update_missing_partial_complete_state(self):
        body, status = self._post_regions(self._two_regions())
        self.assertEqual(status, 200)
        profile_id = body["layout_profile_id"]
        created_at = body["layout_profile"]["created_at"]
        reloaded = self._context()
        self.assertEqual(reloaded["layout_profile_id"], profile_id)
        self.assertEqual(reloaded["layout_profile"]["regions"], body["layout_profile"]["regions"])
        self.assertEqual(reloaded["status"], "partial")

        body, status = self._post_regions([self._two_regions()[0]])
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "partial")
        self.assertEqual(body["configured_question_ids"], ["17.1"])
        self.assertEqual(body["layout_profile"]["layout_profile_id"], profile_id)
        self.assertEqual(body["layout_profile"]["created_at"], created_at)

        body, status = self._post_regions(self._two_regions() + [
            {"question_id": "17.3", "page_number": 4, "region_type": "subjective_answer", "bbox": {"x1": .1, "y1": .6, "x2": .9, "y2": .8}},
            {"question_id": "18.1", "page_number": 4, "region_type": "subjective_answer", "bbox": {"x1": .1, "y1": .6, "x2": .9, "y2": .8}},
            {"question_id": "18.2", "page_number": 4, "region_type": "subjective_answer", "bbox": {"x1": .1, "y1": .6, "x2": .9, "y2": .8}},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "complete")
        self.assertEqual(body["configured_count"], 5)

        body, status = self._post_regions([])
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "missing")
        self.assertIsNone(body["layout_profile"])

    def test_pdf_hash_mismatch_disables_stale_profile_without_returning_regions(self):
        self.assertEqual(self._post_regions(self._two_regions())[1], 200)
        conn = app.get_db()
        saved = json.loads(conn.execute("SELECT layout_profile_json FROM grading_exams WHERE id=?", (self.exam_id,)).fetchone()[0])
        saved["source_pdf_sha256"] = "0" * 64
        conn.execute("UPDATE grading_exams SET layout_profile_json=? WHERE id=?", (json.dumps(saved), self.exam_id))
        conn.commit()
        conn.close()
        context = self._context()
        self.assertTrue(context["profile_hash_mismatch"])
        self.assertEqual(context["status"], "missing")
        self.assertIsNone(context["layout_profile"])

    def test_no_pdf_reference_cannot_save_profile(self):
        conn = app.get_db()
        no_pdf_meta = {"source": {"layout_source": None}, "layout_reference": {"pages": [], "page_roles": []}}
        conn.execute("UPDATE assignments SET import_metadata_json=? WHERE id=?", (json.dumps(no_pdf_meta), self.assignment_id))
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(ValueError, "没有可用的打印 PDF"):
            self._post_regions(self._two_regions())

    def test_imported_questions_and_rubric_are_not_changed_by_region_edits(self):
        conn = app.get_db()
        before = tuple(conn.execute("SELECT sub_questions_json,criteria_json,standard_answer FROM assignments WHERE id=?", (self.assignment_id,)).fetchone())
        conn.close()
        self._post_regions(self._two_regions())
        conn = app.get_db()
        after = tuple(conn.execute("SELECT sub_questions_json,criteria_json,standard_answer FROM assignments WHERE id=?", (self.assignment_id,)).fetchone())
        conn.close()
        self.assertEqual(before, after)

    def test_page_roles_include_blank_answer_sheet_but_hide_answer_pages(self):
        context = self._context()
        self.assertEqual([page["page_number"] for page in context["references"]], [3, 4])
        self.assertEqual([page["page_role"] for page in context["references"]], ["exam", "answer_sheet"])
        self.assertEqual(context["status"], "missing")
        self.assertEqual(layout_profile_status(["17.1", "17.2"], None), {
            "status": "missing", "configured_count": 0, "required_count": 2, "configured_question_ids": [],
            "objective_status": "not_available", "objective_configured_count": 0,
            "objective_required_count": 0, "objective_configured_question_ids": [],
        })

    def test_isolated_data_copy_resolves_and_lazily_rebuilds_reference_without_poc_paths(self):
        body, status = self._post_regions(self._two_regions())
        self.assertEqual(status, 200)
        page3 = next(page for page in self.refs if page["page_number"] == 3)
        original_page3 = (self.root / page3["image_file"]).read_bytes()
        isolated_root = self.root / "isolated-copy"
        isolated_root.mkdir()
        shutil.copytree(self.data, isolated_root / "data")

        old_values = {name: getattr(app, name) for name in ("PORTABLE_ROOT", "DATA_DIR", "DB_PATH", "BACKUP_DIR")}
        try:
            app.PORTABLE_ROOT = isolated_root
            app.DATA_DIR = isolated_root / "data"
            app.DB_PATH = isolated_root / "data" / "history_grading.sqlite3"
            app.BACKUP_DIR = isolated_root / "backups"
            conn = app.get_db()
            context = app._exam_layout_profile_context(conn, self.exam_id)
            app._resolve_exam_reference_assets(conn, context)
            self.assertEqual(context["reference_asset_status"], "reference_asset_ready")
            copied_page = next(page for page in context["references"] if page["page_number"] == 3)
            self.assertTrue(Path(copied_page["resolved_image_path"]).is_relative_to(isolated_root / "data"))
            self.assertEqual(Path(copied_page["resolved_image_path"]).read_bytes(), original_page3)

            missing_path = Path(copied_page["resolved_image_path"])
            missing_path.unlink()

            def fake_pdftoppm(command, **_kwargs):
                output_png = Path(command[-1]).with_suffix(".png")
                output_png.write_bytes(original_page3)
                return SimpleNamespace(returncode=0)

            with patch("exam_reference_assets.shutil.which", return_value="synthetic-pdftoppm"), patch("exam_reference_assets.subprocess.run", side_effect=fake_pdftoppm):
                context = app._exam_layout_profile_context(conn, self.exam_id)
                app._resolve_exam_reference_assets(conn, context)
            self.assertEqual(context["reference_asset_status"], "reference_asset_ready")
            self.assertEqual(context["reference_rebuilt_pages"], [3])
            rebuilt_page = next(page for page in context["references"] if page["page_number"] == 3)
            self.assertEqual(Path(rebuilt_page["resolved_image_path"]).read_bytes(), original_page3)
            saved = conn.execute("SELECT import_metadata_json FROM assignments WHERE id=?", (self.assignment_id,)).fetchone()
            saved_meta = json.loads(saved[0])
            saved_page = next(page for page in saved_meta["layout_reference"]["pages"] if page["page_number"] == 3)
            self.assertTrue(saved_page["reference_rebuilt"])
            self.assertTrue(saved_page["reference_hash_match"])
            self.assertFalse(Path(saved_page["image_file"]).is_absolute())
            profile_after = json.loads(conn.execute("SELECT layout_profile_json FROM grading_exams WHERE id=?", (self.exam_id,)).fetchone()[0])
            self.assertEqual(profile_after["regions"], body["layout_profile"]["regions"])
            manifest = isolated_root / "data" / "exam_assets" / self.exam_id / self.pdf_hash / "manifest.json"
            self.assertTrue(manifest.is_file())
        finally:
            try:
                conn.close()
            except UnboundLocalError:
                pass
            for name, value in old_values.items():
                setattr(app, name, value)

    def test_missing_reference_and_source_are_not_misreported_as_missing_layout_profile(self):
        self._post_regions(self._two_regions())
        asset_dir = self.data / "exam_assets" / self.exam_id / self.pdf_hash
        shutil.rmtree(asset_dir)
        context = self._context()
        self.assertTrue(context["layout_profile"])
        self.assertTrue(context["has_pdf_reference"])
        conn = app.get_db()
        try:
            app._resolve_exam_reference_assets(conn, context)
        finally:
            conn.close()
        self.assertEqual(context["reference_asset_status"], "reference_asset_missing")
        self.assertIn("source_pdf", context["reference_asset_failures"][0]["reason"])

    def test_saved_profile_without_layout_source_metadata_is_asset_missing_not_profile_missing(self):
        self._post_regions(self._two_regions())
        conn = app.get_db()
        try:
            row = conn.execute(
                "SELECT import_metadata_json FROM assignments WHERE id=?",
                (self.assignment_id,),
            ).fetchone()
            metadata = json.loads(row["import_metadata_json"])
            metadata["source"].pop("layout_source", None)
            metadata.pop("layout_reference", None)
            conn.execute(
                "UPDATE assignments SET import_metadata_json=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), self.assignment_id),
            )
            conn.commit()
        finally:
            conn.close()

        context = self._context()
        self.assertIsNotNone(context["layout_profile"])
        self.assertTrue(context["layout_profile_id"])
        self.assertFalse(context["has_pdf_reference"])
        self.assertEqual(context["reference_asset_status"], "reference_asset_missing")

        from student_auto_crop_service import run_student_auto_crop
        output = self.root / "no-assets-run"
        result = run_student_auto_crop(
            exam_context=context,
            student_image_bytes=b"not-decoded-because-reference-missing",
            input_mode="double_spread",
            reference_loader=lambda page: None,
            output_dir=output,
            run_id="c" * 32,
        )
        self.assertEqual(result["status"], "reference_asset_missing")
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
