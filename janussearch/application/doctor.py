#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only health diagnostics for query, corpus, and operations profiles."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from janussearch.application.evaluation import status as evaluation_status


def _check(name: str, status: str, message: str, **details: Any) -> Dict[str, Any]:
    """Build one structured diagnostic result."""
    return {"name": name, "status": status, "message": message, "details": details}


def check_database(db_path: Path) -> Iterable[Dict[str, Any]]:
    """Check SQLite readability, integrity, and FTS availability."""
    if not db_path.exists():
        yield _check("database.exists", "error", f"Database is missing: {db_path}")
        return
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        fts = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='papers_fts'"
        ).fetchone()
        count = connection.execute("SELECT COUNT(*) FROM papers").fetchone()
    except sqlite3.Error as exc:
        yield _check("database.readable", "error", str(exc))
        return
    finally:
        if "connection" in locals():
            connection.close()
    integrity_value = str(integrity[0]) if integrity else "missing"
    yield _check(
        "database.integrity",
        "pass" if integrity_value == "ok" else "error",
        f"SQLite quick_check={integrity_value}",
    )
    yield _check(
        "database.fts",
        "pass" if fts else "error",
        "papers_fts is available" if fts else "papers_fts is missing",
    )
    yield _check("database.paper_count", "pass", f"Catalog contains {int(count[0])} papers")


def check_vectors(vectors_root: Path, collection_name: str) -> Iterable[Dict[str, Any]]:
    """Open the local Chroma collection to detect absent or corrupt state."""
    if not vectors_root.exists():
        yield _check("vectors.exists", "warning", f"Vector store is missing: {vectors_root}")
        return
    try:
        import chromadb
        from chromadb.config import Settings

        client = chromadb.PersistentClient(
            path=str(vectors_root),
            settings=Settings(anonymized_telemetry=False),
        )
        collection = client.get_collection(name=collection_name)
        count = collection.count()
    except Exception as exc:
        yield _check("vectors.readable", "error", str(exc))
        return
    yield _check("vectors.readable", "pass", f"Collection {collection_name} contains {count} vectors")


def check_corpus(raw_root: Path, archives_root: Path) -> Iterable[Dict[str, Any]]:
    """Check canonical and historical JSON layers without modifying them."""
    canonical_files = sorted(raw_root.glob("*/*.json")) if raw_root.exists() else []
    archive_files = sorted(archives_root.glob("*.json")) if archives_root.exists() else []
    if not canonical_files:
        yield _check("corpus.canonical", "error", f"No canonical JSON files under {raw_root}")
    else:
        invalid: list[str] = []
        for path in canonical_files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or not isinstance(payload.get("papers"), list):
                    invalid.append(str(path))
            except (OSError, json.JSONDecodeError):
                invalid.append(str(path))
        yield _check(
            "corpus.canonical",
            "pass" if not invalid else "error",
            f"Canonical files={len(canonical_files)}, invalid={len(invalid)}",
            invalid_files=invalid,
        )
    yield _check(
        "corpus.archives",
        "pass" if archive_files else "warning",
        f"Historical input files={len(archive_files)}",
    )


def execute(
    *,
    profile: str,
    db_path: Path,
    vectors_root: Path,
    collection_name: str,
    raw_root: Path,
    archives_root: Path,
    evaluation_report: Path,
    topics_file: Path,
    fixed_query_file: Path,
) -> Tuple[Dict[str, Any], bool]:
    """Run one diagnostic profile and return structured results."""
    if profile not in {"query", "corpus", "ops"}:
        raise ValueError(f"Unknown doctor profile: {profile}")
    checks: list[Dict[str, Any]] = []
    if profile in {"query", "ops"}:
        checks.extend(check_database(db_path))
        checks.extend(check_vectors(vectors_root, collection_name))
    if profile in {"corpus", "ops"}:
        checks.extend(check_corpus(raw_root, archives_root))
    if profile == "ops":
        try:
            eval_result, eval_pass = evaluation_status(
                report_path=evaluation_report,
                db_path=db_path,
                vectors_root=vectors_root,
                topics_file=topics_file,
                fixed_query_file=fixed_query_file,
            )
            checks.append(
                _check(
                    "evaluation.freshness",
                    "pass" if eval_pass else "error",
                    "Evaluation report is current" if eval_pass else "Evaluation report is stale or failed",
                    **eval_result,
                )
            )
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            checks.append(_check("evaluation.freshness", "error", str(exc)))

    errors = sum(item["status"] == "error" for item in checks)
    warnings = sum(item["status"] == "warning" for item in checks)
    report = {
        "profile": profile,
        "overall_pass": errors == 0,
        "summary": {"checks": len(checks), "errors": errors, "warnings": warnings},
        "checks": checks,
    }
    return report, errors == 0
