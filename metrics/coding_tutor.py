from __future__ import annotations

import re
from typing import Any

from opik.evaluation.metrics import base_metric, score_result

from eval_common import (
    VOICE_BANNED_CHARS,
    coding_tutor_json_format_check,
    coding_tutor_word_limits,
    count_dialogue_turns,
    count_words,
    detect_coding_tutor_mode,
    duration_metrics,
    extract_evaluation_result,
    extract_json_object,
    extract_trigger_reason,
    last_user_message,
    not_applicable_score,
    parse_active_coding_language,
    parse_input_messages,
    pass_fail_score,
    safe_str,
    student_code_appears_empty,
)

_SOLUTION_MARKERS = re.compile(r"## solution code|solution code:", re.I)
_GREETING_RE = re.compile(r"^(hi|hey|hello)\b", re.IGNORECASE)
_BRUTE_OPTIMAL_RE = re.compile(r"brute[\s-]?force|optimal", re.IGNORECASE)
_NO_CODE_RE = re.compile(
    r"not (?:written|started)|haven'?t started|no code|no attempt",
    re.IGNORECASE,
)


class CodingResponseFormatMetric(base_metric.BaseMetric):
    def __init__(self, name: str = "response_format_compliance"):
        self.name = name

    def score(self, output: str, **_: Any):
        ok, reason, _ = coding_tutor_json_format_check(output)
        if not ok:
            return pass_fail_score(self.name, False, reason)
        return pass_fail_score(self.name, True, "valid JSON with required keys")


class CodingWordLimitMetric(base_metric.BaseMetric):
    def __init__(self, name: str = "word_limit_by_mode"):
        self.name = name

    def score(self, output: str, system_prompt: str = "", input_messages: list = None, **_: Any):
        mode = detect_coding_tutor_mode(system_prompt, last_user_message(input_messages or []))
        lo, hi = coding_tutor_word_limits(mode)
        ok, reason, data = coding_tutor_json_format_check(output, max_reply_words=hi)
        if not ok:
            return not_applicable_score(self.name, reason)
        words = count_words(safe_str(data.get("reply")))
        if words < lo or words > hi:
            return pass_fail_score(
                self.name,
                False,
                f"{mode} mode: {words} words (expected {lo}-{hi})",
            )
        return pass_fail_score(self.name, True, f"{mode} mode word count {words}")


class VoiceModeRestrictionsMetric(base_metric.BaseMetric):
    def __init__(self, name: str = "voice_mode_restrictions"):
        self.name = name

    def score(self, output: str, system_prompt: str = "", input_messages: list = None, **_: Any):
        mode = detect_coding_tutor_mode(system_prompt, last_user_message(input_messages or []))
        if mode != "voice":
            return not_applicable_score(self.name, "not voice mode")
        ok, reason, data = coding_tutor_json_format_check(output)
        if not ok:
            return not_applicable_score(self.name, reason)
        reply = safe_str(data.get("reply"))
        bad = [c for c in reply if c in VOICE_BANNED_CHARS]
        if bad:
            return pass_fail_score(self.name, False, f"voice reply contains banned symbols: {bad[:5]}")
        return pass_fail_score(self.name, True, "voice mode symbol restrictions ok")


class CodingSessionRulesMetric(base_metric.BaseMetric):
    """Deterministic session rules: no-code copy, brute/optimal ask, dialogue closure, greeting."""

    def __init__(self, name: str = "coding_session_rules"):
        self.name = name

    def score(self, output: str, input_messages: list = None, **_: Any):
        ok, reason, data = coding_tutor_json_format_check(output)
        if not ok:
            return not_applicable_score(self.name, reason)
        student = last_user_message(input_messages or [])
        reply = safe_str(data.get("reply"))
        trigger = extract_trigger_reason(student)
        turns = count_dialogue_turns(input_messages or [])

        if turns > 1 and _GREETING_RE.search(reply.strip()):
            return pass_fail_score(self.name, False, "greeting should only appear on first activation")

        if turns > 5:
            if safe_str(data.get("should_student_reply")) != "No":
                return pass_fail_score(self.name, False, "after 5+ user turns should_student_reply must be No")
            ctas = data.get("followup_ctas", [])
            if ctas:
                return pass_fail_score(self.name, False, "followup_ctas must be empty when closing conversation")

        if student_code_appears_empty(student) and trigger in (
            "Unable to start coding",
            "User triggered",
        ):
            if not _NO_CODE_RE.search(reply):
                return pass_fail_score(self.name, False, "must state user has not written code yet")
            if not _BRUTE_OPTIMAL_RE.search(reply):
                return pass_fail_score(
                    self.name,
                    False,
                    "must ask brute-force vs optimal when no code and approach not chosen",
                )

        return pass_fail_score(self.name, True, "session rules ok")


class CodingReasonFlagMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "reason_flag_consistency"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Is the student query off-topic or unrelated to the current coding problem?

Student input: {student_input}

Return JSON: {{"reason": "...", "score": 1 if off-topic else 0}}
"""

    def score(self, output: str, input_messages: list = None, **_: Any):
        data, err = extract_json_object(output)
        if data is None:
            return not_applicable_score(self.name, err)
        flag = safe_str(data.get("reason_flag"))
        if flag not in ("Yes", "No"):
            return pass_fail_score(self.name, False, "invalid reason_flag")
        student = last_user_message(input_messages or [])
        prompt = self.prompt_template.format(student_input=student)
        result = self._judge(prompt)
        off_topic = float(result["score"]) == 1.0
        expected = "Yes" if off_topic else "No"
        if flag != expected:
            return pass_fail_score(
                self.name,
                False,
                f"reason_flag {flag} but expected {expected} ({result['reason']})",
            )
        return pass_fail_score(self.name, True, "reason_flag matches off-topic verdict")


class SolutionLeakageMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "solution_leakage"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
You audit a DSA coding tutor reply for solution leakage.

Question and reference material (includes solution — tutor must NOT reveal it):
{question_content}

Student input: {student_input}
Tutor JSON output:
{output}

Score 1 if tutor gives only Socratic hints / micro syntax fragments, no full solution or copy-pasteable answer.
Score 0 if full class/function solution, complete algorithm, or substantial copied solution code appears.

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(
        self,
        output: str,
        question_content: str = "",
        input_messages: list = None,
        **_: Any,
    ):
        prompt = self.prompt_template.format(
            question_content=safe_str(question_content)[:12000],
            student_input=last_user_message(input_messages or []),
            output=output,
        )
        result = self._judge(prompt)
        return score_result.ScoreResult(
            name=self.name, value=float(result["score"]), reason=result["reason"]
        )


class SocraticGuidanceMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "socratic_guidance_quality"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Evaluate Socratic tutoring quality for a DSA coding tutor.

Student input: {student_input}
Tutor output:
{output}

Score 1 if tutor guides with questions/hints toward understanding (constraints, correctness, complexity), not direct answers.
Score 0 if dumps solution or only generic encouragement.

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(self, output: str, input_messages: list = None, **_: Any):
        prompt = self.prompt_template.format(
            student_input=last_user_message(input_messages or []),
            output=output,
        )
        result = self._judge(prompt)
        return score_result.ScoreResult(
            name=self.name, value=float(result["score"]), reason=result["reason"]
        )


class LanguageComplianceMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "language_compliance"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Check active coding language compliance.

Active coding language (from student message): {active_language}
Student input: {student_input}
Tutor JSON output:
{output}

Score 1 if:
- Code/language mismatch: polite notice only, no debugging wrong-language code.
- Off-language query: mismatch notice only, ask for active-language questions.
- When code matches active language: reply does NOT redundantly name the language ("your active language is X").

Score 0 otherwise.

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(
        self,
        output: str,
        system_prompt: str = "",
        input_messages: list = None,
        **_: Any,
    ):
        student = last_user_message(input_messages or [])
        prompt = self.prompt_template.format(
            active_language=parse_active_coding_language(student),
            student_input=student,
            output=output,
        )
        result = self._judge(prompt)
        return score_result.ScoreResult(
            name=self.name, value=float(result["score"]), reason=result["reason"]
        )


class CodingSubmissionGuidanceMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "submission_guidance"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Evaluate tutor behavior for test submission context.

Evaluation result (if any): {evaluation_result}
Student input: {student_input}
Tutor JSON output: {output}

If evaluation result is CORRECT: score 1 only if reply acknowledges success AND states time AND space complexity (Big-O); may ask about better approach if one exists. Score 0 if only generic edge-case advice without complexity.

If WRONG submission (INCORRECT, TLE, MLE, COMPILATION_ERROR, RUN_TIME_ERROR, ERROR): score 1 if tutor acknowledges at least one correct part and guides per failure type without giving full solution. Score 0 if no acknowledgment or hands full fix.

If no evaluation result or not a submission context: score -1.

Return JSON: {{"reason": "...", "score": -1 or 0 or 1}}
"""

    def score(self, output: str, input_messages: list = None, **_: Any):
        student = last_user_message(input_messages or [])
        eval_res = extract_evaluation_result(student)
        if not eval_res and "Wrong submission" not in extract_trigger_reason(student):
            return not_applicable_score(self.name, "no submission evaluation context")
        prompt = self.prompt_template.format(
            evaluation_result=eval_res or "UNKNOWN",
            student_input=student[:8000],
            output=output,
        )
        result = self._judge(prompt, allowed_scores={-1.0, 0.0, 1.0})
        val = float(result["score"])
        if val == -1.0:
            return not_applicable_score(self.name, result["reason"])
        return score_result.ScoreResult(name=self.name, value=val, reason=result["reason"])


class CodingPedagogyMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "coding_pedagogy"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
DSA tutor pedagogy check.

Student input: {student_input}
Tutor JSON output: {output}

Score 1 if ALL apply:
- Defaults to brute-force guidance unless student explicitly chose optimal.
- Conceptual doubt replies avoid bullet/label formatting (no "Key point:", numbered lists).
- Problem-explanation requests explain the problem only, no solution steps or pseudocode.

Score 0 if violates any rule. If none of these situations apply, score 1.

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(self, output: str, input_messages: list = None, **_: Any):
        prompt = self.prompt_template.format(
            student_input=last_user_message(input_messages or []),
            output=output,
        )
        result = self._judge(prompt)
        return score_result.ScoreResult(
            name=self.name, value=float(result["score"]), reason=result["reason"]
        )


class UseOfStudentCodeMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "use_of_student_code"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Does the tutor response engage with the student's actual code/question?

Student input (includes user code): {student_input}
Question summary: {question_content}
Tutor output: {output}

Score 1 if response is grounded in their code or stated doubt.
Score 0 if generic and ignores their code/context.

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(
        self,
        output: str,
        question_content: str = "",
        input_messages: list = None,
        **_: Any,
    ):
        prompt = self.prompt_template.format(
            student_input=last_user_message(input_messages or [])[:8000],
            question_content=safe_str(question_content)[:4000],
            output=output,
        )
        result = self._judge(prompt)
        return score_result.ScoreResult(
            name=self.name, value=float(result["score"]), reason=result["reason"]
        )


class CodingBoundaryMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "boundary_enforcement"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Coding tutor boundary / assessment protection.

Student input: {student_input}
Tutor output: {output}

If on-topic coding question: score -1.
If off-topic or asks for full assessment answer and tutor refuses appropriately (reason_flag Yes): score 1.
If mishandled: score 0.

Return JSON: {{"reason": "...", "score": -1 or 0 or 1}}
"""

    def score(self, output: str, input_messages: list = None, **_: Any):
        prompt = self.prompt_template.format(
            student_input=last_user_message(input_messages or []),
            output=output,
        )
        result = self._judge(prompt, allowed_scores={-1.0, 0.0, 1.0})
        val = float(result["score"])
        if val == -1.0:
            return not_applicable_score(self.name, result["reason"])
        return score_result.ScoreResult(name=self.name, value=val, reason=result["reason"])


def build_coding_tutor_metrics(judge_fn):
    return [
        CodingResponseFormatMetric(),
        CodingWordLimitMetric(),
        VoiceModeRestrictionsMetric(),
        CodingSessionRulesMetric(),
        CodingReasonFlagMetric(judge_fn),
        SolutionLeakageMetric(judge_fn),
        SocraticGuidanceMetric(judge_fn),
        LanguageComplianceMetric(judge_fn),
        CodingSubmissionGuidanceMetric(judge_fn),
        CodingPedagogyMetric(judge_fn),
        UseOfStudentCodeMetric(judge_fn),
        CodingBoundaryMetric(judge_fn),
        *duration_metrics(),
    ]


def coding_evaluation_task_extras(dataset_item: dict) -> dict:
    messages = parse_input_messages(dataset_item.get("input"))
    return {
        "prev_conversation": "",
        "input_messages": messages,
        "question_content": safe_str(dataset_item.get("question_content")),
        "system_prompt": safe_str(dataset_item.get("current_prompt")),
    }
