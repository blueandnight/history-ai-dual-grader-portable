from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .templates import BUILTIN_TEMPLATES, make_grid_config


OPTIONS = "ABCD"
def synthetic_font(size: int):
    # Pillow's built-in scalable font; no system-font file is copied or required.
    return ImageFont.load_default(size=size)


def render_omr_card(config: dict[str, Any], choices: list[str | None], faults: dict[str, Any] | None = None) -> Image.Image:
    faults = faults or {}
    width, height = int(config["page_width"]), int(config["page_height"])
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    label_font = synthetic_font(max(22, round(width * 0.021)))
    title_font = synthetic_font(max(26, round(width * 0.028)))
    draw.text((round(width * 0.08), round(height * 0.155)), "OBJECTIVE ANSWER SHEET / SYNTHETIC", fill="#111111", font=title_font)

    marker_size = round(width * 0.040)
    for x_norm, y_norm in config["anchors"]:
        cx, cy = round(x_norm * width), round(y_norm * height)
        draw.rectangle((cx - marker_size // 2, cy - marker_size // 2, cx + marker_size // 2, cy + marker_size // 2), fill="#000000")
    if faults.get("missing_anchor"):
        x_norm, y_norm = config["anchors"][0]
        cx, cy = round(x_norm * width), round(y_norm * height)
        pad = round(marker_size * 0.8)
        draw.rectangle((cx - pad, cy - pad, cx + pad, cy + pad), fill="#ffffff")

    signature_size = round(width * 0.026)
    for bit, (x_norm, y_norm) in zip(config["signature_bits"], config["signature_centers"]):
        cx, cy = round(x_norm * width), round(y_norm * height)
        bounds = (cx - signature_size // 2, cy - signature_size // 2, cx + signature_size // 2, cy + signature_size // 2)
        draw.rectangle(bounds, outline="#111111", width=max(3, round(width * 0.004)))
        if bit == "1":
            draw.rectangle(bounds, fill="#111111")

    answer_list = choices[:int(config["question_count"])]
    shifted = bool(faults.get("shift_rows"))
    for cell in config["question_cells"]:
        qno = int(cell["question"])
        y = round(float(cell["y"]) * height)
        x_label = round(float(cell["label_x"]) * width)
        draw.text((x_label, y - round(width * 0.012)), f"{qno:02d}.", fill="#111111", font=label_font)
        answer = answer_list[qno - 1] if qno - 1 < len(answer_list) else None
        if faults.get("blank_question") == qno:
            answer = None
        if shifted:
            answer = answer_list[qno] if qno < len(answer_list) else None
        selected = [OPTIONS.index(answer)] if answer and answer in OPTIONS else []
        if faults.get("double_mark_question") == qno:
            selected = sorted(set(selected + [((selected[0] + 1) % 4) if selected else 1]))
        for index, x_norm in enumerate(cell["option_x"]):
            cx = round(float(x_norm) * width)
            radius = round(width * float(config["bubble_radius_norm"]))
            draw.ellipse((cx - radius, y - radius, cx + radius, y + radius), outline="#252525", width=max(2, round(width * 0.002)))
            draw.text((cx - round(width * 0.006), y + radius + 4), OPTIONS[index], fill="#222222", font=synthetic_font(max(14, round(width * 0.012))))
            if index in selected:
                fill_r = round(radius * 0.72)
                draw.ellipse((cx - fill_r, y - fill_r, cx + fill_r, y + fill_r), fill="#050505")
            if faults.get("ambiguous_question") == qno and index == 0:
                draw.line((cx - radius // 2, y, cx + radius // 2, y), fill="#555555", width=max(2, radius // 4))

    if faults.get("signature_override") is not None:
        other = f"{int(faults['signature_override']):04b}"
        for bit, (x_norm, y_norm) in zip(other, config["signature_centers"]):
            cx, cy = round(x_norm * width), round(y_norm * height)
            size = signature_size
            bounds = (cx - size // 2, cy - size // 2, cx + size // 2, cy + size // 2)
            draw.rectangle(bounds, fill="white", outline="#111111", width=max(3, round(width * 0.004)))
            if bit == "1":
                draw.rectangle(bounds, fill="#111111")
    return image


def photograph(image: Image.Image, rng: random.Random, *, crop_marker: bool = False) -> bytes:
    width, height = image.size
    source = np.array(image)
    canvas_w, canvas_h = round(width * 1.24), round(height * 1.22)
    margin_x, margin_y = round(width * 0.11), round(height * 0.09)
    jitter_x = [rng.randint(-12, 12) for _ in range(4)]
    jitter_y = [rng.randint(-12, 12) for _ in range(4)]
    destination = np.float32([
        [margin_x + jitter_x[0], margin_y + jitter_y[0]],
        [margin_x + width + jitter_x[1], margin_y + jitter_y[1]],
        [margin_x + width + jitter_x[2], margin_y + height + jitter_y[2]],
        [margin_x + jitter_x[3], margin_y + height + jitter_y[3]],
    ])
    if crop_marker:
        destination[0] = [-20, -20]
    source_points = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
    matrix = cv2.getPerspectiveTransform(source_points, destination)
    warped = cv2.warpPerspective(source, matrix, (canvas_w, canvas_h), borderValue=(242, 242, 242))
    brightness = rng.uniform(0.91, 1.03)
    warped = np.clip(warped.astype(np.float32) * brightness, 0, 255).astype(np.uint8)
    warped = cv2.GaussianBlur(warped, (3, 3), rng.uniform(0.1, 0.45))
    ok, encoded = cv2.imencode(".png", warped)
    if not ok:
        raise RuntimeError("synthetic image encoder failed")
    return encoded.tobytes()


def render_handwriting_page(choices: list[str], seed: int, style: int = 0) -> Image.Image:
    rng = random.Random(seed)
    width, height = 1200, 1500
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = synthetic_font(34)
    label_font = synthetic_font(47)
    handwriting_files = ["Inkfree.ttf", "segoepr.ttf", "comic.ttf"]
    handwriting_font = ImageFont.truetype(str(FONT_ROOT / handwriting_files[style % len(handwriting_files)]), 74)
    draw.text((85, 42), "SYNTHETIC HANDWRITTEN ABCD ANSWERS", fill="#111111", font=title_font)
    for index, letter in enumerate(choices, start=1):
        y = 140 + (index - 1) * 145
        draw.text((110, y), f"{index:02d}.", fill="#222222", font=label_font)
        layer = Image.new("RGBA", (170, 110), (255, 255, 255, 0))
        layer_draw = ImageDraw.Draw(layer)
        layer_draw.text((6 + rng.randint(-4, 4), 4 + rng.randint(-5, 5)), letter, fill=(15, 15, 15, 255), font=handwriting_font)
        layer = layer.rotate(rng.uniform(-3.0, 3.0), resample=Image.Resampling.BICUBIC, expand=False)
        image.paste(layer, (245, y - 20), layer)
    return image


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_set(root: Path, count: int = 100, seed: int = 20260928) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    omr_root = root / "omr_100_single_page"
    hand_root = root / "handwriting_100_single_page"
    omr_root.mkdir(exist_ok=True)
    hand_root.mkdir(exist_ok=True)
    omr_rows: list[dict[str, Any]] = []
    hand_rows: list[dict[str, Any]] = []
    for index in range(count):
        template = BUILTIN_TEMPLATES[index % len(BUILTIN_TEMPLATES)]
        config = template["config"]
        answers = [rng.choice(OPTIONS) for _ in range(config["question_count"])]
        clean = render_omr_card(config, answers)
        content = photograph(clean, rng)
        omr_file = omr_root / f"synthetic-{index + 1:03d}.png"
        omr_file.write_bytes(content)
        omr_rows.append({"fixture_id": f"omr-{index + 1:03d}", "file": omr_file.name, "sha256": hashlib.sha256(content).hexdigest(), "template_id": template["id"], "answers": answers, "synthetic": True})

        style = index % 3
        handwritten_answers = [rng.choice(OPTIONS) for _ in range(8)]
        handwriting = render_handwriting_page(handwritten_answers, seed + index, style)
        hand_file = hand_root / f"synthetic-{index + 1:03d}.png"
        handwriting.save(hand_file, format="PNG")
        hand_rows.append({"fixture_id": f"hand-{index + 1:03d}", "file": hand_file.name, "sha256": _hash_file(hand_file), "style": style, "answers": handwritten_answers, "synthetic": True})

    negative_root = root / "omr_negative"
    negative_root.mkdir(exist_ok=True)
    template = BUILTIN_TEMPLATES[0]
    cfg = template["config"]
    base = [rng.choice(OPTIONS) for _ in range(cfg["question_count"])]
    negative_specs = [
        ("blank-answer", {"blank_question": 1}, "unanswered"),
        ("double-mark", {"double_mark_question": 1}, "multiple_marks"),
        ("erasures", {"ambiguous_question": 2}, "ambiguous_or_erasure"),
        ("unknown-layout", {"signature_override": 12}, "layout_signature_mismatch"),
        ("missing-anchor", {"missing_anchor": True}, "missing_fiducials"),
        ("question-shift", {"shift_rows": True}, "needs_review"),
    ]
    negatives: list[dict[str, Any]] = []
    for name, faults, expected in negative_specs:
        case_answers = list(base)
        if name == "erasures":
            case_answers[1] = "C"  # Keep the partial erased mark distinct from a fully filled answer.
        expected_answers: list[str | None] = list(case_answers)
        if faults.get("blank_question"):
            expected_answers[int(faults["blank_question"]) - 1] = None
        if faults.get("double_mark_question"):
            expected_answers[int(faults["double_mark_question"]) - 1] = None
        if faults.get("ambiguous_question"):
            expected_answers[int(faults["ambiguous_question"]) - 1] = None
        if faults.get("shift_rows"):
            expected_answers = case_answers[1:] + [None]
        image = render_omr_card(cfg, case_answers, faults)
        encoded = photograph(image, rng, crop_marker=bool(faults.get("crop_marker")))
        path = negative_root / f"{name}.png"
        path.write_bytes(encoded)
        negatives.append({"fixture_id": f"negative-{name}", "file": path.name, "sha256": hashlib.sha256(encoded).hexdigest(), "expected": expected, "answers": expected_answers, "synthetic": True})

    (omr_root / "truth.json").write_text(json.dumps(omr_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (hand_root / "truth.json").write_text(json.dumps(hand_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (negative_root / "truth.json").write_text(json.dumps(negatives, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"seed": seed, "omr_count": len(omr_rows), "handwriting_count": len(hand_rows), "negative_count": len(negatives), "omr": omr_rows, "handwriting": hand_rows, "negatives": negatives}


def create_new_template_fixture(root: Path, *, template_id: str = "omr-new-16-v1", signature: int = 13) -> tuple[Path, dict[str, Any]]:
    config = make_grid_config(question_count=16, columns=2, signature=signature, page_width=1360, page_height=1840)
    choices = ["A", "D", "B", "C"] * 4
    image = render_omr_card(config, choices)
    content = photograph(image, random.Random(17))
    path = root / f"{template_id}-calibration.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path, config
