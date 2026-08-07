#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Versioned collection-result sidecars shared by corpus collectors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

COLLECTION_RESULT_NAME = ".janus-collection.json"
COLLECTION_OUTCOMES = {"collected", "no_update", "incomplete_source"}


def write_collection_result(
    root: Path,
    *,
    outcome: str,
    venue: str,
    year: int,
    sources: Sequence[str],
    reason: str,
    metrics: Mapping[str, Any] | None = None,
) -> Path:
    """Write an auditable collection result beside, never inside, canonical data."""
    if outcome not in COLLECTION_OUTCOMES:
        raise ValueError(f"Unsupported collection outcome: {outcome}")
    root.mkdir(parents=True, exist_ok=True)
    path = root / COLLECTION_RESULT_NAME
    payload = {
        "schema_version": 1,
        "outcome": outcome,
        "venue": venue.upper(),
        "year": year,
        "sources": list(sources),
        "reason": reason,
        "metrics": dict(metrics or {}),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_collection_result(root: Path) -> dict[str, Any] | None:
    """Load and validate a collection sidecar when present."""
    path = root / COLLECTION_RESULT_NAME
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    outcome = payload.get("outcome")
    if payload.get("schema_version") != 1 or outcome not in COLLECTION_OUTCOMES:
        raise ValueError(f"Invalid collection result: {path}")
    return payload
