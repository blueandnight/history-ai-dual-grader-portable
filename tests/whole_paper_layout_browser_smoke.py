"""Same-page objective + subjective layout: real-browser smoke on synthetic data.

No model calls, no production access; synthetic reference images and DB only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

import app
from tests import test_exam_layout_profile as fixtures

NODE_MODULES = Path(os.environ.get('NODE_PATH', str(Path(__file__).resolve().parents[1] / 'frontend' / 'node_modules')))


def main() -> None:
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture = fixtures.ExamLayoutProfileTests()
    fixture.setUp()
    # The layout fixture inserts a minimal sub_questions_json; the legacy
    # assignment list validates criteria on read, so give the synthetic exam a
    # valid teacher-created shape without changing the layout under test.
    sub_questions = []
    for major, label in ((17, "17"), (18, "18")):
        count = 3 if major == 17 else 2
        for index in range(1, count + 1):
            sub_questions.append({
                "id": f"Q{label}.{index}", "label": f"{label}.{index}", "question_type": "subjective",
                "question": f"第{label}题第{index}问", "standard_answer": "参考答案",
                "criteria": [{"id": "C1", "label": "要点一", "score": 2}, {"id": "C2", "label": "要点二", "score": 2}],
                "scoring_mode": "weighted", "point_value": 2.0, "max_score": 4,
            })
    conn = app.get_db()
    try:
        conn.execute(
            "UPDATE assignments SET sub_questions_json=? WHERE id=?",
            (json.dumps(sub_questions, ensure_ascii=False), fixture.assignment_id),
        )
        conn.commit()
    finally:
        conn.close()
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.AppHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    env = os.environ.copy()
    env["NODE_PATH"] = str(NODE_MODULES)
    try:
        for width, mode in ((1280, "full"), (390, "compact")):
            result = subprocess.run(
                ["node", str(Path(__file__).with_suffix(".js")), base, str(output / f"ui-{width}"), str(width), mode],
                capture_output=True, text=True, encoding="utf-8", timeout=180, env=env,
            )
            assert result.returncode == 0, result.stdout + "\n" + result.stderr

        conn = app.get_db()
        try:
            row = conn.execute(
                "SELECT layout_profile_id, layout_profile_json FROM grading_exams WHERE id=?",
                (fixture.exam_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None and row["layout_profile_id"], "layout profile was not saved"
        profile = json.loads(row["layout_profile_json"])
        types = [region["region_type"] for region in profile["regions"]]
        assert sorted(types) == ["handwritten_abcd", "handwritten_abcd", "subjective_answer"], types
        objective = [region for region in profile["regions"] if region["region_type"] == "handwritten_abcd"]
        assert [region["question_mapping"] for region in objective] == [
            [f"Q{number}" for number in range(1, 11)],
            [f"Q{number}" for number in range(11, 17)],
        ]
        assert [region["page_number"] for region in objective] == [4, 4]
        subjective = [region for region in profile["regions"] if region["region_type"] == "subjective_answer"]
        assert len(subjective) == 1
        assert subjective[0]["question_id"] == "17.1"
        assert subjective[0]["page_number"] == 3
        from layout_profile import validate_layout_profile
        validate_layout_profile(profile)
        (output / "summary.json").write_text(json.dumps({
            "desktop": "PASS",
            "mobile390": "PASS",
            "objective_regions": 2,
            "objective_question_count": 16,
            "subjective_regions": 1,
            "model_calls": 0,
            "production_access": False,
            "profile_id_present": True,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("PASS desktop 1280 + mobile 390; objective 16/16 + subjective 17.1 persisted; 0 model calls")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        try:
            fixture.tearDown()
        except PermissionError:
            # Windows can keep the temporary SQLite file open until the last
            # handler thread exits; the smoke result itself is already recorded.
            pass


if __name__ == "__main__":
    main()
