#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for M4 validation CLI and evaluation logic."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tools.m2_db import run_load
from tools.m4_validate import (
    build_arg_parser,
    build_sampled_queries,
    run_fixed_suite,
    run_m4,
    run_status,
)


def build_payload(venue: str, year: int, papers: list[dict]) -> dict:
    """Build canonical source payload for DB ingestion tests."""
    total = len(papers)
    placeholder_total = sum(1 for paper in papers if paper.get("record_status") == "placeholder")
    resolved_total = total - placeholder_total
    return {
        "venue": venue,
        "year": year,
        "collected_at": "2026-02-21T00:00:00+00:00",
        "source": "openreview",
        "count": total,
        "metrics": {
            "total": total,
            "resolved_total": resolved_total,
            "placeholder_total": placeholder_total,
            "full_authors_coverage": 100.0,
            "full_abstract_coverage": 100.0,
            "resolved_authors_coverage": 100.0,
            "resolved_abstract_coverage": 100.0,
            "duplicate_title_count": 0,
        },
        "papers": papers,
    }


def paper_item(
    *,
    paper_id: str,
    title: str,
    venue: str,
    year: int,
    abstract: str = "Test abstract",
) -> dict:
    """Build one canonical paper record."""
    return {
        "paper_id": paper_id,
        "title": title,
        "authors": ["Alice", "Bob"],
        "venue": venue,
        "year": year,
        "abstract": abstract,
        "doi": None,
        "url": f"https://example.org/{paper_id.lower()}",
        "citation_count": 5,
        "source_provider": "openreview",
        "source_ids": {"openreview_id": f"OR-{paper_id}"},
        "keywords": ["continual learning", "replay"],
        "track": "conference",
        "track_display_name": "Conference",
        "track_group": "main",
        "presentation_level": "poster",
        "institutions": ["Test University"],
        "record_status": "resolved",
        "quality_flags": [],
        "collected_at": "2026-02-21T00:00:00+00:00",
    }


class TestM4Validate(unittest.TestCase):
    """Coverage tests for M4 validation behavior."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.input_root = self.root / "data" / "raw"
        self.index_root = self.root / "index"
        self.db_path = self.root / "data" / "papers.db"
        self.vectors_root = self.root / "data" / "vectors" / "chroma"
        self.topics_file = self.index_root / "m3_topic_assignments.json"
        self.fixed_query_file = self.root / "docs" / "fixtures" / "m4_fixed_queries.yaml"
        self.output_json = self.index_root / "m4_eval_report.json"
        self.output_md = self.index_root / "m4_eval_report.md"
        self.sampled_dump = self.index_root / "m4_sampled_queries.json"

        (self.input_root / "iclr").mkdir(parents=True, exist_ok=True)
        (self.input_root / "icml").mkdir(parents=True, exist_ok=True)
        (self.input_root / "neurips").mkdir(parents=True, exist_ok=True)
        self.vectors_root.mkdir(parents=True, exist_ok=True)
        self.fixed_query_file.parent.mkdir(parents=True, exist_ok=True)
        self.index_root.mkdir(parents=True, exist_ok=True)

        iclr_payload = build_payload(
            "ICLR",
            2022,
            [
                paper_item(
                    paper_id="P-ICLR-MEMORY",
                    title="Memory Replay with Data Compression for Continual Learning",
                    venue="ICLR",
                    year=2022,
                )
            ],
        )
        icml_payload = build_payload(
            "ICML",
            2024,
            [
                paper_item(
                    paper_id="P-ICML-LAYERWISE",
                    title="Layerwise Proximal Replay: A Proximal Point Method for Online Continual Learning",
                    venue="ICML",
                    year=2024,
                )
            ],
        )
        neurips_payload = build_payload(
            "NEURIPS",
            2022,
            [
                paper_item(
                    paper_id="P-NEURIPS-RAR",
                    title="A simple but strong baseline for online continual learning: Repeated Augmented Rehearsal",
                    venue="NEURIPS",
                    year=2022,
                )
            ],
        )

        with (self.input_root / "iclr" / "2022.json").open("w", encoding="utf-8") as handle:
            json.dump(iclr_payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        with (self.input_root / "icml" / "2024.json").open("w", encoding="utf-8") as handle:
            json.dump(icml_payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        with (self.input_root / "neurips" / "2022.json").open("w", encoding="utf-8") as handle:
            json.dump(neurips_payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

        run_load(input_root=self.input_root, db_path=self.db_path, index_root=self.index_root)

        topics_payload = {
            "summary": {"paper_count": 3, "topic_count": 2, "subtopic_count": 3},
            "topics": [
                {
                    "topic_slug": "continual_learning",
                    "topic_name": "Continual Learning",
                    "subtopics": [
                        {
                            "subtopic_slug": "replay_methods",
                            "subtopic_name": "Replay Methods",
                        },
                        {
                            "subtopic_slug": "regularization_methods",
                            "subtopic_name": "Regularization Methods",
                        },
                    ],
                },
                {
                    "topic_slug": "optimization",
                    "topic_name": "Optimization",
                    "subtopics": [
                        {
                            "subtopic_slug": "gradient_methods",
                            "subtopic_name": "Gradient Methods",
                        }
                    ],
                },
            ],
        }
        self.topics_file.write_text(
            json.dumps(topics_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        fixed_payload = {
            "cases": [
                {
                    "case_id": "fixed_search_basic",
                    "mode": "search",
                    "query": "continual learning replay",
                    "filters": {"venue": ["ICLR", "ICML", "NEURIPS"]},
                    "expect_min_results": 1,
                    "expect_any_title_fragments": ["replay"],
                }
            ]
        }
        self.fixed_query_file.write_text(
            yaml.safe_dump(fixed_payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_run_fails_without_key(self) -> None:
        with patch.dict("os.environ", {"JANUS_EMBED_API_KEY": "", "JANUS_LLM_API_KEY": ""}, clear=False):
            with self.assertRaises(ValueError):
                run_m4(
                    db_path=self.db_path,
                    vectors_root=self.vectors_root,
                    collection_name="papers_v1",
                    topics_file=self.topics_file,
                    fixed_query_file=self.fixed_query_file,
                    embed_base_url="https://api.siliconflow.cn/v1/embeddings",
                    embed_model="Qwen/Qwen3-Embedding-8B",
                    embed_api_key=None,
                    sample_topics=2,
                    sample_per_topic=1,
                    sample_seed=42,
                    top_k=20,
                    output_json=self.output_json,
                    output_md=self.output_md,
                    sampled_dump=self.sampled_dump,
                )

    def test_run_fails_when_online_gate_fails(self) -> None:
        with patch("tools.m4_validate.run_online_healthcheck", return_value={"pass": False, "error": "boom"}):
            report = run_m4(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name="papers_v1",
                topics_file=self.topics_file,
                fixed_query_file=self.fixed_query_file,
                embed_base_url="https://api.siliconflow.cn/v1/embeddings",
                embed_model="Qwen/Qwen3-Embedding-8B",
                embed_api_key="sk-test",
                sample_topics=2,
                sample_per_topic=1,
                sample_seed=42,
                top_k=20,
                output_json=self.output_json,
                output_md=self.output_md,
                sampled_dump=self.sampled_dump,
            )
        self.assertFalse(report["summary"]["overall_pass"])
        self.assertEqual(report["fixed_suite"]["total_cases"], 0)
        self.assertTrue(self.output_json.exists())
        self.assertTrue(self.output_md.exists())
        self.assertTrue(self.sampled_dump.exists())

    def test_fixed_suite_expectation_logic(self) -> None:
        cases = [
            {
                "case_id": "ok_case",
                "mode": "search",
                "query": "replay",
                "filters": {},
                "top_k": 10,
                "expect_min_results": 2,
                "expect_any_title_fragments": ["Replay"],
                "expect_all_title_fragments": ["Continual"],
            },
            {
                "case_id": "bad_case",
                "mode": "hybrid",
                "query": "missing",
                "filters": {},
                "top_k": 10,
                "expect_min_results": 1,
                "expect_any_title_fragments": ["NotFound"],
            },
        ]
        with (
            patch(
                "tools.m4_validate.run_search",
                return_value={
                    "total": 2,
                    "results": [
                        {"title": "Replay Methods for Continual Learning"},
                        {"title": "Another Replay Baseline"},
                    ],
                },
            ),
            patch(
                "tools.m4_validate.run_hybrid",
                return_value={"total": 1, "results": [{"title": "Unrelated Paper"}]},
            ),
        ):
            report = run_fixed_suite(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name="papers_v1",
                cases=cases,
                default_top_k=20,
                embed_base_url="https://api.siliconflow.cn/v1/embeddings",
                embed_model="Qwen/Qwen3-Embedding-8B",
                embed_api_key="sk-test",
            )
        self.assertEqual(report["total_cases"], 2)
        self.assertEqual(report["passed_cases"], 1)
        self.assertFalse(report["all_pass"])
        self.assertTrue(report["cases"][0]["pass"])
        self.assertFalse(report["cases"][1]["pass"])

    def test_sampled_queries_reproducible(self) -> None:
        first = build_sampled_queries(
            topics_file=self.topics_file,
            sample_topics=2,
            sample_per_topic=1,
            seed=42,
            top_k=20,
        )
        second = build_sampled_queries(
            topics_file=self.topics_file,
            sample_topics=2,
            sample_per_topic=1,
            seed=42,
            top_k=20,
        )
        self.assertEqual(first["cases"], second["cases"])
        self.assertGreater(len(first["cases"]), 0)

    def test_report_schema_and_status(self) -> None:
        with (
            patch(
                "tools.m4_validate.run_online_healthcheck",
                return_value={
                    "pass": True,
                    "base_url": "https://api.siliconflow.cn/v1",
                    "model": "Qwen/Qwen3-Embedding-8B",
                    "embedding_dim": 4096,
                    "latency_ms": 120,
                    "error": None,
                },
            ),
            patch(
                "tools.m4_validate.run_fixed_suite",
                return_value={
                    "total_cases": 1,
                    "passed_cases": 1,
                    "pass_rate": 1.0,
                    "all_pass": True,
                    "cases": [],
                },
            ),
            patch(
                "tools.m4_validate.run_sampled_suite",
                return_value={
                    "total_cases": 2,
                    "passed_cases": 2,
                    "pass_rate": 1.0,
                    "threshold": 0.9,
                    "pass_threshold": True,
                    "cases": [],
                },
            ),
        ):
            report = run_m4(
                db_path=self.db_path,
                vectors_root=self.vectors_root,
                collection_name="papers_v1",
                topics_file=self.topics_file,
                fixed_query_file=self.fixed_query_file,
                embed_base_url="https://api.siliconflow.cn/v1/embeddings",
                embed_model="Qwen/Qwen3-Embedding-8B",
                embed_api_key="sk-test",
                sample_topics=2,
                sample_per_topic=1,
                sample_seed=42,
                top_k=20,
                output_json=self.output_json,
                output_md=self.output_md,
                sampled_dump=self.sampled_dump,
            )
        self.assertTrue(report["summary"]["overall_pass"])
        self.assertIn("summary", report)
        self.assertIn("online_gate", report)
        self.assertIn("fixed_suite", report)
        self.assertIn("sampled_suite", report)
        self.assertTrue(self.output_json.exists())
        self.assertTrue(self.output_md.exists())
        self.assertTrue(self.sampled_dump.exists())

        loaded = run_status(self.output_json)
        self.assertTrue(loaded["summary"]["overall_pass"])

    def test_status_missing_report(self) -> None:
        with self.assertRaises(FileNotFoundError):
            run_status(self.root / "index" / "does_not_exist.json")

    def test_cli_accepts_subcommand_first_arguments(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(
            [
                "run",
                "--db-path",
                str(self.db_path),
                "--output-json",
                str(self.output_json),
            ]
        )
        self.assertEqual(args.command, "run")
        self.assertEqual(Path(args.db_path), self.db_path)


if __name__ == "__main__":
    unittest.main()
