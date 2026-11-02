import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PIL import ImageDraw, ImageFont, ImageStat

import app


class LargeImageHandlingTests(unittest.TestCase):
    @staticmethod
    def jpeg_data_url(size=(640, 480)) -> str:
        output = io.BytesIO()
        Image.new("RGB", size, "white").save(output, format="JPEG")
        return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")

    def test_processing_decode_size_bounds_the_long_edge(self):
        self.assertEqual(app.processing_decode_size(4000, 3000), (3840, 2880))
        self.assertEqual(app.processing_decode_size(8000, 5000), (3840, 2400))
        self.assertEqual(app.processing_decode_size(1200, 900), (1200, 900))

    def test_jpeg_draft_plan_uses_half_scale_for_large_photos(self):
        self.assertEqual(app.jpeg_draft_size(4000, 3000), (4000, 3000))
        self.assertEqual(app.jpeg_draft_size(4500, 3500), (4500, 3500))
        self.assertEqual(app.jpeg_draft_size(5472, 3648), (2736, 1824))
        self.assertEqual(app.jpeg_draft_size(6000, 4000), (3000, 2000))
        self.assertEqual(app.jpeg_draft_size(8000, 5000), (4000, 2500))
        self.assertIsNone(app.jpeg_draft_size(4500, 4500))

    def test_24mp_jpeg_is_draft_decoded_before_full_rgb_allocation(self):
        image = Image.new("RGB", (6000, 4000), "white")
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.load_default(size=80)
        except TypeError:
            font = ImageFont.load_default()
        draw.text((420, 300), "History evidence 2026", fill=(0, 0, 0), font=font)
        draw.line((360, 470, 5600, 470), fill=(0, 0, 0), width=5)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95)

        decoded, original_size = app.decode_image_for_processing(output.getvalue(), "image/jpeg")
        self.assertEqual(original_size, (6000, 4000))
        self.assertEqual(decoded.size, (3000, 2000))
        model = app._encode_model_image(decoded, [])
        region = {"photo_index": 1, "x": 0.04, "y": 0.04, "width": 0.9, "height": 0.18}
        crop = app.crop_model_image_bytes(model, region)
        with Image.open(io.BytesIO(crop)) as cropped:
            gray = cropped.convert("L")
            histogram = gray.histogram()
            dark_ratio = sum(histogram[:96]) / max(1, sum(histogram))
            local_contrast = float(ImageStat.Stat(gray).stddev[0])
        self.assertGreater(dark_ratio, 0.005)
        self.assertGreater(local_contrast, 20.0)

    def test_non_jpeg_above_full_decode_budget_is_rejected_before_load(self):
        with patch.object(
            app, "validate_image_bytes", return_value=("PNG", (6000, 4000))
        ), patch.object(app.Image, "open") as image_open:
            with self.assertRaisesRegex(ValueError, "1500万像素"):
                app.decode_image_for_processing(b"not-decoded", "image/png")
        image_open.assert_not_called()

    def test_same_upload_is_not_base64_decoded_twice(self):
        data_url = self.jpeg_data_url()
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "page_01.jpg"
            with patch.object(app, "data_url_bytes", wraps=app.data_url_bytes) as decode:
                meta = app.save_photo(data_url, target)
                self.assertTrue(target.exists())
                raw_size = len(base64.b64decode(data_url.split(",", 1)[1]))
                self.assertEqual(meta["original_bytes"], raw_size)
                self.assertEqual(decode.call_count, 1)

    def test_multipart_client_does_not_encode_unused_original_as_base64(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        start = source.index("async function buildSubmissionBody")
        end = source.index("function drawAiRedactions", start)
        self.assertNotIn("readFileDataUrl(file)", source[start:end])

    def test_non_unit_perspective_corners_keep_page_orientation(self):
        image = Image.new("RGB", (1000, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 499, 399), fill="red")
        draw.rectangle((500, 0, 999, 399), fill="green")
        draw.rectangle((500, 400, 999, 799), fill="blue")
        draw.rectangle((0, 400, 499, 799), fill="yellow")

        corrected, transform = app.rectify_image(
            image, [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)]
        )
        self.assertAlmostEqual(corrected.width, 800, delta=2)
        self.assertAlmostEqual(corrected.height, 640, delta=2)
        self.assertEqual(transform["type"], "manual_quad")
        self.assertEqual(corrected.getpixel((20, 20)), (255, 0, 0))
        self.assertEqual(corrected.getpixel((corrected.width - 20, 20)), (0, 128, 0))
        self.assertEqual(corrected.getpixel((corrected.width - 20, corrected.height - 20)), (0, 0, 255))
        self.assertEqual(corrected.getpixel((20, corrected.height - 20)), (255, 255, 0))

    def test_small_text_survives_scaling_and_evidence_crop(self):
        image = Image.new("RGB", (4000, 3000), "white")
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.load_default(size=64)
        except TypeError:  # Older Pillow versions only expose the bitmap font.
            font = ImageFont.load_default()
        text = "History evidence 2026: material question"
        draw.text((300, 220), text, fill=(0, 0, 0), font=font)
        draw.line((250, 360, 3750, 360), fill=(0, 0, 0), width=4)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=95)

        decoded, original_size = app.decode_image_for_processing(output.getvalue(), "image/jpeg")
        self.assertEqual(original_size, (4000, 3000))
        scale = app.MAX_IMAGE_EDGE / max(decoded.size)
        if scale < 1:
            decoded = decoded.resize(
                (round(decoded.width * scale), round(decoded.height * scale)),
                Image.Resampling.LANCZOS,
            )
        model = app._encode_model_image(decoded, [])
        region = {"photo_index": 1, "x": 0.05, "y": 0.04, "width": 0.9, "height": 0.18}
        crop = app.crop_model_image_bytes(model, region)
        with Image.open(io.BytesIO(crop)) as cropped:
            gray = cropped.convert("L")
            histogram = gray.histogram()
            dark_ratio = sum(histogram[:96]) / max(1, sum(histogram))
            local_contrast = float(ImageStat.Stat(gray).stddev[0])
        self.assertGreater(dark_ratio, 0.001)
        self.assertGreater(local_contrast, 20.0)

    def test_40mp_png_is_rejected_before_full_decode(self):
        image = Image.new("L", (8000, 5000), 255)
        output = io.BytesIO()
        image.save(output, format="PNG")
        raw = output.getvalue()

        with patch.object(app.Image, "open", wraps=app.Image.open) as image_open:
            with self.assertRaisesRegex(ValueError, "1500万像素"):
                app.decode_image_for_processing(raw, "image/png")
        self.assertEqual(image_open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
