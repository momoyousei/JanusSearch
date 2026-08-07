#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite catalog application workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

from janussearch.infrastructure import catalog_sqlite as legacy


def execute(
    operation: str,
    *,
    input_root: Path,
    db_path: Path,
    index_root: Path,
) -> Tuple[Dict[str, Any], bool]:
    """Execute one catalog operation through the compatible implementation."""
    if operation == "build":
        payload = legacy.run_load(input_root=input_root, db_path=db_path, index_root=index_root)
        return payload, True
    if operation == "validate":
        return legacy.run_validate(input_root=input_root, db_path=db_path, index_root=index_root)
    if operation == "reindex-fts":
        payload = legacy.run_reindex_fts(db_path=db_path, index_root=index_root)
        return payload, True
    if operation == "stats":
        payload = legacy.run_stats(db_path=db_path)
        return payload, True
    raise ValueError(f"Unknown catalog operation: {operation}")
