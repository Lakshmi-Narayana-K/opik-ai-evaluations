"""Shared helpers for Opik LLM evaluation notebooks."""

from __future__ import annotations

import json
import re
import time
from contextvars import ContextVar
from typing import Any, Callable, Optional

_judge_duration_sec: ContextVar[float] = ContextVar("judge_duration_sec", default=0.0)

# Grader (task model) OpenRouter extras — e.g. openai/gpt-6.1-sol reasoning latency.
# Set GRADER_REASONING_EFFORT to None for models that reject reasoning_effort.
GRADER_REASONING_EFFORT: Optional[str] = "low"
GRADER_MAX_COMPLETION_TOKENS: Optional[int] = None
GRADER_CHAT_EXTRA_KWARGS: dict[str, Any] = {}


def reset_judge_duration() -> None:
    _judge_duration_sec.set(0.0)


def get_judge_duration_sec() -> float:
    return float(_judge_duration_sec.get())


def _add_judge_duration(seconds: float) -> None:
    _judge_duration_sec.set(_judge_duration_sec.get() + seconds)

import pandas as pd
from opik.evaluation.metrics import base_metric, score_result

_EMPTY_SENTINELS = frozenset({"", "nan", "none", "null"})
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002300-\U000023FF"
    "]",
    flags=re.UNICODE,
)
_HEX_COLOR_HASHTAG_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_FORBIDDEN_TONE_WORDS = re.compile(
    r"\b(assignment|task|requirements)\b", re.IGNORECASE
)
_INTERACTIVITY_CLAIM_RE = re.compile(
    r"\b(click|toggle|interactive|addEventListener|dom\b|javascript function|on click)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+")
_VIDEO_CLOSING_CANONICAL = (
    "you're welcome! glad i could help.",
    "happy to help, keep up the great work.",
    "great! you're on the right track.",
    "no problem at all, keep learning confidently.",
)
_TUTOR_BASE_KEYS = frozenset({"reply", "should_student_reply", "reason_flag"})
_CODING_EXTRA_KEYS = frozenset({"followup_ctas"})
VOICE_BANNED_CHARS = frozenset("(){}[]<>;:=+-*/_`\"'\\|")
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#\w+")
_CCBP_TAGS = frozenset(
    {
        "#CCBP",
        "#Coding",
        "#DeveloperJourney",
        "#Learning",
        "#Programming",
        "#NextLevelCoding",
    }
)


def safe_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if text.casefold() in _EMPTY_SENTINELS:
        return ""
    return text


def unescape_exported_prompt_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace('""', '"')
    return text.replace("{{", "{").replace("}}", "}")


def prompt_field(value: Any) -> str:
    return unescape_exported_prompt_text(safe_str(value))


def normalize_production_text(text: str) -> str:
    """Match textual AI eval backend normalization (non-case-sensitive)."""
    return safe_str(text).lower().replace("\n", "")


def strip_outer_json_fences(raw: str) -> str:
    text = safe_str(raw)
    text = _JSON_FENCE_RE.sub("", text).strip()
    return text


def extract_json_object(raw: str) -> tuple[Optional[dict], str]:
    """Return (parsed dict or None, error reason)."""
    text = strip_outer_json_fences(raw)
    if not text:
        return None, "empty output"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None, "no JSON object found"
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            return None, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "JSON root is not an object"
    return data, ""


def count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", safe_str(text)))


def count_emojis(text: str) -> int:
    return len(_EMOJI_RE.findall(safe_str(text)))


def last_user_message(messages: list) -> str:
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return safe_str(msg.get("content"))
    return ""


def format_chat_history(messages: list, exclude_last_user: bool = True) -> str:
    if not messages:
        return ""
    span = list(messages)
    if exclude_last_user:
        last_user_idx = -1
        for i in range(len(span) - 1, -1, -1):
            if isinstance(span[i], dict) and span[i].get("role") == "user":
                last_user_idx = i
                break
        span = span[:last_user_idx] if last_user_idx > 0 else span[:-1]
    lines = []
    for msg in span:
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
            lines.append(f"{msg['role']}: {msg.get('content', '')}")
    return "\n".join(lines)


def parse_input_messages(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    text = safe_str(raw)
    if not text:
        return []
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def not_applicable_score(name: str, reason: str) -> score_result.ScoreResult:
    """Excluded from pass-rate aggregates in Opik (scoring_failed=True)."""
    return score_result.ScoreResult(
        name=name,
        value=None,
        reason=f"N/A: {reason}",
        scoring_failed=True,
    )


def pass_fail_score(name: str, passed: bool, reason: str) -> score_result.ScoreResult:
    return score_result.ScoreResult(
        name=name,
        value=1.0 if passed else 0.0,
        reason=reason,
    )


def call_judge(
    openrouter_client,
    model: str,
    prompt: str,
    allowed_scores: Optional[set[float]] = None,
    retries: int = 3,
) -> dict:
    allowed = allowed_scores or {0.0, 1.0}
    last_error = ""
    for _ in range(retries):
        judge_start = time.perf_counter()
        response = openrouter_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.001,
        )
        _add_judge_duration(time.perf_counter() - judge_start)
        raw = response.choices[0].message.content or ""
        raw = strip_outer_json_fences(raw)
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_error = str(exc)
            continue
        if not isinstance(result, dict):
            last_error = "judge output is not a JSON object"
            continue
        if "score" not in result or "reason" not in result:
            last_error = "judge JSON missing score or reason"
            continue
        try:
            score_val = float(result["score"])
        except (TypeError, ValueError):
            last_error = "judge score is not numeric"
            continue
        if score_val not in allowed:
            last_error = f"judge score {score_val} not in {sorted(allowed)}"
            continue
        result["score"] = score_val
        return result
    raise ValueError(f"Judge failed after {retries} attempts: {last_error}")


def _grader_chat_completion_kwargs(
    *,
    reasoning_effort: Optional[str] = None,
    max_completion_tokens: Optional[int] = None,
    **extra: Any,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = dict(GRADER_CHAT_EXTRA_KWARGS)
    effort = (
        reasoning_effort
        if reasoning_effort is not None
        else GRADER_REASONING_EFFORT
    )
    if effort is not None:
        kwargs["reasoning_effort"] = effort
    max_tokens = (
        max_completion_tokens
        if max_completion_tokens is not None
        else GRADER_MAX_COMPLETION_TOKENS
    )
    if max_tokens is not None:
        kwargs["max_completion_tokens"] = max_tokens
    kwargs.update(extra)
    return kwargs


def make_tracked_llm_call(
    tracked_client,
    model: str,
    messages: list,
    *,
    reasoning_effort: Optional[str] = None,
    max_completion_tokens: Optional[int] = None,
    **extra_completion_kwargs: Any,
) -> str:
    response = tracked_client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.001,
        **_grader_chat_completion_kwargs(
            reasoning_effort=reasoning_effort,
            max_completion_tokens=max_completion_tokens,
            **extra_completion_kwargs,
        ),
    )
    content = response.choices[0].message.content
    if content is None:
        raise ValueError("LLM returned empty content")
    return content


def tutor_json_format_check(output: str, max_reply_words: Optional[int] = None) -> tuple[bool, str, Optional[dict]]:
    """Video session JSON: exactly three keys."""
    data, err = extract_json_object(output)
    if data is None:
        return False, err, None
    keys = set(data.keys())
    if keys != _TUTOR_BASE_KEYS:
        return False, f"expected keys {_TUTOR_BASE_KEYS}, got {keys}", data
    return _validate_tutor_json_fields(data, max_reply_words)


def coding_tutor_json_format_check(
    output: str,
    max_reply_words: Optional[int] = None,
    min_paragraphs: int = 3,
) -> tuple[bool, str, Optional[dict]]:
    """Coding tutor JSON: base keys plus optional followup_ctas."""
    raw = safe_str(output)
    if raw.lstrip().startswith("```"):
        return False, "JSON must not be wrapped in code fences", None
    data, err = extract_json_object(output)
    if data is None:
        return False, err, None
    keys = set(data.keys())
    allowed = _TUTOR_BASE_KEYS | _CODING_EXTRA_KEYS
    if not keys <= allowed or not _TUTOR_BASE_KEYS <= keys:
        return False, f"expected keys {_TUTOR_BASE_KEYS} (+ optional followup_ctas), got {keys}", data
    ok, reason = _validate_followup_ctas(data)
    if not ok:
        return False, reason, data
    ok, reason, data = _validate_tutor_json_fields(data, max_reply_words)
    if not ok:
        return False, reason, data
    paragraphs = [p.strip() for p in safe_str(data.get("reply")).split("\n") if p.strip()]
    if len(paragraphs) < min_paragraphs:
        return False, f"reply needs at least {min_paragraphs} paragraphs, got {len(paragraphs)}", data
    return True, "ok", data


def _validate_tutor_json_fields(
    data: dict, max_reply_words: Optional[int]
) -> tuple[bool, str, Optional[dict]]:
    for key in ("should_student_reply", "reason_flag"):
        val = safe_str(data.get(key))
        if val not in ("Yes", "No"):
            return False, f"{key} must be 'Yes' or 'No', got {val!r}", data
    reply = safe_str(data.get("reply"))
    if max_reply_words is not None and count_words(reply) > max_reply_words:
        return False, f"reply exceeds {max_reply_words} words ({count_words(reply)})", data
    return True, "ok", data


def _validate_followup_ctas(data: dict) -> tuple[bool, str]:
    if "followup_ctas" not in data:
        return True, "ok"
    ctas = data.get("followup_ctas")
    if not isinstance(ctas, list):
        return False, "followup_ctas must be a JSON array"
    if len(ctas) > 4:
        return False, f"followup_ctas max 4 items, got {len(ctas)}"
    for item in ctas:
        if not isinstance(item, str):
            return False, "followup_ctas items must be strings"
        if len(item) > 35:
            return False, f"CTA exceeds 35 chars ({len(item)}): {item[:40]}"
    if safe_str(data.get("should_student_reply")) == "No" and ctas:
        return False, "followup_ctas must be [] when should_student_reply is No"
    return True, "ok"


def parse_active_coding_language(student_input: str) -> str:
    m = re.search(
        r"Active coding language:\s*([A-Za-z0-9_+.]+)",
        safe_str(student_input),
        re.IGNORECASE,
    )
    return m.group(1).strip() if m else ""


def parse_student_video_timestamp(student_input: str) -> Optional[int]:
    """Return timestamp in seconds from 'Timestamp: HH:MM:SS' if present."""
    m = re.search(
        r"Timestamp:\s*(\d{1,2}):(\d{2}):(\d{2})",
        safe_str(student_input),
        re.IGNORECASE,
    )
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return h * 3600 + mi * 60 + s


def _vtt_time_to_seconds(h: int, mi: int, s: int, ms: int = 0) -> int:
    return h * 3600 + mi * 60 + s + (1 if ms >= 500 else 0)


def extract_transcript_snippet(system_prompt: str, max_chars: int = 12000) -> str:
    """Excerpt from the video transcript block inside the tutor system prompt."""
    text = safe_str(system_prompt)
    if not text:
        return ""
    header = "## Video Transcript:"
    start = text.find(header)
    if start >= 0:
        start += len(header)
    else:
        match = re.search(r"\n\s*1\s*\n\s*00:\d{2}:\d{2},\d{3}\s*-->", text)
        start = match.start() if match else -1
    if start < 0:
        return ""
    return text[start : start + max_chars].lstrip()


def extract_transcript_window(
    system_prompt: str,
    student_input: str = "",
    window_seconds: int = 180,
    max_chars: int = 12000,
) -> str:
    """Transcript excerpt centered on the student's Timestamp when available."""
    full = extract_transcript_snippet(system_prompt, max_chars=500_000)
    if not full:
        return ""
    target = parse_student_video_timestamp(student_input)
    if target is None:
        return full[:max_chars]
    cue_re = re.compile(
        r"(\d+)\s*\n\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->",
    )
    best_start = 0
    best_dist = 10**9
    for m in cue_re.finditer(full):
        sec = _vtt_time_to_seconds(
            int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        )
        dist = abs(sec - target)
        if dist < best_dist:
            best_dist = dist
            best_start = m.start()
    lo = max(0, best_start - window_seconds * 40)
    hi = min(len(full), best_start + window_seconds * 40)
    return full[lo:hi][:max_chars]


def normalize_closing_reply(text: str) -> str:
    return re.sub(r"\s+", " ", safe_str(text).strip().lower())


def is_canonical_video_closing_reply(reply: str) -> bool:
    norm = normalize_closing_reply(reply)
    for phrase in _VIDEO_CLOSING_CANONICAL:
        if norm == phrase or norm.rstrip(".") == phrase.rstrip("."):
            return True
    return False


def is_closing_student_message(student_input: str) -> bool:
    t = safe_str(student_input).lower().strip()
    if not t:
        return False
    closing_markers = (
        "thank",
        "thanks",
        "got it",
        "understood",
        "clear now",
        "that helps",
        "appreciate",
    )
    return any(m in t for m in closing_markers) and len(t.split()) <= 12


def _strip_emojis_for_count(text: str) -> str:
    return _EMOJI_RE.sub("", safe_str(text))


def web_share_post_lines(text: str) -> tuple[list[str], list[str], str, str]:
    lines = [ln.strip() for ln in safe_str(text).splitlines() if ln.strip()]
    if len(lines) < 2:
        return lines, lines, "", ""
    hashtag_line = lines[-1]
    url_line = lines[-2]
    main_lines = lines[:-2]
    return lines, main_lines, url_line, hashtag_line


def web_share_analyze_code(html: str, css: str, js: str) -> dict[str, bool]:
    html, css, js = safe_str(html), safe_str(css), safe_str(js)
    blob = html + css + js
    return {
        "has_html": bool(html.strip()),
        "has_css": bool(css.strip()),
        "has_js": bool(js.strip()),
        "has_media_queries": "@media" in css,
        "has_responsive_bootstrap": bool(
            re.search(
                r"col-(?:sm|md|lg|xl)|container-fluid|d-(?:sm|md|lg|xl)",
                blob,
                re.I,
            )
        ),
        "has_js_events": bool(
            re.search(r"addEventListener|onclick|onchange|querySelector|getElementById", js, re.I)
        ),
        "has_react": bool(re.search(r"\breact\b|jsx|from\s+['\"]react", blob, re.I)),
        "has_bootstrap": bool(re.search(r"\bbootstrap\b", blob, re.I)),
        "has_animation": bool(
            re.search(r"transition|animation|@keyframes|transform", css, re.I)
        ),
        "has_flexbox": bool(re.search(r"display\s*:\s*flex|flex-direction|flexbox", css, re.I)),
        "has_grid": bool(re.search(r"display\s*:\s*grid", css, re.I)),
    }


def tech_hashtag_supported(tag: str, html: str, css: str, js: str, code: dict[str, bool]) -> bool:
    blob = (html + css + js).lower()
    if tag == "#HTML":
        return code["has_html"]
    if tag == "#CSS":
        return code["has_css"]
    if tag == "#JavaScript":
        return code["has_js"]
    if tag == "#ResponsiveDesign":
        return code["has_media_queries"] or code["has_responsive_bootstrap"]
    if tag == "#Bootstrap":
        return code["has_bootstrap"]
    if tag == "#React":
        return code["has_react"]
    if tag == "#Flexbox":
        return code["has_flexbox"]
    if tag == "#Grid":
        return code["has_grid"]
    if tag in ("#Animations", "#Animation"):
        return code["has_animation"]
    keyword = tag[1:]
    parts = re.findall(r"[A-Za-z][a-z]*", keyword)
    if not parts:
        return keyword.lower() in blob
    return any(p.lower() in blob for p in parts if len(p) >= 3)


def _body_has_non_color_hashtags(body: str) -> bool:
    for tag in _HASHTAG_RE.findall(body):
        if not _HEX_COLOR_HASHTAG_RE.match(tag):
            return True
    return False


def score_web_share_structure(output: str, social_media: str, live_url: str) -> score_result.ScoreResult:
    name = "platform_structure_compliance"
    text = safe_str(output)
    platform = safe_str(social_media).upper()
    url = safe_str(live_url)

    if not text:
        return pass_fail_score(name, False, "empty post")

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return pass_fail_score(name, False, "expected at least 3 non-empty lines (content, URL, hashtags)")

    hashtag_line = lines[-1]
    url_line = lines[-2]
    main_lines = lines[:-2]
    if not main_lines:
        return pass_fail_score(name, False, "missing main content section")

    if not _HASHTAG_RE.search(hashtag_line):
        return pass_fail_score(name, False, "last line must be hashtags")

    if url not in text:
        return pass_fail_score(name, False, "published URL not found in post")

    url_line_without_url = url_line.replace(url, "").strip()
    if len(url_line_without_url.split()) < 2:
        return pass_fail_score(name, False, "URL line needs a short lead-in phrase before the URL")

    emoji_count = count_emojis(text)
    if emoji_count < 3 or emoji_count > 4:
        return pass_fail_score(name, False, f"expected 3-4 emojis, found {emoji_count}")

    if "```" in text or "**" in text:
        return pass_fail_score(name, False, "post must be plain text (no markdown/code fences)")

    main_text = _strip_emojis_for_count("\n".join(main_lines))
    main_words = count_words(main_text)
    if platform == "TWITTER" or platform == "X":
        if len(text) > 280:
            return pass_fail_score(name, False, f"Twitter/X limit 280 chars, got {len(text)}")
    else:
        if main_words < 80 or main_words > 120:
            return pass_fail_score(
                name,
                False,
                f"LinkedIn main-content word count {main_words}, expected 80-120",
            )

    return pass_fail_score(
        name, True, f"structure ok ({main_words} main words, {emoji_count} emojis)"
    )


def score_web_share_content_rules(
    output: str, html: str, css: str, js: str
) -> score_result.ScoreResult:
    name = "content_rules_compliance"
    text = safe_str(output)
    _, main_lines, url_line, _ = web_share_post_lines(text)
    if not main_lines:
        return pass_fail_score(name, False, "missing main content")

    content_and_url = "\n".join(main_lines + ([url_line] if url_line else []))
    if _FORBIDDEN_TONE_WORDS.search(content_and_url):
        return pass_fail_score(name, False, "forbidden tone word (assignment/task/requirements)")

    if not re.search(r"\bproject\b", content_and_url, re.IGNORECASE):
        return pass_fail_score(name, False, 'post must refer to a "project"')

    for line in main_lines:
        parts = [p for p in _SENTENCE_SPLIT_RE.split(line) if p.strip()]
        if len(parts) > 1:
            return pass_fail_score(name, False, "each sentence should be on its own line")

    js_clean = safe_str(js)
    if js_clean.casefold() in _EMPTY_SENTINELS or not js_clean.strip():
        body = "\n".join(main_lines).lower()
        if _INTERACTIVITY_CLAIM_RE.search(body):
            return pass_fail_score(
                name, False, "interactive claims require JavaScript in USER_CODE"
            )

    return pass_fail_score(name, True, "content rules ok")


def score_web_share_hashtags_tone(
    output: str, html: str, css: str, js: str
) -> score_result.ScoreResult:
    name = "hashtags_and_tone"
    text = safe_str(output)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return pass_fail_score(name, False, "empty post")

    hashtag_line = lines[-1]
    tags = _HASHTAG_RE.findall(hashtag_line)
    if not tags:
        return pass_fail_score(name, False, "no hashtags on final line")
    if tags[0] != "#NxtWave":
        return pass_fail_score(name, False, "hashtags must start with #NxtWave")

    if len(set(tags)) != len(tags):
        return pass_fail_score(name, False, "duplicate hashtags")

    body = "\n".join(lines[:-1])
    if _body_has_non_color_hashtags(body):
        return pass_fail_score(name, False, "hashtags must appear only on final line")

    ccbp = [t for t in tags if t in _CCBP_TAGS]
    if len(ccbp) < 1 or len(ccbp) > 2:
        return pass_fail_score(name, False, f"need 1-2 CCBP category tags, found {ccbp}")

    code = web_share_analyze_code(html, css, js)
    html_s, css_s, js_s = safe_str(html), safe_str(css), safe_str(js)

    tech_tags = [t for t in tags if t not in ("#NxtWave",) and t not in _CCBP_TAGS]
    if len(tech_tags) < 1 or len(tech_tags) > 2:
        return pass_fail_score(name, False, f"need 1-2 technology tags, found {tech_tags}")

    for t in tech_tags:
        if not tech_hashtag_supported(t, html_s, css_s, js_s, code):
            return pass_fail_score(
                name,
                False,
                f"technology tag {t} not supported by USER_CODE",
            )

    return pass_fail_score(name, True, f"hashtags {tags}, tone ok")


_TEXTUAL_JSON_DEFAULT_KEYS = frozenset(
    {"similarity_percentage", "appreciation_feedback", "improvement_feedback"}
)


def _json_template_block(response_format: str) -> str:
    """Extract the JSON example block from response_format (ignore prose like 'format:')."""
    fmt = prompt_field(response_format)
    if not fmt:
        return ""
    best = ""
    depth = 0
    start = None
    for i, ch in enumerate(fmt):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                block = fmt[start : i + 1]
                if "similarity_percentage" in block or "appreciation_feedback" in block:
                    if len(block) > len(best):
                        best = block
                start = None
    return best


def required_textual_json_keys(response_format: str) -> set[str]:
    block = _json_template_block(response_format)
    if not block:
        return set(_TEXTUAL_JSON_DEFAULT_KEYS)
    keys = set(re.findall(r'"(\w+)"\s*:', block))
    if not keys:
        keys = set(re.findall(r"'(\w+)'\s*:", block))
    return keys if keys else set(_TEXTUAL_JSON_DEFAULT_KEYS)


def score_textual_structural_compliance(output: str, response_format: str) -> score_result.ScoreResult:
    name = "structural_compliance"
    required = required_textual_json_keys(response_format)
    data, err = extract_json_object(output)
    if data is None:
        return pass_fail_score(name, False, err)
    if set(data.keys()) != required:
        return pass_fail_score(name, False, f"keys {set(data.keys())} != {required}")
    try:
        score = float(str(data.get("similarity_percentage", "")).replace("%", ""))
    except (TypeError, ValueError):
        return pass_fail_score(name, False, "similarity_percentage not numeric")
    if score < 0 or score > 100:
        return pass_fail_score(name, False, f"similarity_percentage {score} out of 0-100")
    for field in required - {"similarity_percentage"}:
        if not safe_str(data.get(field)):
            return pass_fail_score(name, False, f"empty field {field}")
    return pass_fail_score(name, True, "valid JSON and required fields")


def detect_coding_tutor_mode(system_prompt: str, user_input: str) -> str:
    """Return 'chat' or 'voice'. Default chat unless an explicit mode flag is set."""
    blob = safe_str(system_prompt) + "\n" + safe_str(user_input)
    flag = re.search(
        r'Current\s+mode\s*=\s*["\']?(CHAT|VOICE)["\']?',
        blob,
        re.IGNORECASE,
    )
    if flag:
        return "voice" if flag.group(1).upper() == "VOICE" else "chat"
    if re.search(r"(?i)\b(?:you are in|mode is|tutor mode[:\s]+)\s*voice\b", blob):
        return "voice"
    return "chat"


def coding_tutor_word_limits(mode: str) -> tuple[int, int]:
    if mode == "voice":
        return 1, 60
    return 40, 80


def extract_trigger_reason(student_input: str) -> str:
    m = re.search(r"Trigger reason:\s*([^\n.]+)", safe_str(student_input), re.IGNORECASE)
    return safe_str(m.group(1)) if m else ""


def extract_evaluation_result(student_input: str) -> str:
    m = re.search(
        r"evaluation result:\s*`?([A-Z_]+)`?",
        safe_str(student_input),
        re.IGNORECASE,
    )
    return m.group(1).upper() if m else ""


def student_code_appears_empty(student_input: str) -> bool:
    text = safe_str(student_input)
    if re.search(r"User code:\s*''\s*(?:Active|$)", text, re.IGNORECASE):
        return True
    if re.search(r'User code:\s*""\s*(?:Active|$)', text, re.IGNORECASE):
        return True
    m = re.search(
        r"User code:\s*(.+?)\s+Active coding language:",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if m and not m.group(1).strip().strip("`'"):
        return True
    return False


def count_dialogue_turns(messages: list) -> int:
    return sum(1 for m in messages or [] if isinstance(m, dict) and m.get("role") == "user")


_TIME_IN_REPLY_VIDEO_RE = re.compile(
    r"\b(timestamp|current timestamp|at this timestamp|at\s+\d{1,2}:\d{2}|"
    r"in the video at|this point in the video)\b",
    re.IGNORECASE,
)


class GraderDurationMetric(base_metric.BaseMetric):
    """Surfaces grader (feature) LLM latency as an experiment column (seconds)."""

    def __init__(self, name: str = "grader_duration_sec"):
        self.name = name

    def score(self, grader_duration_sec: float = 0.0, **_: Any):
        seconds = float(grader_duration_sec or 0.0)
        return score_result.ScoreResult(
            name=self.name,
            value=seconds,
            reason=f"grader API time: {seconds:.3f}s",
        )


class JudgeDurationMetric(base_metric.BaseMetric):
    """Total judge LLM time for this row (sum of all call_judge calls). Run last."""

    def __init__(self, name: str = "judge_duration_sec"):
        self.name = name

    def score(self, **_: Any):
        seconds = get_judge_duration_sec()
        return score_result.ScoreResult(
            name=self.name,
            value=seconds,
            reason=f"judge API time: {seconds:.3f}s",
        )


def duration_metrics() -> list:
    """Grader + judge timing columns; append as the last metrics in each eval."""
    return [GraderDurationMetric(), JudgeDurationMetric()]
