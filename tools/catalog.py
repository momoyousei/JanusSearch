#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capability CLI for the local SQLite paper catalog."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from janussearch.application.catalog import execute
from janussearch.domain.run import ExitCode
from janussearch.infrastructure.manifests import RunManifest

LOGGER = logging.getLogger("janussearch.catalog")


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the catalog CLI parser."""
    parser = argparse.ArgumentParser(description="Build, validate, and inspect the SQLite catalog")
    parser.add_argument("--input-root", default="data/raw", help="Canonical JSON root")
    parser.add_argument("--db-path", default="data/papers.db", help="SQLite catalog path")
    parser.add_argument("--index-root", default="artifacts", help="Report root")
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("build", "Atomically build and publish the SQLite catalog"),
        ("validate", "Validate the catalog against canonical JSON"),
        ("reindex-fts", "Rebuild the FTS index"),
        ("stats", "Show catalog statistics"),
    ):
        subparsers.add_parser(command, help=help_text)
    return parser


def main() -> int:
    """Run a catalog operation with an audit manifest."""
    args = build_arg_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    manifest = RunManifest(
        capability="catalog",
        operation=args.command,
        scope={"input_root": args.input_root, "db_path": args.db_path},
        config=vars(args),
    )
    manifest.write()
    try:
        payload, passed = execute(
            args.command,
            input_root=Path(args.input_root),
            db_path=Path(args.db_path),
            index_root=Path(args.index_root),
        )
        exit_code = ExitCode.SUCCESS if passed else ExitCode.OPERATION_FAILED
        manifest.add_step(args.command, "passed" if passed else "failed")
        manifest_path = manifest.finish(exit_code=exit_code)
        print(json.dumps({"result": payload, "run_manifest": str(manifest_path)}, ensure_ascii=False, indent=2))
        return int(exit_code)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        LOGGER.error("%s", exc)
        manifest.add_issue("catalog_operation_failed", str(exc))
        manifest.finish(exit_code=ExitCode.OPERATION_FAILED)
        return int(ExitCode.OPERATION_FAILED)


if __name__ == "__main__":
    raise SystemExit(main())

