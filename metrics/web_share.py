from __future__ import annotations

from typing import Any

from opik.evaluation.metrics import base_metric, score_result

from eval_common import (
    duration_metrics,
    pass_fail_score,
    safe_str,
    score_web_share_content_rules,
    score_web_share_hashtags_tone,
    score_web_share_structure,
)


def build_web_share_prompt(dataset_item: dict) -> str:
    prompt = safe_str(dataset_item.get("prompt"))
    variables = {
        "question_description": safe_str(dataset_item.get("question_description")),
        "html_code": safe_str(dataset_item.get("html_code")),
        "css_code": safe_str(dataset_item.get("css_code")),
        "js_code": safe_str(dataset_item.get("js_code")),
        "live_url": safe_str(dataset_item.get("live_url")),
        "social_media": safe_str(dataset_item.get("social_media")),
    }
    for key, value in variables.items():
        prompt = prompt.replace("{{" + key + "}}", value)
    return prompt


class CodeImplementationAccuracyMetric(base_metric.BaseMetric):
    def __init__(self, judge_fn, name: str = "code_implementation_accuracy"):
        self.name = name
        self._judge = judge_fn
        self.prompt_template = """
Evaluate the social post against USER_CODE and QUESTION_DESCRIPTION.

QUESTION_DESCRIPTION: {question_description}
USER_CODE:
HTML: {html_code}
CSS: {css_code}
JS: {js_code}

Generated post:
{output}

Score 1 only if ALL hold:
- Every feature/technology claim is verifiable in USER_CODE (not only in QUESTION_DESCRIPTION).
- Tone is positive and enthusiastic; describes a student project (not an assignment).
- Main content focuses on 1-2 verified aspects (not a long list of every feature).

Score 0 if any invented feature, negative tone, or features mentioned only in QUESTION_DESCRIPTION.

Return JSON: {{"reason": "...", "score": 0 or 1}}
"""

    def score(
        self,
        output: str,
        question_description: str = "",
        html_code: str = "",
        css_code: str = "",
        js_code: str = "",
        **_: Any,
    ):
        prompt = self.prompt_template.format(
            question_description=safe_str(question_description),
            html_code=safe_str(html_code),
            css_code=safe_str(css_code),
            js_code=safe_str(js_code),
            output=output,
        )
        result = self._judge(prompt)
        return score_result.ScoreResult(
            name=self.name, value=float(result["score"]), reason=result["reason"]
        )


class PlatformStructureComplianceMetric(base_metric.BaseMetric):
    def __init__(self, name: str = "platform_structure_compliance"):
        self.name = name

    def score(self, output: str, social_media: str = "", live_url: str = "", **_: Any):
        return score_web_share_structure(output, social_media, live_url)


class HashtagsAndToneMetric(base_metric.BaseMetric):
    def __init__(self, name: str = "hashtags_and_tone"):
        self.name = name

    def score(
        self,
        output: str,
        html_code: str = "",
        css_code: str = "",
        js_code: str = "",
        **_: Any,
    ):
        return score_web_share_hashtags_tone(output, html_code, css_code, js_code)


class ContentRulesComplianceMetric(base_metric.BaseMetric):
    def __init__(self, name: str = "content_rules_compliance"):
        self.name = name

    def score(
        self,
        output: str,
        html_code: str = "",
        css_code: str = "",
        js_code: str = "",
        **_: Any,
    ):
        return score_web_share_content_rules(output, html_code, css_code, js_code)


def build_web_share_metrics(judge_fn):
    return [
        CodeImplementationAccuracyMetric(judge_fn),
        PlatformStructureComplianceMetric(),
        HashtagsAndToneMetric(),
        ContentRulesComplianceMetric(),
        *duration_metrics(),
    ]
