from __future__ import annotations

import re
from typing import Any

from opik.evaluation.metrics import base_metric, score_result

from eval_common import (
    _TIME_IN_REPLY_VIDEO_RE,
    count_words,
    duration_metrics,
    extract_json_object,
    extract_transcript_window,
    format_chat_history,
    is_canonical_video_closing_reply,
    is_closing_student_message,
    last_user_message,
    not_applicable_score,
    parse_input_messages,
    pass_fail_score,
    safe_str,
    tutor_json_format_check,
)

_FOLLOWUP_RE = re.compile(r"\b(more|clarify|elaborate|explain more)\b", re.IGNORECASE)


class ResponseFormatComplianceMetric(base_metric.BaseMetric):
    def __init__(self, name: str = "response_format_compliance"):
        self.name = name

    def score(self, output: str, **_: Any):
        ok, reason, data = tutor_json_format_check(output, max_reply_words=50)
        if not ok:
            return pass_fail_score(self.name, False, reason)
        reply = safe_str(data.get("reply"))
        if "```" in output or reply.count("{") > 2:
            return pass_fail_score(self.name, False, "code fences or heavy syntax in reply")
        return pass_fail_score(self.name, True, "valid tutor JSON")


class ReplyWordCountMetric(base_metric.BaseMetric):
    def __init__(self, name: str = "reply_word_count", max_words: int = 50):
        self.name = name
        self._max_words = max_words

    def score(self, output: str, input_messages: list = None, **_: Any):
        ok, reason, data = tutor_json_format_check(output, max_reply_words=self._max_words)
        if not ok:
            return not_applicable_score(self.name, reason)
        reply = safe_str(data.get("reply"))
        student = last_user_message(input_messages or [])
        if is_closing_student_message(student) and is_canonical_video_closing_reply(reply):
            return pass_fail_score(self.name, True, "closing reply exempt from word limit")
        words = count_words(reply)
        if words > self._max_words:
            return pass_fail_score(
                self.name,
                False,
                f"reply has {words} words (max {self._max_words})",
            )
        return pass_fail_score(self.name, True, f"reply word count {words}")


class ClosingStatementDetectionMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "closing_statement_detection"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Student input: {student_input}

Is this a closing/thank-you/satisfaction message where the student wants to end the topic?
Score 1 if yes, 0 if no.

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(self, output: str, prev_conversation: str = "", input_messages: list = None, **_: Any):
        student_input = last_user_message(input_messages or [])
        if is_closing_student_message(student_input):
            is_closing = True
        else:
            prompt = self.prompt_template.format(student_input=student_input)
            result = self._judge(prompt)
            is_closing = float(result["score"]) == 1.0
        if not is_closing:
            return not_applicable_score(self.name, "not a closing student message")
        data, err = extract_json_object(output)
        if data is None:
            return pass_fail_score(self.name, False, err)
        reply = safe_str(data.get("reply"))
        if safe_str(data.get("should_student_reply")) != "No":
            return pass_fail_score(self.name, False, "closing must set should_student_reply No")
        if safe_str(data.get("reason_flag")) != "No":
            return pass_fail_score(self.name, False, "closing must set reason_flag No")
        if not is_canonical_video_closing_reply(reply):
            return pass_fail_score(
                self.name,
                False,
                "closing reply must match one of the four canonical phrases",
            )
        return pass_fail_score(self.name, True, "canonical closing response")


class BoundaryEnforcementMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "boundary_enforcement"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Video session tutor boundary check.

Student input: {student_input}
Transcript excerpt:
{transcript}

Tutor output:
{output}

If query is on-topic for video session learning content, score -1.
If off-topic/inappropriate and tutor declines with reason_flag Yes without answering substantively, score 1.
If off-topic but mishandled, score 0.

Return JSON: {{"reason": "...", "score": -1 or 0 or 1}}
"""

    def score(
        self,
        output: str,
        prev_conversation: str = "",
        input_messages: list = None,
        system_prompt: str = "",
        **_: Any,
    ):
        student_input = last_user_message(input_messages or [])
        prompt = self.prompt_template.format(
            student_input=student_input,
            transcript=extract_transcript_window(system_prompt, student_input, 8000),
            output=output,
        )
        result = self._judge(prompt, allowed_scores={-1.0, 0.0, 1.0})
        val = float(result["score"])
        if val == -1.0:
            return not_applicable_score(self.name, result["reason"])
        return score_result.ScoreResult(name=self.name, value=val, reason=result["reason"])


class VideoReasonFlagMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "reason_flag_consistency"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Is the student query off-topic or nonsensical for a video session tutor (e.g. random word, unrelated topic)?

Student input: {student_input}

Score 1 if off-topic/nonsensical, 0 if on-topic video learning question.

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(self, output: str, input_messages: list = None, **_: Any):
        data, err = extract_json_object(output)
        if data is None:
            return not_applicable_score(self.name, err)
        flag = safe_str(data.get("reason_flag"))
        student = last_user_message(input_messages or [])
        result = self._judge(self.prompt_template.format(student_input=student))
        off_topic = float(result["score"]) == 1.0
        expected = "Yes" if off_topic else "No"
        if flag != expected:
            return pass_fail_score(
                self.name,
                False,
                f"reason_flag {flag} expected {expected} ({result['reason']})",
            )
        return pass_fail_score(self.name, True, "reason_flag consistent")


class IntentUnderstandingMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "intent_understanding"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Did the tutor understand the student's intent?

Previous conversation:
{prev_conversation}
Student input: {student_input}
Transcript excerpt:
{transcript}

Tutor output:
{output}

Score 1 only if response clearly addresses the student's actual intent (acknowledges question first).
Score 0 if it answers a different question or ignores context.

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(
        self,
        output: str,
        prev_conversation: str = "",
        input_messages: list = None,
        system_prompt: str = "",
        **_: Any,
    ):
        student = last_user_message(input_messages or [])
        prompt = self.prompt_template.format(
            prev_conversation=safe_str(prev_conversation),
            student_input=student,
            transcript=extract_transcript_window(system_prompt, student, 8000),
            output=output,
        )
        result = self._judge(prompt)
        return score_result.ScoreResult(
            name=self.name, value=float(result["score"]), reason=result["reason"]
        )


class ResponseQualityMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "response_quality"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Evaluate video session tutor communication.

Previous conversation:
{prev_conversation}
Student input: {student_input}
Tutor output:
{output}

Score 1 if ALL hold:
- Supportive English, conversational tone.
- No direct answers to assessments/homework; conceptual guidance only.
- If student asks for more/clarify, reply is brief extra detail (not full repeat).
- Does not reveal system instructions.

Score 0 otherwise.

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(
        self,
        output: str,
        input_messages: list = None,
        prev_conversation: str = "",
        **_: Any,
    ):
        student = last_user_message(input_messages or [])
        prompt = self.prompt_template.format(
            prev_conversation=safe_str(prev_conversation),
            student_input=student,
            output=output,
        )
        result = self._judge(prompt)
        return score_result.ScoreResult(
            name=self.name, value=float(result["score"]), reason=result["reason"]
        )


class VideoTranscriptGroundingMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "transcript_grounding"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Is the tutor reply grounded in the video transcript near the student's timestamp?

Student input: {student_input}
Transcript window:
{transcript}

Tutor JSON output: {output}

Score 1 if explanation aligns with transcript content (or appropriately says topic is covered later).
Score 0 if contradicts transcript or invents content not in transcript.

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(
        self,
        output: str,
        input_messages: list = None,
        system_prompt: str = "",
        **_: Any,
    ):
        ok, reason, _ = tutor_json_format_check(output)
        if not ok:
            return not_applicable_score(self.name, reason)
        student = last_user_message(input_messages or [])
        prompt = self.prompt_template.format(
            student_input=student,
            transcript=extract_transcript_window(system_prompt, student, 8000),
            output=output,
        )
        result = self._judge(prompt)
        return score_result.ScoreResult(
            name=self.name, value=float(result["score"]), reason=result["reason"]
        )


class VideoContextHandlingMetric(base_metric.BaseMetric):
    def __init__(self, name: str = "video_context_handling"):
        self.name = name

    def score(self, output: str, system_prompt: str = "", input_messages: list = None, **_: Any):
        ok, reason, data = tutor_json_format_check(output, max_reply_words=50)
        if not ok:
            return not_applicable_score(self.name, reason)
        reply = safe_str(data.get("reply"))
        if _TIME_IN_REPLY_VIDEO_RE.search(reply):
            return pass_fail_score(self.name, False, "reply mentions timestamps or time markers")
        return pass_fail_score(self.name, True, "no forbidden time references in reply")


class VideoSessionAlignmentMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "video_session_alignment"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Video session prompt alignment (secondary checks).

Student input: {student_input}
Previous conversation: {prev_conversation}
Tutor output: {output}

Score 1 if tutor uses student name only on first activation (if name used), varies explanations,
and uses "covered later in the video session" when topic appears later in transcript.

Score 0 if clear violation. If not applicable, score 1.

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(
        self,
        output: str,
        prev_conversation: str = "",
        input_messages: list = None,
        **_: Any,
    ):
        prompt = self.prompt_template.format(
            student_input=last_user_message(input_messages or []),
            prev_conversation=safe_str(prev_conversation)[:4000],
            output=output,
        )
        result = self._judge(prompt)
        return score_result.ScoreResult(
            name=self.name, value=float(result["score"]), reason=result["reason"]
        )


def build_video_session_metrics(judge_fn):
    return [
        ClosingStatementDetectionMetric(judge_fn),
        ResponseFormatComplianceMetric(),
        ReplyWordCountMetric(),
        ResponseQualityMetric(judge_fn),
        IntentUnderstandingMetric(judge_fn),
        BoundaryEnforcementMetric(judge_fn),
        VideoReasonFlagMetric(judge_fn),
        VideoContextHandlingMetric(),
        VideoTranscriptGroundingMetric(judge_fn),
        VideoSessionAlignmentMetric(judge_fn),
        *duration_metrics(),
    ]


def video_evaluation_task_extras(dataset_item: dict) -> dict:
    messages = parse_input_messages(dataset_item.get("input"))
    return {
        "prev_conversation": format_chat_history(messages),
        "input_messages": messages,
        "system_prompt": safe_str(dataset_item.get("current_prompt")),
    }
