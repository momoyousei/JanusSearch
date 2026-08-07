#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline-first evaluation CLI with stale-report detection."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from pathlib import Path

import yaml

from janussearch.application.evaluation import execute, status
from janussearch.domain.run import ExitCode
from janussearch.infrastructure.manifests import RunManifest
from janussearch.infrastructure.service_config import (
    embed_base_url as configured_embed_base_url,
    embed_model as configured_embed_model,
)
from janussearch.application import evaluation_pipeline as legacy

LOGGER = logging.getLogger("janussearch.evaluate")
DEFAULT_OUTPUT_JSON = Path("artifacts/evaluate/eval_report.json")
DEFAULT_OUTPUT_MD = Path("artifacts/evaluate/eval_report.md")
DEFAULT_SAMPLED_DUMP = Path("artifacts/evaluate/sampled_queries.json")


def _add_common(parser: argparse.ArgumentParser) -> None:
    """Add shared evaluation input arguments."""
    parser.add_argument("--db-path", default=str(legacy.DEFAULT_DB_PATH))
    parser.add_argument("--vectors-root", default=str(legacy.DEFAULT_VECTORS_ROOT))
    parser.add_argument("--collection-name", default=legacy.DEFAULT_COLLECTION_NAME)
    parser.add_argument("--topics-file", default=str(legacy.DEFAULT_TOPICS_FILE))
    parser.add_argument("--fixed-query-file", default=str(legacy.DEFAULT_FIXED_QUERY_FILE))
    parser.add_argument("--output-json", default=str(DEFAULT_OUTPUT_JSON))


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the evaluation parser."""
    parser = argparse.ArgumentParser(description="Evaluate retrieval offline by default")
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Run an offline, online, or complete suite")
    _add_common(run)
    run.add_argument("--suite", choices=("offline", "online", "all"), default="offline")
    run.add_argument("--embed-base-url", default=configured_embed_base_url(legacy.DEFAULT_EMBED_BASE_URL))
    run.add_argument("--embed-model", default=configured_embed_model(legacy.DEFAULT_EMBED_MODEL))
    run.add_argument("--embed-api-key", default=os.getenv("JANUS_EMBED_API_KEY") or os.getenv("JANUS_LLM_API_KEY"))
    run.add_argument("--sample-topics", type=int, default=legacy.DEFAULT_SAMPLE_TOPICS)
    run.add_argument("--sample-per-topic", type=int, default=legacy.DEFAULT_SAMPLE_PER_TOPIC)
    run.add_argument("--sample-seed", type=int, default=legacy.DEFAULT_SAMPLE_SEED)
    run.add_argument("--top-k", type=int, default=legacy.DEFAULT_TOP_K)
    run.add_argument("--output-md", default=str(DEFAULT_OUTPUT_MD))
    run.add_argument("--sampled-dump", default=str(DEFAULT_SAMPLED_DUMP))

    status_parser = subparsers.add_parser("status", help="Reject failed or stale evaluation reports")
    _add_common(status_parser)
    return parser


def main() -> int:
    """Execute evaluation or read freshness-aware status."""
    args = build_arg_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    db_path = Path(args.db_path)
    vectors_root = Path(args.vectors_root)
    topics_file = Path(args.topics_file)
    fixed_query_file = Path(args.fixed_query_file)
    output_json = Path(args.output_json)
    try:
        if args.command == "status":
            payload, passed = status(
                report_path=output_json,
                db_path=db_path,
                vectors_root=vectors_root,
                topics_file=topics_file,
                fixed_query_file=fixed_query_file,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return int(ExitCode.SUCCESS if passed else ExitCode.OPERATION_FAILED)

        manifest = RunManifest(
            capability="evaluate",
            operation=args.suite,
            scope={"db_path": args.db_path, "suite": args.suite},
            config=vars(args),
        )
        manifest.write()
        report, passed = execute(
            suite=args.suite,
            db_path=db_path,
            vectors_root=vectors_root,
            collection_name=args.collection_name,
            topics_file=topics_file,
            fixed_query_file=fixed_query_file,
            embed_base_url=args.embed_base_url,
            embed_model=args.embed_model,
            embed_api_key=args.embed_api_key,
            sample_topics=args.sample_topics,
            sample_per_topic=args.sample_per_topic,
            sample_seed=args.sample_seed,
            top_k=args.top_k,
            output_json=output_json,
            output_md=Path(args.output_md),
            sampled_dump=Path(args.sampled_dump),
        )
        exit_code = ExitCode.SUCCESS if passed else ExitCode.OPERATION_FAILED
        manifest.add_step(args.suite, "passed" if passed else "failed", artifacts=[output_json, args.output_md])
        manifest_path = manifest.finish(exit_code=exit_code)
        print(json.dumps({"summary": report["summary"], "run_manifest": str(manifest_path)}, ensure_ascii=False, indent=2))
        return int(exit_code)
    except (
        FileNotFoundError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
        yaml.YAMLError,
    ) as exc:
        LOGGER.error("%s", exc)
        if "manifest" in locals():
            manifest.add_issue("evaluation_failed", str(exc))
            manifest.finish(exit_code=ExitCode.OPERATION_FAILED)
        return int(ExitCode.OPERATION_FAILED)


if __name__ == "__main__":
    raise SystemExit(main())
