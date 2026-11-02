import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.previous_paths = app.APP_PATHS

    def tearDown(self):
        app._install_paths(self.previous_paths)

    def configure(self, temp: str) -> Path:
        root = Path(temp) / "portable"
        app.configure_paths(root, Path(temp) / "program")
        return root

    @staticmethod
    def image_data_url(size=(1000, 800), image_format="JPEG") -> str:
        image = Image.new("RGB", size, "white")
        output = io.BytesIO()
        image.save(output, format=image_format)
        mime = "image/png" if image_format == "PNG" else "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(output.getvalue()).decode("ascii")

    def test_rules_demo_is_always_blocked(self):
        criteria = app.clean_criteria([{"label": "要点", "score": 2, "keywords": ["要点"]}])
        result = app.score_by_rules(criteria, "答出要点")
        self.assertTrue(result["blocked"])
        self.assertIn("规则演示", " ".join(result["block_reasons"]))

    def test_resource_limits_reject_dimensions_and_pixels(self):
        with self.assertRaisesRegex(ValueError, "单边尺寸"):
            app.save_photo(self.image_data_url((12_001, 1), "PNG"), Path("too-wide.jpg"))
        with self.assertRaisesRegex(ValueError, "总像素"):
            app.save_photo(self.image_data_url((6_325, 6_325), "PNG"), Path("too-many-pixels.jpg"))

    def test_svg_upload_is_rejected_before_it_can_be_served_as_original(self):
        payload = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>').decode("ascii")
        with self.assertRaisesRegex(ValueError, "SVG"):
            app.data_url_bytes(f"data:image/svg+xml;base64,{payload}", require_image=True)

    def test_manual_perspective_transform_is_derived_and_original_is_kept(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "rectified.jpg"
            original = Path(temp) / "original.bin"
            meta = app.save_photo(
                self.image_data_url((1200, 900), "JPEG"),
                target,
                original,
                [(0.05, 0.05), (0.95, 0.05), (0.95, 0.95), (0.05, 0.95)],
            )
            self.assertEqual(meta["transform"]["type"], "manual_quad")
            self.assertTrue(target.exists())
            self.assertTrue(original.exists())
            self.assertEqual(meta["original_bytes"], original.stat().st_size)

    def test_model_evidence_requires_normalized_box(self):
        criteria = app.clean_criteria([{"label": "要点", "score": 2, "keywords": []}])
        result = app.normalize_model_result(
            {"criteria": [{
                "criterion_id": "C1",
                "suggested_score": 2,
                "decision": "full",
                "student_evidence": "答案证据",
                "evidence": {"photo_index": 1, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.2},
            }]},
            criteria,
        )
        self.assertFalse(result["blocked"])
        self.assertEqual(result["criteria"][0]["evidence"]["photo_index"], 1)
        invalid = app.normalize_model_result(
            {"criteria": [{"criterion_id": "C1", "suggested_score": 2, "student_evidence": "答案证据"}]},
            criteria,
        )
        self.assertTrue(invalid["blocked"])
        self.assertIn("证据框", " ".join(invalid["block_reasons"]))
        nonfinite_box = app.normalize_model_result(
            {"criteria": [{
                "criterion_id": "C1", "suggested_score": 2, "decision": "full", "student_evidence": "答案证据",
                "evidence": {"photo_index": 1, "x": float("nan"), "y": 0.2, "width": 0.3, "height": 0.2},
            }]},
            criteria,
        )
        self.assertTrue(nonfinite_box["blocked"])
        self.assertIsNone(nonfinite_box["criteria"][0]["evidence"])

    def test_evidence_crop_is_derived_from_processed_photo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.configure(temp)
            photo_path = root / "data" / "a" / "students" / "s" / "page_01.jpg"
            original_path = photo_path.with_name("page_01.original")
            meta = app.save_photo(self.image_data_url((1000, 800), "JPEG"), photo_path, original_path)
            result = {
                "mode": "vision-model",
                "sub_questions": [{
                    "sub_question_id": "Q1",
                    "criteria": [{
                        "criterion_id": "C1",
                        "evidence": {"photo_index": 1, "x": 0.1, "y": 0.1, "width": 0.4, "height": 0.4},
                    }],
                }],
            }
            app.materialize_evidence_crops(result, [meta], [photo_path])
            evidence = result["sub_questions"][0]["criteria"][0]["evidence"]
            self.assertEqual(evidence["source_sha256"], meta["sha256"])
            self.assertTrue((root / evidence["crop_path"]).exists())

    def test_failed_evidence_crop_reblocks_a_previously_scored_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.configure(temp)
            photo_path = root / "data" / "a" / "students" / "broken" / "page_01.jpg"
            photo_path.parent.mkdir(parents=True, exist_ok=True)
            photo_path.write_bytes(b"not an image")
            result = {
                "mode": "vision-model", "blocked": False, "block_reasons": [],
                "sub_questions": [{
                    "sub_question_id": "Q1", "blocked": False, "block_reasons": [],
                    "criteria": [{
                        "criterion_id": "C1", "claimed_score": 2, "suggested_score": 0,
                        "student_evidence": "超出封顶但有效的作答",
                        "evidence": {"photo_index": 1, "x": .1, "y": .1, "width": .3, "height": .3},
                    }],
                }],
            }
            app.materialize_evidence_crops(result, [{"path": "data/a/students/broken/page_01.jpg"}], [photo_path])
            criterion = result["sub_questions"][0]["criteria"][0]
            self.assertTrue(result["blocked"])
            self.assertTrue(result["sub_questions"][0]["blocked"])
            self.assertIsNone(criterion["evidence"])
            self.assertEqual(criterion["claimed_score"], 2)
            self.assertIn("图片裁切失败", " ".join(result["block_reasons"]))

    def test_missing_source_photo_blocks_all_scored_candidate_evidence(self):
        result = {
            "mode": "vision-model", "blocked": False, "block_reasons": [],
            "sub_questions": [{
                "sub_question_id": "Q1", "blocked": False, "block_reasons": [],
                "criteria": [{
                    "criterion_id": "C1", "claimed_score": 2, "suggested_score": 0,
                    "student_evidence": "超出封顶但有效的作答",
                    "evidence": {"photo_index": 1, "x": .1, "y": .1, "width": .3, "height": .3},
                }],
            }],
        }
        app.materialize_evidence_crops(result, [], [])
        self.assertTrue(result["blocked"])
        self.assertIsNone(result["sub_questions"][0]["criteria"][0]["evidence"])

    def test_out_of_range_evidence_page_reblocks_scored_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.configure(temp)
            photo_path = root / "data" / "a" / "students" / "s" / "page_01.jpg"
            meta = app.save_photo(self.image_data_url(), photo_path)
            result = {
                "mode": "vision-model", "blocked": False, "block_reasons": [],
                "sub_questions": [{
                    "sub_question_id": "Q1", "blocked": False, "block_reasons": [],
                    "criteria": [{
                        "criterion_id": "C1", "claimed_score": 2, "suggested_score": 0,
                        "student_evidence": "与页码无关的模拟答案证据",
                        "evidence": {"photo_index": 2, "x": .1, "y": .1, "width": .3, "height": .3},
                    }],
                }],
            }

            app.materialize_evidence_crops(result, [meta], [photo_path])

            criterion = result["sub_questions"][0]["criteria"][0]
            self.assertTrue(result["blocked"])
            self.assertTrue(result["needs_review"])
            self.assertTrue(result["sub_questions"][0]["blocked"])
            self.assertTrue(criterion["needs_review"])
            self.assertIsNone(criterion["evidence"])
            self.assertIn("照片不存在", " ".join(result["block_reasons"]))

    def test_non_loopback_http_and_credentials_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            app.validate_model_base_url("http://192.168.1.20:8000/v1")
        with self.assertRaisesRegex(ValueError, "用户名或密码"):
            app.validate_model_base_url("https://user:pass@example.com/v1")
        normalized, origin = app.validate_model_base_url("http://127.0.0.1:8000/v1")
        self.assertEqual(normalized, "http://127.0.0.1:8000/v1")
        self.assertEqual(origin, "http://127.0.0.1:8000")

    def test_schema_migration_is_additive_and_creates_recovery_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.configure(temp)
            app.DATA_DIR.mkdir(parents=True, exist_ok=True)
            legacy = app.sqlite3.connect(app.DB_PATH)
            legacy.execute(
                "CREATE TABLE assignments (id TEXT PRIMARY KEY, name TEXT NOT NULL, question TEXT NOT NULL, standard_answer TEXT NOT NULL, criteria_json TEXT NOT NULL, sub_questions_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL)"
            )
            legacy.execute(
                "CREATE TABLE submissions (id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL, student_label TEXT NOT NULL, answer_text TEXT NOT NULL, photos_json TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            legacy.execute(
                "CREATE TABLE roster_students (id TEXT PRIMARY KEY, class_name TEXT NOT NULL, student_no TEXT NOT NULL DEFAULT '', student_name TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            legacy.execute(
                "INSERT INTO assignments VALUES ('legacy', '旧题', '材料', '答案', '[]', '[]', 'now')"
            )
            legacy.execute(
                "INSERT INTO submissions VALUES ('old-record', 'legacy', '旧学生', '', '[]', '{\"suggested_total\":2}', 'old-time')"
            )
            legacy.commit()
            legacy.close()
            conn = app.get_db()
            self.assertEqual(conn.execute("SELECT name FROM assignments WHERE id='legacy'").fetchone()[0], "旧题")
            self.assertEqual(conn.execute("SELECT schema_version FROM schema_meta WHERE id=1").fetchone()[0], app.DB_SCHEMA_VERSION)
            old_record = conn.execute("SELECT result_json, batch_id, batch_sequence FROM submissions WHERE id='old-record'").fetchone()
            self.assertEqual(old_record["result_json"], '{"suggested_total":2}')
            self.assertIsNone(old_record["batch_id"])
            self.assertIsNone(old_record["batch_sequence"])
            columns = {row[1] for row in conn.execute("PRAGMA table_info(submissions)")}
            self.assertIn("idempotency_key", columns)
            conn.close()
            migration_copies = list((root / "backups").glob("pre-migration-*.sqlite3"))
            self.assertEqual(len(migration_copies), 1)

    def test_key_binding_never_reports_unbound_local_file_as_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            self.configure(temp)
            local = Path(temp) / "local-app-data"
            with patch.dict("os.environ", {"LOCALAPPDATA": str(local), "HISTORY_GRADER_BASE_URL": "https://api.example.com/v1"}, clear=False):
                path = app.api_key_path("deepseek")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("not-a-real-key-file", encoding="ascii")
                self.assertFalse(app.local_api_key_exists("deepseek"))

    def test_custom_model_presets_round_trip_without_secret_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.configure(temp)
            preset = app.normalize_custom_model_preset(
                {
                    "label": "本地视觉模型",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "model": "history-vision-test",
                    "note": "只用于单元测试",
                    "api_key": "must-never-be-written",
                }
            )
            saved = app.write_custom_model_presets([preset])
            self.assertEqual(saved[0]["id"], preset["id"])
            self.assertNotIn("api_key", app.CUSTOM_MODELS_PATH.read_text(encoding="utf-8").casefold())
            loaded = app.read_custom_model_presets()
            self.assertEqual(loaded[0]["model"], "history-vision-test")
            self.assertTrue(loaded[0]["user_defined"])
            with patch.dict(
                os.environ,
                {
                    "HISTORY_GRADER_PROVIDER": "custom",
                    "HISTORY_GRADER_BASE_URL": "http://127.0.0.1:8000/v1",
                    "HISTORY_GRADER_MODEL": "history-vision-test",
                },
                clear=False,
            ):
                self.assertEqual(app.current_model_preset_id(), preset["id"])

    def test_custom_model_preset_rejects_untrusted_http_endpoint(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            app.normalize_custom_model_preset(
                {
                    "label": "不安全接口",
                    "base_url": "http://192.168.1.20:8000/v1",
                    "model": "vision",
                }
            )


if __name__ == "__main__":
    unittest.main()
