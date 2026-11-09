"""Real-browser smoke for the text-only AI import parse (synthetic DB, mocked model)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import app
from exam_import_ai import build_exam_import_ai_source
from tests.test_exam_import_ai import raw_ai_result, synthetic_draft

NODE_MODULES = Path(os.environ.get('NODE_PATH', str(Path(__file__).resolve().parents[1] / 'frontend' / 'node_modules')))


def main() -> None:
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    draft_id = "a" * 32
    draft = synthetic_draft()
    draft["source"] = {
        "content_source": {"type": "docx", "file_name": "合成.docx", "sha256": "a" * 64},
        "layout_source": None,
        "source_pair_status": "not_applicable",
        "source_conflicts": [],
    }
    (output / "draft.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    scoring_text = "评分标准：史实、论证。0–4分：史实不足。5–8分：史实支持观点。"
    examples = ["范例一：从制度角度论证。", "范例二：从交流角度论证。"]
    source = build_exam_import_ai_source("\n".join([*examples, scoring_text]), ["合成 PDF 文字"])
    model_result = raw_ai_result(26)
    model_result["subjective_questions"].append({
        "question_id": "19.1", "question": "自拟论题并论证", "question_type": "essay", "max_score": 8,
        "reference_examples": examples,
        "scoring_guide": {"mode": "holistic", "source_quote": scoring_text, "dimensions": ["史实", "论证"],
                          "levels": [{"min_score": 0, "max_score": 4, "description": "史实不足"},
                                     {"min_score": 5, "max_score": 8, "description": "史实支持观点"}]},
    })
    config = {
        "provider": "synthetic", "provider_label": "合成模型", "model": "synthetic-model",
        "base_url": "https://api.example.test/v1", "api_key": "synthetic-key", "timeout": "30",
        "model_role": "AI-A",
    }
    with tempfile.TemporaryDirectory() as temp:
        old_paths = app.APP_PATHS
        app.configure_paths(Path(temp) / "portable", old_paths.program_dir)
        draft_dir = app.DATA_DIR / "exam_import_drafts" / draft_id
        draft_dir.mkdir(parents=True)
        (draft_dir / "exam_import_draft.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.AppHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        env = os.environ.copy()
        env["NODE_PATH"] = str(NODE_MODULES)
        try:
            with patch.object(app, "_exam_import_ai_source", return_value=source), \
                 patch.object(app, "rubric_generation_model_config", return_value=config), \
                 patch.object(app, "model_chat_completions_endpoint", return_value="https://api.example.test/v1/chat/completions"), \
                 patch.object(app, "call_exam_import_ai_model", return_value=model_result) as model_call, \
                 patch.object(app, "cached_local_network_interfaces", return_value=[]), \
                 patch.object(app, "append_audit_event"):
                for width in (1280,390):
                    # Restore the frozen server draft for each independent viewport.
                    (draft_dir / "exam_import_draft.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
                    result = subprocess.run(
                        ["node", str(Path(__file__).with_suffix(".js")), base, str(output / f"ui-{width}"),
                         str(width), str(output / "draft.json")],
                        capture_output=True, text=True, encoding="utf-8", timeout=180, env=env,
                    )
                    assert result.returncode == 0, result.stdout + "\n" + result.stderr
            assert model_call.call_count == 4
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            app._install_paths(old_paths)
    result_data = json.loads((output / "ui-1280.json").read_text(encoding="utf-8"))
    assert result_data["passed"] is True
    assert result_data["objective_controls"] == 26
    (output / "summary.json").write_text(json.dumps({
        "browser": "PASS",
        "ai_parse_button": "PASS",
        "confirm_dialog": "PASS",
        "objective_controls_after_ai": 26,
        "image_parts_sent": False,
        "model_calls": "mocked",
        "errors": result_data["errors"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS browser AI import parse: 26 objective controls, no image payload, no page errors")


if __name__ == "__main__":
    main()
