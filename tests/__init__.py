"""Test bootstrap: every test process starts on an isolated portable root.

The prior acceptance run observed a production-data file mtime change during a
test process.  Tests that call ``app.get_db()`` before a fixture configures its
own temporary root were the risk.  Redirect the default app paths once, before
any test module imports app code, so a stray/default ``get_db()`` can only open
the per-process temporary database.  Individual tests may still call
``configure_paths`` for their own isolated roots.
"""
from __future__ import annotations

import atexit
import tempfile
from pathlib import Path

import app

_TEST_ROOT = tempfile.TemporaryDirectory(prefix="history-grader-tests-", ignore_cleanup_errors=True)
app.configure_paths(
    portable_root=Path(_TEST_ROOT.name) / "portable",
    program_dir=app.default_program_dir(),
)
atexit.register(_TEST_ROOT.cleanup)
