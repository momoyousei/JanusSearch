#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline-first retrieval evaluation and freshness-aware status."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, Tuple

from janussearch.infrastructure.fingerprints import fingerprint_paths
from janussearch.application import evaluation_pipeline as legacy


def evaluation_fingerprint(
    *,
    db_path: Path,
    vectors_root: Path,
    topics_file: Path,
    fixed_query_file: Path,
) -> str:
    """Fingerprint every persisted input that can change evaluation meaning."""
    return fingerprint_paths(
        {
            "database": db_path,
            "vectors": vectors_root,
            "topics": topics_file,
            "fixed_queries": fixed_query_file,
        }
    )


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write an indented JSON object."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_markdown(path: Path, report: Mapping[str, Any]) -> None:
    """Write a compact capability-oriented evaluation report."""
    summary = report.get("summary", {})
    lines = [
        "# JanusSearch Evaluation Report",
        "",
        f"- Suite: {summary.get('suite')}",
        f"- Overall pass: {summary.get('overall_pass')}",
        f"- Generated at (UTC): {summary.get('generated_at_utc')}",
        f"- Input fingerprint: `{summary.get('input_fingerprint')}`",
        "",
        "## Fixed-query cases",
        "",
        "| Case | Requested mode | Effective mode | Pass | Results |",
        "|---|---|---|---:|---:|",
    ]
    fixed = report.get("offline_suite") or report.get("fixed_suite") or {}
    for item in fixed.get("cases", []):
        lines.append(
            f"| {item.get('case_id')} | {item.get('requested_mode', item.get('mode'))} | "
            f"{item.get('effective_mode', item.get('mode'))} | {item.get('pass')} | "
            f"{item.get('actual_total', 0)} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_offline_suite(
    *,
    db_path: Path,
    vectors_root: Path,
    collection_name: str,
    fixed_query_file: Path,
    top_k: int,
) -> Dict[str, Any]:
    """Run every fixed case through deterministic local FTS only."""
    cases = legacy._load_fixed_query_cases(fixed_query_file)
    requested_modes = [str(case.get("mode")) for case in cases]
    effective_cases = []
    for case in cases:
        effective = dict(case)
        effective["mode"] = "search"
        effective_cases.append(effective)
    result = legacy.run_fixed_suite(
        db_path=db_path,
        vectors_root=vectors_root,
        collection_name=collection_name,
        cases=effective_cases,
        default_top_k=top_k,
        embed_base_url="offline://unused",
        embed_model="offline",
        embed_api_key="offline",
    )
    for item, requested_mode in zip(result.get("cases", []), requested_modes):
        item["requested_mode"] = requested_mode
        item["effective_mode"] = "search"
    return result


def execute(
    *,
    suite: str,
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
) -> Tuple[Dict[str, Any], bool]:
    """Execute the selected suite and persist its input fingerprint."""
    if suite not in {"offline", "online", "all"}:
        raise ValueError(f"Unknown evaluation suite: {suite}")
    input_fingerprint = evaluation_fingerprint(
        db_path=db_path,
        vectors_root=vectors_root,
        topics_file=topics_file,
        fixed_query_file=fixed_query_file,
    )

    offline_suite: Dict[str, Any] | None = None
    if suite in {"offline", "all"}:
        offline_suite = run_offline_suite(
            db_path=db_path,
            vectors_root=vectors_root,
            collection_name=collection_name,
            fixed_query_file=fixed_query_file,
            top_k=top_k,
        )

    if suite == "offline":
        summary = {
            "generated_at_utc": legacy.utc_now_iso(),
            "suite": suite,
            "overall_pass": bool(offline_suite and offline_suite.get("all_pass")),
            "input_fingerprint": input_fingerprint,
            "gate_policy": "offline_fixed_queries",
            "db_path": str(db_path),
            "vectors_root": str(vectors_root),
            "topics_file": str(topics_file),
            "fixed_query_file": str(fixed_query_file),
        }
        report = {"summary": summary, "offline_suite": offline_suite}
    else:
        report = legacy.run_m4(
            db_path=db_path,
            vectors_root=vectors_root,
            collection_name=collection_name,
            topics_file=topics_file,
            fixed_query_file=fixed_query_file,
            embed_base_url=embed_base_url,
            embed_model=embed_model,
            embed_api_key=embed_api_key,
            sample_topics=sample_topics,
            sample_per_topic=sample_per_topic,
            sample_seed=sample_seed,
            top_k=top_k,
            output_json=output_json,
            output_md=output_md,
            sampled_dump=sampled_dump,
        )
        summary = report.setdefault("summary", {})
        summary["suite"] = suite
        summary["input_fingerprint"] = input_fingerprint
        if offline_suite is not None:
            report["offline_suite"] = offline_suite
            summary["offline_suite_pass"] = bool(offline_suite.get("all_pass"))
            summary["overall_pass"] = bool(summary.get("overall_pass")) and bool(
                offline_suite.get("all_pass")
            )

    _write_json(output_json, report)
    _write_markdown(output_md, report)
    return report, bool(report.get("summary", {}).get("overall_pass"))


def status(
    *,
    report_path: Path,
    db_path: Path,
    vectors_root: Path,
    topics_file: Path,
    fixed_query_file: Path,
) -> Tuple[Dict[str, Any], bool]:
    """Return status while refusing to reuse a report for changed inputs."""
    if not report_path.exists():
        raise FileNotFoundError(f"Evaluation report not found: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not isinstance(report.get("summary"), dict):
        raise ValueError(f"Invalid evaluation report: {report_path}")
    summary = dict(report["summary"])
    current = evaluation_fingerprint(
        db_path=db_path,
        vectors_root=vectors_root,
        topics_file=topics_file,
        fixed_query_file=fixed_query_file,
    )
    recorded = summary.get("input_fingerprint")
    stale = not recorded or recorded != current
    result = {
        "report_path": str(report_path),
        "generated_at_utc": summary.get("generated_at_utc"),
        "suite": summary.get("suite", "legacy"),
        "reported_pass": bool(summary.get("overall_pass")),
        "stale": stale,
        "current_input_fingerprint": current,
        "recorded_input_fingerprint": recorded,
        "overall_pass": bool(summary.get("overall_pass")) and not stale,
    }
    return result, bool(result["overall_pass"])
