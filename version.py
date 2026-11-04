"""Shared application and data-format versions.

The portable build script replaces only GIT_COMMIT while packaging, then
restores this source file. Runtime builds never need to inspect a .git folder.
"""

APP_VERSION = "0.2.2-beta.2"
GIT_COMMIT = "unknown"
RUN_SCHEMA_VERSION = "submission-result-v5"
TEACHER_REVIEW_SCHEMA_VERSION = "teacher-review-v2"
