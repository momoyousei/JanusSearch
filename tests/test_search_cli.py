#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for SQL+FTS search CLI helpers."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.m2_db import run_load
from tools.search import run_get, run_search, run_stats


def build_payload() -> dict:
    """Build fixture payload with mixed record statuses and years."""
    papers = [
        {
            "paper_id": "P1",
            "title": "Continual Learning with Replay Memory",
            "authors": ["Alice", "Bob"],
            "venue": "ICLR",
            "year": 2023,
            "abstract": "We study replay strategies for continual learning settings.",
            "doi": None,
            "url": "https://example.org/p1",
            "citation_count": 10,
            "source_provider": "openreview",
            "source_ids": {"openreview_id": "OR-P1"},
            "keywords": ["continual learning", "replay"],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "poster",
            "institutions": ["Uni A"],
            "record_status": "resolved",
            "quality_flags": [],
            "collected_at": "2026-02-19T00:00:00+00:00",
        },
        {
            "paper_id": "P2",
            "title": "Replay Baseline Placeholder",
            "authors": [],
            "venue": "ICLR",
            "year": 2024,
            "abstract": "",
            "doi": None,
            "url": "https://example.org/p2",
            "citation_count": None,
            "source_provider": "openreview",
            "source_ids": {"openreview_id": "OR-P2"},
            "keywords": [],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "poster",
            "institutions": [],
            "record_status": "placeholder",
            "quality_flags": ["placeholder_external_only"],
            "collected_at": "2026-02-19T00:00:00+00:00",
        },
        {
            "paper_id": "P3",
            "title": "Continual Learning via Regularization",
            "authors": ["Carol"],
            "venue": "ICML",
            "year": 2021,
            "abstract": "Continual learning can be improved by regularization.",
            "doi": None,
            "url": "https://example.org/p3",
            "citation_count": 50,
            "source_provider": "openreview",
            "source_ids": {"openreview_id": "OR-P3"},
            "keywords": ["continual learning"],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "oral",
            "institutions": [],
            "record_status": "resolved",
            "quality_flags": [],
            "collected_at": "2026-02-19T00:00:00+00:00",
        },
        {
            "paper_id": "P4",
            "title": "Replay in Continual Learning: New Perspectives",
            "authors": ["Dan"],
            "venue": "ICLR",
            "year": 2024,
            "abstract": "Replay remains strong for continual learning in modern settings.",
            "doi": None,
            "url": "https://example.org/p4",
            "citation_count": 5,
            "source_provider": "openreview",
            "source_ids": {"openreview_id": "OR-P4"},
            "keywords": ["replay", "continual learning"],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "bestpaper",
            "institutions": [],
            "record_status": "resolved",
            "quality_flags": [],
            "collected_at": "2026-02-19T00:00:00+00:00",
        },
    ]
    for paper in papers:
        paper["field_provenance"] = {
            field: "official"
            for field in ("abstract", "authors", "url", "track_group", "presentation_level")
            if paper.get(field)
        }
    return {
        "venue": "ICLR",
        "year": 2024,
        "collected_at": "2026-02-19T00:00:00+00:00",
        "source": "openreview",
        "count": len(papers),
        "metrics": {
            "total": len(papers),
            "resolved_total": 3,
            "placeholder_total": 1,
            "full_authors_coverage": 75.0,
            "full_abstract_coverage": 75.0,
            "resolved_authors_coverage": 100.0,
            "resolved_abstract_coverage": 100.0,
            "duplicate_title_count": 0,
        },
        "papers": papers,
    }


class TestSearchCLI(unittest.TestCase):
    """Behavior tests for M2-B search helpers."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.input_root = self.root / "data" / "raw"
        self.index_root = self.root / "artifacts"
        self.db_path = self.root / "data" / "papers.db"
        (self.input_root / "iclr").mkdir(parents=True, exist_ok=True)
        payload = build_payload()
        with (self.input_root / "iclr" / "2024.json").open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        run_load(input_root=self.input_root, db_path=self.db_path, index_root=self.index_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_search_excludes_placeholder_by_default(self) -> None:
        result = run_search(
            db_path=self.db_path,
            query="replay continual learning",
            venues=[],
            year_from=None,
            year_to=None,
            track=None,
            presentation_level=None,
            include_placeholder=False,
            order="bm25",
            top_k=20,
            offset=0,
        )
        ids = [item["paper_id"] for item in result["results"]]
        self.assertIn("P1", ids)
        self.assertIn("P4", ids)
        self.assertNotIn("P2", ids)

    def test_search_can_include_placeholder(self) -> None:
        result = run_search(
            db_path=self.db_path,
            query="replay",
            venues=[],
            year_from=None,
            year_to=None,
            track=None,
            presentation_level=None,
            include_placeholder=True,
            order="bm25",
            top_k=20,
            offset=0,
        )
        ids = [item["paper_id"] for item in result["results"]]
        self.assertIn("P2", ids)

    def test_filter_combination(self) -> None:
        result = run_search(
            db_path=self.db_path,
            query="replay",
            venues=["ICLR"],
            year_from=2024,
            year_to=2024,
            track="conference",
            presentation_level="bestpaper",
            include_placeholder=False,
            order="bm25",
            top_k=20,
            offset=0,
        )
        ids = [item["paper_id"] for item in result["results"]]
        self.assertEqual(ids, ["P4"])

    def test_order_year(self) -> None:
        result = run_search(
            db_path=self.db_path,
            query="continual learning",
            venues=[],
            year_from=None,
            year_to=None,
            track=None,
            presentation_level=None,
            include_placeholder=False,
            order="year",
            top_k=20,
            offset=0,
        )
        years = [item["year"] for item in result["results"]]
        self.assertEqual(years, sorted(years, reverse=True))

    def test_order_citation(self) -> None:
        result = run_search(
            db_path=self.db_path,
            query="continual learning",
            venues=[],
            year_from=None,
            year_to=None,
            track=None,
            presentation_level=None,
            include_placeholder=False,
            order="citation",
            top_k=20,
            offset=0,
        )
        self.assertGreaterEqual(result["results"][0]["citation_count"], result["results"][-1]["citation_count"])
        self.assertEqual(result["results"][0]["paper_id"], "P3")

    def test_get_paper(self) -> None:
        payload = run_get(db_path=self.db_path, paper_id="P1")
        self.assertEqual(payload["paper_id"], "P1")
        self.assertEqual(payload["venue"], "ICLR")
        self.assertTrue(payload["authors"])
        self.assertTrue(payload["keywords"])
        self.assertIn("openreview_id", payload["source_ids"])

    def test_stats(self) -> None:
        payload = run_stats(db_path=self.db_path)
        self.assertEqual(payload["paper_count"], 4)
        self.assertTrue(payload["fts_table_exists"])
        self.assertEqual(payload["fts_row_count"], 4)
        self.assertTrue(payload["fts_aligned"])

    def test_missing_fts_table_error(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("DROP TABLE papers_fts")
            conn.commit()
        finally:
            conn.close()

        with self.assertRaises(RuntimeError):
            run_search(
                db_path=self.db_path,
                query="continual learning",
                venues=[],
                year_from=None,
                year_to=None,
                track=None,
                presentation_level=None,
                include_placeholder=False,
                order="bm25",
                top_k=20,
                offset=0,
            )


if __name__ == "__main__":
    unittest.main()
