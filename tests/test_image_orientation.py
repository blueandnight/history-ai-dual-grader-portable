"""Auto-upright detection for sideways answer page uploads (synthetic images only)."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

import app
import image_orientation


def ruled_page(size=(1200, 900)):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    for index in range(1, 12):
        y = round(size[1] * index / 13)
        draw.line((size[0] * 0.08, y, size[0] * 0.92, y), fill="black", width=3)
    draw.rectangle((size[0] * 0.05, size[1] * 0.05, size[0] * 0.1, size[1] * 0.09), fill="black")
    return image


def jpeg_bytes(image):
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


def signature(score, footer=None):
    return {"score": score, "footer_position": footer}


class OrientationTests(unittest.TestCase):
    def setUp(self):
        self.previous_paths = app.APP_PATHS
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name) / "portable"
        app.configure_paths(root, Path(self.temp.name) / "program")

    def tearDown(self):
        app._install_paths(self.previous_paths)
        self.temp.cleanup()

    def test_upright_page_is_not_rotated(self):
        rotation, detail = image_orientation.detect_upright_rotation(ruled_page())
        self.assertEqual(rotation, 0)
        self.assertEqual(detail["basis"], "rule_lines")

    def test_footer_position_decides_direction(self):
        sideways = ruled_page().rotate(90, expand=True)
        with patch.object(
            image_orientation, "_text_signature",
            side_effect=[signature(120, footer=0.82), signature(120, footer=0.12)],
        ):
            rotation, detail = image_orientation.detect_upright_rotation(sideways)
        self.assertEqual(rotation, 90)
        self.assertEqual(detail["basis"], "footer_position")
        with patch.object(
            image_orientation, "_text_signature",
            side_effect=[signature(120, footer=0.10), signature(120, footer=0.85)],
        ):
            rotation, detail = image_orientation.detect_upright_rotation(sideways)
        self.assertEqual(rotation, -90)
        self.assertEqual(detail["basis"], "footer_position")

    def test_text_yield_decides_when_footer_is_missing(self):
        sideways = ruled_page().rotate(90, expand=True)
        with patch.object(image_orientation, "_text_signature", side_effect=[signature(200), signature(50)]):
            rotation, detail = image_orientation.detect_upright_rotation(sideways)
        self.assertEqual(rotation, 90)
        self.assertEqual(detail["basis"], "ocr")
        with patch.object(image_orientation, "_text_signature", side_effect=[signature(50), signature(200)]):
            rotation, detail = image_orientation.detect_upright_rotation(sideways)
        self.assertEqual(rotation, -90)

    def test_inconclusive_direction_defaults_without_blocking(self):
        sideways = ruled_page().rotate(90, expand=True)
        with patch.object(image_orientation, "_text_signature", side_effect=[signature(100), signature(100)]):
            rotation, detail = image_orientation.detect_upright_rotation(sideways)
        self.assertEqual(rotation, 90)
        self.assertEqual(detail["basis"], "tie_default_ccw")
        self.assertEqual(detail["confidence"], "low")

    def test_sideways_upload_is_stored_upright_and_records_orientation(self):
        sideways = ruled_page().rotate(90, expand=True)
        target = Path(self.temp.name) / "page.jpg"
        original = Path(self.temp.name) / "page.original"
        with patch.object(image_orientation, "_text_signature", side_effect=[signature(200), signature(50)]):
            meta = app.save_photo_bytes(jpeg_bytes(sideways), "image/jpeg", target, original)
        self.assertGreater(meta["width"], meta["height"])
        self.assertEqual(meta["transform"]["type"], "auto_orient_and_resize")
        self.assertEqual(meta["transform"]["orientation"]["rotation"], 90)
        with Image.open(target) as saved:
            self.assertGreater(saved.width, saved.height)

    def test_auto_orientation_can_be_disabled(self):
        sideways = ruled_page().rotate(90, expand=True)
        target = Path(self.temp.name) / "disabled.jpg"
        with patch.dict(app.os.environ, {"HISTORY_GRADER_AUTO_ORIENT": "0"}):
            meta = app.save_photo_bytes(jpeg_bytes(sideways), "image/jpeg", target, None)
        self.assertLess(meta["width"], meta["height"])
        self.assertEqual(meta["transform"]["type"], "exif_transpose_and_resize")

    def test_privacy_preview_uses_the_same_orientation_as_the_stored_page(self):
        sideways = ruled_page().rotate(90, expand=True)
        with patch.object(image_orientation, "_text_signature", side_effect=[signature(200), signature(50)]):
            preview = app.prepare_ai_preview_image_bytes(jpeg_bytes(sideways), "image/jpeg", [])
        with Image.open(io.BytesIO(preview)) as image:
            self.assertGreater(image.width, image.height)


if __name__ == "__main__":
    unittest.main()
