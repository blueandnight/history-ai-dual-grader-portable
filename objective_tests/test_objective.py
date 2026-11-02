from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from objective.app import create_app
from objective_tests.auth_helpers import ObjectiveTestClient, login_client
from objective.synthetic import create_new_template_fixture, photograph, render_omr_card, render_handwriting_page
from objective.templates import BUILTIN_TEMPLATES, make_grid_config


class ObjectiveFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="objective-test-")
        self.root = Path(self.temp.name)
        self.app = create_app(self.root / "runtime")
        self.app.testing = True
        self.app.test_client_class = ObjectiveTestClient
        self.client = login_client(self.app.test_client())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_assessment(self, count: int, points: float = 1.0) -> str:
        key = [{"question_no": i, "correct_option": "A", "points": points} for i in range(1, count + 1)]
        response = self.client.post("/api/assessments", json={"title": "Synthetic test", "answer_key": key})
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["assessment_id"]

    def omr_image(self, template, choices):
        rendered = render_omr_card(template["config"], choices)
        return photograph(rendered, __import__("random").Random(11))

    def upload(self, assessment_id, images, *, grouping="one_per_image", mode="omr", template_id="omr-single-20-v1"):
        data = {"assessment_id": assessment_id, "mode": mode, "grouping": grouping}
        if mode == "omr":
            data["template_id"] = template_id
        data["files"] = [(io.BytesIO(content), f"synthetic-{i}.png") for i, content in enumerate(images)]
        response = self.client.post("/api/uploads", data=data, content_type="multipart/form-data")
        return response

    def wait_job(self, job_id):
        deadline = time.time() + 40
        while time.time() < deadline:
            response = self.client.get(f"/api/jobs/{job_id}")
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            if data["job"]["status"] in ("completed", "needs_review", "recoverable", "failed"):
                return data
            time.sleep(0.05)
        self.fail("job did not finish within 40 seconds")

    def test_single_images_are_separate_records_and_score_deterministically(self):
        template = BUILTIN_TEMPLATES[0]
        assessment_id = self.create_assessment(20, 2)
        images = [self.omr_image(template, ["A"] * 20) for _ in range(3)]
        response = self.upload(assessment_id, images)
        self.assertEqual(response.status_code, 202, response.get_json())
        job = self.wait_job(response.get_json()["job_id"])
        self.assertEqual(job["job"]["total_submissions"], 3)
        self.assertEqual([row["score"] for row in job["submissions"]], [40, 40, 40])
        self.assertTrue(all(row["state"] == "completed" for row in job["submissions"]))

    def test_explicit_multi_page_maps_pages_to_one_submission(self):
        template = BUILTIN_TEMPLATES[0]
        assessment_id = self.create_assessment(40)
        images = [self.omr_image(template, ["A"] * 20) for _ in range(2)]
        response = self.upload(assessment_id, images, grouping="multi_page_all")
        self.assertEqual(response.status_code, 202, response.get_json())
        job = self.wait_job(response.get_json()["job_id"])
        self.assertEqual(job["job"]["total_submissions"], 1)
        self.assertEqual(job["submissions"][0]["page_count"], 2)
        self.assertEqual(job["submissions"][0]["score"], 40)
        self.assertEqual(job["submissions"][0]["state"], "completed")

    def test_auto_template_selection_handles_two_confirmed_layouts_in_one_batch(self):
        assessment_id = self.create_assessment(20)
        images = [self.omr_image(template, ["A"] * 20) for template in BUILTIN_TEMPLATES]
        response = self.upload(assessment_id, images, template_id="auto")
        self.assertEqual(response.status_code, 202, response.get_json())
        job = self.wait_job(response.get_json()["job_id"])
        self.assertEqual(job["job"]["total_submissions"], 2)
        self.assertTrue(all(row["state"] == "completed" for row in job["submissions"]))
        events = self.client.get(f"/api/audit?job_id={job['job']['job_id']}").get_json()["events"]
        detected = [event["details"].get("template_id") for event in events if event["event_type"] == "recognition_completed"]
        self.assertEqual(set(detected), {"omr-single-20-v1", "omr-double-20-v1"})

    def test_unknown_layout_is_held_and_manual_change_is_audited(self):
        assessment_id = self.create_assessment(20)
        template = BUILTIN_TEMPLATES[0]
        config = template["config"]
        image = photograph(render_omr_card(config, ["A"] * 20, {"signature_override": 12}), __import__("random").Random(3))
        response = self.upload(assessment_id, [image])
        job = self.wait_job(response.get_json()["job_id"])
        submission = job["submissions"][0]
        self.assertEqual(submission["state"], "needs_review")
        queue = self.client.get(f"/api/review-queue?assessment_id={assessment_id}").get_json()
        self.assertGreater(queue["count"], 0)
        item = queue["items"][0]
        page = self.client.get(f"/api/submissions/{item['submission_id']}/pages/{item['page_no']}")
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.headers["ETag"].strip('"'))
        revised = self.client.post(f"/api/submissions/{item['submission_id']}/answers/{item['question_no']}/review", json={"option": "A", "reason": "synthetic fixture verified"})
        self.assertEqual(revised.status_code, 200, revised.get_json())
        with self.app.app_context():
            pass
        audit = self.client.get(f"/api/audit?job_id={job['job']['job_id']}").get_json()["events"]
        self.assertTrue(any(event["event_type"] == "answer_manually_revised" for event in audit))

    def test_custom_template_calibration_and_reuse(self):
        sample_path, _ = create_new_template_fixture(self.root / "fixtures")
        with sample_path.open("rb") as stream:
            response = self.client.post("/api/templates/setup", data={
                "sample": (stream, "synthetic-calibration.png"), "name": "Synthetic 16Q",
                "question_count": "16", "columns": "2", "signature": "13", "confirm": "true",
            }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 201, response.get_json())
        template_id = response.get_json()["template_id"]
        self.assertEqual(response.get_json()["confirmed"], True)
        assessment_id = self.create_assessment(16)
        choices = ["A", "D", "B", "C"] * 4
        custom = {"id": template_id, "config": make_grid_config(question_count=16, columns=2, signature=13, page_width=1360, page_height=1840)}
        image = self.omr_image(custom, choices)
        upload = self.upload(assessment_id, [image], template_id=template_id)
        job = self.wait_job(upload.get_json()["job_id"])
        self.assertEqual(job["submissions"][0]["state"], "completed")

    def test_handwriting_path_uses_real_rapidocr_and_unified_answer_rows(self):
        choices = ["A", "B", "C", "D", "A", "B", "C", "D"]
        page = render_handwriting_page(choices, 91, 1)
        output = io.BytesIO()
        page.save(output, format="PNG")
        assessment_id = self.create_assessment(8)
        response = self.upload(assessment_id, [output.getvalue()], mode="handwriting")
        self.assertEqual(response.status_code, 202, response.get_json())
        job = self.wait_job(response.get_json()["job_id"])
        detail = self.client.get(f"/api/submissions/{job['submissions'][0]['submission_id']}").get_json()
        self.assertEqual(len(detail["answers"]), 8)
        self.assertTrue(all(row["source"] == "handwriting" for row in detail["answers"]))
        if any(row["state"] == "needs_review" for row in detail["answers"]):
            self.assertTrue(all(row["state"] == "needs_review" for row in detail["answers"]))
        audit = self.client.get(f"/api/audit?job_id={job['job']['job_id']}").get_json()["events"]
        self.assertTrue(any("rapidocr-onnxruntime" in str(event["details"].get("recognizer", "")) for event in audit))


if __name__ == "__main__":
    unittest.main(verbosity=2)
