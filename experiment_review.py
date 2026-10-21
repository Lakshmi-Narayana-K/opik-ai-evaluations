"""Fetch Opik experiment runs and build review context for metric checking."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import pandas as pd

from opik.api_objects import opik_client
from opik.evaluation.evaluation_result import EvaluationResult

RUN_PRESETS: Dict[str, Dict[str, Any]] = {
    "textual_baseline_gpt_chat_latest": {
        "surface": "textual",
        "project_name": "AI Evaluations Baseline V2",
        "experiment_name": "textual-eval/openai/gpt-chat-latest",
        "experiment_id": None,
        "dataset_name": "ai_eval_textual_prod_final",
        "use_latest_run": True,
        "name_search": "textual-eval/openai/gpt-chat-latest",
    },
    "web_share_baseline": {
        "surface": "web_share",
        "project_name": "AI Evaluations Baseline V2",
        "experiment_name": None,
        "experiment_id": None,
        "dataset_name": "ai_eval_web_share_prod",
        "use_latest_run": True,
        "name_search": "web-share",
    },
    "video_session_baseline": {
        "surface": "video_session",
        "project_name": "AI Evaluations Baseline V2",
        "experiment_name": None,
        "experiment_id": None,
        "dataset_name": "ai_tutor_video_session_prod",
        "use_latest_run": True,
        "name_search": "video",
    },
    "coding_tutor_dsa_baseline": {
        "surface": "coding_tutor",
        "project_name": "AI Evaluations Baseline V2",
        "experiment_name": None,
        "experiment_id": None,
        "dataset_name": "ai_tutor_coding_practise_dsa_prompts",
        "use_latest_run": True,
        "name_search": "coding",
    },
}


def apply_run_preset(preset_key: str) -> Dict[str, Any]:
    if preset_key not in RUN_PRESETS:
        raise KeyError(f"Unknown RUN_PRESET '{preset_key}'. Options: {list(RUN_PRESETS)}")
    return dict(RUN_PRESETS[preset_key])


SURFACE_PRESETS: Dict[str, Dict[str, Any]] = {
    "textual": {
        "context_keys": [
            "question",
            "student_answer",
            "question_text",
            "user_answer",
            "general_evaluation_criteria",
            "specific_evaluation_criteria",
            "critical_cases",
            "feedback_guidelines",
            "response_format",
        ],
        "metrics": [
            "rubric_compliance",
            "scoring_consistency",
            "feedback_relevance",
            "structural_compliance",
            "grader_duration_sec",
            "judge_duration_sec",
        ],
    },
    "web_share": {
        "context_keys": [
            "question_description",
            "html_code",
            "css_code",
            "js_code",
            "social_media",
            "live_url",
        ],
        "metrics": [
            "code_implementation_accuracy",
            "platform_structure_compliance",
            "hashtags_and_tone",
            "grader_duration_sec",
            "judge_duration_sec",
        ],
    },
    "video_session": {
        "context_keys": [
            "input",
            "current_prompt",
            "prev_conversation",
            "system_prompt",
        ],
        "metrics": [
            "closing_statement_detection",
            "response_format_compliance",
            "reply_word_count",
            "response_quality",
            "intent_understanding",
            "boundary_enforcement",
            "video_context_handling",
            "grader_duration_sec",
            "judge_duration_sec",
        ],
    },
    "coding_tutor": {
        "context_keys": [
            "input",
            "current_prompt",
            "question_content",
            "system_prompt",
            "prev_conversation",
        ],
        "metrics": [
            "response_format_compliance",
            "word_limit_by_mode",
            "voice_mode_restrictions",
            "solution_leakage",
            "socratic_guidance_quality",
            "language_compliance",
            "use_of_student_code",
            "boundary_enforcement",
            "grader_duration_sec",
            "judge_duration_sec",
        ],
    },
}


def pick_latest_experiment(matches: Sequence[Any]) -> Any:
    """Pick newest run when several experiments share the same name (UUID v7 ids sort by time)."""
    return max(matches, key=lambda m: m.id)


def resolve_experiment(
    client: opik_client.Opik,
    *,
    experiment_id: Optional[str] = None,
    experiment_name: Optional[str] = None,
    project_name: Optional[str] = None,
    use_latest_run: bool = False,
):
    if experiment_id:
        return client.get_experiment_by_id(experiment_id)
    if not experiment_name:
        raise ValueError("Set experiment_id or experiment_name")
    matches = client.get_experiments_by_name(experiment_name, project_name=project_name)
    if not matches:
        raise ValueError(f"No experiment matching name: {experiment_name}")
    if len(matches) > 1:
        if use_latest_run:
            return pick_latest_experiment(matches)
        ids = "\n".join(f"  {m.id}  {m.name}" for m in sorted(matches, key=lambda m: m.id))
        raise ValueError(
            f"Multiple experiments match '{experiment_name}'. "
            f"Set use_latest_run=True or EXPERIMENT_ID to one of:\n{ids}"
        )
    return matches[0]


def list_experiments_by_name_substring(
    client: opik_client.Opik,
    name_substring: str,
    project_name: Optional[str] = None,
) -> List[Tuple[str, str, str]]:
    """Returns (id, name, dataset_name) rows for UI picking, newest first."""
    matches = client.get_experiments_by_name(name_substring, project_name=project_name)
    matches = sorted(matches, key=lambda m: m.id, reverse=True)
    return [(m.id, m.name, m.dataset_name) for m in matches]


def experiment_dashboard_urls(base_host: str, experiment_id: str, project_name: str) -> Dict[str, str]:
    """Build UI links (self-hosted Opik). base_host e.g. https://3.110.54.210"""
    base = base_host.rstrip("/")
    return {
        "experiment": f"{base}/opik/default/experiments/{experiment_id}/compare",
        "project_traces": f"{base}/opik/default/projects?projectName={project_name}",
    }


def fetch_dataset_items_map(
    client: opik_client.Opik,
    dataset_name: str,
    project_name: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    dataset = client.get_dataset(name=dataset_name, project_name=project_name)
    dataset.get_version_info = lambda: None  # older Opik backends
    return {item["id"]: item for item in dataset.get_items()}


def _merge_dataset_record(
    embedded: Optional[Dict[str, Any]], full: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(full or {})
    for key, val in (embedded or {}).items():
        if val not in (None, "", "NaN", "nan"):
            merged[key] = val
    return merged


def enrich_dataframe_with_dataset(
    df: pd.DataFrame, dataset_by_id: Dict[str, Dict[str, Any]]
) -> pd.DataFrame:
    if df.empty or not dataset_by_id:
        return df

    enriched = []
    for _, row in df.iterrows():
        row_dict = row.to_dict()
        ds_id = row_dict.get("dataset_item_id")
        embedded = row_dict.get("dataset_item_json") or {}
        full = dataset_by_id.get(ds_id, {})
        row_dict["dataset_item_json"] = _merge_dataset_record(embedded, full)
        enriched.append(row_dict)
    return pd.DataFrame(enriched)


def load_experiment_review_dataframe(
    client: opik_client.Opik,
    experiment,
    *,
    truncate: bool = False,
    enrich_dataset: bool = True,
    project_name: Optional[str] = None,
) -> pd.DataFrame:
    df = experiment_items_to_dataframe(experiment, truncate=truncate)
    if enrich_dataset and experiment.dataset_name:
        dataset_by_id = fetch_dataset_items_map(
            client, experiment.dataset_name, project_name=project_name
        )
        df = enrich_dataframe_with_dataset(df, dataset_by_id)
    return df


def attach_trace_durations(
    client: opik_client.Opik,
    df: pd.DataFrame,
    *,
    max_rows: Optional[int] = None,
) -> pd.DataFrame:
    """Add trace_duration_sec from Opik traces (matches UI Duration column)."""
    out = df.copy()
    trace_durations: Dict[Union[int, str], Optional[float]] = {}
    indices = list(out.index[: max_rows or len(out)])
    for idx in indices:
        trace_id = out.at[idx, "trace_id"]
        if not trace_id:
            trace_durations[idx] = None
            continue
        try:
            trace = client.get_trace_content(id=trace_id)
            trace_durations[idx] = getattr(trace, "duration", None)
        except Exception:
            trace_durations[idx] = None
    out["trace_duration_sec"] = out.index.map(trace_durations.get)
    return out


def duration_sanity_check(df: pd.DataFrame) -> pd.DataFrame:
    """Compare grader + judge metrics to trace duration where available."""
    grader_col = "metric__grader_duration_sec"
    judge_col = "metric__judge_duration_sec"
    if grader_col not in df.columns or judge_col not in df.columns:
        raise ValueError("DataFrame missing grader/judge duration metric columns")

    grader = pd.to_numeric(df[grader_col], errors="coerce")
    judge = pd.to_numeric(df[judge_col], errors="coerce")
    grader_plus_judge = grader + judge
    result = pd.DataFrame(
        {
            "grader_duration_sec": grader,
            "judge_duration_sec": judge,
            "grader_plus_judge_sec": grader_plus_judge,
        }
    )
    if "trace_duration_sec" in df.columns:
        trace = pd.to_numeric(df["trace_duration_sec"], errors="coerce")
        result["trace_duration_sec"] = trace
        result["trace_minus_grader_judge_sec"] = trace - grader_plus_judge
    return result.describe()


def feedback_scores_to_dict(feedback_scores: Optional[Sequence[Dict[str, Any]]]):
    scores: Dict[str, Any] = {}
    reasons: Dict[str, Any] = {}
    for fs in feedback_scores or []:
        name = fs.get("name")
        if not name:
            continue
        scores[name] = fs.get("value")
        reasons[name] = fs.get("reason")
    return scores, reasons


def experiment_items_to_dataframe(experiment, *, truncate: bool = False, max_results: int = 10000) -> pd.DataFrame:
    items = experiment.get_items(truncate=truncate, max_results=max_results)
    rows = []
    for it in items:
        task = it.evaluation_task_output or {}
        dataset = it.dataset_item_data or {}
        scores, reasons = feedback_scores_to_dict(it.feedback_scores)
        row: Dict[str, Any] = {
            "experiment_item_id": it.id,
            "dataset_item_id": it.dataset_item_id,
            "trace_id": it.trace_id,
            "grader_output": task.get("output"),
            "task_output_json": task,
            "dataset_item_json": dataset,
        }
        for k, v in scores.items():
            row[f"metric__{k}"] = v
        for k, v in reasons.items():
            row[f"reason__{k}"] = v
        rows.append(row)
    return pd.DataFrame(rows)


def evaluation_result_to_dataframe(result: EvaluationResult) -> pd.DataFrame:
    rows = []
    for tr in result.test_results:
        tc = tr.test_case
        task = tc.task_output or {}
        dataset = tc.dataset_item_content or {}
        row: Dict[str, Any] = {
            "dataset_item_id": tc.dataset_item_id,
            "trace_id": tc.trace_id,
            "grader_output": task.get("output"),
            "task_output_json": task,
            "dataset_item_json": dataset,
            "task_execution_time": tr.task_execution_time,
            "scoring_time": tr.scoring_time,
        }
        for sr in tr.score_results:
            if sr.scoring_failed:
                continue
            row[f"metric__{sr.name}"] = sr.value
            row[f"reason__{sr.name}"] = sr.reason
        rows.append(row)
    return pd.DataFrame(rows)


def metric_summary(df: pd.DataFrame) -> pd.DataFrame:
    metric_cols = [c for c in df.columns if c.startswith("metric__")]
    if not metric_cols:
        return pd.DataFrame()
    numeric = df[metric_cols].apply(pd.to_numeric, errors="coerce")
    summary = numeric.describe().T
    summary["metric"] = summary.index.str.replace("metric__", "", regex=False)
    summary.index = summary["metric"]
    duration_names = {"grader_duration_sec", "judge_duration_sec"}
    pass_rates = {}
    for c in metric_cols:
        name = c.replace("metric__", "", 1)
        if name in duration_names:
            continue
        pass_rates[name] = float((numeric[c] == 1).mean())
    if pass_rates:
        summary["pass_rate"] = summary["metric"].map(pass_rates)
    return summary


def filter_review_dataframe(
    df: pd.DataFrame,
    *,
    metric_names: Optional[Sequence[str]] = None,
    only_fails: bool = False,
    max_rows: Optional[int] = None,
) -> pd.DataFrame:
    metric_cols = [c for c in df.columns if c.startswith("metric__")]
    if metric_names:
        wanted = {f"metric__{m}" for m in metric_names}
        metric_cols = [c for c in metric_cols if c in wanted]

    out = df.copy()
    if only_fails and metric_cols:
        numeric = out[metric_cols].apply(pd.to_numeric, errors="coerce")
        mask = (numeric == 0).any(axis=1)
        out = out[mask]

    if max_rows is not None:
        out = out.head(max_rows)
    return out


def _pick_context(dataset: Dict[str, Any], task: Dict[str, Any], keys: Sequence[str]) -> Dict[str, Any]:
    context: Dict[str, Any] = {}
    for k in keys:
        val = dataset.get(k)
        if val in (None, "", "NaN"):
            val = task.get(k)
        if val not in (None, "", "NaN"):
            context[k] = val
    return context


def build_metric_review_packet(
    row: Dict[str, Any],
    *,
    context_keys: Sequence[str],
    metrics: Sequence[str],
) -> Dict[str, Any]:
    dataset = row.get("dataset_item_json") or {}
    task = row.get("task_output_json") or {}
    if isinstance(dataset, str):
        dataset = json.loads(dataset)
    if isinstance(task, str):
        task = json.loads(task)

    metric_blocks = []
    for m in metrics:
        val = row.get(f"metric__{m}")
        reason = row.get(f"reason__{m}")
        if val is None and reason is None:
            continue
        metric_blocks.append({"metric": m, "value": val, "reason": reason})

    return {
        "dataset_item_id": row.get("dataset_item_id"),
        "trace_id": row.get("trace_id"),
        "experiment_item_id": row.get("experiment_item_id"),
        "context": _pick_context(dataset, task, context_keys),
        "grader_output": row.get("grader_output"),
        "metrics": metric_blocks,
    }


def packet_to_markdown(packet: Dict[str, Any], *, max_context_chars: int = 12000, max_output_chars: int = 8000) -> str:
    context_json = json.dumps(packet.get("context") or {}, indent=2, ensure_ascii=False)
    if len(context_json) > max_context_chars:
        context_json = context_json[:max_context_chars] + "\n... (truncated)"

    output = packet.get("grader_output") or ""
    if len(output) > max_output_chars:
        output = output[:max_output_chars] + "\n... (truncated)"

    lines = [
        f"## dataset_item_id: {packet.get('dataset_item_id')}",
        f"trace_id: {packet.get('trace_id')}",
        "",
        "### Context",
        "```json",
        context_json,
        "```",
        "",
        "### Grader output",
        "```",
        output,
        "```",
        "",
        "### Metrics",
    ]
    for m in packet.get("metrics") or []:
        lines.append(f"- **{m['metric']}** = {m['value']}")
        if m.get("reason"):
            lines.append(f"  - reason: {m['reason']}")
    return "\n".join(lines)


def export_review_jsonl(
    df: pd.DataFrame,
    path,
    *,
    context_keys: Sequence[str],
    metrics: Sequence[str],
) -> int:
    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for _, row in df.iterrows():
            packet = build_metric_review_packet(
                row.to_dict(), context_keys=context_keys, metrics=metrics
            )
            f.write(json.dumps(packet, ensure_ascii=False) + "\n")
            count += 1
    return count
