from __future__ import annotations

from typing import Any


def make_grid_config(
    *,
    question_count: int,
    columns: int,
    signature: int,
    page_width: int = 1200,
    page_height: int = 1680,
) -> dict[str, Any]:
    if not 1 <= question_count <= 100:
        raise ValueError("question_count must be between 1 and 100")
    if columns not in (1, 2):
        raise ValueError("columns must be 1 or 2")
    if not 1 <= signature <= 14:
        raise ValueError("signature must be between 1 and 14")
    if page_width < 800 or page_height < 1000:
        raise ValueError("template page is too small")

    rows_per_column = (question_count + columns - 1) // columns
    cells: list[dict[str, Any]] = []
    option_positions = [0.16, 0.26, 0.36, 0.46] if columns == 2 else [0.22, 0.40, 0.58, 0.76]
    for number in range(1, question_count + 1):
        col = (number - 1) // rows_per_column
        row = (number - 1) % rows_per_column
        y = 0.22 + (0.68 * row / max(rows_per_column - 1, 1))
        if columns == 1:
            xs = option_positions
            label_x = 0.09
        else:
            base = col * 0.5
            xs = [base + x for x in option_positions]
            label_x = base + 0.045
        cells.append({"question": number, "column": col, "row": row, "label_x": label_x, "y": y, "option_x": xs})

    bits = f"{signature:04b}"
    sig_xs = [0.425, 0.475, 0.525, 0.575]
    anchors = [[0.055, 0.055], [0.945, 0.055], [0.945, 0.945], [0.055, 0.945]]
    return {
        "schema": 1,
        "page_width": int(page_width),
        "page_height": int(page_height),
        "question_count": int(question_count),
        "columns": int(columns),
        "signature": int(signature),
        "signature_bits": bits,
        "signature_centers": [[x, 0.12] for x in sig_xs],
        "anchors": anchors,
        "bubble_radius_norm": 0.012,
        "dark_threshold": 105,
        "blank_ratio_max": 0.055,
        "mark_ratio_min": 0.23,
        "question_cells": cells,
    }


BUILTIN_TEMPLATES = (
    {
        "id": "omr-single-20-v1",
        "name": "单栏20题·V1",
        "config": make_grid_config(question_count=20, columns=1, signature=3),
    },
    {
        "id": "omr-double-20-v1",
        "name": "双栏20题·V1",
        "config": make_grid_config(question_count=20, columns=2, signature=10),
    },
)
