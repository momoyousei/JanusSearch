#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for M1 pipeline helpers."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.m1_pipeline import (
    FileContext,
    backfill_from_papers_cool,
    backfill_records,
    canonicalize_doi,
    dedupe_records,
    is_placeholder_record,
    normalize_title,
    parse_papers_cool_venue_html,
    resolve_icml_pmlr_volume,
    run_migrate_provenance,
    transform_record,
)


class TestM1Pipeline(unittest.TestCase):
    """Core behavior tests for normalization and deduplication."""

    def test_normalize_title(self) -> None:
        self.assertEqual(
            normalize_title("A Simple, Strong Baseline!"),
            "asimplestrongbaseline",
        )

    def test_canonicalize_doi(self) -> None:
        self.assertEqual(
            canonicalize_doi("https://doi.org/10.1109/CVPR.2016.90"),
            "10.1109/CVPR.2016.90",
        )
        self.assertIsNone(canonicalize_doi(""))

    def test_icml_pmlr_volume_mapping(self) -> None:
        self.assertEqual(resolve_icml_pmlr_volume(2021), "v139")
        self.assertIsNone(resolve_icml_pmlr_volume(2019))

    def test_placeholder_detection(self) -> None:
        placeholder = {
            "paper_title": "X",
            "authors": [],
            "abstract": "",
            "external_only": True,
        }
        resolved = {
            "paper_title": "Y",
            "authors": ["A"],
            "abstract": "",
        }
        self.assertTrue(is_placeholder_record(placeholder))
        self.assertFalse(is_placeholder_record(resolved))

    def test_dedupe_prefers_complete_record(self) -> None:
        base = {
            "paper_title": "Adaptive Machine Unlearning",
            "authors": ["Author A"],
            "institutions": [],
            "abstract": "full abstract",
            "keywords": ["k1"],
            "doi": None,
            "openreview_id": "abc",
            "openalex_id": None,
            "external_only": False,
            "quality_flags": [],
        }
        duplicate = {
            "paper_title": "Adaptive Machine Unlearning",
            "authors": [],
            "institutions": [],
            "abstract": "",
            "keywords": [],
            "doi": None,
            "openreview_id": "def",
            "openalex_id": None,
            "external_only": True,
            "quality_flags": [],
        }
        deduped, removed = dedupe_records([base, duplicate])
        self.assertEqual(removed, 1)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["abstract"], "full abstract")
        self.assertEqual(deduped[0]["authors"], ["Author A"])

    def test_transform_record_populates_default_field_provenance(self) -> None:
        context = FileContext(
            path=Path("ACL-24.json"),
            venue="ACL",
            year=2024,
            provider="acl_anthology",
            generated_at="2026-04-05T00:00:00+00:00",
            target="ACL-2024",
        )
        record = transform_record(
            {
                "paper_title": "A Test Paper",
                "authors": ["Alice"],
                "abstract": "Abstract",
                "url": "https://aclanthology.org/2024.acl-long.1/",
                "track": "conference",
                "track_group": "main",
                "presentation_level": "poster",
                "source_provider": "acl_anthology",
            },
            context,
        )
        self.assertEqual(
            record["field_provenance"],
            {
                "abstract": "official",
                "authors": "official",
                "url": "official",
                "track_group": "official",
                "presentation_level": "official",
            },
        )

    @patch("tools.m1_pipeline.backfill_from_pmlr", return_value=False)
    def test_backfill_metadata_only_does_not_mark_abstract_repaired(
        self,
        _pmlr_mock,
    ) -> None:
        class FakeSemanticScholarClient:
            @staticmethod
            def lookup_by_doi(_doi: str) -> dict:
                return {
                    "title": "Target Paper",
                    "abstract": None,
                    "citationCount": 7,
                    "paperId": "s2-target",
                }

            @staticmethod
            def search_by_title(_title: str) -> dict:
                return {}

        record = {
            "paper_title": "Target Paper",
            "title": "Target Paper",
            "abstract": "",
            "authors": ["Alice"],
            "doi": "10.1000/target",
            "url": "https://example.org/target",
            "record_status": "resolved",
            "source_ids": {},
            "quality_flags": ["missing_abstract"],
        }
        stats = backfill_records(
            [record],
            venue="TEST",
            year=2025,
            client=FakeSemanticScholarClient(),
            timeout=1.0,
            retries=1,
            max_records=0,
            enable_arxiv_title=False,
            enable_papers_cool=False,
            papers_cool_policy="full_fields",
        )

        self.assertEqual(record["abstract"], "")
        self.assertEqual(record["record_status"], "resolved")
        self.assertEqual(stats["updated_records"], 0)
        self.assertEqual(stats["failed_records"], 1)

    def test_migrate_provenance_preserves_other_record_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical_root = root / "data" / "raw"
            target = canonical_root / "iclr" / "2024.json"
            report_path = root / "artifacts" / "m1" / "provenance.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "paper_id": "P1",
                "title": "A Test Paper",
                "authors": ["Alice"],
                "venue": "ICLR",
                "year": 2024,
                "abstract": "Abstract",
                "doi": None,
                "url": "https://example.org/p1",
                "citation_count": 1,
                "source_provider": "openreview",
                "source_ids": {"openreview_id": "P1"},
                "keywords": [],
                "track": "conference",
                "track_display_name": "Conference",
                "track_group": "main",
                "presentation_level": "poster",
                "institutions": [],
                "record_status": "resolved",
                "quality_flags": [],
                "collected_at": "2026-08-06T00:00:00+00:00",
            }
            payload = {
                "venue": "ICLR",
                "year": 2024,
                "count": 1,
                "papers": [record],
            }
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            before = copy.deepcopy(payload)

            report = run_migrate_provenance(canonical_root, report_path)
            after = json.loads(target.read_text(encoding="utf-8"))

            provenance = after["papers"][0].pop("field_provenance")
            self.assertEqual(after, before)
            self.assertEqual(
                provenance,
                {
                    "abstract": "official",
                    "authors": "official",
                    "url": "official",
                    "track_group": "official",
                    "presentation_level": "official",
                },
            )
            self.assertEqual(report["summary"]["updated_file_count"], 1)
            self.assertEqual(report["summary"]["updated_record_count"], 1)

    def test_parse_papers_cool_venue_html_acl(self) -> None:
        html = """
        <div id="2024.acl-long.1@ACL" class="panel paper">
          <h2 class="title">
            <a href="https://aclanthology.org/2024.acl-long.1/" target="_blank" title="1/1"><span>#1</span></a>
            <a id="title-2024.acl-long.1@ACL" class="title-link notranslate" href="/venue/2024.acl-long.1@ACL" target="_blank">Quantized Side Tuning</a>
            <a id="pdf-2024.acl-long.1@ACL" class="title-pdf notranslate" data="https://aclanthology.org/2024.acl-long.1.pdf">[PDF]</a>
          </h2>
          <p id="authors-2024.acl-long.1@ACL" class="metainfo authors notranslate"><strong>Authors</strong>:
            <a class="author notranslate" href="https://www.google.com/search?q=Alice" target="_blank">Alice</a>,
            <a class="author notranslate" href="https://www.google.com/search?q=Bob" target="_blank">Bob</a>
          </p>
          <p id="summary-2024.acl-long.1@ACL" class="summary notranslate">An abstract.</p>
          <p id="subjects-2024.acl-long.1@ACL" class="metainfo subjects"><strong>Subject</strong>:
            <a class="subject-1" href="/venue/ACL.2024?group=Long Papers" target="_blank">ACL.2024 - Long Papers</a>
          </p>
          <hr id="fold-2024.acl-long.1@ACL"></hr>
        </div>
        """
        parsed = parse_papers_cool_venue_html(html, venue="ACL")
        self.assertIn("quantizedsidetuning", parsed)
        entry = parsed["quantizedsidetuning"]
        self.assertEqual(entry["official_url"], "https://aclanthology.org/2024.acl-long.1/")
        self.assertEqual(entry["pdf_url"], "https://aclanthology.org/2024.acl-long.1.pdf")
        self.assertEqual(entry["authors"], ["Alice", "Bob"])
        self.assertEqual(entry["abstract"], "An abstract.")
        self.assertEqual(entry["subject"], "ACL.2024 - Long Papers")

    @patch("tools.m1_pipeline.resolve_papers_cool_entry_by_title")
    def test_backfill_from_papers_cool_updates_missing_fields_only(self, resolve_mock) -> None:
        resolve_mock.return_value = {
            "title": "Quantized Side Tuning",
            "page_url": "https://papers.cool/venue/2024.acl-long.1@ACL",
            "official_url": "https://aclanthology.org/2024.acl-long.1/",
            "pdf_url": "https://aclanthology.org/2024.acl-long.1.pdf",
            "authors": ["Alice", "Bob"],
            "abstract": "Recovered abstract",
            "subject": "ACL.2024 - Long Papers",
        }
        record = {
            "paper_title": "Quantized Side Tuning",
            "title": "Quantized Side Tuning",
            "authors": [],
            "abstract": "",
            "url": "",
            "track": "conference",
            "track_group": "main",
            "presentation_level": "poster",
            "source_provider": "acl_anthology",
            "source_ids": {},
            "quality_flags": [],
        }
        result = backfill_from_papers_cool(
            record=record,
            venue="ACL",
            year=2024,
            timeout=30.0,
            retries=2,
            policy="full_fields",
        )
        self.assertTrue(result["updated"])
        self.assertEqual(record["authors"], ["Alice", "Bob"])
        self.assertEqual(record["abstract"], "Recovered abstract")
        self.assertEqual(record["url"], "https://aclanthology.org/2024.acl-long.1/")
        self.assertEqual(record["field_provenance"]["abstract"], "papers_cool")
        self.assertEqual(record["field_provenance"]["authors"], "papers_cool")
        self.assertEqual(record["field_provenance"]["url"], "papers_cool")
        self.assertEqual(
            record["source_ids"]["papers_cool_page_url"],
            "https://papers.cool/venue/2024.acl-long.1@ACL",
        )
        self.assertIn("aggregator_fallback_papers_cool", record["quality_flags"])

    @patch("tools.m1_pipeline.resolve_papers_cool_entry_by_title")
    def test_backfill_from_papers_cool_does_not_overwrite_existing_fields(self, resolve_mock) -> None:
        resolve_mock.return_value = {
            "title": "Quantized Side Tuning",
            "page_url": "https://papers.cool/venue/2024.acl-long.1@ACL",
            "official_url": "https://aclanthology.org/2024.acl-long.1/",
            "pdf_url": "https://aclanthology.org/2024.acl-long.1.pdf",
            "authors": ["Alice", "Bob"],
            "abstract": "Recovered abstract",
            "subject": "ACL.2024 - Long Papers",
        }
        record = {
            "paper_title": "Quantized Side Tuning",
            "title": "Quantized Side Tuning",
            "authors": ["Existing Author"],
            "abstract": "Existing abstract",
            "url": "https://aclanthology.org/existing",
            "track": "conference",
            "track_group": "main",
            "presentation_level": "poster",
            "source_provider": "acl_anthology",
            "source_ids": {},
            "quality_flags": [],
        }
        result = backfill_from_papers_cool(
            record=record,
            venue="ACL",
            year=2024,
            timeout=30.0,
            retries=2,
            policy="full_fields",
        )
        self.assertFalse(result["updated"])
        self.assertEqual(record["authors"], ["Existing Author"])
        self.assertEqual(record["abstract"], "Existing abstract")
        self.assertEqual(record["url"], "https://aclanthology.org/existing")
        self.assertEqual(record["source_ids"], {})


if __name__ == "__main__":
    unittest.main()
