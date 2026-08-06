#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Staged corpus collection, preparation, validation, and publication."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from janussearch.collectors.registry import get_collector, parse_years
from tools import m1_pipeline as legacy


def inspect(input_glob: str, report_path: Path) -> Dict[str, Any]:
    """Inspect historical or staged inputs without modifying them."""
    return legacy.run_inventory(input_glob=input_glob, report_path=report_path)


def plan(venue: str, years: str, output_root: Path) -> Dict[str, Any]:
    """Return a deterministic collection plan for a venue and year scope."""
    normalized_venue = venue.strip().upper()
    parsed_years = parse_years(years)
    spec = get_collector(normalized_venue)
    commands: List[List[str]] = []
    if spec.mode == "target":
        for year in parsed_years:
            commands.append(spec.command(venue=normalized_venue, years=str(year), output_root=output_root))
    else:
        commands.append(spec.command(venue=normalized_venue, years=years, output_root=output_root))
    return {
        "venue": normalized_venue,
        "years": parsed_years,
        "provider": spec.provider,
        "collector_module": spec.module,
        "supports_abstracts": spec.supports_abstracts,
        "output_root": str(output_root),
        "commands": [[sys.executable, *command] for command in commands],
    }


def collect(venue: str, years: str, output_root: Path) -> Dict[str, Any]:
    """Execute the registered collector into a run-scoped immutable snapshot."""
    collection_plan = plan(venue=venue, years=years, output_root=output_root)
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"Collection snapshot is not empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    executions: List[Dict[str, Any]] = []
    for command in collection_plan["commands"]:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        execution = {
            "command": command,
            "exit_code": result.returncode,
            "stdout_tail": result.stdout[-4000:],
            "stderr_tail": result.stderr[-4000:],
        }
        executions.append(execution)
        if result.returncode != 0:
            raise RuntimeError(
                f"Collector failed with exit code {result.returncode}: {result.stderr[-1000:]}"
            )
    files = sorted(str(path) for path in output_root.rglob("*.json"))
    return {**collection_plan, "executions": executions, "files": files}


def prepare(
    *,
    input_glob: str,
    staging_root: Path,
    reports_root: Path,
    enrich: bool,
    timeout: float,
    retries: int,
    max_records_per_file: int,
    min_interval: float,
    enable_arxiv_title: bool,
    enable_papers_cool: bool,
    papers_cool_policy: str,
) -> Dict[str, Any]:
    """Normalize and optionally enrich inputs into an isolated staging tree."""
    if staging_root.exists() and any(staging_root.rglob("*.json")):
        raise FileExistsError(f"Staging root already contains JSON: {staging_root}")
    normalize_report = reports_root / "normalize_report.json"
    backfill_report = reports_root / "backfill_report.json"
    normalized = legacy.run_normalize(
        input_glob=input_glob,
        canonical_root=staging_root,
        backup_root=reports_root / "unused_backups",
        report_path=normalize_report,
        write_back=False,
    )
    result: Dict[str, Any] = {
        "staging_root": str(staging_root),
        "normalize": normalized,
        "backfill": None,
    }
    if enrich:
        result["backfill"] = legacy.run_backfill(
            input_glob=input_glob,
            canonical_root=staging_root,
            backup_root=reports_root / "unused_backups",
            report_path=backfill_report,
            write_back=False,
            timeout=timeout,
            retries=retries,
            max_records_per_file=max_records_per_file,
            min_interval=min_interval,
            enable_arxiv_title=enable_arxiv_title,
            enable_papers_cool=enable_papers_cool,
            papers_cool_policy=papers_cool_policy,
        )
    metadata_path = staging_root / ".janus-corpus.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps({"schema_version": 1, "source_input_glob": input_glob}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    result["prepare_metadata"] = str(metadata_path)
    return result


def validate(
    *,
    input_glob: str,
    reports_root: Path,
    threshold_authors: float,
    threshold_abstract: float,
    strict_official_alignment: bool,
) -> Tuple[Dict[str, Any], bool]:
    """Run hard quality gates plus warning-only or strict official alignment."""
    source_input_glob: str | None = None
    files = legacy.find_input_files(input_glob)
    for ancestor in files[0].parents:
        metadata_path = ancestor / ".janus-corpus.json"
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            source_input_glob = str(metadata.get("source_input_glob") or "") or None
            break

    report, hard_pass = legacy.run_validate(
        input_glob=input_glob,
        report_path=reports_root / "quality_report.json",
        stats_md_path=reports_root / "stats.md",
        threshold_authors=threshold_authors,
        threshold_abstract=threshold_abstract,
        enforce_official_alignment=(strict_official_alignment and not source_input_glob),
    )

    official_report: Dict[str, Any] | None = None
    official_pass = True
    if source_input_glob:
        official_report, official_pass = legacy.run_validate(
            input_glob=source_input_glob,
            report_path=reports_root / "official_alignment_report.json",
            stats_md_path=reports_root / "official_alignment_stats.md",
            threshold_authors=0.0,
            threshold_abstract=0.0,
            enforce_official_alignment=strict_official_alignment,
        )
        official_summary = official_report.get("summary", {})
        summary = report.setdefault("summary", {})
        for key in (
            "alignment_pass_files",
            "alignment_fail_files",
            "alignment_warning_files",
            "enforce_official_alignment",
        ):
            summary[key] = official_summary.get(key, 0)
        report["official_alignment"] = {
            "source_input_glob": source_input_glob,
            "strict": strict_official_alignment,
            "summary": official_summary,
            "files": official_report.get("files", []),
        }
    all_pass = hard_pass and (official_pass if strict_official_alignment else True)
    report["summary"]["all_pass"] = all_pass
    legacy.write_json(reports_root / "quality_report.json", report)
    return report, all_pass


def _staged_files(staging_root: Path) -> Iterable[Path]:
    """Yield canonical-shaped staged files."""
    return sorted(path for path in staging_root.glob("*/*.json") if path.is_file())


def publish(staging_root: Path, canonical_root: Path) -> Dict[str, Any]:
    """Publish a validated staging tree with rollback on replacement failure."""
    sources = list(_staged_files(staging_root))
    if not sources:
        raise FileNotFoundError(f"No staged canonical JSON under {staging_root}")

    prepared: List[tuple[Path, Path, Path | None]] = []
    for source in sources:
        relative = source.relative_to(staging_root)
        target = canonical_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.publish.tmp")
        backup = target.with_name(f".{target.name}.publish.bak") if target.exists() else None
        shutil.copy2(source, temporary)
        json.loads(temporary.read_text(encoding="utf-8"))
        prepared.append((target, temporary, backup))

    published: List[str] = []
    try:
        for target, temporary, backup in prepared:
            if backup is not None:
                shutil.copy2(target, backup)
            temporary.replace(target)
            published.append(str(target))
    except Exception:
        for target, _temporary, backup in reversed(prepared):
            if backup is not None and backup.exists():
                backup.replace(target)
            elif str(target) in published and target.exists():
                target.unlink()
        raise
    finally:
        for _target, temporary, backup in prepared:
            temporary.unlink(missing_ok=True)
            if backup is not None:
                backup.unlink(missing_ok=True)

    return {"published_files": published, "published_count": len(published)}
