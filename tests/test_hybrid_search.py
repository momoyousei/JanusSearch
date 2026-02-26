#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for M3 hybrid retrieval behavior."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

from tools.m2_db import run_load
from tools.search import run_hybrid


class FakeVectorCollection:
    """Minimal vector collection fixture for hybrid recall."""

    def __init__(self, ids: List[str], distances: List[float]) -> None:
        self.ids = ids
        self.distances = distances

    def query(
        self,
        *,
        query_embeddings: List[List[float]],
        n_results: int,
        include: List[str] | None = None,
    ) -> Dict[str, Any]:
        _ = query_embeddings
        _ = include
        ids = self.ids[:n_results]
        distances = self.distances[:n_results]
        return {"ids": [ids], "distances": [distances], "metadatas": [[]]}


def build_payload() -> Dict[str, Any]:
    """Build fixture payload for hybrid tests."""
    papers = [
        {
            "paper_id": "H1",
            "title": "Continual Learning with Replay Memory",
            "authors": ["Alice", "Bob"],
            "venue": "ICLR",
            "year": 2023,
            "abstract": "Replay methods for continual learning settings.",
            "doi": None,
            "url": "https://example.org/h1",
            "citation_count": 11,
            "source_provider": "openreview",
            "source_ids": {"openreview_id": "OR-H1"},
            "keywords": ["continual learning", "replay"],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "poster",
            "institutions": ["Uni A"],
            "record_status": "resolved",
            "quality_flags": [],
            "collected_at": "2026-02-20T00:00:00+00:00",
        },
        {
            "paper_id": "H2",
            "title": "Replay Placeholder Entry",
            "authors": [],
            "venue": "ICLR",
            "year": 2024,
            "abstract": "",
            "doi": None,
            "url": "https://example.org/h2",
            "citation_count": None,
            "source_provider": "openreview",
            "source_ids": {"openreview_id": "OR-H2"},
            "keywords": [],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "poster",
            "institutions": [],
            "record_status": "placeholder",
            "quality_flags": ["placeholder_external_only"],
            "collected_at": "2026-02-20T00:00:00+00:00",
        },
        {
            "paper_id": "H3",
            "title": "Continual Learning by Regularization",
            "authors": ["Carol"],
            "venue": "ICML",
            "year": 2021,
            "abstract": "Regularization baselines for continual learning.",
            "doi": None,
            "url": "https://example.org/h3",
            "citation_count": 30,
            "source_provider": "openreview",
            "source_ids": {"openreview_id": "OR-H3"},
            "keywords": ["continual learning"],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "oral",
            "institutions": [],
            "record_status": "resolved",
            "quality_flags": [],
            "collected_at": "2026-02-20T00:00:00+00:00",
        },
        {
            "paper_id": "H4",
            "title": "Replay in Continual Learning: Strong Baseline",
            "authors": ["Dan"],
            "venue": "ICLR",
            "year": 2024,
            "abstract": "A strong replay baseline for continual learning.",
            "doi": None,
            "url": "https://example.org/h4",
            "citation_count": 5,
            "source_provider": "openreview",
            "source_ids": {"openreview_id": "OR-H4"},
            "keywords": ["replay", "continual learning"],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "bestpaper",
            "institutions": [],
            "record_status": "resolved",
            "quality_flags": [],
            "collected_at": "2026-02-20T00:00:00+00:00",
        },
    ]
    return {
        "venue": "ICLR",
        "year": 2024,
        "collected_at": "2026-02-20T00:00:00+00:00",
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


class TestHybridSearch(unittest.TestCase):
    """Behavior tests for hybrid retrieval."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.input_root = self.root / "data" / "raw"
        self.db_path = self.root / "data" / "papers.db"
        self.index_root = self.root / "artifacts"
        (self.input_root / "iclr").mkdir(parents=True, exist_ok=True)

        payload = build_payload()
        with (self.input_root / "iclr" / "2024.json").open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        run_load(input_root=self.input_root, db_path=self.db_path, index_root=self.index_root)

        self.collection = FakeVectorCollection(
            ids=["H4", "H2", "H1", "H3"],
            distances=[0.05, 0.10, 0.20, 0.90],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _patch_hybrid(self) -> List[Any]:
        return [
            patch("tools.search._embed_query", return_value=[0.1, 0.2, 0.3]),
            patch("tools.search._load_vector_collection", return_value=self.collection),
        ]

    def test_hybrid_returns_results_default_excludes_placeholder(self) -> None:
        patches = self._patch_hybrid()
        for item in patches:
            item.start()
        try:
            result = run_hybrid(
                db_path=self.db_path,
                query="continual learning replay",
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                alpha=0.6,
                vector_top_k=100,
                bm25_top_k=100,
                vectors_root=self.root / "data" / "vectors" / "chroma",
                collection_name="papers_v1",
                venues=[],
                year_from=None,
                year_to=None,
                track=None,
                presentation_level=None,
                include_placeholder=False,
                top_k=20,
                offset=0,
            )
        finally:
            for item in reversed(patches):
                item.stop()

        ids = [item["paper_id"] for item in result["results"]]
        self.assertTrue(ids)
        self.assertIn("H1", ids)
        self.assertIn("H4", ids)
        self.assertNotIn("H2", ids)

    def test_hybrid_include_placeholder(self) -> None:
        patches = self._patch_hybrid()
        for item in patches:
            item.start()
        try:
            result = run_hybrid(
                db_path=self.db_path,
                query="replay",
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                alpha=0.6,
                vector_top_k=100,
                bm25_top_k=100,
                vectors_root=self.root / "data" / "vectors" / "chroma",
                collection_name="papers_v1",
                venues=[],
                year_from=None,
                year_to=None,
                track=None,
                presentation_level=None,
                include_placeholder=True,
                top_k=20,
                offset=0,
            )
        finally:
            for item in reversed(patches):
                item.stop()

        ids = [item["paper_id"] for item in result["results"]]
        self.assertIn("H2", ids)

    def test_hybrid_filter_combination(self) -> None:
        patches = self._patch_hybrid()
        for item in patches:
            item.start()
        try:
            result = run_hybrid(
                db_path=self.db_path,
                query="replay",
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                alpha=0.6,
                vector_top_k=100,
                bm25_top_k=100,
                vectors_root=self.root / "data" / "vectors" / "chroma",
                collection_name="papers_v1",
                venues=["ICLR"],
                year_from=2024,
                year_to=2024,
                track="conference",
                presentation_level="bestpaper",
                include_placeholder=False,
                top_k=20,
                offset=0,
            )
        finally:
            for item in reversed(patches):
                item.stop()

        ids = [item["paper_id"] for item in result["results"]]
        self.assertEqual(ids, ["H4"])

    def test_hybrid_ranking_repeatable(self) -> None:
        patches = self._patch_hybrid()
        for item in patches:
            item.start()
        try:
            first = run_hybrid(
                db_path=self.db_path,
                query="continual learning replay",
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                alpha=0.6,
                vector_top_k=100,
                bm25_top_k=100,
                vectors_root=self.root / "data" / "vectors" / "chroma",
                collection_name="papers_v1",
                venues=[],
                year_from=None,
                year_to=None,
                track=None,
                presentation_level=None,
                include_placeholder=False,
                top_k=20,
                offset=0,
            )
            second = run_hybrid(
                db_path=self.db_path,
                query="continual learning replay",
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                alpha=0.6,
                vector_top_k=100,
                bm25_top_k=100,
                vectors_root=self.root / "data" / "vectors" / "chroma",
                collection_name="papers_v1",
                venues=[],
                year_from=None,
                year_to=None,
                track=None,
                presentation_level=None,
                include_placeholder=False,
                top_k=20,
                offset=0,
            )
        finally:
            for item in reversed(patches):
                item.stop()

        first_ids = [item["paper_id"] for item in first["results"]]
        second_ids = [item["paper_id"] for item in second["results"]]
        self.assertEqual(first_ids, second_ids)


if __name__ == "__main__":
    unittest.main()
