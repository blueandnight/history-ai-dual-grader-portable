import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

import app


NEW_PRESETS = {
    "mimo-v26-pro": "mimo-v2.6-pro",
    "qwen35-plus": "qwen3.5-plus",
    "qwen36-plus": "qwen3.6-plus",
    "qwen37-plus": "qwen3.7-plus",
    "qwen3-vl-plus": "qwen3-vl-plus",
    "gemini-31-pro-preview": "gemini-3.1-pro-preview",
    "openai-gpt-54": "gpt-5.4",
    "openai-gpt-54-mini": "gpt-5.4-mini",
}


class ModelCatalogExpansionTests(unittest.TestCase):
    def test_new_models_can_be_saved_and_loaded_on_either_side(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(app, "CONFIG_DIR", Path(temporary)):
            for side in ("A", "B"):
                for preset_id, model in NEW_PRESETS.items():
                    with self.subTest(side=side, preset=preset_id):
                        selected = app.write_dual_review_preset_ids(side, preset_id)
                        self.assertEqual(app.read_dual_review_preset_ids(), selected)
                        spec = app.dual_review_model_specs()[side]
                        self.assertEqual(spec["model"], model)
                        self.assertEqual(spec["preset_id"], preset_id)
                        self.assertTrue(spec["api_origin"].startswith("https://"))
            self.assertEqual(len(set(app.DUAL_REVIEW_AVAILABLE_PRESETS)), 18)
            self.assertTrue(app.MODEL_PRESETS["gemini-31-pro-preview"]["experimental"])

    def test_default_pair_and_existing_payload_options_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(app, "CONFIG_DIR", Path(temporary)):
            self.assertEqual(app.read_dual_review_preset_ids(), {"A": "deepseek-v41-flash", "B": "mimo-v26-flash"})
            with self.assertRaises(ValueError):
                app.write_dual_review_preset_ids("A", "custom")
        expected = {
            ("deepseek", "deepseek-flash"): {"thinking": {"type": "disabled"}},
            ("mimo", "mimo-v2.6-flash"): {"thinking": {"type": "disabled"}},
            ("qwen", "qwen3.8-max"): {"reasoning_effort": "low"},
            ("qwen", "qwen3.5-flash"): {},
            ("openai", "gpt-4.1"): {},
            ("google", "gemini-3.8-flash"): {},
        }
        for arguments, options in expected.items():
            self.assertEqual(app.deepseek_json_payload_options(*arguments), options)

    def test_new_model_requests_use_compatible_parameters_and_same_grading_input(self):
        questions = app.clean_sub_questions([{
            "id": "Q1", "label": "（1）", "question": "识别图中字母",
            "standard_answer": "A", "criteria": [{"id": "C1", "label": "写出A", "score": 1}],
        }])
        response_json = json.dumps({"sub_questions": [{
            "sub_question_id": "Q1", "criteria": [{
                "criterion_id": "C1", "suggested_score": 1, "decision": "full",
                "student_evidence": "A", "reason": "图中有A",
                "evidence": {"photo_index": 1, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.3},
            }],
        }]})
        inputs = []
        with tempfile.TemporaryDirectory() as temporary:
            image_path = Path(temporary) / "synthetic.jpg"
            Image.new("RGB", (320, 200), "white").save(image_path)
            for preset_id in NEW_PRESETS:
                with self.subTest(preset=preset_id):
                    preset = app.MODEL_PRESETS[preset_id]
                    diagnostics = {}
                    context = MagicMock()
                    with patch.object(app, "open_model_request", return_value=context) as request, patch.object(
                        app, "read_model_chat_content", return_value=response_json
                    ), patch.object(app, "preflight_private_model_diagnostic_dir", return_value=None), patch.object(
                        app, "cloud_mode_enabled", return_value=False
                    ):
                        result = app.call_vision_model(
                            {"id": "synthetic", "question": "识别图中字母"}, questions, "", [image_path],
                            diagnostics, [], False, model_config_override={
                                **preset, "api_key": "SYNTHETIC_ONLY",
                            },
                        )
                    request.assert_called_once()
                    payload = json.loads(request.call_args.args[0].data)
                    self.assertEqual(payload["model"], NEW_PRESETS[preset_id])
                    self.assertEqual(payload["response_format"], {"type": "json_object"})
                    self.assertEqual(diagnostics["schema_validation"], "passed")
                    self.assertEqual(result["sub_questions"][0]["suggested_total"], 1)
                    inputs.append(payload["messages"])
                    if preset["provider"] == "qwen":
                        self.assertFalse(payload["enable_thinking"])
                        self.assertIn("max_completion_tokens", payload)
                        self.assertNotIn("max_tokens", payload)
                        self.assertEqual(payload["temperature"], 0)
                    elif preset["provider"] == "mimo":
                        self.assertEqual(request.call_args.args[0].full_url, "https://api.xiaomimimo.com/v1/chat/completions")
                        self.assertIn("max_completion_tokens", payload)
                        self.assertNotIn("max_tokens", payload)
                    elif preset["provider"] == "openai":
                        self.assertIn("max_completion_tokens", payload)
                        self.assertNotIn("max_tokens", payload)
                        self.assertNotIn("temperature", payload)
                        self.assertEqual(payload["reasoning_effort"], "low")
                    else:
                        self.assertIn("max_tokens", payload)
                        self.assertEqual(payload["temperature"], 0)
                        self.assertNotIn("enable_thinking", payload)
                        self.assertNotIn("reasoning_effort", payload)
            self.assertTrue(all(messages == inputs[0] for messages in inputs))


if __name__ == "__main__":
    unittest.main()
