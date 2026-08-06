#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for TSV export in tools.search."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from tools.m2_db import run_load
from tools.search import EXPORT_COLUMNS, run_export


def build_payload() -> dict:
    """Build fixture payload for export behavior."""
    papers = [
        {
            "paper_id": "P1",
            "title": "Continual Learning with Replay Memory",
            "authors": ["Alice", "Bob"],
            "venue": "ICLR",
            "year": 2024,
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
            "keywords": ["replay"],
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
            "title": "Flatness-Aware Optimization for Continual Learning",
            "authors": ["Carol"],
            "venue": "ICML",
            "year": 2023,
            "abstract": "We revisit flatness-aware methods for continual learning.",
            "doi": None,
            "url": "https://example.org/p3",
            "citation_count": 50,
            "source_provider": "openreview",
            "source_ids": {"openreview_id": "OR-P3"},
            "keywords": ["flatness", "continual learning"],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "oral",
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
            "resolved_total": 2,
            "placeholder_total": 1,
            "full_authors_coverage": 66.7,
            "full_abstract_coverage": 66.7,
            "resolved_authors_coverage": 100.0,
            "resolved_abstract_coverage": 100.0,
            "duplicate_title_count": 0,
        },
        "papers": papers,
    }


def write_keywords_json(path: Path) -> None:
    """Write keyword groups with priority ordering."""
    payload = {
        "query": "continual learning replay",
        "keywords": [
            {
                "label": "Continual Learning / Class-Incremental Learning（持续学习/类增量）",
                "aliases": ["continual learning", "class-incremental learning", "CIL", "持续学习", "类增量学习"],
            },
            {
                "label": "Replay Methods（回放/重放）",
                "aliases": ["replay", "rehearsal", "experience replay", "回放", "重放"],
            },
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_tsv(path: Path) -> list[dict]:
    """Read TSV into a list of dict rows."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        return list(reader)


class TestSearchExport(unittest.TestCase):
    """Tests for tools.search export."""

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

    def test_export_search_writes_full_tsv(self) -> None:
        keywords_json = self.root / "keywords.json"
        write_keywords_json(keywords_json)
        topics_json = self.root / "topics.json"
        topics_payload = {
            "assignments": [
                {
                    "paper_id": "P1",
                    "topic_name": "Continual Learning",
                    "subtopic_name": "Replay Methods",
                }
            ]
        }
        topics_json.write_text(
            json.dumps(topics_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        out_tsv = self.root / "out.tsv"
        payload = run_export(
            db_path=self.db_path,
            query="continual learning",
            mode="search",
            out_tsv=out_tsv,
            keywords_json=keywords_json,
            topics_json=topics_json,
            max_export=2000,
            venues=[],
            year_from=None,
            year_to=None,
            track=None,
            presentation_level=None,
            include_placeholder=False,
            order="bm25",
            embed_base_url="https://example.org/v1",
            embed_model="fake",
            embed_api_key=None,
            alpha=0.6,
            vector_top_k=100,
            bm25_top_k=100,
            vectors_root=self.root / "vectors",
            collection_name="papers_v1",
        )

        self.assertTrue(out_tsv.exists())
        self.assertEqual(payload["mode"], "search")
        self.assertGreaterEqual(payload["total"], 2)
        self.assertEqual(payload["exported"], 2)

        rows = read_tsv(out_tsv)
        self.assertEqual(len(rows), 2)
        self.assertEqual(list(rows[0].keys()), EXPORT_COLUMNS)

        p1 = next(item for item in rows if item["paper_id"] == "P1")
        self.assertEqual(p1["matched_topic"], "Continual Learning / Class-Incremental Learning（持续学习/类增量）")
        self.assertTrue(p1["matched_keyword"])
        self.assertEqual(p1["janus_topic"], "Continual Learning")
        self.assertEqual(p1["janus_subtopic"], "Replay Methods")
        self.assertIn("Alice", p1["authors"])
        self.assertIn("continual learning", p1["keywords"])
        self.assertTrue(p1["source_ids_json"])

    def test_matched_topic_priority(self) -> None:
        keywords_json = self.root / "keywords.json"
        write_keywords_json(keywords_json)
        out_tsv = self.root / "out.tsv"
        run_export(
            db_path=self.db_path,
            query="continual learning replay",
            mode="search",
            out_tsv=out_tsv,
            keywords_json=keywords_json,
            topics_json=self.root / "missing_topics.json",
            max_export=2000,
            venues=[],
            year_from=None,
            year_to=None,
            track=None,
            presentation_level=None,
            include_placeholder=False,
            order="bm25",
            embed_base_url="https://example.org/v1",
            embed_model="fake",
            embed_api_key=None,
            alpha=0.6,
            vector_top_k=100,
            bm25_top_k=100,
            vectors_root=self.root / "vectors",
            collection_name="papers_v1",
        )
        rows = read_tsv(out_tsv)
        p1 = next(item for item in rows if item["paper_id"] == "P1")
        # P1 contains both "continual learning" and "replay"; should match the first group.
        self.assertEqual(p1["matched_topic"], "Continual Learning / Class-Incremental Learning（持续学习/类增量）")

    def test_topics_json_missing_degrades_gracefully(self) -> None:
        keywords_json = self.root / "keywords.json"
        write_keywords_json(keywords_json)
        out_tsv = self.root / "out.tsv"
        run_export(
            db_path=self.db_path,
            query="continual learning",
            mode="search",
            out_tsv=out_tsv,
            keywords_json=keywords_json,
            topics_json=self.root / "missing_topics.json",
            max_export=2000,
            venues=[],
            year_from=None,
            year_to=None,
            track=None,
            presentation_level=None,
            include_placeholder=False,
            order="bm25",
            embed_base_url="https://example.org/v1",
            embed_model="fake",
            embed_api_key=None,
            alpha=0.6,
            vector_top_k=100,
            bm25_top_k=100,
            vectors_root=self.root / "vectors",
            collection_name="papers_v1",
        )
        rows = read_tsv(out_tsv)
        self.assertTrue(rows)
        for item in rows:
            self.assertEqual(item["janus_topic"], "")
            self.assertEqual(item["janus_subtopic"], "")

    def test_include_placeholder(self) -> None:
        keywords_json = self.root / "keywords.json"
        write_keywords_json(keywords_json)

        out_excluded = self.root / "excluded.tsv"
        run_export(
            db_path=self.db_path,
            query="replay",
            mode="search",
            out_tsv=out_excluded,
            keywords_json=keywords_json,
            topics_json=self.root / "missing_topics.json",
            max_export=2000,
            venues=[],
            year_from=None,
            year_to=None,
            track=None,
            presentation_level=None,
            include_placeholder=False,
            order="bm25",
            embed_base_url="https://example.org/v1",
            embed_model="fake",
            embed_api_key=None,
            alpha=0.6,
            vector_top_k=100,
            bm25_top_k=100,
            vectors_root=self.root / "vectors",
            collection_name="papers_v1",
        )
        rows = read_tsv(out_excluded)
        ids = {row["paper_id"] for row in rows}
        self.assertIn("P1", ids)
        self.assertNotIn("P2", ids)

        out_included = self.root / "included.tsv"
        run_export(
            db_path=self.db_path,
            query="replay",
            mode="search",
            out_tsv=out_included,
            keywords_json=keywords_json,
            topics_json=self.root / "missing_topics.json",
            max_export=2000,
            venues=[],
            year_from=None,
            year_to=None,
            track=None,
            presentation_level=None,
            include_placeholder=True,
            order="bm25",
            embed_base_url="https://example.org/v1",
            embed_model="fake",
            embed_api_key=None,
            alpha=0.6,
            vector_top_k=100,
            bm25_top_k=100,
            vectors_root=self.root / "vectors",
            collection_name="papers_v1",
        )
        rows = read_tsv(out_included)
        ids = {row["paper_id"] for row in rows}
        self.assertIn("P2", ids)


if __name__ == "__main__":
    unittest.main()
