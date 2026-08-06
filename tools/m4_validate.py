#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4 validation CLI: cloud-gated end-to-end evaluation for JanusSearch."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import yaml
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from tools.search import (
    DEFAULT_COLLECTION_NAME,
    DEFAULT_DB_PATH,
    DEFAULT_EMBED_BASE_URL,
    DEFAULT_EMBED_MODEL,
    DEFAULT_VECTORS_ROOT,
    ensure_str,
    normalize_openai_base_url,
    run_hybrid,
    run_search,
)

LOGGER = logging.getLogger("m4_validate")

DEFAULT_TOPICS_FILE = Path("artifacts/m3/topic_assignments.json")
DEFAULT_FIXED_QUERY_FILE = Path("docs/fixtures/m4_fixed_queries.yaml")
DEFAULT_OUTPUT_JSON = Path("artifacts/m4/eval_report.json")
DEFAULT_OUTPUT_MD = Path("artifacts/m4/eval_report.md")
DEFAULT_SAMPLED_DUMP = Path("artifacts/m4/sampled_queries.json")

DEFAULT_SAMPLE_TOPICS = 20
DEFAULT_SAMPLE_PER_TOPIC = 2
DEFAULT_SAMPLE_SEED = 42
DEFAULT_TOP_K = 50

DEFAULT_VECTOR_TOP_K = 100
DEFAULT_BM25_TOP_K = 100
DEFAULT_ALPHA = 0.6
SAMPLED_PASS_THRESHOLD = 0.9

def utc_now_iso() -> str:
    """Return UTC timestamp in ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write UTF-8 JSON payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_text(path: Path, content: str) -> None:
    """Write UTF-8 text file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _normalize_title(text: str) -> str:
    """Normalize title for robust fuzzy matching."""
    lowered = ensure_str(text).lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(lowered.split())


def _resolve_embed_api_key(explicit_key: str | None) -> str:
    """Resolve embedding API key from explicit arg then env fallback."""
    key = (
        ensure_str(explicit_key)
        or ensure_str(os.getenv("JANUS_EMBED_API_KEY"))
        or ensure_str(os.getenv("JANUS_LLM_API_KEY"))
    )
    if not key:
        raise ValueError(
            "Embedding API key is required. Set --embed-api-key or env "
            "`JANUS_EMBED_API_KEY` / `JANUS_LLM_API_KEY`."
        )
    return key


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _request_embedding_healthcheck(
    *,
    base_url: str,
    api_key: str,
    model: str,
) -> int:
    """Call OpenAI-compatible embedding endpoint and return embedding dimension."""
    try:
        from openai import OpenAI
        import httpx
    except ImportError as exc:
        raise RuntimeError("Missing dependency `openai`/`httpx`. Run `uv add openai httpx`.") from exc

    client = OpenAI(
        base_url=normalize_openai_base_url(base_url),
        api_key=api_key,
        http_client=httpx.Client(trust_env=False, timeout=30.0),
    )
    try:
        response = client.embeddings.create(model=model, input=["healthcheck"])
    finally:
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            close_fn()

    if not response.data:
        raise RuntimeError("Embedding healthcheck returned empty `data`.")

    embedding = response.data[0].embedding
    if not embedding:
        raise RuntimeError("Embedding healthcheck returned empty vector.")
    return len(embedding)


def run_online_healthcheck(
    *,
    base_url: str,
    model: str,
    api_key: str,
) -> Dict[str, Any]:
    """Run cloud embedding healthcheck under hard-gate policy."""
    start = time.perf_counter()
    normalized_base = normalize_openai_base_url(base_url)
    try:
        dim = _request_embedding_healthcheck(
            base_url=normalized_base,
            api_key=api_key,
            model=model,
        )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "pass": True,
            "base_url": normalized_base,
            "model": model,
            "embedding_dim": dim,
            "latency_ms": elapsed_ms,
            "error": None,
        }
    except Exception as exc:  # pragma: no cover - handled by tests via stubs/mocks
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "pass": False,
            "base_url": normalized_base,
            "model": model,
            "embedding_dim": None,
            "latency_ms": elapsed_ms,
            "error": str(exc),
        }


def _load_fixed_query_cases(path: Path) -> List[Dict[str, Any]]:
    """Load fixed evaluation cases from YAML."""
    if not path.exists():
        raise FileNotFoundError(f"Fixed query file not found: {path}")

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Fixed query file must be a mapping with key `cases`.")

    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("Fixed query file `cases` must be a non-empty list.")

    cases: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_cases, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Fixed query case #{index} must be a mapping.")
        case_id = ensure_str(item.get("case_id")) or f"fixed_case_{index:03d}"
        mode = ensure_str(item.get("mode")).lower()
        if mode not in {"search", "hybrid"}:
            raise ValueError(f"{case_id}: mode must be `search` or `hybrid`.")
        query = ensure_str(item.get("query"))
        if not query:
            raise ValueError(f"{case_id}: query must not be empty.")

        expect_min_results = int(item.get("expect_min_results", 1))
        if expect_min_results < 0:
            raise ValueError(f"{case_id}: expect_min_results must be >= 0.")

        case_top_k = int(item.get("top_k", DEFAULT_TOP_K))
        if case_top_k <= 0:
            raise ValueError(f"{case_id}: top_k must be > 0.")

        any_fragments = item.get("expect_any_title_fragments") or []
        all_fragments = item.get("expect_all_title_fragments") or []
        if not isinstance(any_fragments, list) or not isinstance(all_fragments, list):
            raise ValueError(f"{case_id}: title fragment expectations must be string lists.")
        any_fragments = [ensure_str(value) for value in any_fragments if ensure_str(value)]
        all_fragments = [ensure_str(value) for value in all_fragments if ensure_str(value)]

        filters = item.get("filters") or {}
        if not isinstance(filters, dict):
            raise ValueError(f"{case_id}: filters must be a mapping.")

        cases.append(
            {
                "case_id": case_id,
                "mode": mode,
                "query": query,
                "top_k": case_top_k,
                "expect_min_results": expect_min_results,
                "expect_any_title_fragments": any_fragments,
                "expect_all_title_fragments": all_fragments,
                "filters": filters,
            }
        )
    return cases


def _parse_venues(raw_value: Any) -> List[str]:
    """Normalize venue filters into uppercase list."""
    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        raw_items = [item.strip() for item in raw_value.split(",")]
    elif isinstance(raw_value, list):
        raw_items = [ensure_str(item) for item in raw_value]
    else:
        raw_items = [ensure_str(raw_value)]
    return [item.upper() for item in raw_items if item]


def _to_optional_int(value: Any) -> int | None:
    """Convert optional value to int."""
    if value is None:
        return None
    text = ensure_str(value)
    if not text:
        return None
    return int(text)


def _extract_case_filters(filters: Mapping[str, Any]) -> Dict[str, Any]:
    """Convert untyped filter mapping into typed search filters."""
    venues = _parse_venues(filters.get("venue"))
    return {
        "venues": venues,
        "year_from": _to_optional_int(filters.get("year_from")),
        "year_to": _to_optional_int(filters.get("year_to")),
        "track": ensure_str(filters.get("track")) or None,
        "presentation_level": ensure_str(filters.get("presentation_level")) or None,
        "include_placeholder": bool(filters.get("include_placeholder", False)),
    }


def _titles_satisfy_expectations(
    *,
    titles: Sequence[str],
    expect_any: Sequence[str],
    expect_all: Sequence[str],
) -> Dict[str, Any]:
    """Evaluate title-fragment expectations against result titles."""
    normalized_titles = [_normalize_title(title) for title in titles if ensure_str(title)]

    any_hits = [
        fragment
        for fragment in expect_any
        if any(_normalize_title(fragment) in title for title in normalized_titles)
    ]
    all_hits = [
        fragment
        for fragment in expect_all
        if any(_normalize_title(fragment) in title for title in normalized_titles)
    ]

    any_pass = True if not expect_any else len(any_hits) >= 1
    all_pass = True if not expect_all else len(all_hits) == len(expect_all)
    return {
        "any_pass": any_pass,
        "all_pass": all_pass,
        "any_hits": any_hits,
        "all_hits": all_hits,
    }


def run_fixed_suite(
    *,
    db_path: Path,
    vectors_root: Path,
    collection_name: str,
    cases: Sequence[Mapping[str, Any]],
    default_top_k: int,
    embed_base_url: str,
    embed_model: str,
    embed_api_key: str,
) -> Dict[str, Any]:
    """Execute fixed query cases and evaluate strict pass criteria."""
    result_cases: List[Dict[str, Any]] = []
    passed = 0

    for raw_case in cases:
        case = dict(raw_case)
        case_id = ensure_str(case.get("case_id"))
        mode = ensure_str(case.get("mode")).lower()
        query = ensure_str(case.get("query"))
        case_top_k = int(case.get("top_k", default_top_k))
        expect_min_results = int(case.get("expect_min_results", 1))
        expect_any = list(case.get("expect_any_title_fragments") or [])
        expect_all = list(case.get("expect_all_title_fragments") or [])
        filters = _extract_case_filters(case.get("filters") or {})

        payload: Dict[str, Any]
        error: str | None = None
        try:
            if mode == "search":
                payload = run_search(
                    db_path=db_path,
                    query=query,
                    venues=filters["venues"],
                    year_from=filters["year_from"],
                    year_to=filters["year_to"],
                    track=filters["track"],
                    presentation_level=filters["presentation_level"],
                    include_placeholder=filters["include_placeholder"],
                    order="bm25",
                    top_k=case_top_k,
                    offset=0,
                )
            elif mode == "hybrid":
                payload = run_hybrid(
                    db_path=db_path,
                    query=query,
                    embed_base_url=embed_base_url,
                    embed_model=embed_model,
                    embed_api_key=embed_api_key,
                    alpha=DEFAULT_ALPHA,
                    vector_top_k=DEFAULT_VECTOR_TOP_K,
                    bm25_top_k=DEFAULT_BM25_TOP_K,
                    vectors_root=vectors_root,
                    collection_name=collection_name,
                    venues=filters["venues"],
                    year_from=filters["year_from"],
                    year_to=filters["year_to"],
                    track=filters["track"],
                    presentation_level=filters["presentation_level"],
                    include_placeholder=filters["include_placeholder"],
                    top_k=case_top_k,
                    offset=0,
                )
            else:
                raise ValueError(f"Unsupported case mode: {mode}")
        except Exception as exc:
            payload = {"total": 0, "results": []}
            error = str(exc)

        total = int(payload.get("total", 0))
        titles = [ensure_str(item.get("title")) for item in payload.get("results", [])]
        expectation = _titles_satisfy_expectations(
            titles=titles,
            expect_any=expect_any,
            expect_all=expect_all,
        )

        count_pass = total >= expect_min_results
        case_pass = error is None and count_pass and expectation["any_pass"] and expectation["all_pass"]
        if case_pass:
            passed += 1

        result_cases.append(
            {
                "case_id": case_id,
                "mode": mode,
                "query": query,
                "filters": filters,
                "expect_min_results": expect_min_results,
                "expect_any_title_fragments": expect_any,
                "expect_all_title_fragments": expect_all,
                "actual_total": total,
                "count_pass": count_pass,
                "any_pass": expectation["any_pass"],
                "all_pass": expectation["all_pass"],
                "any_hits": expectation["any_hits"],
                "all_hits": expectation["all_hits"],
                "pass": case_pass,
                "error": error,
            }
        )

    total_cases = len(result_cases)
    pass_rate = float(passed / total_cases) if total_cases else 0.0
    return {
        "total_cases": total_cases,
        "passed_cases": passed,
        "pass_rate": pass_rate,
        "all_pass": passed == total_cases and total_cases > 0,
        "cases": result_cases,
    }


def build_sampled_queries(
    *,
    topics_file: Path,
    sample_topics: int,
    sample_per_topic: int,
    seed: int,
    top_k: int,
) -> Dict[str, Any]:
    """Build deterministic sampled hybrid queries from topic assignments."""
    if sample_topics <= 0:
        raise ValueError("`--sample-topics` must be > 0.")
    if sample_per_topic <= 0:
        raise ValueError("`--sample-per-topic` must be > 0.")

    if not topics_file.exists():
        raise FileNotFoundError(f"Topics assignment file not found: {topics_file}")
    payload = json.loads(topics_file.read_text(encoding="utf-8"))
    topics = payload.get("topics")
    if not isinstance(topics, list) or not topics:
        raise ValueError(f"{topics_file} does not contain non-empty `topics` array.")

    rng = random.Random(seed)
    selected_topics = (
        rng.sample(topics, k=sample_topics) if sample_topics < len(topics) else list(topics)
    )

    cases: List[Dict[str, Any]] = []
    for topic_index, topic in enumerate(selected_topics, start=1):
        topic_name = ensure_str(topic.get("topic_name")) or ensure_str(topic.get("topic_slug")) or "topic"
        topic_slug = ensure_str(topic.get("topic_slug")) or f"topic_{topic_index:02d}"
        subtopics = topic.get("subtopics") if isinstance(topic, dict) else []
        if not isinstance(subtopics, list):
            subtopics = []

        if subtopics:
            selected_subtopics = (
                rng.sample(subtopics, k=sample_per_topic)
                if sample_per_topic < len(subtopics)
                else list(subtopics)
            )
        else:
            selected_subtopics = []

        if not selected_subtopics:
            case_id = f"sample_{topic_slug}_topic"
            cases.append(
                {
                    "case_id": case_id,
                    "mode": "hybrid",
                    "query": topic_name,
                    "topic_slug": topic_slug,
                    "subtopic_slug": None,
                    "top_k": top_k,
                }
            )
            continue

        for subtopic in selected_subtopics:
            subtopic_name = (
                ensure_str(subtopic.get("subtopic_name"))
                or ensure_str(subtopic.get("subtopic_slug"))
                or "subtopic"
            )
            subtopic_slug = ensure_str(subtopic.get("subtopic_slug")) or "subtopic"
            case_id = f"sample_{topic_slug}_{subtopic_slug}"
            query = f"{topic_name} {subtopic_name}".strip()
            cases.append(
                {
                    "case_id": case_id,
                    "mode": "hybrid",
                    "query": query,
                    "topic_slug": topic_slug,
                    "subtopic_slug": subtopic_slug,
                    "top_k": top_k,
                }
            )

    return {
        "generated_at_utc": utc_now_iso(),
        "topics_file": str(topics_file),
        "seed": seed,
        "sample_topics": sample_topics,
        "sample_per_topic": sample_per_topic,
        "requested_top_k": top_k,
        "case_count": len(cases),
        "cases": cases,
    }


def load_topic_membership(topics_file: Path) -> Dict[tuple[str, str | None], set[str]]:
    """Load paper membership for every topic and exact subtopic."""
    if not topics_file.exists():
        raise FileNotFoundError(f"Topics assignment file not found: {topics_file}")
    payload = json.loads(topics_file.read_text(encoding="utf-8"))
    assignments = payload.get("assignments")
    if not isinstance(assignments, list):
        raise ValueError(f"{topics_file} does not contain an `assignments` array.")

    membership: Dict[tuple[str, str | None], set[str]] = {}
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        paper_id = ensure_str(assignment.get("paper_id"))
        topic_slug = ensure_str(assignment.get("topic_slug"))
        subtopic_slug = ensure_str(assignment.get("subtopic_slug"))
        if not paper_id or not topic_slug:
            continue
        membership.setdefault((topic_slug, None), set()).add(paper_id)
        if subtopic_slug:
            membership.setdefault((topic_slug, subtopic_slug), set()).add(paper_id)
    return membership


def run_sampled_suite(
    *,
    db_path: Path,
    vectors_root: Path,
    collection_name: str,
    sampled_cases: Sequence[Mapping[str, Any]],
    topic_membership: Mapping[tuple[str, str | None], set[str]],
    embed_base_url: str,
    embed_model: str,
    embed_api_key: str,
) -> Dict[str, Any]:
    """Execute sampled hybrid cases and evaluate health threshold."""
    results: List[Dict[str, Any]] = []
    passed = 0

    for raw_case in sampled_cases:
        case = dict(raw_case)
        case_id = ensure_str(case.get("case_id"))
        query = ensure_str(case.get("query"))
        topic_slug = ensure_str(case.get("topic_slug"))
        subtopic_slug = ensure_str(case.get("subtopic_slug")) or None
        top_k = int(case.get("top_k", DEFAULT_TOP_K))
        error: str | None = None
        payload: Dict[str, Any]
        try:
            payload = run_hybrid(
                db_path=db_path,
                query=query,
                embed_base_url=embed_base_url,
                embed_model=embed_model,
                embed_api_key=embed_api_key,
                alpha=DEFAULT_ALPHA,
                vector_top_k=DEFAULT_VECTOR_TOP_K,
                bm25_top_k=DEFAULT_BM25_TOP_K,
                vectors_root=vectors_root,
                collection_name=collection_name,
                venues=[],
                year_from=None,
                year_to=None,
                track=None,
                presentation_level=None,
                include_placeholder=False,
                top_k=top_k,
                offset=0,
            )
        except Exception as exc:
            payload = {"total": 0, "results": []}
            error = str(exc)

        total = int(payload.get("total", 0))
        result_items = payload.get("results")
        if not isinstance(result_items, list):
            result_items = []
        structure_ok = bool(result_items) and all(
            isinstance(item, dict)
            and ensure_str(item.get("paper_id"))
            and ensure_str(item.get("title"))
            and ensure_str(item.get("venue"))
            and item.get("year") is not None
            for item in result_items
        )
        returned_ids = {
            ensure_str(item.get("paper_id"))
            for item in result_items
            if isinstance(item, dict) and ensure_str(item.get("paper_id"))
        }
        expected_ids = topic_membership.get((topic_slug, subtopic_slug), set())
        relevant_result_ids = sorted(returned_ids & expected_ids)
        membership_available = bool(expected_ids)
        relevance_ok = bool(relevant_result_ids)
        case_pass = (
            error is None
            and total > 0
            and structure_ok
            and membership_available
            and relevance_ok
        )
        if case_pass:
            passed += 1

        results.append(
            {
                "case_id": case_id,
                "query": query,
                "topic_slug": topic_slug,
                "subtopic_slug": subtopic_slug,
                "top_k": top_k,
                "actual_total": total,
                "returned_result_count": len(result_items),
                "structure_ok": structure_ok,
                "membership_available": membership_available,
                "expected_member_count": len(expected_ids),
                "relevant_hit_count": len(relevant_result_ids),
                "relevant_result_ids": relevant_result_ids,
                "relevance_ok": relevance_ok,
                "pass": case_pass,
                "error": error,
            }
        )

    total_cases = len(results)
    pass_rate = float(passed / total_cases) if total_cases else 0.0
    pass_threshold = pass_rate >= SAMPLED_PASS_THRESHOLD
    return {
        "total_cases": total_cases,
        "passed_cases": passed,
        "pass_rate": pass_rate,
        "threshold": SAMPLED_PASS_THRESHOLD,
        "pass_threshold": pass_threshold,
        "cases": results,
    }


def aggregate_summary(
    *,
    db_path: Path,
    vectors_root: Path,
    collection_name: str,
    topics_file: Path,
    fixed_query_file: Path,
    output_json: Path,
    output_md: Path,
    sampled_dump: Path,
    online_gate: Mapping[str, Any],
    fixed_suite: Mapping[str, Any],
    sampled_suite: Mapping[str, Any],
) -> Dict[str, Any]:
    """Aggregate M4 summary and final gate status."""
    online_gate_pass = bool(online_gate.get("pass"))
    fixed_pass = bool(fixed_suite.get("all_pass"))
    sampled_pass = bool(sampled_suite.get("pass_threshold"))
    overall_pass = online_gate_pass and fixed_pass and sampled_pass
    return {
        "generated_at_utc": utc_now_iso(),
        "overall_pass": overall_pass,
        "gate_policy": "cloud_hard_fail",
        "online_gate_pass": online_gate_pass,
        "fixed_suite_pass": fixed_pass,
        "sampled_suite_pass": sampled_pass,
        "db_path": str(db_path),
        "vectors_root": str(vectors_root),
        "collection_name": collection_name,
        "topics_file": str(topics_file),
        "fixed_query_file": str(fixed_query_file),
        "output_json": str(output_json),
        "output_md": str(output_md),
        "sampled_dump": str(sampled_dump),
        "fixed_case_count": int(fixed_suite.get("total_cases", 0)),
        "fixed_passed_count": int(fixed_suite.get("passed_cases", 0)),
        "fixed_pass_rate": float(fixed_suite.get("pass_rate", 0.0)),
        "sampled_case_count": int(sampled_suite.get("total_cases", 0)),
        "sampled_passed_count": int(sampled_suite.get("passed_cases", 0)),
        "sampled_pass_rate": float(sampled_suite.get("pass_rate", 0.0)),
        "sampled_threshold": float(sampled_suite.get("threshold", SAMPLED_PASS_THRESHOLD)),
    }


def _render_markdown_report(report: Mapping[str, Any]) -> str:
    """Render markdown summary report."""
    summary = report.get("summary", {})
    online = report.get("online_gate", {})
    fixed_suite = report.get("fixed_suite", {})
    sampled_suite = report.get("sampled_suite", {})

    lines = [
        "# M4 Agent Validation Report",
        "",
        f"- Generated at (UTC): {summary.get('generated_at_utc')}",
        f"- Overall pass: {'PASS' if summary.get('overall_pass') else 'FAIL'}",
        f"- Gate policy: {summary.get('gate_policy')}",
        "",
        "## Online Gate",
        f"- Pass: {online.get('pass')}",
        f"- Base URL: {online.get('base_url')}",
        f"- Model: {online.get('model')}",
        f"- Latency ms: {online.get('latency_ms')}",
    ]
    if online.get("error"):
        lines.append(f"- Error: {online.get('error')}")

    lines.extend(
        [
            "",
            "## Suite Summary",
            "| Suite | Pass | Passed/Total | Rate |",
            "|---|---:|---:|---:|",
            (
                f"| Fixed | {str(fixed_suite.get('all_pass'))} | "
                f"{fixed_suite.get('passed_cases', 0)}/{fixed_suite.get('total_cases', 0)} | "
                f"{float(fixed_suite.get('pass_rate', 0.0)):.2%} |"
            ),
            (
                f"| Sampled | {str(sampled_suite.get('pass_threshold'))} | "
                f"{sampled_suite.get('passed_cases', 0)}/{sampled_suite.get('total_cases', 0)} | "
                f"{float(sampled_suite.get('pass_rate', 0.0)):.2%} |"
            ),
            "",
            "## Failed Cases",
        ]
    )

    failed_lines = []
    for item in fixed_suite.get("cases", []):
        if not item.get("pass"):
            failed_lines.append(
                f"- [fixed] {item.get('case_id')}: total={item.get('actual_total')} error={item.get('error')}"
            )
    for item in sampled_suite.get("cases", []):
        if not item.get("pass"):
            failed_lines.append(
                f"- [sampled] {item.get('case_id')}: total={item.get('actual_total')} error={item.get('error')}"
            )
    if failed_lines:
        lines.extend(failed_lines)
    else:
        lines.append("- None")

    return "\n".join(lines) + "\n"


def run_m4(
    *,
    db_path: Path,
    vectors_root: Path,
    collection_name: str,
    topics_file: Path,
    fixed_query_file: Path,
    embed_base_url: str,
    embed_model: str,
    embed_api_key: str | None,
    sample_topics: int,
    sample_per_topic: int,
    sample_seed: int,
    top_k: int,
    output_json: Path,
    output_md: Path,
    sampled_dump: Path,
) -> Dict[str, Any]:
    """Run full M4 validation pipeline and persist reports."""
    api_key = _resolve_embed_api_key(embed_api_key)
    online_gate = run_online_healthcheck(
        base_url=embed_base_url,
        model=embed_model,
        api_key=api_key,
    )

    sampled_payload: Dict[str, Any] = {
        "generated_at_utc": utc_now_iso(),
        "case_count": 0,
        "cases": [],
        "reason": None,
    }

    if not bool(online_gate.get("pass")):
        fixed_suite = {
            "total_cases": 0,
            "passed_cases": 0,
            "pass_rate": 0.0,
            "all_pass": False,
            "cases": [],
            "reason": "skipped_due_to_online_gate_failure",
        }
        sampled_suite = {
            "total_cases": 0,
            "passed_cases": 0,
            "pass_rate": 0.0,
            "threshold": SAMPLED_PASS_THRESHOLD,
            "pass_threshold": False,
            "cases": [],
            "reason": "skipped_due_to_online_gate_failure",
        }
        sampled_payload["reason"] = "skipped_due_to_online_gate_failure"
    else:
        fixed_cases = _load_fixed_query_cases(fixed_query_file)
        fixed_suite = run_fixed_suite(
            db_path=db_path,
            vectors_root=vectors_root,
            collection_name=collection_name,
            cases=fixed_cases,
            default_top_k=top_k,
            embed_base_url=embed_base_url,
            embed_model=embed_model,
            embed_api_key=api_key,
        )

        sampled_payload = build_sampled_queries(
            topics_file=topics_file,
            sample_topics=sample_topics,
            sample_per_topic=sample_per_topic,
            seed=sample_seed,
            top_k=top_k,
        )
        topic_membership = load_topic_membership(topics_file)
        sampled_suite = run_sampled_suite(
            db_path=db_path,
            vectors_root=vectors_root,
            collection_name=collection_name,
            sampled_cases=sampled_payload.get("cases", []),
            topic_membership=topic_membership,
            embed_base_url=embed_base_url,
            embed_model=embed_model,
            embed_api_key=api_key,
        )

    summary = aggregate_summary(
        db_path=db_path,
        vectors_root=vectors_root,
        collection_name=collection_name,
        topics_file=topics_file,
        fixed_query_file=fixed_query_file,
        output_json=output_json,
        output_md=output_md,
        sampled_dump=sampled_dump,
        online_gate=online_gate,
        fixed_suite=fixed_suite,
        sampled_suite=sampled_suite,
    )
    report = {
        "summary": summary,
        "online_gate": online_gate,
        "fixed_suite": fixed_suite,
        "sampled_suite": sampled_suite,
    }

    write_json(sampled_dump, sampled_payload)
    write_json(output_json, report)
    write_text(output_md, _render_markdown_report(report))
    LOGGER.info("M4 sampled queries written: %s", sampled_dump)
    LOGGER.info("M4 report written: %s", output_json)
    LOGGER.info("M4 markdown report written: %s", output_md)
    return report


def _render_status_text(report: Mapping[str, Any]) -> str:
    """Render short status text for CLI."""
    summary = report.get("summary", {})
    status = "PASS" if summary.get("overall_pass") else "FAIL"
    return "\n".join(
        [
            f"M4 status: {status}",
            f"generated_at_utc: {summary.get('generated_at_utc')}",
            f"online_gate_pass: {summary.get('online_gate_pass')}",
            f"fixed_suite_pass: {summary.get('fixed_suite_pass')}",
            f"sampled_suite_pass: {summary.get('sampled_suite_pass')}",
        ]
    )


def run_status(report_path: Path) -> Dict[str, Any]:
    """Load and return existing M4 report."""
    if not report_path.exists():
        raise FileNotFoundError(
            f"M4 report not found: {report_path}. Run `python3 -m tools.m4_validate run` first."
        )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "summary" not in payload:
        raise ValueError(f"Invalid M4 report format: {report_path}")
    return payload


def build_arg_parser() -> argparse.ArgumentParser:
    """Build argument parser for M4 CLI."""
    parser = argparse.ArgumentParser(description="M4 cloud-gated end-to-end validation")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Log level",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite db path (default: {DEFAULT_DB_PATH})",
    )
    common.add_argument(
        "--vectors-root",
        default=str(DEFAULT_VECTORS_ROOT),
        help=f"Chroma vectors root (default: {DEFAULT_VECTORS_ROOT})",
    )
    common.add_argument(
        "--collection-name",
        default=DEFAULT_COLLECTION_NAME,
        help=f"Chroma collection name (default: {DEFAULT_COLLECTION_NAME})",
    )
    common.add_argument(
        "--topics-file",
        default=str(DEFAULT_TOPICS_FILE),
        help=f"M3 topics assignment file (default: {DEFAULT_TOPICS_FILE})",
    )
    common.add_argument(
        "--fixed-query-file",
        default=str(DEFAULT_FIXED_QUERY_FILE),
        help=f"Fixed query YAML file (default: {DEFAULT_FIXED_QUERY_FILE})",
    )
    common.add_argument(
        "--embed-base-url",
        default=ensure_str(os.getenv("JANUS_EMBED_BASE_URL")) or DEFAULT_EMBED_BASE_URL,
        help="Embedding endpoint base URL.",
    )
    common.add_argument(
        "--embed-model",
        default=DEFAULT_EMBED_MODEL,
        help=f"Embedding model (default: {DEFAULT_EMBED_MODEL})",
    )
    common.add_argument(
        "--embed-api-key",
        default=ensure_str(os.getenv("JANUS_EMBED_API_KEY"))
        or ensure_str(os.getenv("JANUS_LLM_API_KEY"))
        or None,
        help="Embedding API key (required by hard gate).",
    )
    common.add_argument(
        "--sample-topics",
        type=int,
        default=DEFAULT_SAMPLE_TOPICS,
        help=f"Sampled topics count (default: {DEFAULT_SAMPLE_TOPICS})",
    )
    common.add_argument(
        "--sample-per-topic",
        type=int,
        default=DEFAULT_SAMPLE_PER_TOPIC,
        help=f"Sampled queries per topic (default: {DEFAULT_SAMPLE_PER_TOPIC})",
    )
    common.add_argument(
        "--sample-seed",
        type=int,
        default=DEFAULT_SAMPLE_SEED,
        help=f"Random seed for sampled queries (default: {DEFAULT_SAMPLE_SEED})",
    )
    common.add_argument(
        "--top-k",
        type=int,
        default=DEFAULT_TOP_K,
        help=f"Top-k for fixed/sampled cases (default: {DEFAULT_TOP_K})",
    )
    common.add_argument(
        "--output-json",
        default=str(DEFAULT_OUTPUT_JSON),
        help=f"M4 JSON report path (default: {DEFAULT_OUTPUT_JSON})",
    )
    common.add_argument(
        "--output-md",
        default=str(DEFAULT_OUTPUT_MD),
        help=f"M4 markdown report path (default: {DEFAULT_OUTPUT_MD})",
    )
    common.add_argument(
        "--sampled-dump",
        default=str(DEFAULT_SAMPLED_DUMP),
        help=f"M4 sampled query dump path (default: {DEFAULT_SAMPLED_DUMP})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "run",
        parents=[common],
        help="Run online gate + fixed/sampled suites",
    )
    subparsers.add_parser(
        "status",
        parents=[common],
        help="Show brief status from latest M4 report",
    )
    return parser


def main() -> int:
    """CLI entrypoint."""
    parser = build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    db_path = Path(args.db_path)
    vectors_root = Path(args.vectors_root)
    topics_file = Path(args.topics_file)
    fixed_query_file = Path(args.fixed_query_file)
    output_json = Path(args.output_json)
    output_md = Path(args.output_md)
    sampled_dump = Path(args.sampled_dump)

    try:
        if args.command == "run":
            report = run_m4(
                db_path=db_path,
                vectors_root=vectors_root,
                collection_name=ensure_str(args.collection_name) or DEFAULT_COLLECTION_NAME,
                topics_file=topics_file,
                fixed_query_file=fixed_query_file,
                embed_base_url=args.embed_base_url,
                embed_model=args.embed_model,
                embed_api_key=args.embed_api_key,
                sample_topics=int(args.sample_topics),
                sample_per_topic=int(args.sample_per_topic),
                sample_seed=int(args.sample_seed),
                top_k=int(args.top_k),
                output_json=output_json,
                output_md=output_md,
                sampled_dump=sampled_dump,
            )
            summary = report.get("summary", {})
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0 if bool(summary.get("overall_pass")) else 1

        if args.command == "status":
            report = run_status(output_json)
            print(_render_status_text(report))
            return 0 if bool(report.get("summary", {}).get("overall_pass")) else 1

        parser.error(f"Unknown command: {args.command}")
        return 2
    except (FileNotFoundError, RuntimeError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
