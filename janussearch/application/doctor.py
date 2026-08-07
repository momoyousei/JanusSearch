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


def check_vectors(
    vectors_root: Path,
    collection_name: str,
    db_path: Path | None = None,
) -> Iterable[Dict[str, Any]]:
    """Check vector readability, candidate coverage, and required metadata."""
    if not vectors_root.exists():
        yield _check("vectors.exists", "warning", f"Vector store is missing: {vectors_root}")
        return
    chroma_db = vectors_root / "chroma.sqlite3"
    if not chroma_db.is_file():
        yield _check("vectors.readable", "error", f"Chroma database is missing: {chroma_db}")
        return
    vector_connection: sqlite3.Connection | None = None
    try:
        vector_connection = sqlite3.connect(
            f"{chroma_db.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
        )
        collection_row = vector_connection.execute(
            "SELECT id FROM collections WHERE name = ?",
            (collection_name,),
        ).fetchone()
        if collection_row is None:
            raise sqlite3.DatabaseError(f"Collection is missing: {collection_name}")
        collection_id = str(collection_row[0])
        records = vector_connection.execute(
            """
            SELECT
                e.embedding_id,
                MAX(CASE WHEN m.key = 'paper_id' THEN m.string_value END),
                MAX(CASE WHEN m.key = 'embed_model' THEN m.string_value END),
                MAX(CASE WHEN m.key = 'embedding_text_sha256' THEN m.string_value END),
                MAX(
                    CASE WHEN m.key = 'vector_schema_version'
                    THEN COALESCE(m.int_value, CAST(m.string_value AS INTEGER)) END
                )
            FROM embeddings AS e
            JOIN segments AS s ON s.id = e.segment_id
            LEFT JOIN embedding_metadata AS m ON m.id = e.id
            WHERE s.collection = ? AND s.scope = 'METADATA'
            GROUP BY e.id, e.embedding_id
            ORDER BY e.id
            """,
            (collection_id,),
        ).fetchall()
        count = len(records)
    except sqlite3.Error as exc:
        yield _check("vectors.readable", "error", str(exc))
        return
    finally:
        if vector_connection is not None:
            vector_connection.close()
    yield _check("vectors.readable", "pass", f"Collection {collection_name} contains {count} vectors")
    if db_path is None:
        return

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        expected_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT paper_id FROM papers WHERE record_status != 'placeholder'"
            ).fetchall()
        }
    except sqlite3.Error as exc:
        yield _check("vectors.coverage", "error", f"Unable to read vector candidates: {exc}")
        return
    finally:
        if connection is not None:
            connection.close()

    actual_ids = {str(row[0]) for row in records}
    malformed_metadata: list[str] = []
    for row in records:
        paper_id = str(row[0])
        if (
            str(row[1] or "") != paper_id
            or not row[2]
            or not row[3]
            or int(row[4] or 0) < 2
        ):
            malformed_metadata.append(paper_id)

    missing_ids = sorted(expected_ids - actual_ids)
    extra_ids = sorted(actual_ids - expected_ids)
    coverage_pass = not missing_ids and not extra_ids and len(actual_ids) == count
    yield _check(
        "vectors.coverage",
        "pass" if coverage_pass else "error",
        (
            "Vector IDs exactly cover non-placeholder papers"
            if coverage_pass
            else "Vector IDs do not cover non-placeholder papers"
        ),
        expected_count=len(expected_ids),
        actual_count=len(actual_ids),
        missing_count=len(missing_ids),
        extra_count=len(extra_ids),
        missing_sample=missing_ids[:20],
        extra_sample=extra_ids[:20],
    )
    yield _check(
        "vectors.metadata",
        "pass" if not malformed_metadata else "error",
        (
            "Vector metadata contract is complete"
            if not malformed_metadata
            else "Vector metadata contract is incomplete"
        ),
        malformed_count=len(malformed_metadata),
        malformed_sample=malformed_metadata[:20],
    )


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
        checks.extend(check_vectors(vectors_root, collection_name, db_path))
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
