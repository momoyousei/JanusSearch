#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Staged corpus collection, preparation, validation, and publication."""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import subprocess
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from janussearch.collectors.outcomes import COLLECTION_RESULT_NAME, read_collection_result
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
    collection_result = read_collection_result(output_root)
    files = sorted(
        str(path)
        for path in output_root.rglob("*.json")
        if path.name != COLLECTION_RESULT_NAME and not path.name.startswith(".")
    )
    if collection_result is None:
        if not files:
            raise RuntimeError("Collector produced neither paper JSON nor a collection result")
        collection_result = {
            "schema_version": 1,
            "outcome": "collected",
            "reason": "legacy_collector_with_nonempty_output",
        }
    outcome = str(collection_result["outcome"])
    if outcome == "collected" and not files:
        raise RuntimeError("Collector declared collected but produced no paper JSON")
    if outcome == "no_update" and files:
        raise RuntimeError("Collector declared no_update but produced paper JSON")
    if outcome == "incomplete_source":
        raise RuntimeError(f"Collector source incomplete: {collection_result.get('reason')}")
    return {
        **collection_plan,
        "executions": executions,
        "files": files,
        "outcome": outcome,
        "collection_result": collection_result,
    }


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


RECONCILIATION_NAME = ".janus-reconciliation.json"
RUN_NOISE_FIELDS = {"collected_at", "generated_at_utc", "normalized_at_utc"}


def _sha256(path: Path) -> str:
    """Return a full content hash."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized_title(value: Any) -> str:
    """Normalize a title for deterministic matching."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(character for character in text.casefold() if character.isalnum())


def _normalized_author(value: Any) -> str:
    """Normalize one author name across accents and punctuation."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(character for character in text.casefold() if character.isalnum())


def _author_signature(record: dict[str, Any]) -> tuple[str, ...]:
    values = record.get("authors")
    if not isinstance(values, list):
        return ()
    return tuple(sorted(value for value in (_normalized_author(item) for item in values) if value))


def _stable_ids(record: dict[str, Any]) -> set[str]:
    """Extract stable DOI/OpenReview/source identifiers, excluding run-local event ids."""
    values: set[str] = set()
    paper_id = str(record.get("paper_id") or "").strip()
    if paper_id:
        values.add(f"paper_id:{paper_id}")
    doi = str(record.get("doi") or "").strip().casefold()
    if doi:
        values.add(f"doi:{doi}")
    openreview_id = str(record.get("openreview_id") or "").strip()
    if openreview_id:
        values.add(f"openreview:{openreview_id}")
    source_ids = record.get("source_ids")
    if isinstance(source_ids, dict):
        for key, value in source_ids.items():
            normalized_key = str(key).casefold()
            normalized_value = str(value or "").strip()
            if not normalized_value:
                continue
            if normalized_key in {"doi", "openreview_id", "arxiv_id", "dblp_key"}:
                values.add(f"{normalized_key}:{normalized_value.casefold()}")
    return values


def _unique_index(records: list[dict[str, Any]], key_builder: Any) -> dict[Any, int]:
    """Build an index containing unique keys only."""
    candidates: dict[Any, list[int]] = {}
    for index, record in enumerate(records):
        keys = key_builder(record)
        if not isinstance(keys, (set, list, tuple)):
            keys = [keys]
        for key in keys:
            if key:
                candidates.setdefault(key, []).append(index)
    return {key: indices[0] for key, indices in candidates.items() if len(indices) == 1}


def _author_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_names = list(_author_signature(left))
    right_names = list(_author_signature(right))
    if not left_names or not right_names:
        return 0.0
    available = set(range(len(right_names)))
    matched = 0.0
    for left_name in left_names:
        scored = sorted(
            (
                (SequenceMatcher(None, left_name, right_names[index]).ratio(), index)
                for index in available
            ),
            reverse=True,
        )
        if scored and scored[0][0] >= 0.72:
            matched += scored[0][0]
            available.remove(scored[0][1])
    return matched / max(len(left_names), len(right_names))


def _fuzzy_score(left: dict[str, Any], right: dict[str, Any]) -> tuple[float, float, float]:
    title_score = SequenceMatcher(
        None,
        _normalized_title(left.get("title")),
        _normalized_title(right.get("title")),
    ).ratio()
    author_score = _author_similarity(left, right)
    return (0.42 * title_score + 0.58 * author_score, title_score, author_score)


def _strip_noise(value: Any) -> Any:
    """Remove run timestamps before material-change comparison."""
    if isinstance(value, dict):
        return {
            key: _strip_noise(item)
            for key, item in value.items()
            if key not in RUN_NOISE_FIELDS
        }
    if isinstance(value, list):
        return [_strip_noise(item) for item in value]
    return value


def _merge_existing_completeness(
    old_record: dict[str, Any], new_record: dict[str, Any]
) -> None:
    """Keep higher-quality canonical fields while accepting official live changes."""
    old_authors = old_record.get("authors")
    new_authors = new_record.get("authors")
    if (
        isinstance(old_authors, list)
        and isinstance(new_authors, list)
        and len(old_authors) == len(new_authors)
        and _author_similarity(old_record, new_record) >= 0.9
    ):
        new_record["authors"] = old_authors

    old_abstract = str(old_record.get("abstract") or "")
    new_abstract = str(new_record.get("abstract") or "")
    if old_abstract and re.sub(r"\s+", " ", old_abstract).strip() == re.sub(
        r"\s+", " ", new_abstract
    ).strip():
        new_record["abstract"] = old_abstract

    for list_field in ("keywords", "institutions"):
        if not new_record.get(list_field) and old_record.get(list_field):
            new_record[list_field] = old_record[list_field]

    for scalar_field in (
        "doi",
        "openalex_id",
        "semantic_scholar_paper_id",
        "citation_count",
    ):
        if new_record.get(scalar_field) in (None, "") and old_record.get(scalar_field) not in (None, ""):
            new_record[scalar_field] = old_record[scalar_field]

    old_source_ids = old_record.get("source_ids")
    new_source_ids = new_record.get("source_ids")
    if isinstance(old_source_ids, dict):
        merged_source_ids = dict(old_source_ids)
        if isinstance(new_source_ids, dict):
            merged_source_ids.update(new_source_ids)
        new_record["source_ids"] = merged_source_ids

    old_provenance = old_record.get("field_provenance")
    new_provenance = new_record.get("field_provenance")
    if isinstance(old_provenance, dict):
        merged_provenance = dict(old_provenance)
        if isinstance(new_provenance, dict):
            merged_provenance.update(new_provenance)
        new_record["field_provenance"] = merged_provenance
    new_record["field_provenance"] = legacy.normalize_field_provenance(new_record)
    new_record["quality_flags"] = legacy.normalize_quality_flags(new_record)


def _load_policy(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"schema_version": 1, "targets": {}}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("targets"), dict):
        raise ValueError(f"Invalid reconciliation policy: {path}")
    return payload


def reconcile(
    *,
    staging_root: Path,
    canonical_root: Path,
    output_root: Path,
    policy_path: Path | None = None,
) -> Dict[str, Any]:
    """Reconcile staged records with canonical IDs and block unknown deletions."""
    sources = list(_staged_files(staging_root))
    if not sources:
        raise FileNotFoundError(f"No staged canonical JSON under {staging_root}")
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"Reconciliation output is not empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    policy = _load_policy(policy_path)
    file_reports: list[dict[str, Any]] = []
    overall_outcome = "no_update"

    metadata = staging_root / ".janus-corpus.json"
    if metadata.is_file():
        shutil.copy2(metadata, output_root / metadata.name)

    for source in sources:
        relative = source.relative_to(staging_root)
        target = canonical_root / relative
        destination = output_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged_payload = json.loads(source.read_text(encoding="utf-8"))
        new_records = staged_payload.get("papers")
        if not isinstance(new_records, list):
            raise ValueError(f"Staged payload has no papers list: {source}")
        old_payload = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else None
        old_records = old_payload.get("papers", []) if isinstance(old_payload, dict) else []
        if not isinstance(old_records, list):
            raise ValueError(f"Canonical payload has no papers list: {target}")

        matches: dict[int, tuple[int, str, float | None]] = {}
        used_old: set[int] = set()
        old_id_index = _unique_index(old_records, _stable_ids)
        for new_index, record in enumerate(new_records):
            candidates = {old_id_index[key] for key in _stable_ids(record) if key in old_id_index}
            candidates -= used_old
            if len(candidates) > 1:
                raise RuntimeError(f"Ambiguous stable-id mapping in {relative}: {record.get('title')}")
            if candidates:
                old_index = candidates.pop()
                matches[new_index] = (old_index, "stable_id", None)
                used_old.add(old_index)

        old_title_index = _unique_index(old_records, lambda item: _normalized_title(item.get("title")))
        for new_index, record in enumerate(new_records):
            if new_index in matches:
                continue
            old_index = old_title_index.get(_normalized_title(record.get("title")))
            if old_index is not None and old_index not in used_old:
                matches[new_index] = (old_index, "normalized_title", None)
                used_old.add(old_index)

        target_policy = policy["targets"].get(relative.as_posix(), {})
        fuzzy_enabled = bool(target_policy.get("allow_fuzzy_retitle"))
        if fuzzy_enabled:
            old_author_index = _unique_index(old_records, _author_signature)
            for new_index, record in enumerate(new_records):
                if new_index in matches:
                    continue
                signature = _author_signature(record)
                old_index = old_author_index.get(signature)
                if signature and old_index is not None and old_index not in used_old:
                    matches[new_index] = (old_index, "exact_author_set", 1.0)
                    used_old.add(old_index)

            scored_candidates: list[tuple[float, float, float, int, int]] = []
            for new_index, new_record in enumerate(new_records):
                if new_index in matches:
                    continue
                for old_index, old_record in enumerate(old_records):
                    if old_index in used_old:
                        continue
                    score, title_score, author_score = _fuzzy_score(new_record, old_record)
                    if score >= 0.63 and (author_score >= 0.50 or title_score >= 0.84):
                        scored_candidates.append((score, title_score, author_score, new_index, old_index))
            for score, title_score, author_score, new_index, old_index in sorted(scored_candidates, reverse=True):
                if new_index in matches or old_index in used_old:
                    continue
                matches[new_index] = (old_index, "fuzzy_retitle", round(score, 6))
                used_old.add(old_index)

        mapping_records: list[dict[str, Any]] = []
        for new_index, (old_index, method, score) in sorted(matches.items()):
            old_record = old_records[old_index]
            new_record = new_records[new_index]
            _merge_existing_completeness(old_record, new_record)
            old_paper_id = str(old_record.get("paper_id") or "")
            if not old_paper_id:
                raise RuntimeError(f"Canonical record lacks paper_id: {target}")
            new_record["paper_id"] = old_paper_id
            mapping_records.append(
                {
                    "paper_id": old_paper_id,
                    "old_title": old_record.get("title"),
                    "new_title": new_record.get("title"),
                    "method": method,
                    "score": score,
                    "changed": _strip_noise(old_record) != _strip_noise(new_record),
                }
            )

        unmatched_old = [index for index in range(len(old_records)) if index not in used_old]
        removed_ids = {str(old_records[index].get("paper_id") or "") for index in unmatched_old}
        allowed_removals = set(target_policy.get("allowed_removals", []))
        unknown_removals = sorted(removed_ids - allowed_removals)
        if unknown_removals:
            raise RuntimeError(
                f"Unknown removals blocked for {relative}: count={len(unknown_removals)} ids={unknown_removals[:8]}"
            )
        unmatched_new = [index for index in range(len(new_records)) if index not in matches]
        fuzzy_count = sum(item["method"] in {"exact_author_set", "fuzzy_retitle"} for item in mapping_records)
        retitle_count = sum(
            item["method"] in {"normalized_title", "exact_author_set", "fuzzy_retitle"}
            for item in mapping_records
        )
        minimum_fuzzy = int(target_policy.get("minimum_fuzzy_retitle_matches", 0))
        if fuzzy_count < minimum_fuzzy:
            raise RuntimeError(
                f"Fuzzy retitle reconciliation regressed for {relative}: {fuzzy_count} < {minimum_fuzzy}"
            )
        expected_retitle = target_policy.get("expected_retitle_matches")
        if expected_retitle is not None and retitle_count != int(expected_retitle):
            raise RuntimeError(
                f"Retitle reconciliation changed for {relative}: "
                f"{retitle_count} != {int(expected_retitle)}"
            )
        changed_count = sum(bool(item["changed"]) for item in mapping_records)
        file_outcome = (
            "no_update"
            if not unmatched_old and not unmatched_new and changed_count == 0
            else "updated"
        )
        if file_outcome == "updated":
            overall_outcome = "updated"
        destination.write_text(
            json.dumps(staged_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        file_reports.append(
            {
                "relative_path": relative.as_posix(),
                "outcome": file_outcome,
                "before_count": len(old_records),
                "after_count": len(new_records),
                "matched_count": len(matches),
                "changed_count": changed_count,
                "added_count": len(unmatched_new),
                "added_titles": [new_records[index].get("title") for index in unmatched_new],
                "removed_count": len(unmatched_old),
                "approved_removed_ids": sorted(removed_ids),
                "unknown_removed_ids": unknown_removals,
                "mapping_method_counts": {
                    method: sum(item["method"] == method for item in mapping_records)
                    for method in sorted({item["method"] for item in mapping_records})
                },
                "retitle_match_count": retitle_count,
                "mappings": mapping_records,
                "source_sha256": _sha256(source),
                "canonical_sha256": _sha256(target) if target.is_file() else None,
                "output_sha256": _sha256(destination),
            }
        )

    report = {
        "schema_version": 1,
        "outcome": overall_outcome,
        "policy_path": str(policy_path) if policy_path else None,
        "files": file_reports,
    }
    report_path = output_root / RECONCILIATION_NAME
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {**report, "report_path": str(report_path), "output_root": str(output_root)}


def _verify_reconciliation(staging_root: Path, canonical_root: Path) -> dict[str, Any]:
    """Recheck reconciliation hashes immediately before publication."""
    report_path = staging_root / RECONCILIATION_NAME
    if not report_path.is_file():
        raise RuntimeError(f"Missing reconciliation report: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("schema_version") != 1:
        raise RuntimeError(f"Unsupported reconciliation report: {report_path}")
    for item in report.get("files", []):
        relative = Path(item["relative_path"])
        source = staging_root / relative
        target = canonical_root / relative
        if _sha256(source) != item.get("output_sha256"):
            raise RuntimeError(f"Reconciled staging hash changed: {source}")
        expected_canonical = item.get("canonical_sha256")
        current_canonical = _sha256(target) if target.is_file() else None
        if current_canonical != expected_canonical:
            raise RuntimeError(f"Canonical changed since reconciliation: {target}")
        if item.get("unknown_removed_ids"):
            raise RuntimeError(f"Reconciliation contains unknown removals: {relative}")
    return report


def publish(
    staging_root: Path,
    canonical_root: Path,
    *,
    require_reconciliation: bool = False,
) -> Dict[str, Any]:
    """Publish a validated staging tree with rollback on replacement failure."""
    reconciliation = (
        _verify_reconciliation(staging_root, canonical_root)
        if require_reconciliation
        else None
    )
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

    return {
        "published_files": published,
        "published_count": len(published),
        "reconciliation_outcome": reconciliation.get("outcome") if reconciliation else None,
    }
