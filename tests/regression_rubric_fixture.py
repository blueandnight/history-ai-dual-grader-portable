"""Explicit teacher-confirmation fixture for isolated regression only."""
import copy
import app


def confirm_frozen_rubric(questions, *, title, teacher_confirmed, confirmed_at):
    if not confirmed_at:
        raise ValueError("confirmation timestamp required")
    subs = [{"question_id": q["id"], "question": q["question"],
             "reference_answer": q["standard_answer"], "max_score": q["max_score"],
             "rubric_draft": copy.deepcopy(q["criteria"])} for q in questions]
    major = {"label": "regression", "prompt": "", "sub_questions": subs}
    draft = {"title": title, "objective_questions": [], "material_questions": [major]}
    confirmed, _, warnings = app._confirmed_import_payload(draft, {
        "teacher_confirmed": teacher_confirmed, "exam_name": title,
        "objective_answer_key": {}, "material_questions": [copy.deepcopy(major)]})
    # The import UI normalizes imported questions to weighted. This fixture uses
    # its confirmation validation only and keeps the existing frozen point-bank
    # configuration byte-for-byte rather than replacing it with imported content.
    for original, checked in zip(questions, confirmed["material_questions"][0]["sub_questions"]):
        assert original["question"] == checked["question"]
        assert original["standard_answer"] == checked["reference_answer"]
        assert original["max_score"] == checked["max_score"]
        assert [(p["label"],p["score"]) for p in original["criteria"]] == [(p["label"],p["score"]) for p in checked["rubric_draft"]]
    confirmed["teacher_confirmed_at"] = confirmed_at
    confirmed["regression_fixture_only"] = True
    confirmed["confirmation_basis"] = "explicit_user_authorized_frozen_rubric_confirmation"
    return confirmed, warnings
