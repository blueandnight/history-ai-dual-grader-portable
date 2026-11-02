"""Static answer-area geometry for one verified fixed Q9 exam layout.

Profiles are opt-in by exact rubric fingerprint and page count. They only
provide an initial crop preview; teachers still review the exact outbound crops.
"""

from __future__ import annotations

from typing import Any


Q9_FIXED_RUBRIC_SHA256 = "2b3c20b6a3ed770554734d128621d4380d012d5594476d265d3c9d7e1e277743"

Q9_FIXED_ANSWER_REGION_PROFILE: dict[str, Any] = {
    "profile_id": "q9-fixed-sheet-2026-10-v1",
    "rubric_sha256": Q9_FIXED_RUBRIC_SHA256,
    "page_count": 1,
    "regions": [
        # Normalized on the CCW-rotated, perspective-corrected page.
        {"region_id": "q9-17-1", "photo_index": 1, "x": 0.02, "y": 0.56, "width": 0.47, "height": 0.18, "sub_question_ids": ["Q1"]},
        {"region_id": "q9-17-2", "photo_index": 1, "x": 0.02, "y": 0.74, "width": 0.47, "height": 0.19, "sub_question_ids": ["Q2"]},
        {"region_id": "q9-17-3", "photo_index": 1, "x": 0.51, "y": 0.00, "width": 0.47, "height": 0.18, "sub_question_ids": ["Q3"]},
        {"region_id": "q9-18-1", "photo_index": 1, "x": 0.51, "y": 0.56, "width": 0.47, "height": 0.18, "sub_question_ids": ["Q4"]},
        {"region_id": "q9-18-2", "photo_index": 1, "x": 0.51, "y": 0.74, "width": 0.47, "height": 0.19, "sub_question_ids": ["Q5"]},
    ],
}


def answer_region_profile(rubric_sha256: str, page_count: int) -> dict[str, Any] | None:
    """Return a detached profile only for the verified Q9 rubric and one page."""
    if rubric_sha256 != Q9_FIXED_RUBRIC_SHA256 or int(page_count) != 1:
        return None
    return {
        **Q9_FIXED_ANSWER_REGION_PROFILE,
        "regions": [
            {
                **region,
                "coordinate_system": "ccw_rotated_page_normalized",
                "rotation_ccw": 90,
                "invocation_scope": "answer_region",
                "profile_id": Q9_FIXED_ANSWER_REGION_PROFILE["profile_id"],
            }
            for region in Q9_FIXED_ANSWER_REGION_PROFILE["regions"]
        ],
    }
