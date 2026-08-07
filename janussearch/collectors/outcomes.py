#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Versioned collection-result sidecars shared by corpus collectors."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

COLLECTION_RESULT_NAME = ".janus-collection.json"
COLLECTION_OUTCOMES = {"collected", "no_update", "incomplete_source"}


def write_collection_result(
    root: Path,
    *,
    outcome: str,
    venue: str,
    sources: Sequence[str],
    reason: str,
    year: int | None = None,
    years: Sequence[int] | None = None,
    metrics: Mapping[str, Any] | None = None,
) -> Path:
    """Write an auditable collection result beside, never inside, canonical data."""
    if outcome not in COLLECTION_OUTCOMES:
        raise ValueError(f"Unsupported collection outcome: {outcome}")
    normalized_years = sorted({int(item) for item in (years or ([year] if year else []))})
    if not normalized_years or any(item < 1900 or item > 2100 for item in normalized_years):
        raise ValueError("Collection result requires concrete years")
    root.mkdir(parents=True, exist_ok=True)
    path = root / COLLECTION_RESULT_NAME
    existing: dict[str, Any] | None = None
    if path.is_file():
        candidate = json.loads(path.read_text(encoding="utf-8"))
        if candidate.get("schema_version") == 2:
            existing = candidate
            if str(existing.get("venue") or "").upper() != venue.upper():
                raise ValueError(f"Collection result venue conflict: {path}")
            normalized_years = sorted(
                set(normalized_years) | {int(item) for item in existing.get("years", [])}
            )
    source_values = list(sources)
    if existing:
        source_values = list(existing.get("sources", [])) + source_values
    unique_sources = list(dict.fromkeys(str(item) for item in source_values if str(item)))
    files = []
    for item in sorted(root.rglob("*.json")):
        if item.name == COLLECTION_RESULT_NAME or item.name.startswith("."):
            continue
        files.append(
            {
                "relative_path": item.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
            }
        )
    merged_outcome = outcome
    if existing and "incomplete_source" in {existing.get("outcome"), outcome}:
        merged_outcome = "incomplete_source"
    elif existing and "collected" in {existing.get("outcome"), outcome}:
        merged_outcome = "collected"
    payload = {
        "schema_version": 2,
        "outcome": merged_outcome,
        "venue": venue.upper(),
        "years": normalized_years,
        "sources": unique_sources,
        "reason": reason,
        "metrics": dict(metrics or {}),
        "files": files,
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def read_collection_result(
    root: Path,
    *,
    expected_venue: str | None = None,
    expected_years: Sequence[int] | None = None,
) -> dict[str, Any] | None:
    """Load and validate a collection sidecar when present."""
    path = root / COLLECTION_RESULT_NAME
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    outcome = payload.get("outcome")
    years = payload.get("years")
    files = payload.get("files")
    if (
        payload.get("schema_version") != 2
        or outcome not in COLLECTION_OUTCOMES
        or not isinstance(years, list)
        or not years
        or not isinstance(files, list)
    ):
        raise ValueError(f"Invalid collection result: {path}")
    if expected_venue and str(payload.get("venue") or "").upper() != expected_venue.upper():
        raise ValueError(f"Collection result venue mismatch: {path}")
    if expected_years is not None and sorted(int(item) for item in years) != sorted(
        {int(item) for item in expected_years}
    ):
        raise ValueError(f"Collection result year scope mismatch: {path}")
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
            raise ValueError(f"Invalid collection result file entry: {path}")
        relative = Path(item["relative_path"])
        if relative.is_absolute() or ".." in relative.parts or item["relative_path"] in seen:
            raise ValueError(f"Invalid collection result file path: {path}")
        seen.add(item["relative_path"])
        file_path = root / relative
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest() if file_path.is_file() else None
        if digest != item.get("sha256"):
            raise ValueError(f"Collection result file hash mismatch: {file_path}")
    actual_files = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*.json")
        if item.name != COLLECTION_RESULT_NAME and not item.name.startswith(".")
    }
    if actual_files != seen:
        raise ValueError(f"Collection result file set mismatch: {path}")
    return payload
