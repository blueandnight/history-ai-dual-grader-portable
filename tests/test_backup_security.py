import json
import threading
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import app


class BackupAndMobileAccessTests(unittest.TestCase):
    def setUp(self):
        self.previous_paths = app.APP_PATHS
        app.revoke_mobile_token()

    def tearDown(self):
        app._install_paths(self.previous_paths)
        app.revoke_mobile_token()
        with app.ACTIVE_TASKS_CONDITION:
            app.ACTIVE_TASKS = 0
            app.TASK_DRAINING = False
            app.ACTIVE_TASKS_CONDITION.notify_all()

    def configure_temp(self, temp: str) -> Path:
        root = Path(temp) / "portable"
        app.configure_paths(root, Path(temp) / "program")
        return root

    def seed_data(self, root: Path, assignment_id: str = "before") -> None:
        conn = app.get_db()
        conn.execute(
            "INSERT INTO assignments (id, name, question, standard_answer, criteria_json, sub_questions_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (assignment_id, "测试题", "测试材料", "测试答案", "[]", "[]", "now"),
        )
        conn.commit()
        conn.close()
        photo = root / "data" / assignment_id / "students" / "student" / "page_01.jpg"
        photo.parent.mkdir(parents=True, exist_ok=True)
        photo.write_bytes(b"portable-photo-test")

    def test_backup_contains_data_only_and_no_key_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.configure_temp(temp)
            self.seed_data(root)
            archive_path = app.create_backup_archive()

            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))

            self.assertTrue(all(name == "manifest.json" or name.startswith("data/") for name in names))
            self.assertFalse(any("api-key" in name.casefold() or name.casefold().endswith(".dpapi") for name in names))
            self.assertFalse(manifest["contains_api_key"])
            self.assertFalse(manifest["includes_config"])
            self.assertEqual(names.count("data/history_grading.sqlite3"), 1)

    def test_backup_rejects_unlisted_data_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.configure_temp(temp)
            self.seed_data(root)
            archive_path = app.create_backup_archive()
            with zipfile.ZipFile(archive_path, "a") as archive:
                archive.writestr("data/unlisted.bin", b"not in the manifest")
            with self.assertRaisesRegex(ValueError, "清单不一致"):
                app.validate_backup_archive(archive_path)

    def test_backup_requires_file_hash_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.configure_temp(temp)
            self.seed_data(root)
            source_path = app.create_backup_archive()
            incomplete_path = root / "backups" / "missing-files-manifest.zip"
            with zipfile.ZipFile(source_path, "r") as source, zipfile.ZipFile(incomplete_path, "w") as target:
                manifest = json.loads(source.read("manifest.json").decode("utf-8"))
                manifest.pop("files", None)
                target.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
                for info in source.infolist():
                    if info.filename != "manifest.json":
                        target.writestr(info, source.read(info.filename))
            with self.assertRaisesRegex(ValueError, "文件清单"):
                app.validate_backup_archive(incomplete_path)

    def test_restore_round_trip_keeps_current_instance_and_creates_safety_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.configure_temp(temp)
            instance_id = app.ensure_portable_instance_id()
            self.seed_data(root)
            archive_path = app.create_backup_archive()

            conn = app.get_db()
            conn.execute(
                "INSERT INTO assignments (id, name, question, standard_answer, criteria_json, sub_questions_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("after", "修改后的题", "材料", "答案", "[]", "[]", "now"),
            )
            conn.commit()
            conn.close()

            restored_archive, safety_backup = app.restore_backup_archive(archive_path.name)
            self.assertEqual(restored_archive.name, archive_path.name)
            self.assertTrue(safety_backup.exists())
            self.assertEqual(instance_id, app.ensure_portable_instance_id())
            conn = app.get_db()
            assignment_ids = {row[0] for row in conn.execute("SELECT id FROM assignments")}
            conn.close()
            self.assertEqual(assignment_ids, {"before"})
            self.assertTrue((root / "data" / "before" / "students" / "student" / "page_01.jpg").exists())
            self.assertEqual(len(app.list_backups()), 2)

    def test_restore_rejects_unsafe_archive_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.configure_temp(temp)
            app.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            archive_path = app.BACKUP_DIR / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("manifest.json", json.dumps({"format": "history-grader-backup-v1"}))
                archive.writestr("data/../escape.txt", "no")
            with self.assertRaisesRegex(ValueError, "不安全"):
                app.validate_backup_archive(archive_path)
            self.assertTrue(root.exists())

    def test_restore_failure_preserves_original_data_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.configure_temp(temp)
            self.seed_data(root)
            archive_path = app.create_backup_archive()
            original_rename = Path.rename

            def fail_replacement_and_rollback(path, target):
                target = Path(target)
                if target == root / "data" and (
                    path.parent.name.startswith(".restore-staging-")
                    or path.name.startswith(".data-before-restore-")
                ):
                    raise OSError("simulated data directory rename failure")
                return original_rename(path, target)

            with patch.object(Path, "rename", new=fail_replacement_and_rollback):
                with self.assertRaisesRegex(RuntimeError, "原数据保留"):
                    app.restore_backup_archive(archive_path.name)
            preserved = list(root.glob(".data-before-restore-*"))
            self.assertEqual(len(preserved), 1)
            self.assertTrue((preserved[0] / "history_grading.sqlite3").exists())

    def test_safe_shutdown_waits_for_active_task(self):
        started = threading.Event()
        release = threading.Event()

        def worker():
            with app.active_task("test"):
                started.set()
                release.wait(timeout=2)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        self.assertFalse(app.wait_for_active_tasks(0.05))
        release.set()
        thread.join(timeout=2)
        self.assertTrue(app.wait_for_active_tasks(0.2))

    def test_restore_blocks_new_background_tasks(self):
        with patch.object(app, "RESTORE_IN_PROGRESS", True):
            with self.assertRaises(app.ServiceDrainingError):
                with app.active_task("grade_submission"):
                    pass

    def test_mobile_token_is_temporary_and_memory_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.configure_temp(temp)
            self.assertEqual(app.MOBILE_TOKEN_TTL_SECONDS, 45 * 60)
            token, expires_at = app.issue_mobile_token()
            self.assertTrue(app.valid_mobile_token(token))
            self.assertFalse((root / "mobile-token").exists())
            with patch.object(app.time, "time", return_value=expires_at + 1):
                self.assertFalse(app.valid_mobile_token(token))
            app.revoke_mobile_token()
            self.assertFalse(app.valid_mobile_token(token))

    def test_storage_snapshot_reports_data_and_free_space(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.configure_temp(temp)
            self.seed_data(root)
            snapshot = app.storage_snapshot()
            self.assertGreater(snapshot["data_bytes"], 0)
            self.assertGreater(snapshot["disk_free_bytes"], 0)
            self.assertEqual(Path(snapshot["data_dir"]), root / "data")


if __name__ == "__main__":
    unittest.main()
