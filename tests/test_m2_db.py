#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for M2 SQLite load and validation pipeline."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.m2_db import run_load, run_reindex_fts, run_validate


def build_sample_payload(venue: str = "ICLR", year: int = 2024) -> dict:
    """Build a minimal canonical payload fixture."""
    return {
        "venue": venue,
        "year": year,
        "collected_at": "2026-02-19T00:00:00+00:00",
        "source": "openreview",
        "count": 1,
        "metrics": {
            "total": 1,
            "resolved_total": 1,
            "placeholder_total": 0,
            "full_authors_coverage": 100.0,
            "full_abstract_coverage": 100.0,
            "resolved_authors_coverage": 100.0,
            "resolved_abstract_coverage": 100.0,
            "duplicate_title_count": 0,
        },
        "papers": [
            {
                "paper_id": "S2-test-paper-1",
                "title": "A Test Paper",
                "authors": ["Alice", "Bob"],
                "venue": venue,
                "year": year,
                "abstract": "Test abstract",
                "doi": "10.1000/test",
                "url": "https://example.org/paper",
                "citation_count": 7,
                "source_provider": "openreview",
                "source_ids": {"openreview_id": "OR-123", "doi": "10.1000/test"},
                "keywords": ["continual learning", "replay"],
                "track": "conference",
                "track_display_name": "Conference",
                "track_group": "main",
                "presentation_level": "poster",
                "institutions": ["Test University"],
                "record_status": "resolved",
                "quality_flags": [],
                "collected_at": "2026-02-19T00:00:00+00:00",
            }
        ],
    }


class TestM2DB(unittest.TestCase):
    """Test M2 load/validate behavior."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.input_root = self.root / "data" / "raw"
        self.index_root = self.root / "artifacts"
        self.db_path = self.root / "data" / "papers.db"
        (self.input_root / "iclr").mkdir(parents=True, exist_ok=True)
        payload = build_sample_payload()
        with (self.input_root / "iclr" / "2024.json").open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_schema_create_ok(self) -> None:
        run_load(input_root=self.input_root, db_path=self.db_path, index_root=self.index_root)
        self.assertTrue(self.db_path.exists())
        conn = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("papers", tables)
            self.assertIn("paper_authors", tables)
            self.assertIn("source_files", tables)
            self.assertIn("ingestion_runs", tables)
        finally:
            conn.close()

    def test_load_single_file_ok(self) -> None:
        report = run_load(
            input_root=self.input_root,
            db_path=self.db_path,
            index_root=self.index_root,
        )
        self.assertEqual(report["summary"]["status"], "success")
        self.assertEqual(report["summary"]["file_count"], 1)
        self.assertEqual(report["summary"]["paper_count"], 1)

        conn = sqlite3.connect(self.db_path)
        try:
            paper_count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            author_count = conn.execute("SELECT COUNT(*) FROM paper_authors").fetchone()[0]
            keyword_count = conn.execute("SELECT COUNT(*) FROM paper_keywords").fetchone()[0]
            source_id_count = conn.execute("SELECT COUNT(*) FROM paper_source_ids").fetchone()[0]
            self.assertEqual(paper_count, 1)
            self.assertEqual(author_count, 2)
            self.assertEqual(keyword_count, 2)
            self.assertEqual(source_id_count, 2)
        finally:
            conn.close()

    def test_load_rebuild_idempotent(self) -> None:
        run_load(input_root=self.input_root, db_path=self.db_path, index_root=self.index_root)
        run_load(input_root=self.input_root, db_path=self.db_path, index_root=self.index_root)
        conn = sqlite3.connect(self.db_path)
        try:
            paper_count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            self.assertEqual(paper_count, 1)
        finally:
            conn.close()

    def test_validate_detects_mismatch(self) -> None:
        run_load(input_root=self.input_root, db_path=self.db_path, index_root=self.index_root)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DELETE FROM papers WHERE paper_id = 'S2-test-paper-1'")
            conn.commit()
        finally:
            conn.close()

        report, all_pass = run_validate(
            input_root=self.input_root,
            db_path=self.db_path,
            index_root=self.index_root,
        )
        self.assertFalse(all_pass)
        self.assertIn("paper_count mismatch", report["issues"])

    def test_source_file_manifest(self) -> None:
        run_load(input_root=self.input_root, db_path=self.db_path, index_root=self.index_root)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT declared_count, loaded_count FROM source_files"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], row[1])
            self.assertEqual(row[0], 1)
        finally:
            conn.close()

    def test_fts_table_exists_and_counts_match(self) -> None:
        run_load(input_root=self.input_root, db_path=self.db_path, index_root=self.index_root)
        conn = sqlite3.connect(self.db_path)
        try:
            table_row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='papers_fts'"
            ).fetchone()
            self.assertIsNotNone(table_row)
            paper_count = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            fts_count = conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0]
            self.assertEqual(fts_count, paper_count)
        finally:
            conn.close()

    def test_reindex_fts_command(self) -> None:
        run_load(input_root=self.input_root, db_path=self.db_path, index_root=self.index_root)
        report = run_reindex_fts(db_path=self.db_path, index_root=self.index_root)
        self.assertTrue(report["summary"]["aligned"])
        self.assertEqual(report["summary"]["paper_count"], report["summary"]["fts_rows"])


if __name__ == "__main__":
    unittest.main()
