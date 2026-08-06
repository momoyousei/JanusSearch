#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only environment and artifact diagnostics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from janussearch.application.doctor import execute
from janussearch.domain.run import ExitCode


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the doctor parser."""
    parser = argparse.ArgumentParser(description="Diagnose JanusSearch without repairing state")
    parser.add_argument("--profile", choices=("query", "corpus", "ops"), required=True)
    parser.add_argument("--db-path", default="data/papers.db")
    parser.add_argument("--vectors-root", default="data/vectors/chroma")
    parser.add_argument("--collection-name", default="papers_v1")
    parser.add_argument("--raw-root", default="data/raw")
    parser.add_argument("--archives-root", default="archives/root_json")
    parser.add_argument("--evaluation-report", default="artifacts/evaluate/eval_report.json")
    parser.add_argument("--topics-file", default="artifacts/m3/topic_assignments.json")
    parser.add_argument("--fixed-query-file", default="docs/fixtures/m4_fixed_queries.yaml")
    return parser


def main() -> int:
    """Run a read-only diagnostic profile."""
    args = build_arg_parser().parse_args()
    report, passed = execute(
        profile=args.profile,
        db_path=Path(args.db_path),
        vectors_root=Path(args.vectors_root),
        collection_name=args.collection_name,
        raw_root=Path(args.raw_root),
        archives_root=Path(args.archives_root),
        evaluation_report=Path(args.evaluation_report),
        topics_file=Path(args.topics_file),
        fixed_query_file=Path(args.fixed_query_file),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(ExitCode.SUCCESS if passed else ExitCode.OPERATION_FAILED)


if __name__ == "__main__":
    raise SystemExit(main())

