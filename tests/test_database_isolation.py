"""Guard: the test bootstrap must never default to the real project data root."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import app


class TestDatabaseIsolation(unittest.TestCase):
    def test_default_app_paths_are_process_temporary(self):
        project = Path(__file__).resolve().parents[1]
        temp_root = Path(tempfile.gettempdir()).resolve()
        self.assertTrue(Path(app.PORTABLE_ROOT).resolve().is_relative_to(temp_root))
        self.assertTrue(Path(app.DB_PATH).resolve().is_relative_to(Path(app.PORTABLE_ROOT).resolve()))
        self.assertNotEqual(Path(app.DB_PATH).resolve(), (project / "data" / "history_grading.sqlite3").resolve())

    def test_get_db_uses_the_isolated_database(self):
        conn = app.get_db()
        try:
            self.assertEqual(Path(app.DB_PATH).resolve(), Path(app.PORTABLE_ROOT).resolve() / "data" / "history_grading.sqlite3")
            self.assertTrue(Path(app.DB_PATH).exists())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
