#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for M3 pipeline (vectors, topics, cache, validate)."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

from tools.m2_db import run_load
from tools.m3_pipeline import (
    check_chroma_sqlite_integrity,
    run_build_cache,
    run_build_topics,
    run_build_vectors,
    run_validate,
)


def _vectorize(text: str) -> List[float]:
    """Deterministic toy embedding for tests."""
    lowered = text.lower()
    tokens = re.findall(r"[a-z]+", lowered)
    replay = float("replay" in tokens)
    continual = float("continual" in tokens)
    memory = float("memory" in tokens)
    length = float(min(len(tokens), 50)) / 50.0
    checksum = float(sum(ord(ch) for ch in lowered) % 97) / 97.0
    return [replay, continual, memory, length, checksum]


class FakeCollection:
    """Minimal in-memory Chroma-like collection."""

    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}

    def add(
        self,
        *,
        ids: List[str],
        documents: List[str],
        embeddings: List[List[float]],
        metadatas: List[Dict[str, Any]],
    ) -> None:
        for idx, paper_id in enumerate(ids):
            self.rows[paper_id] = {
                "id": paper_id,
                "document": documents[idx],
                "embedding": embeddings[idx],
                "metadata": metadatas[idx],
            }

    def get(
        self,
        ids: List[str] | None = None,
        include: List[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Dict[str, Any]:
        _ = include
        row_ids = [item for item in (ids or list(self.rows.keys())) if item in self.rows]
        start = int(offset or 0)
        end = start + int(limit) if limit is not None else len(row_ids)
        row_ids = row_ids[start:end]
        return {
            "ids": row_ids,
            "documents": [self.rows[item]["document"] for item in row_ids],
            "embeddings": [self.rows[item]["embedding"] for item in row_ids],
            "metadatas": [self.rows[item]["metadata"] for item in row_ids],
        }

    def count(self) -> int:
        return len(self.rows)

    def delete(self, *, ids: List[str]) -> None:
        for paper_id in ids:
            self.rows.pop(paper_id, None)

    def query(
        self,
        *,
        query_embeddings: List[List[float]],
        n_results: int,
        include: List[str] | None = None,
    ) -> Dict[str, Any]:
        query = query_embeddings[0]

        def cosine_distance(vec: List[float]) -> float:
            dot = sum(a * b for a, b in zip(query, vec))
            q_norm = math.sqrt(sum(a * a for a in query))
            v_norm = math.sqrt(sum(a * a for a in vec))
            if q_norm == 0.0 or v_norm == 0.0:
                return 1.0
            similarity = dot / (q_norm * v_norm)
            return 1.0 - similarity

        ranked = sorted(
            self.rows.values(),
            key=lambda row: cosine_distance(row["embedding"]),
        )[:n_results]
        return {
            "ids": [[row["id"] for row in ranked]],
            "distances": [[cosine_distance(row["embedding"]) for row in ranked]],
            "metadatas": [[row["metadata"] for row in ranked]],
        }


class FakeChromaClient:
    """Minimal in-memory Chroma-like client."""

    def __init__(self, registry: Dict[str, FakeCollection]) -> None:
        self.registry = registry

    def delete_collection(self, name: str) -> None:
        self.registry.pop(name, None)

    def get_or_create_collection(self, name: str, metadata: Dict[str, Any] | None = None) -> FakeCollection:
        if name not in self.registry:
            self.registry[name] = FakeCollection()
        return self.registry[name]


def build_payload() -> Dict[str, Any]:
    """Create canonical payload fixture for M3 tests."""
    papers = [
        {
            "paper_id": "M3-P1",
            "title": "Replay Memory for Continual Learning",
            "authors": ["Alice", "Bob"],
            "venue": "ICLR",
            "year": 2024,
            "abstract": "We study replay memory methods in continual learning.",
            "doi": None,
            "url": "https://example.org/m3-p1",
            "citation_count": 12,
            "source_provider": "openreview",
            "source_ids": {"openreview_id": "OR-M3-P1"},
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
            "paper_id": "M3-P2",
            "title": "Adaptive Rehearsal Buffers",
            "authors": ["Carol"],
            "venue": "ICLR",
            "year": 2024,
            "abstract": "A replay buffer strategy for stable continual adaptation.",
            "doi": None,
            "url": "https://example.org/m3-p2",
            "citation_count": 8,
            "source_provider": "openreview",
            "source_ids": {"openreview_id": "OR-M3-P2"},
            "keywords": ["replay"],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "oral",
            "institutions": ["Uni B"],
            "record_status": "resolved",
            "quality_flags": [],
            "collected_at": "2026-02-20T00:00:00+00:00",
        },
        {
            "paper_id": "M3-P3",
            "title": "Continual Learning with Distillation",
            "authors": ["Dan"],
            "venue": "ICML",
            "year": 2023,
            "abstract": "Distillation for continual learning without replay.",
            "doi": None,
            "url": "https://example.org/m3-p3",
            "citation_count": 21,
            "source_provider": "openreview",
            "source_ids": {"openreview_id": "OR-M3-P3"},
            "keywords": ["continual learning"],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "poster",
            "institutions": ["Uni C"],
            "record_status": "resolved",
            "quality_flags": [],
            "collected_at": "2026-02-20T00:00:00+00:00",
        },
        {
            "paper_id": "M3-P4",
            "title": "Placeholder Replay Entry",
            "authors": [],
            "venue": "ICLR",
            "year": 2024,
            "abstract": "",
            "doi": None,
            "url": "https://example.org/m3-p4",
            "citation_count": None,
            "source_provider": "openreview",
            "source_ids": {"openreview_id": "OR-M3-P4"},
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


class TestM3Pipeline(unittest.TestCase):
    """M3 pipeline behavior tests."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.input_root = self.root / "data" / "raw"
        self.db_path = self.root / "data" / "papers.db"
        self.index_root = self.root / "artifacts"
        self.vectors_root = self.root / "data" / "vectors" / "chroma"
        self.master_index_path = self.root / "artifacts" / "indexes" / "master_index.md"
        self.venues_root = self.root / "venues"
        self.topics_root = self.root / "topics"
        self.subtopics_root = self.root / "subtopics"
        self.collection_name = "papers_v1"

        (self.input_root / "iclr").mkdir(parents=True, exist_ok=True)
        payload = build_payload()
        with (self.input_root / "iclr" / "2024.json").open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        run_load(input_root=self.input_root, db_path=self.db_path, index_root=self.index_root)

        self.registry: Dict[str, FakeCollection] = {}
        self.label_counter = {"topic": 0, "subtopic": 0}
        self.raw_path = self.input_root / "iclr" / "2024.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _patch_m3(self) -> List[Any]:
        """Create all patches required for isolated M3 tests."""
        def fake_make_chroma_client(_vectors_root: Path) -> FakeChromaClient:
            return FakeChromaClient(self.registry)

        def fake_embed_batch(
            _client: Any,
            _model: str,
            texts: List[str],
            _timeout_seconds: float,
        ) -> List[List[float]]:
            return [_vectorize(text) for text in texts]

        def fake_make_embedding_client(base_url: str, api_key: str | None = None) -> object:
            _ = base_url
            _ = api_key
            return object()

        def fake_make_llm_client(base_url: str, api_key: str | None = None) -> object:
            _ = base_url
            _ = api_key
            return object()

        def fake_generate_topic_label(
            *,
            client: Any,
            model: str,
            level: str,
            sample_titles: List[str],
            parent_topic: str | None = None,
        ) -> Dict[str, str]:
            _ = client
            _ = model
            _ = parent_topic
            self.label_counter[level] += 1
            prefix = "Topic" if level == "topic" else "Subtopic"
            base = re.sub(r"[^a-zA-Z0-9]+", " ", sample_titles[0]).strip()[:24] or "Cluster"
            return {
                "name": f"{prefix} {self.label_counter[level]} {base}",
                "description": f"{prefix} description for {base}",
            }

        return [
            patch("tools.m3_pipeline.make_chroma_client", side_effect=fake_make_chroma_client),
            patch("tools.m3_pipeline.make_embedding_client", side_effect=fake_make_embedding_client),
            patch("tools.m3_pipeline.embed_batch", side_effect=fake_embed_batch),
            patch("tools.m3_pipeline.make_llm_client", side_effect=fake_make_llm_client),
            patch("tools.m3_pipeline.generate_topic_label", side_effect=fake_generate_topic_label),
            patch(
                "tools.m3_pipeline.check_chroma_sqlite_integrity",
                return_value={
                    "pass": True,
                    "db_path": str(self.vectors_root / "chroma.sqlite3"),
                    "quick_check": ["ok"],
                    "error": None,
                },
            ),
        ]

    def test_chroma_sqlite_integrity_check(self) -> None:
        self.vectors_root.mkdir(parents=True, exist_ok=True)
        chroma_db = self.vectors_root / "chroma.sqlite3"
        conn = sqlite3.connect(chroma_db)
        try:
            conn.execute("CREATE VIRTUAL TABLE embedding_fulltext_search USING fts5(text)")
            conn.execute("INSERT INTO embedding_fulltext_search (text) VALUES ('healthy index')")
            conn.commit()
        finally:
            conn.close()

        result = check_chroma_sqlite_integrity(self.vectors_root)
        self.assertTrue(result["pass"])
        self.assertEqual(result["quick_check"], ["ok"])

    def test_build_vectors_success(self) -> None:
        patches = self._patch_m3()
        for item in patches:
            item.start()
        try:
            payload = run_build_vectors(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name=self.collection_name,
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                embed_batch_size=32,
                embed_timeout_seconds=60.0,
                embed_cooldown_seconds=0.0,
                exclude_placeholder=True,
                embed_api_key=None,
            )
        finally:
            for item in reversed(patches):
                item.stop()

        self.assertEqual(payload["summary"]["db_candidate_count"], 3)
        self.assertEqual(payload["summary"]["embedded_count"], 3)
        self.assertEqual(payload["summary"]["collection_count"], 3)

    def test_topics_assignments_full_and_unique(self) -> None:
        patches = self._patch_m3()
        for item in patches:
            item.start()
        try:
            run_build_vectors(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name=self.collection_name,
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                embed_batch_size=32,
                embed_timeout_seconds=60.0,
                embed_cooldown_seconds=0.0,
                exclude_placeholder=True,
                embed_api_key=None,
            )
            topics_payload = run_build_topics(
                vectors_root=self.vectors_root,
                collection_name=self.collection_name,
                index_root=self.index_root,
                llm_base_url="https://api.siliconflow.cn/v1",
                llm_model="Qwen/Qwen3-8B",
                llm_api_key="test-key",
            )
        finally:
            for item in reversed(patches):
                item.stop()

        assignments = topics_payload["assignments"]
        self.assertEqual(len(assignments), 3)
        self.assertEqual(len({item["paper_id"] for item in assignments}), 3)
        self.assertGreaterEqual(topics_payload["summary"]["topic_count"], 1)
        self.assertGreaterEqual(topics_payload["summary"]["subtopic_count"], 1)

    def test_build_vectors_uses_source_file_marker_and_force_rebuild(self) -> None:
        patches = self._patch_m3()
        for item in patches:
            item.start()
        try:
            first = run_build_vectors(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name=self.collection_name,
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                embed_batch_size=32,
                embed_timeout_seconds=60.0,
                embed_cooldown_seconds=0.0,
                exclude_placeholder=True,
                embed_api_key=None,
            )
            second = run_build_vectors(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name=self.collection_name,
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                embed_batch_size=32,
                embed_timeout_seconds=60.0,
                embed_cooldown_seconds=0.0,
                exclude_placeholder=True,
                embed_api_key=None,
            )
            third = run_build_vectors(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name=self.collection_name,
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                embed_batch_size=32,
                embed_timeout_seconds=60.0,
                embed_cooldown_seconds=0.0,
                exclude_placeholder=True,
                embed_api_key=None,
                force_rebuild_vectors=True,
            )
        finally:
            for item in reversed(patches):
                item.stop()

        self.assertEqual(first["summary"]["embedded_count"], 3)
        self.assertEqual(second["summary"]["embedded_count"], 0)
        self.assertEqual(second["summary"]["source_files_skipped_by_marker"], 1)
        self.assertEqual(third["summary"]["embedded_count"], 3)
        self.assertTrue(third["force_rebuild_vectors"])

    def test_build_vectors_embeds_only_missing_or_changed_papers(self) -> None:
        patches = self._patch_m3()
        for item in patches:
            item.start()
        try:
            first = run_build_vectors(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name=self.collection_name,
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                embed_batch_size=32,
                embed_timeout_seconds=60.0,
                embed_cooldown_seconds=0.0,
                exclude_placeholder=True,
                embed_api_key=None,
            )

            payload = json.loads(self.raw_path.read_text(encoding="utf-8"))
            payload["papers"][1]["abstract"] = (
                "An updated replay buffer strategy for stable continual adaptation."
            )
            new_paper = dict(payload["papers"][0])
            new_paper.update(
                {
                    "paper_id": "M3-P5",
                    "title": "Prototype Replay for Continual Learning",
                    "abstract": "Prototype replay reduces memory use in continual learning.",
                    "url": "https://example.org/m3-p5",
                    "source_ids": {"openreview_id": "OR-M3-P5"},
                }
            )
            payload["papers"].append(new_paper)
            payload["count"] = len(payload["papers"])
            payload["metrics"]["total"] = len(payload["papers"])
            payload["metrics"]["resolved_total"] = 4
            self.raw_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            run_load(input_root=self.input_root, db_path=self.db_path, index_root=self.index_root)

            second = run_build_vectors(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name=self.collection_name,
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                embed_batch_size=32,
                embed_timeout_seconds=60.0,
                embed_cooldown_seconds=0.0,
                exclude_placeholder=True,
                embed_api_key=None,
            )

            payload["papers"] = [
                paper for paper in payload["papers"] if paper["paper_id"] != "M3-P3"
            ]
            payload["count"] = len(payload["papers"])
            payload["metrics"]["total"] = len(payload["papers"])
            payload["metrics"]["resolved_total"] = 3
            self.raw_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            run_load(input_root=self.input_root, db_path=self.db_path, index_root=self.index_root)

            third = run_build_vectors(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name=self.collection_name,
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                embed_batch_size=32,
                embed_timeout_seconds=60.0,
                embed_cooldown_seconds=0.0,
                exclude_placeholder=True,
                embed_api_key=None,
            )
        finally:
            for item in reversed(patches):
                item.stop()

        self.assertEqual(first["summary"]["embedded_count"], 3)
        self.assertEqual(second["summary"]["embedded_count"], 2)
        self.assertEqual(second["summary"]["embedded_missing_count"], 1)
        self.assertEqual(second["summary"]["reembedded_changed_count"], 1)
        self.assertEqual(second["summary"]["skipped_existing_verified_count"], 2)
        self.assertEqual(third["summary"]["embedded_count"], 0)
        self.assertEqual(third["summary"]["deleted_stale_vector_count"], 1)
        self.assertEqual(third["summary"]["collection_count"], 3)

    def test_build_cache_generates_l1_l4(self) -> None:
        patches = self._patch_m3()
        for item in patches:
            item.start()
        try:
            run_build_vectors(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name=self.collection_name,
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                embed_batch_size=32,
                embed_timeout_seconds=60.0,
                embed_cooldown_seconds=0.0,
                exclude_placeholder=True,
                embed_api_key=None,
            )
            topic_payload = run_build_topics(
                vectors_root=self.vectors_root,
                collection_name=self.collection_name,
                index_root=self.index_root,
                llm_base_url="https://api.siliconflow.cn/v1",
                llm_model="Qwen/Qwen3-8B",
                llm_api_key="test-key",
            )
            cache_payload = run_build_cache(
                db_path=self.db_path,
                index_root=self.index_root,
                master_index_path=self.master_index_path,
                venues_root=self.venues_root,
                topics_root=self.topics_root,
                subtopics_root=self.subtopics_root,
            )
            validate_payload = run_validate(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name=self.collection_name,
                index_root=self.index_root,
                master_index_path=self.master_index_path,
                venues_root=self.venues_root,
                topics_root=self.topics_root,
                subtopics_root=self.subtopics_root,
                exclude_placeholder=True,
            )
        finally:
            for item in reversed(patches):
                item.stop()

        self.assertTrue(self.master_index_path.exists())
        self.assertTrue((self.topics_root / "_topic_index.md").exists())
        self.assertGreaterEqual(cache_payload["summary"]["venue_page_count"], 1)
        self.assertGreaterEqual(cache_payload["summary"]["topic_page_count"], 1)
        self.assertGreaterEqual(cache_payload["summary"]["subtopic_overview_count"], 1)
        self.assertGreaterEqual(cache_payload["summary"]["subtopic_page_count"], 1)

        for topic in topic_payload["topics"]:
            topic_slug = topic["topic_slug"]
            self.assertTrue((self.topics_root / f"{topic_slug}.md").exists())
            self.assertTrue((self.subtopics_root / topic_slug / "_overview.md").exists())

        self.assertTrue(validate_payload["summary"]["all_pass"])

    def test_build_topics_hard_fail_when_llm_unavailable(self) -> None:
        patches = self._patch_m3()
        for item in patches:
            item.start()
        fail_patch = patch(
            "tools.m3_pipeline.generate_topic_label",
            side_effect=RuntimeError("llm unavailable"),
        )
        fail_patch.start()
        try:
            run_build_vectors(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name=self.collection_name,
                embed_base_url="http://127.0.0.1:1234/v1",
                embed_model="text-embedding-qwen3-embedding-8b",
                embed_batch_size=32,
                embed_timeout_seconds=60.0,
                embed_cooldown_seconds=0.0,
                exclude_placeholder=True,
                embed_api_key=None,
            )
            with self.assertRaises(RuntimeError):
                run_build_topics(
                    vectors_root=self.vectors_root,
                    collection_name=self.collection_name,
                    index_root=self.index_root,
                    llm_base_url="https://api.siliconflow.cn/v1",
                    llm_model="Qwen/Qwen3-8B",
                    llm_api_key="test-key",
                )
        finally:
            fail_patch.stop()
            for item in reversed(patches):
                item.stop()


if __name__ == "__main__":
    unittest.main()
