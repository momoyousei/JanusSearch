#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capability CLI for staged corpus lifecycle operations."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from janussearch.application import corpus
from janussearch.application.catalog import execute as execute_catalog
from janussearch.domain.errors import ConfigurationError
from janussearch.domain.run import ExitCode
from janussearch.infrastructure.manifests import RunManifest
from janussearch.application.corpus_pipeline import (
    DEFAULT_ABSTRACT_THRESHOLD,
    DEFAULT_AUTHORS_THRESHOLD,
    PAPERS_COOL_DEFAULT_POLICY,
    PAPERS_COOL_POLICY_CHOICES,
)

LOGGER = logging.getLogger("janussearch.corpus")


def _add_scope(parser: argparse.ArgumentParser) -> None:
    """Add venue/year collection scope."""
    parser.add_argument("--venue", required=True, help="Venue code, for example ACL")
    parser.add_argument("--years", required=True, help="Year or inclusive range, for example 2021-2025")
    parser.add_argument("--output-root", default="", help="Snapshot root; default is run-scoped")


def _add_validation(parser: argparse.ArgumentParser) -> None:
    """Add stable quality-gate options."""
    parser.add_argument("--threshold-authors", type=float, default=DEFAULT_AUTHORS_THRESHOLD)
    parser.add_argument("--threshold-abstract", type=float, default=DEFAULT_ABSTRACT_THRESHOLD)
    parser.add_argument(
        "--strict-official-alignment",
        action="store_true",
        help="Promote official paper/track/presentation alignment warnings to hard failures",
    )


def _add_prepare(parser: argparse.ArgumentParser) -> None:
    """Add normalization and enrichment options."""
    parser.add_argument("--enrich", action="store_true", help="Run network-backed missing-field enrichment")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--max-records-per-file", type=int, default=0)
    parser.add_argument("--min-interval", type=float, default=3.0)
    parser.add_argument("--enable-arxiv-title", action="store_true")
    parser.add_argument("--enable-papers-cool", action="store_true")
    parser.add_argument(
        "--papers-cool-policy",
        default=PAPERS_COOL_DEFAULT_POLICY,
        choices=PAPERS_COOL_POLICY_CHOICES,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the corpus lifecycle parser."""
    parser = argparse.ArgumentParser(description="Plan, stage, validate, and publish corpus changes")
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect source records without mutation")
    inspect_parser.add_argument("--input-glob", default="archives/root_json/*.json")

    plan_parser = subparsers.add_parser("plan", help="Resolve a venue to its production collector")
    _add_scope(plan_parser)

    collect_parser = subparsers.add_parser("collect", help="Collect into an immutable run snapshot")
    _add_scope(collect_parser)

    prepare_parser = subparsers.add_parser("prepare", help="Normalize/enrich into isolated staging")
    prepare_parser.add_argument("--input-glob", default="archives/root_json/*.json")
    prepare_parser.add_argument("--staging-root", default="", help="Staging root; default is run-scoped")
    _add_prepare(prepare_parser)

    validate_parser = subparsers.add_parser("validate", help="Validate staged or canonical JSON")
    validate_parser.add_argument("--input-glob", default="data/raw/*/*.json")
    _add_validation(validate_parser)

    reconcile_parser = subparsers.add_parser(
        "reconcile", help="Preserve stable IDs and audit additions/removals before publish"
    )
    reconcile_parser.add_argument("--staging-root", required=True)
    reconcile_parser.add_argument("--output-root", required=True)
    reconcile_parser.add_argument("--canonical-root", default="data/raw")
    reconcile_parser.add_argument(
        "--policy",
        default="config/reconciliation/2026-venue-refresh.json",
        help="Versioned approved-removal and retitle policy",
    )

    publish_parser = subparsers.add_parser("publish", help="Validate then publish staged canonical JSON")
    publish_parser.add_argument("--staging-root", required=True)
    publish_parser.add_argument("--canonical-root", default="data/raw")
    _add_validation(publish_parser)

    add_parser = subparsers.add_parser("add", help="Collect, stage, validate, publish, and rebuild catalog")
    _add_scope(add_parser)
    _add_prepare(add_parser)
    _add_validation(add_parser)
    add_parser.add_argument("--canonical-root", default="data/raw")
    add_parser.add_argument("--db-path", default="data/papers.db")
    add_parser.add_argument("--build-projections", action="store_true")
    return parser


def _manifest(args: argparse.Namespace) -> RunManifest:
    """Create a manifest with safe generic scope."""
    return RunManifest(
        capability="corpus",
        operation=args.command,
        scope={
            key: value
            for key, value in vars(args).items()
            if key in {"venue", "years", "input_glob", "staging_root", "canonical_root"}
        },
        config=vars(args),
    )


def _prepare_kwargs(args: argparse.Namespace, *, input_glob: str, staging_root: Path, reports_root: Path) -> dict[str, Any]:
    """Map parsed options into the application prepare contract."""
    return {
        "input_glob": input_glob,
        "staging_root": staging_root,
        "reports_root": reports_root,
        "enrich": bool(args.enrich),
        "timeout": args.timeout,
        "retries": args.retries,
        "max_records_per_file": args.max_records_per_file,
        "min_interval": args.min_interval,
        "enable_arxiv_title": bool(args.enable_arxiv_title),
        "enable_papers_cool": bool(args.enable_papers_cool),
        "papers_cool_policy": args.papers_cool_policy,
    }


def _record_alignment_warnings(
    manifest: RunManifest,
    validation: dict[str, Any],
    *,
    strict: bool,
) -> int:
    """Persist warning-only official alignment issues in the run manifest."""
    warning_count = int(validation.get("summary", {}).get("alignment_fail_files", 0))
    if warning_count and not strict:
        manifest.add_issue(
            "official_alignment_warning",
            f"{warning_count} files are not aligned with official counts",
            severity="warning",
        )
    return warning_count


def _compact_reconciliation_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep per-record mappings on disk without flooding the terminal JSON."""
    compact = dict(result)
    compact_files: list[dict[str, Any]] = []
    for item in result.get("files", []):
        compact_item = dict(item)
        mappings = compact_item.pop("mappings", [])
        compact_item["mapping_record_count"] = len(mappings)
        compact_files.append(compact_item)
    compact["files"] = compact_files
    return compact


def main() -> int:
    """Execute one staged corpus operation."""
    args = build_arg_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    manifest = _manifest(args)
    manifest.write()
    run_root = manifest.path.parent
    reports_root = run_root / "reports"
    try:
        if args.command == "inspect":
            result = corpus.inspect(args.input_glob, reports_root / "inventory.json")
            manifest.add_step("inspect", "passed", metrics=result.get("summary"), artifacts=[reports_root / "inventory.json"])

        elif args.command == "plan":
            output_root = Path(args.output_root) if args.output_root else run_root / "collected"
            result = corpus.plan(args.venue, args.years, output_root)
            manifest.add_step("plan", "passed")

        elif args.command == "collect":
            output_root = Path(args.output_root) if args.output_root else run_root / "collected"
            result = corpus.collect(args.venue, args.years, output_root)
            step_status = "skipped" if result["outcome"] == "no_update" else "passed"
            manifest.add_step(
                "collect",
                step_status,
                metrics={"files": len(result["files"]), "outcome": result["outcome"]},
                artifacts=[*result["files"], output_root / ".janus-collection.json"],
            )

        elif args.command == "reconcile":
            reconciliation_result = corpus.reconcile(
                staging_root=Path(args.staging_root),
                canonical_root=Path(args.canonical_root),
                output_root=Path(args.output_root),
                policy_path=Path(args.policy) if args.policy else None,
            )
            manifest.add_step(
                "reconcile",
                "skipped" if reconciliation_result["outcome"] == "no_update" else "passed",
                metrics={
                    "outcome": reconciliation_result["outcome"],
                    "files": len(reconciliation_result["files"]),
                    "added": sum(item["added_count"] for item in reconciliation_result["files"]),
                    "removed": sum(item["removed_count"] for item in reconciliation_result["files"]),
                    "changed": sum(item["changed_count"] for item in reconciliation_result["files"]),
                },
                artifacts=[reconciliation_result["report_path"], reconciliation_result["output_root"]],
            )
            result = _compact_reconciliation_result(reconciliation_result)

        elif args.command == "prepare":
            staging_root = Path(args.staging_root) if args.staging_root else run_root / "staging"
            result = corpus.prepare(**_prepare_kwargs(args, input_glob=args.input_glob, staging_root=staging_root, reports_root=reports_root))
            manifest.add_step("prepare", "passed", artifacts=[staging_root, reports_root])

        elif args.command == "validate":
            result, passed = corpus.validate(
                input_glob=args.input_glob,
                reports_root=reports_root,
                threshold_authors=args.threshold_authors,
                threshold_abstract=args.threshold_abstract,
                strict_official_alignment=args.strict_official_alignment,
            )
            warnings = _record_alignment_warnings(
                manifest,
                result,
                strict=args.strict_official_alignment,
            )
            manifest.add_step("validate", "passed" if passed else "failed", metrics=result.get("summary"), artifacts=[reports_root])
            exit_code = ExitCode.SUCCESS if passed else ExitCode.OPERATION_FAILED
            manifest_path = manifest.finish(exit_code=exit_code, warnings=bool(warnings and passed))
            print(json.dumps({"result": result, "run_manifest": str(manifest_path)}, ensure_ascii=False, indent=2))
            return int(exit_code)

        elif args.command == "publish":
            staging_root = Path(args.staging_root)
            validation, passed = corpus.validate(
                input_glob=str(staging_root / "*/*.json"),
                reports_root=reports_root,
                threshold_authors=args.threshold_authors,
                threshold_abstract=args.threshold_abstract,
                strict_official_alignment=args.strict_official_alignment,
            )
            manifest.add_step("validate", "passed" if passed else "failed", metrics=validation.get("summary"))
            if not passed:
                raise RuntimeError("Staging validation failed; canonical corpus was not changed")
            _record_alignment_warnings(
                manifest,
                validation,
                strict=args.strict_official_alignment,
            )
            result = corpus.publish(
                staging_root,
                Path(args.canonical_root),
                require_reconciliation=True,
            )
            manifest.add_step("publish", "passed", metrics={"published_count": result["published_count"]}, artifacts=result["published_files"])

        elif args.command == "add":
            collection_root = Path(args.output_root) if args.output_root else run_root / "collected"
            staging_root = run_root / "staging"
            collected = corpus.collect(args.venue, args.years, collection_root)
            collect_status = "skipped" if collected["outcome"] == "no_update" else "passed"
            manifest.add_step(
                "collect",
                collect_status,
                metrics={"files": len(collected["files"]), "outcome": collected["outcome"]},
                artifacts=[*collected["files"], collection_root / ".janus-collection.json"],
            )
            if collected["outcome"] == "no_update":
                result = {"collection": collected, "publication": None, "catalog": None}
                manifest_path = manifest.finish(exit_code=ExitCode.SUCCESS, warnings=True)
                print(json.dumps({"result": result, "run_manifest": str(manifest_path)}, ensure_ascii=False, indent=2))
                return int(ExitCode.SUCCESS)
            prepared = corpus.prepare(**_prepare_kwargs(args, input_glob=str(collection_root / "[!.]*.json"), staging_root=staging_root, reports_root=reports_root))
            manifest.add_step("prepare", "passed", metrics=prepared["normalize"].get("summary"), artifacts=[staging_root])
            reconciled_root = run_root / "reconciled"
            reconciled = corpus.reconcile(
                staging_root=staging_root,
                canonical_root=Path(args.canonical_root),
                output_root=reconciled_root,
                policy_path=Path("config/reconciliation/2026-venue-refresh.json"),
            )
            manifest.add_step(
                "reconcile",
                "skipped" if reconciled["outcome"] == "no_update" else "passed",
                metrics={"outcome": reconciled["outcome"]},
                artifacts=[reconciled["report_path"]],
            )
            if reconciled["outcome"] == "no_update":
                result = {"collection": collected, "reconciliation": reconciled, "publication": None, "catalog": None}
                manifest_path = manifest.finish(exit_code=ExitCode.SUCCESS, warnings=True)
                print(json.dumps({"result": result, "run_manifest": str(manifest_path)}, ensure_ascii=False, indent=2))
                return int(ExitCode.SUCCESS)
            validation, passed = corpus.validate(
                input_glob=str(reconciled_root / "*/*.json"),
                reports_root=reports_root,
                threshold_authors=args.threshold_authors,
                threshold_abstract=args.threshold_abstract,
                strict_official_alignment=args.strict_official_alignment,
            )
            manifest.add_step("validate", "passed" if passed else "failed", metrics=validation.get("summary"))
            if not passed:
                raise RuntimeError("Staging validation failed; canonical corpus was not changed")
            _record_alignment_warnings(
                manifest,
                validation,
                strict=args.strict_official_alignment,
            )
            published = corpus.publish(
                reconciled_root,
                Path(args.canonical_root),
                require_reconciliation=True,
            )
            manifest.add_step("publish", "passed", metrics={"published_count": published["published_count"]}, artifacts=published["published_files"])
            _catalog_build, _ = execute_catalog("build", input_root=Path(args.canonical_root), db_path=Path(args.db_path), index_root=Path("artifacts"))
            catalog_validation, catalog_passed = execute_catalog("validate", input_root=Path(args.canonical_root), db_path=Path(args.db_path), index_root=Path("artifacts"))
            manifest.add_step("catalog", "passed" if catalog_passed else "failed", metrics=catalog_validation.get("summary"))
            if not catalog_passed:
                raise RuntimeError("Published corpus but catalog validation failed")
            projection_result = None
            if args.build_projections:
                completed = subprocess.run([sys.executable, "-m", "tools.projections", "run"], check=False)
                if completed.returncode != 0:
                    raise RuntimeError(f"Projection build failed with exit code {completed.returncode}")
                manifest.add_step("projections", "passed")
                projection_result = {"exit_code": completed.returncode}
            result = {
                "collection": collected,
                "validation": validation,
                "publication": published,
                "catalog": catalog_validation,
                "projections": projection_result,
            }
        else:
            raise ValueError(f"Unknown corpus operation: {args.command}")

        warning_count = sum(issue.get("severity") == "warning" for issue in manifest.issues)
        manifest_path = manifest.finish(exit_code=ExitCode.SUCCESS, warnings=bool(warning_count))
        print(json.dumps({"result": result, "run_manifest": str(manifest_path)}, ensure_ascii=False, indent=2))
        return int(ExitCode.SUCCESS)
    except ConfigurationError as exc:
        LOGGER.error("%s", exc)
        manifest.add_issue("invalid_configuration", str(exc))
        manifest.finish(exit_code=ExitCode.USAGE_ERROR)
        return int(ExitCode.USAGE_ERROR)
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("%s", exc)
        manifest.add_issue("corpus_operation_failed", str(exc))
        manifest.finish(exit_code=ExitCode.OPERATION_FAILED)
        return int(ExitCode.OPERATION_FAILED)


if __name__ == "__main__":
    raise SystemExit(main())
