from __future__ import annotations

from typing import Any

from opik.evaluation.metrics import base_metric, score_result

from eval_common import (
    duration_metrics,
    normalize_production_text,
    pass_fail_score,
    prompt_field,
    safe_str,
    score_textual_structural_compliance,
)


def build_textual_eval_prompt(dataset_item: dict) -> tuple[str, str]:
    question_text = normalize_production_text(dataset_item.get("question_text"))
    user_answer = normalize_production_text(dataset_item.get("user_answer"))

    evaluation_objective = prompt_field(dataset_item.get("evaluation_objective"))
    evaluation_objective = evaluation_objective.replace("{question_text}", question_text)
    evaluation_objective = evaluation_objective.replace("{user_answer}", user_answer)

    user_sections = [evaluation_objective]
    for key, label in (
        ("general_evaluation_criteria", None),
        ("specific_evaluation_criteria", "Specific Evaluation Criteria"),
        ("critical_cases", "Critical Cases"),
        ("feedback_guidelines", "Feedback Guidelines"),
    ):
        block = prompt_field(dataset_item.get(key))
        if block:
            user_sections.append(f"{label}:\n{block}" if label else block)

    response_format = prompt_field(dataset_item.get("response_format"))
    if response_format:
        user_sections.append(response_format)

    return prompt_field(dataset_item.get("meta_role")), "\n\n".join(user_sections)


def _compose_rubric(**fields: str) -> str:
    parts = []
    for title, text in fields.items():
        if text:
            parts.append(f"## {title}\n{text}")
    return "\n\n".join(parts)


class StructuralComplianceMetric(base_metric.BaseMetric):
    def __init__(self, name: str = "structural_compliance"):
        self.name = name

    def score(self, output: str, response_format: str = "", **_: Any):
        if not prompt_field(response_format):
            return pass_fail_score(self.name, True, "no response_format declared; skipped check")
        return score_textual_structural_compliance(output, response_format)


class RubricComplianceMetric(base_metric.BaseMetric):
    """Single judge for general + specific + critical criteria."""

    def __init__(self, judge_fn, name: str = "rubric_compliance"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
You audit whether the evaluator's similarity_percentage and scoring logic followed the rubric below
(general, specific, critical only). Judge SCORING behavior only — not the wording of appreciation/improvement feedback
(that is a separate metric).

Score 1 if the assigned score and overall evaluation logic are reasonable under the rubric.
Score 0 only if you can quote an explicit rubric rule that the evaluator clearly violated when choosing the score
or when applying a scoring band (e.g. score outside a stated band, score 0 rule ignored, irrelevant answer scored high).

Rules:
- The rubric text is authoritative: if specific/critical sections mention format or bands, apply them even if the
  question text is shorter.
- Do NOT fail because improvement_feedback text is too long, too encouraging, or suggests optional polish.
- Do NOT fail because feedback mentions numbered lists or format unless the rubric required that score treatment
  and the evaluator assigned a score inconsistent with that rule.
- Student answer capitalization/punctuation is case-insensitive; do not fail on that basis.
- If you cannot quote a violated rubric sentence, score 1.

Question: {question}
Student answer: {student_answer}

Rubric:
{rubric}

Evaluator output:
{output}

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(
        self,
        output: str,
        question: str = "",
        student_answer: str = "",
        general_evaluation_criteria: str = "",
        specific_evaluation_criteria: str = "",
        critical_cases: str = "",
        feedback_guidelines: str = "",
        **_: Any,
    ):
        rubric = _compose_rubric(
            general=safe_str(general_evaluation_criteria),
            specific=safe_str(specific_evaluation_criteria),
            critical=safe_str(critical_cases),
        )
        if not rubric.replace("#", "").strip():
            return score_result.ScoreResult(
                name=self.name, value=None, reason="N/A: no rubric sections"
            )
        prompt = self.prompt_template.format(
            question=safe_str(question),
            student_answer=safe_str(student_answer),
            rubric=rubric,
            output=output,
        )
        result = self._judge(prompt)
        return score_result.ScoreResult(
            name=self.name, value=float(result["score"]), reason=result["reason"]
        )


class ScoringConsistencyMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "scoring_consistency"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Check that similarity_percentage is consistent with (1) the appreciation/improvement feedback text and
(2) explicit numeric score bands or thresholds stated in the rubric (quote the band if you use one).

Question: {question}
Student answer: {student_answer}

Rubric (score bands and scoring rules only):
{rubric}

Evaluator output:
{output}

Score 1 if the numeric score matches the feedback tone/severity AND respects any explicit band you cite.
Score 0 if feedback praises a perfect answer but score is very low, or feedback is harsh but score is very high,
or the score clearly violates an explicit band rule you quote from the rubric.

Do NOT fail based on student capitalization/punctuation (case-insensitive evaluation).
If the rubric has no explicit bands, judge only score-vs-feedback alignment.

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(
        self,
        output: str,
        question: str = "",
        student_answer: str = "",
        general_evaluation_criteria: str = "",
        specific_evaluation_criteria: str = "",
        critical_cases: str = "",
        **_: Any,
    ):
        rubric = _compose_rubric(
            general=safe_str(general_evaluation_criteria),
            specific=safe_str(specific_evaluation_criteria),
            critical=safe_str(critical_cases),
        )
        prompt = self.prompt_template.format(
            question=safe_str(question),
            student_answer=safe_str(student_answer),
            rubric=rubric or "(no explicit bands)",
            output=output,
        )
        result = self._judge(prompt)
        return score_result.ScoreResult(
            name=self.name, value=float(result["score"]), reason=result["reason"]
        )


class FeedbackRelevanceMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "feedback_relevance"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Check feedback_guidelines compliance in the evaluator JSON output (appreciation_feedback and improvement_feedback).

Question: {question}
Student answer: {student_answer}

Feedback guidelines:
{feedback_guidelines}

Evaluator output:
{output}

Score 1 unless improvement_feedback contains a CLEAR violation:

PASS (score 1):
- Encouragement only in improvement_feedback ("keep up the good work", "no major improvements needed",
  praise with no actionable edit) — guidelines often ask to encourage correct answers this way.
- Minor optional stylistic polish when the answer/prompt is already complete (e.g. "Act as" vs "think you are",
  slight rephrasing) if it does not claim a required element is missing.
- Strong improvement guidance when the student answer is weak, irrelevant, or incomplete (including sample questions).

FAIL (score 0) only when improvement_feedback:
- Tells the student to add elements that were NOT required or treats mutually exclusive options as all required
  (e.g. question offers Format A/B/C as choices but feedback demands A+B+C).
- Contradicts the guidelines for a complete, correct answer by pushing substantive missing work that the
  question did not ask for.
- Uses "the candidate" instead of "You", or links external tutorials/resources.

Also verify: irrelevant answers should get sample help in improvement_feedback when guidelines require it.
Ignore student answer capitalization (may be lowercased in pipeline).

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(
        self,
        output: str,
        question: str = "",
        student_answer: str = "",
        feedback_guidelines: str = "",
        **_: Any,
    ):
        guidelines = safe_str(feedback_guidelines)
        if not guidelines:
            return score_result.ScoreResult(
                name=self.name, value=None, reason="N/A: no feedback_guidelines"
            )
        prompt = self.prompt_template.format(
            question=safe_str(question),
            student_answer=safe_str(student_answer),
            feedback_guidelines=guidelines,
            output=output,
        )
        result = self._judge(prompt)
        return score_result.ScoreResult(
            name=self.name, value=float(result["score"]), reason=result["reason"]
        )


def build_textual_metrics(judge_fn):
    return [
        RubricComplianceMetric(judge_fn),
        ScoringConsistencyMetric(judge_fn),
        FeedbackRelevanceMetric(judge_fn),
        StructuralComplianceMetric(),
        *duration_metrics(),
    ]


def textual_evaluation_task_fields(dataset_item: dict) -> dict:
    # Judges see original casing; grader prompt uses normalize_production_text in build_textual_eval_prompt.
    q = prompt_field(dataset_item.get("question_text"))
    a = prompt_field(dataset_item.get("user_answer"))
    return {
        "question": q,
        "student_answer": a,
        "general_evaluation_criteria": prompt_field(dataset_item.get("general_evaluation_criteria")),
        "specific_evaluation_criteria": prompt_field(dataset_item.get("specific_evaluation_criteria")),
        "critical_cases": prompt_field(dataset_item.get("critical_cases")),
        "feedback_guidelines": prompt_field(dataset_item.get("feedback_guidelines")),
        "response_format": prompt_field(dataset_item.get("response_format")),
    }
