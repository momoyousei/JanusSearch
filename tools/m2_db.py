#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M2 database pipeline: load canonical JSON files into SQLite and validate consistency."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

LOGGER = logging.getLogger("m2_db")

DEFAULT_INPUT_ROOT = Path("data/raw")
DEFAULT_DB_PATH = Path("data/papers.db")
DEFAULT_INDEX_ROOT = Path("index")


@dataclass
class FileLoadStats:
    """Per-file ingestion statistics."""

    file_path: str
    venue: str
    year: int
    declared_count: int
    loaded_count: int
    author_rows: int
    keyword_rows: int
    institution_rows: int
    quality_flag_rows: int
    source_id_rows: int
    duration_seconds: float


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def ensure_str(value: Any) -> str:
    """Convert value to stripped text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def ensure_list(value: Any) -> List[Any]:
    """Normalize value to list."""
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def ensure_dict(value: Any) -> Dict[str, Any]:
    """Normalize value to dict."""
    if isinstance(value, dict):
        return value
    return {}


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON payload with UTF-8 encoding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def find_source_files(input_root: Path) -> List[Path]:
    """Discover canonical files under data/raw/{venue}/{year}.json."""
    if not input_root.exists() or not input_root.is_dir():
        raise FileNotFoundError(f"Input root does not exist: {input_root}")
    files = sorted(path for path in input_root.glob("*/*.json") if path.is_file())
    if not files:
        raise FileNotFoundError(f"No canonical json files found under: {input_root}")
    return files


def load_payload(path: Path) -> Dict[str, Any]:
    """Load one canonical file."""
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Top-level payload must be object: {path}")
    return payload


def connect_db(db_path: Path) -> sqlite3.Connection:
    """Create SQLite connection with practical pragmas."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """Create all M2 tables and indexes."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            run_id TEXT PRIMARY KEY,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            mode TEXT NOT NULL,
            input_root TEXT NOT NULL,
            db_path TEXT NOT NULL,
            file_count INTEGER NOT NULL DEFAULT 0,
            paper_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS papers (
            paper_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            venue TEXT NOT NULL,
            year INTEGER NOT NULL,
            abstract TEXT NOT NULL DEFAULT '',
            doi TEXT,
            url TEXT,
            citation_count INTEGER,
            source_provider TEXT NOT NULL,
            track TEXT NOT NULL,
            track_display_name TEXT NOT NULL,
            track_group TEXT NOT NULL,
            presentation_level TEXT NOT NULL,
            record_status TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            source_file TEXT NOT NULL,
            ingested_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS paper_authors (
            paper_id TEXT NOT NULL,
            author_index INTEGER NOT NULL,
            author_name TEXT NOT NULL,
            PRIMARY KEY (paper_id, author_index),
            FOREIGN KEY (paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS paper_keywords (
            paper_id TEXT NOT NULL,
            keyword_index INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            PRIMARY KEY (paper_id, keyword_index),
            FOREIGN KEY (paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS paper_institutions (
            paper_id TEXT NOT NULL,
            institution_index INTEGER NOT NULL,
            institution TEXT NOT NULL,
            PRIMARY KEY (paper_id, institution_index),
            FOREIGN KEY (paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS paper_quality_flags (
            paper_id TEXT NOT NULL,
            flag_index INTEGER NOT NULL,
            quality_flag TEXT NOT NULL,
            PRIMARY KEY (paper_id, flag_index),
            FOREIGN KEY (paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS paper_source_ids (
            paper_id TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_value TEXT NOT NULL,
            PRIMARY KEY (paper_id, source_key),
            FOREIGN KEY (paper_id) REFERENCES papers(paper_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS source_files (
            file_path TEXT PRIMARY KEY,
            venue TEXT NOT NULL,
            year INTEGER NOT NULL,
            declared_count INTEGER NOT NULL,
            loaded_count INTEGER NOT NULL,
            collected_at TEXT,
            source TEXT,
            metrics_json TEXT,
            loaded_at_utc TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_papers_venue_year ON papers(venue, year);
        CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(record_status);
        CREATE INDEX IF NOT EXISTS idx_papers_track ON papers(track);
        CREATE INDEX IF NOT EXISTS idx_papers_presentation ON papers(presentation_level);
        CREATE INDEX IF NOT EXISTS idx_papers_doi ON papers(doi);

        CREATE INDEX IF NOT EXISTS idx_paper_authors_paper_id ON paper_authors(paper_id);
        CREATE INDEX IF NOT EXISTS idx_paper_keywords_paper_id ON paper_keywords(paper_id);
        CREATE INDEX IF NOT EXISTS idx_paper_institutions_paper_id ON paper_institutions(paper_id);
        CREATE INDEX IF NOT EXISTS idx_paper_quality_flags_paper_id ON paper_quality_flags(paper_id);
        CREATE INDEX IF NOT EXISTS idx_paper_source_ids_paper_id ON paper_source_ids(paper_id);
        CREATE INDEX IF NOT EXISTS idx_paper_source_ids_key_value ON paper_source_ids(source_key, source_value);
        """
    )
    create_fts_schema(conn)
    conn.commit()


def create_fts_schema(conn: sqlite3.Connection) -> None:
    """Create FTS5 virtual table for title+abstract search."""
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts
            USING fts5(
                paper_id UNINDEXED,
                title,
                abstract,
                tokenize='unicode61'
            )
            """
        )
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "Failed to create FTS table (papers_fts). Ensure SQLite build includes FTS5."
        ) from exc


def fts_table_exists(conn: sqlite3.Connection) -> bool:
    """Check whether papers_fts virtual table exists."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='papers_fts'"
    ).fetchone()
    return row is not None


def rebuild_fts(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Rebuild FTS index from papers table."""
    started = time.perf_counter()
    if not fts_table_exists(conn):
        create_fts_schema(conn)
    with conn:
        conn.execute("DELETE FROM papers_fts")
        conn.execute(
            """
            INSERT INTO papers_fts (paper_id, title, abstract)
            SELECT paper_id, title, abstract
            FROM papers
            """
        )
    fts_rows = int(conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0])
    duration_seconds = round(time.perf_counter() - started, 3)
    return {"fts_rows": fts_rows, "duration_seconds": duration_seconds}


def insert_run_start(
    conn: sqlite3.Connection, run_id: str, input_root: Path, db_path: Path
) -> None:
    """Insert run start metadata."""
    conn.execute(
        """
        INSERT INTO ingestion_runs (
            run_id, started_at_utc, mode, input_root, db_path, status
        ) VALUES (?, ?, 'rebuild', ?, ?, 'running')
        """,
        (run_id, utc_now_iso(), str(input_root), str(db_path)),
    )
    conn.commit()


def finalize_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    file_count: int,
    paper_count: int,
    error_message: str | None = None,
) -> None:
    """Finalize run metadata with success/failure."""
    conn.execute(
        """
        UPDATE ingestion_runs
        SET finished_at_utc = ?,
            file_count = ?,
            paper_count = ?,
            status = ?,
            error_message = ?
        WHERE run_id = ?
        """,
        (utc_now_iso(), file_count, paper_count, status, error_message, run_id),
    )
    conn.commit()


def _paper_row(
    record: Dict[str, Any], source_file: str, ingested_at: str
) -> Tuple[Any, ...]:
    """Map canonical record to papers row tuple."""
    citation_count = record.get("citation_count")
    if isinstance(citation_count, str) and citation_count.strip().isdigit():
        citation_count = int(citation_count.strip())
    if not isinstance(citation_count, int):
        citation_count = None
    return (
        ensure_str(record.get("paper_id")),
        ensure_str(record.get("title")),
        ensure_str(record.get("venue")),
        int(record.get("year")),
        ensure_str(record.get("abstract")),
        ensure_str(record.get("doi")) or None,
        ensure_str(record.get("url")) or None,
        citation_count,
        ensure_str(record.get("source_provider")),
        ensure_str(record.get("track")),
        ensure_str(record.get("track_display_name")),
        ensure_str(record.get("track_group")),
        ensure_str(record.get("presentation_level")),
        ensure_str(record.get("record_status")),
        ensure_str(record.get("collected_at")) or ingested_at,
        source_file,
        ingested_at,
    )


def _assert_required_record_fields(record: Dict[str, Any], source_file: Path) -> None:
    """Fail fast when required fields are missing."""
    required = (
        "paper_id",
        "title",
        "venue",
        "year",
        "abstract",
        "authors",
        "keywords",
        "institutions",
        "quality_flags",
        "source_ids",
        "source_provider",
        "track",
        "track_display_name",
        "track_group",
        "presentation_level",
        "record_status",
        "collected_at",
    )
    missing = [key for key in required if key not in record]
    if missing:
        raise ValueError(f"{source_file}: record missing required keys: {missing}")


def load_one_file(
    conn: sqlite3.Connection, file_path: Path, ingested_at: str
) -> FileLoadStats:
    """Load one canonical JSON file into SQLite."""
    started = time.perf_counter()
    payload = load_payload(file_path)

    top_required = ("venue", "year", "count", "papers")
    missing_top = [key for key in top_required if key not in payload]
    if missing_top:
        raise ValueError(f"{file_path}: missing top-level keys: {missing_top}")

    venue = ensure_str(payload.get("venue"))
    year = int(payload.get("year"))
    declared_count = int(payload.get("count"))
    source = ensure_str(payload.get("source"))
    collected_at = ensure_str(payload.get("collected_at"))
    metrics_json = json.dumps(payload.get("metrics", {}), ensure_ascii=False, sort_keys=True)

    papers = payload.get("papers")
    if not isinstance(papers, list):
        raise ValueError(f"{file_path}: papers must be list")

    loaded_count = len(papers)
    if declared_count != loaded_count:
        raise ValueError(
            f"{file_path}: declared count ({declared_count}) != papers length ({loaded_count})"
        )

    paper_rows: List[Tuple[Any, ...]] = []
    author_rows: List[Tuple[str, int, str]] = []
    keyword_rows: List[Tuple[str, int, str]] = []
    institution_rows: List[Tuple[str, int, str]] = []
    quality_flag_rows: List[Tuple[str, int, str]] = []
    source_id_rows: List[Tuple[str, str, str]] = []

    source_file_str = str(file_path)
    for record in papers:
        if not isinstance(record, dict):
            raise ValueError(f"{file_path}: papers item must be object")
        _assert_required_record_fields(record, file_path)
        paper_id = ensure_str(record.get("paper_id"))
        if not paper_id:
            raise ValueError(f"{file_path}: paper_id is empty")

        paper_rows.append(_paper_row(record, source_file_str, ingested_at))

        for index, value in enumerate(ensure_list(record.get("authors"))):
            author_name = ensure_str(value)
            if author_name:
                author_rows.append((paper_id, index, author_name))

        for index, value in enumerate(ensure_list(record.get("keywords"))):
            keyword = ensure_str(value)
            if keyword:
                keyword_rows.append((paper_id, index, keyword))

        for index, value in enumerate(ensure_list(record.get("institutions"))):
            institution = ensure_str(value)
            if institution:
                institution_rows.append((paper_id, index, institution))

        for index, value in enumerate(ensure_list(record.get("quality_flags"))):
            quality_flag = ensure_str(value)
            if quality_flag:
                quality_flag_rows.append((paper_id, index, quality_flag))

        for key, value in ensure_dict(record.get("source_ids")).items():
            source_key = ensure_str(key)
            source_value = ensure_str(value)
            if source_key and source_value:
                source_id_rows.append((paper_id, source_key, source_value))

    with conn:
        conn.executemany(
            """
            INSERT INTO papers (
                paper_id, title, venue, year, abstract, doi, url, citation_count,
                source_provider, track, track_display_name, track_group,
                presentation_level, record_status, collected_at, source_file, ingested_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            paper_rows,
        )
        conn.executemany(
            "INSERT INTO paper_authors (paper_id, author_index, author_name) VALUES (?, ?, ?)",
            author_rows,
        )
        conn.executemany(
            "INSERT INTO paper_keywords (paper_id, keyword_index, keyword) VALUES (?, ?, ?)",
            keyword_rows,
        )
        conn.executemany(
            """
            INSERT INTO paper_institutions (paper_id, institution_index, institution)
            VALUES (?, ?, ?)
            """,
            institution_rows,
        )
        conn.executemany(
            """
            INSERT INTO paper_quality_flags (paper_id, flag_index, quality_flag)
            VALUES (?, ?, ?)
            """,
            quality_flag_rows,
        )
        conn.executemany(
            "INSERT INTO paper_source_ids (paper_id, source_key, source_value) VALUES (?, ?, ?)",
            source_id_rows,
        )
        conn.execute(
            """
            INSERT INTO source_files (
                file_path, venue, year, declared_count, loaded_count,
                collected_at, source, metrics_json, loaded_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_file_str,
                venue,
                year,
                declared_count,
                loaded_count,
                collected_at,
                source,
                metrics_json,
                ingested_at,
            ),
        )

    elapsed = round(time.perf_counter() - started, 3)
    return FileLoadStats(
        file_path=source_file_str,
        venue=venue,
        year=year,
        declared_count=declared_count,
        loaded_count=loaded_count,
        author_rows=len(author_rows),
        keyword_rows=len(keyword_rows),
        institution_rows=len(institution_rows),
        quality_flag_rows=len(quality_flag_rows),
        source_id_rows=len(source_id_rows),
        duration_seconds=elapsed,
    )


def run_load(input_root: Path, db_path: Path, index_root: Path) -> Dict[str, Any]:
    """Run full rebuild load from canonical files."""
    files = find_source_files(input_root)
    index_root.mkdir(parents=True, exist_ok=True)
    report_path = index_root / "m2_load_report.json"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if db_path.exists():
        db_path.unlink()

    run_id = f"m2-{uuid.uuid4().hex[:12]}"
    ingested_at = utc_now_iso()
    conn = connect_db(db_path)

    file_stats: List[FileLoadStats] = []
    started = time.perf_counter()
    fts_rows = 0
    fts_rebuild_seconds = 0.0
    try:
        create_schema(conn)
        insert_run_start(conn, run_id=run_id, input_root=input_root, db_path=db_path)

        for file_path in files:
            stats = load_one_file(conn=conn, file_path=file_path, ingested_at=ingested_at)
            file_stats.append(stats)
            LOGGER.info(
                "Loaded %s (%s %s): papers=%s authors=%s keywords=%s",
                file_path,
                stats.venue,
                stats.year,
                stats.loaded_count,
                stats.author_rows,
                stats.keyword_rows,
            )

        fts_stats = rebuild_fts(conn)
        fts_rows = int(fts_stats["fts_rows"])
        fts_rebuild_seconds = float(fts_stats["duration_seconds"])
        LOGGER.info("Rebuilt FTS index: rows=%s duration=%.3fs", fts_rows, fts_rebuild_seconds)

        paper_count = sum(item.loaded_count for item in file_stats)
        finalize_run(
            conn,
            run_id=run_id,
            status="success",
            file_count=len(file_stats),
            paper_count=paper_count,
        )
        status = "success"
        error_message = None
    except Exception as exc:  # noqa: BLE001
        error_message = f"{exc.__class__.__name__}: {exc}"
        LOGGER.error("M2 load failed: %s", error_message)
        LOGGER.debug("Traceback:\n%s", traceback.format_exc())
        paper_count = sum(item.loaded_count for item in file_stats)
        try:
            finalize_run(
                conn,
                run_id=run_id,
                status="failed",
                file_count=len(file_stats),
                paper_count=paper_count,
                error_message=error_message,
            )
        except sqlite3.Error:
            LOGGER.warning("Failed to persist ingestion failure metadata.")
        status = "failed"
    finally:
        duration_seconds = round(time.perf_counter() - started, 3)
        conn.close()

    report = {
        "summary": {
            "generated_at_utc": utc_now_iso(),
            "run_id": run_id,
            "status": status,
            "input_root": str(input_root),
            "db_path": str(db_path),
            "file_count": len(file_stats),
            "paper_count": sum(item.loaded_count for item in file_stats),
            "author_rows": sum(item.author_rows for item in file_stats),
            "keyword_rows": sum(item.keyword_rows for item in file_stats),
            "institution_rows": sum(item.institution_rows for item in file_stats),
            "quality_flag_rows": sum(item.quality_flag_rows for item in file_stats),
            "source_id_rows": sum(item.source_id_rows for item in file_stats),
            "fts_rows": fts_rows,
            "fts_rebuild_seconds": fts_rebuild_seconds,
            "duration_seconds": duration_seconds,
            "error_message": error_message,
        },
        "files": [
            {
                "file_path": item.file_path,
                "venue": item.venue,
                "year": item.year,
                "declared_count": item.declared_count,
                "loaded_count": item.loaded_count,
                "author_rows": item.author_rows,
                "keyword_rows": item.keyword_rows,
                "institution_rows": item.institution_rows,
                "quality_flag_rows": item.quality_flag_rows,
                "source_id_rows": item.source_id_rows,
                "duration_seconds": item.duration_seconds,
            }
            for item in file_stats
        ],
    }
    write_json(report_path, report)
    LOGGER.info("M2 load report written: %s", report_path)

    if status != "success":
        raise RuntimeError(error_message or "M2 load failed")
    return report


def _count_by(conn: sqlite3.Connection, table: str, column: str) -> Dict[str, int]:
    """Return grouped counts from one table."""
    rows = conn.execute(
        f"SELECT {column} AS key, COUNT(*) AS c FROM {table} GROUP BY {column}"  # noqa: S608
    ).fetchall()
    result = {ensure_str(row["key"]): int(row["c"]) for row in rows}
    return {key: result[key] for key in sorted(result)}


def _count_venue_year_from_json(files: Iterable[Path]) -> Dict[str, int]:
    """Compute venue-year record counts from canonical JSON files."""
    counts: Dict[str, int] = {}
    for file_path in files:
        payload = load_payload(file_path)
        papers = payload.get("papers", [])
        if not isinstance(papers, list):
            continue
        for record in papers:
            if not isinstance(record, dict):
                continue
            key = f"{ensure_str(record.get('venue'))}-{int(record.get('year'))}"
            counts[key] = counts.get(key, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def _count_venue_year_from_db(conn: sqlite3.Connection) -> Dict[str, int]:
    """Compute venue-year record counts from DB."""
    rows = conn.execute(
        "SELECT venue, year, COUNT(*) AS c FROM papers GROUP BY venue, year ORDER BY venue, year"
    ).fetchall()
    return {f"{ensure_str(row['venue'])}-{int(row['year'])}": int(row["c"]) for row in rows}


def _compare_maps(expected: Dict[str, int], actual: Dict[str, int]) -> Dict[str, int]:
    """Return expected-actual diff map."""
    keys = sorted(set(expected) | set(actual))
    return {key: int(expected.get(key, 0)) - int(actual.get(key, 0)) for key in keys}


def run_validate(input_root: Path, db_path: Path, index_root: Path) -> Tuple[Dict[str, Any], bool]:
    """Validate DB counts against canonical JSON files."""
    files = find_source_files(input_root)
    if not db_path.exists():
        raise FileNotFoundError(f"Database does not exist: {db_path}")
    index_root.mkdir(parents=True, exist_ok=True)
    report_path = index_root / "m2_validate_report.json"

    expected_paper_count = 0
    expected_file_count = len(files)
    expected_status_counts: Dict[str, int] = {}
    expected_relation_counts = {
        "paper_authors": 0,
        "paper_keywords": 0,
        "paper_institutions": 0,
        "paper_quality_flags": 0,
        "paper_source_ids": 0,
    }
    expected_source_file_manifest: Dict[str, Dict[str, int]] = {}

    for file_path in files:
        payload = load_payload(file_path)
        papers = payload.get("papers", [])
        if not isinstance(papers, list):
            raise ValueError(f"{file_path}: papers must be list")
        expected_paper_count += len(papers)
        expected_source_file_manifest[str(file_path)] = {
            "declared_count": int(payload.get("count")),
            "loaded_count": len(papers),
        }
        for record in papers:
            if not isinstance(record, dict):
                continue
            status = ensure_str(record.get("record_status"))
            expected_status_counts[status] = expected_status_counts.get(status, 0) + 1
            expected_relation_counts["paper_authors"] += len(ensure_list(record.get("authors")))
            expected_relation_counts["paper_keywords"] += len(ensure_list(record.get("keywords")))
            expected_relation_counts["paper_institutions"] += len(
                ensure_list(record.get("institutions"))
            )
            expected_relation_counts["paper_quality_flags"] += len(
                ensure_list(record.get("quality_flags"))
            )
            expected_relation_counts["paper_source_ids"] += len(
                ensure_dict(record.get("source_ids"))
            )

    expected_status_counts = {
        key: expected_status_counts[key] for key in sorted(expected_status_counts)
    }
    expected_venue_year_counts = _count_venue_year_from_json(files)

    conn = connect_db(db_path)
    try:
        actual_paper_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        actual_file_count = int(conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0])
        has_fts = fts_table_exists(conn)
        actual_fts_rows = (
            int(conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0]) if has_fts else 0
        )
        actual_status_counts = _count_by(conn, "papers", "record_status")
        actual_venue_year_counts = _count_venue_year_from_db(conn)
        actual_relation_counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
            for table in expected_relation_counts
        }
        duplicate_paper_ids = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT paper_id, COUNT(*) AS c
                    FROM papers
                    GROUP BY paper_id
                    HAVING c > 1
                )
                """
            ).fetchone()[0]
        )
        missing_required_count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM papers
                WHERE paper_id IS NULL OR TRIM(paper_id) = ''
                   OR title IS NULL OR TRIM(title) = ''
                   OR venue IS NULL OR TRIM(venue) = ''
                   OR year IS NULL
                   OR track IS NULL OR TRIM(track) = ''
                   OR presentation_level IS NULL OR TRIM(presentation_level) = ''
                   OR record_status IS NULL OR TRIM(record_status) = ''
                """
            ).fetchone()[0]
        )
        source_file_rows = conn.execute(
            """
            SELECT file_path, declared_count, loaded_count
            FROM source_files
            ORDER BY file_path
            """
        ).fetchall()
    finally:
        conn.close()

    actual_source_file_manifest = {
        ensure_str(row["file_path"]): {
            "declared_count": int(row["declared_count"]),
            "loaded_count": int(row["loaded_count"]),
        }
        for row in source_file_rows
    }

    checks: List[Dict[str, Any]] = []
    issues: List[str] = []

    def add_check(name: str, expected: Any, actual: Any) -> None:
        passed = expected == actual
        checks.append({"name": name, "pass": passed, "expected": expected, "actual": actual})
        if not passed:
            issues.append(f"{name} mismatch")

    add_check("paper_count", expected_paper_count, actual_paper_count)
    add_check("source_file_count", expected_file_count, actual_file_count)
    add_check("fts_table_exists", True, has_fts)
    add_check("fts_row_count", expected_paper_count, actual_fts_rows)
    add_check("status_counts", expected_status_counts, actual_status_counts)
    add_check("venue_year_counts", expected_venue_year_counts, actual_venue_year_counts)

    for table_name, expected_count in expected_relation_counts.items():
        add_check(f"{table_name}_count", expected_count, actual_relation_counts[table_name])

    add_check("duplicate_paper_ids", 0, duplicate_paper_ids)
    add_check("missing_required_fields", 0, missing_required_count)
    add_check("source_file_manifest", expected_source_file_manifest, actual_source_file_manifest)

    report = {
        "summary": {
            "generated_at_utc": utc_now_iso(),
            "input_root": str(input_root),
            "db_path": str(db_path),
            "all_pass": len(issues) == 0,
            "issue_count": len(issues),
            "paper_count_expected": expected_paper_count,
            "paper_count_actual": actual_paper_count,
            "source_file_count_expected": expected_file_count,
            "source_file_count_actual": actual_file_count,
            "fts_row_count_expected": expected_paper_count,
            "fts_row_count_actual": actual_fts_rows,
        },
        "checks": checks,
        "issues": issues,
        "diffs": {
            "status_counts_expected_minus_actual": _compare_maps(
                expected_status_counts, actual_status_counts
            ),
            "venue_year_expected_minus_actual": _compare_maps(
                expected_venue_year_counts, actual_venue_year_counts
            ),
        },
    }
    write_json(report_path, report)
    LOGGER.info("M2 validate report written: %s", report_path)
    return report, len(issues) == 0


def run_stats(db_path: Path) -> Dict[str, Any]:
    """Return and log quick DB statistics."""
    if not db_path.exists():
        raise FileNotFoundError(f"Database does not exist: {db_path}")
    conn = connect_db(db_path)
    try:
        paper_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        status_counts = _count_by(conn, "papers", "record_status")
        venue_year_counts = _count_venue_year_from_db(conn)
        source_file_count = int(conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0])
        has_fts = fts_table_exists(conn)
        fts_row_count = (
            int(conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0]) if has_fts else 0
        )
    finally:
        conn.close()

    payload = {
        "generated_at_utc": utc_now_iso(),
        "db_path": str(db_path),
        "paper_count": paper_count,
        "source_file_count": source_file_count,
        "fts_table_exists": has_fts,
        "fts_row_count": fts_row_count,
        "status_counts": status_counts,
        "venue_year_counts": venue_year_counts,
    }
    LOGGER.info("DB papers: %s", paper_count)
    LOGGER.info("DB source files: %s", source_file_count)
    LOGGER.info("FTS table exists: %s (rows=%s)", has_fts, fts_row_count)
    LOGGER.info("Status counts: %s", status_counts)
    LOGGER.info("Venue-year counts: %s", venue_year_counts)
    return payload


def run_reindex_fts(db_path: Path, index_root: Path) -> Dict[str, Any]:
    """Rebuild FTS index for existing SQLite DB."""
    if not db_path.exists():
        raise FileNotFoundError(f"Database does not exist: {db_path}")
    index_root.mkdir(parents=True, exist_ok=True)
    report_path = index_root / "m2_fts_report.json"

    conn = connect_db(db_path)
    try:
        paper_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        fts_stats = rebuild_fts(conn)
        fts_rows = int(fts_stats["fts_rows"])
        duration_seconds = float(fts_stats["duration_seconds"])
    finally:
        conn.close()

    report = {
        "summary": {
            "generated_at_utc": utc_now_iso(),
            "db_path": str(db_path),
            "paper_count": paper_count,
            "fts_rows": fts_rows,
            "duration_seconds": duration_seconds,
            "aligned": paper_count == fts_rows,
        }
    }
    write_json(report_path, report)
    LOGGER.info("M2 FTS report written: %s", report_path)
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI parser for M2 pipeline."""
    parser = argparse.ArgumentParser(description="M2 database pipeline")
    parser.add_argument(
        "--input-root",
        default=str(DEFAULT_INPUT_ROOT),
        help=f"Canonical input root (default: {DEFAULT_INPUT_ROOT})",
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite db path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--index-root",
        default=str(DEFAULT_INDEX_ROOT),
        help=f"Report output root (default: {DEFAULT_INDEX_ROOT})",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Log level",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("load", help="Full rebuild load into SQLite")
    subparsers.add_parser("validate", help="Validate DB against canonical JSON")
    subparsers.add_parser("run", help="Run load then validate")
    subparsers.add_parser("reindex-fts", help="Rebuild papers_fts index for existing DB")
    subparsers.add_parser("stats", help="Show quick DB stats")
    return parser


def main() -> int:
    """CLI entry point."""
    parser = build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    input_root = Path(args.input_root)
    db_path = Path(args.db_path)
    index_root = Path(args.index_root)

    if args.command == "load":
        run_load(input_root=input_root, db_path=db_path, index_root=index_root)
        return 0

    if args.command == "validate":
        _report, all_pass = run_validate(
            input_root=input_root, db_path=db_path, index_root=index_root
        )
        return 0 if all_pass else 1

    if args.command == "run":
        run_load(input_root=input_root, db_path=db_path, index_root=index_root)
        _report, all_pass = run_validate(
            input_root=input_root, db_path=db_path, index_root=index_root
        )
        return 0 if all_pass else 1

    if args.command == "reindex-fts":
        run_reindex_fts(db_path=db_path, index_root=index_root)
        return 0

    if args.command == "stats":
        run_stats(db_path=db_path)
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
