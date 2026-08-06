#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic fingerprints for configuration and persisted inputs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


def fingerprint_payload(payload: Any) -> str:
    """Return a stable SHA-256 fingerprint for a JSON-compatible payload."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_descriptor(path: Path) -> Mapping[str, Any]:
    """Describe a file using content for small files and metadata for large files."""
    stat = path.stat()
    descriptor: dict[str, Any] = {
        "kind": "file",
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if stat.st_size <= 4 * 1024 * 1024:
        descriptor["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return descriptor


def describe_path(path: Path) -> Mapping[str, Any]:
    """Return a bounded descriptor suitable for freshness checks."""
    path = path.resolve()
    if not path.exists():
        return {"kind": "missing"}
    if path.is_file():
        return _file_descriptor(path)

    file_count = 0
    total_size = 0
    newest_mtime_ns = 0
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        stat = child.stat()
        file_count += 1
        total_size += stat.st_size
        newest_mtime_ns = max(newest_mtime_ns, stat.st_mtime_ns)
    return {
        "kind": "directory",
        "file_count": file_count,
        "total_size": total_size,
        "newest_mtime_ns": newest_mtime_ns,
    }


def fingerprint_paths(paths: Mapping[str, Path]) -> str:
    """Fingerprint named filesystem inputs without embedding absolute paths."""
    payload = {name: describe_path(path) for name, path in sorted(paths.items())}
    return fingerprint_payload(payload)

