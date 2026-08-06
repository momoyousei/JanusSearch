#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for PDF download behavior in tools.search."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools.m2_db import run_load
from tools.search import DEFAULT_PDF_REPORT_NAME, run_download_pdfs


def build_payload() -> dict[str, Any]:
    """Build fixture payload for downloader behavior."""
    papers = [
        {
            "paper_id": "D1",
            "title": "Re-Basin via Implicit Sinkhorn Differentiation",
            "authors": ["Alice"],
            "venue": "CVPR",
            "year": 2023,
            "abstract": "A CVF-hosted paper.",
            "doi": None,
            "url": "https://openaccess.thecvf.com/content/CVPR2023/html/Fake_Paper.html",
            "citation_count": 10,
            "source_provider": "cvf",
            "source_ids": {
                "cvf_pdf_url": "https://example.org/direct.pdf",
            },
            "keywords": ["re-basin"],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "poster",
            "institutions": [],
            "record_status": "resolved",
            "quality_flags": [],
            "collected_at": "2026-03-07T00:00:00+00:00",
        },
        {
            "paper_id": "D2",
            "title": "Linear Mode Connectivity in Multitask and Continual Learning",
            "authors": ["Bob"],
            "venue": "ICLR",
            "year": 2021,
            "abstract": "An OpenReview paper.",
            "doi": None,
            "url": "https://openreview.net/forum?id=OR-D2",
            "citation_count": 20,
            "source_provider": "openreview",
            "source_ids": {
                "openreview_id": "OR-D2",
            },
            "keywords": ["mode connectivity"],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "oral",
            "institutions": [],
            "record_status": "resolved",
            "quality_flags": [],
            "collected_at": "2026-03-07T00:00:00+00:00",
        },
        {
            "paper_id": "D3",
            "title": "Optimizing Mode Connectivity for Class Incremental Learning",
            "authors": ["Carol"],
            "venue": "ICML",
            "year": 2023,
            "abstract": "An arXiv-backed paper.",
            "doi": None,
            "url": "https://arxiv.org/abs/2401.01234",
            "citation_count": 30,
            "source_provider": "arxiv",
            "source_ids": {
                "arxiv_id": "2401.01234",
            },
            "keywords": ["class incremental learning"],
            "track": "conference",
            "track_display_name": "Conference",
            "track_group": "main",
            "presentation_level": "poster",
            "institutions": [],
            "record_status": "resolved",
            "quality_flags": [],
            "collected_at": "2026-03-07T00:00:00+00:00",
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
        "collected_at": "2026-03-07T00:00:00+00:00",
        "source": "mixed",
        "count": len(papers),
        "metrics": {
            "total": len(papers),
            "resolved_total": len(papers),
            "placeholder_total": 0,
            "full_authors_coverage": 100.0,
            "full_abstract_coverage": 100.0,
            "resolved_authors_coverage": 100.0,
            "resolved_abstract_coverage": 100.0,
            "duplicate_title_count": 0,
        },
        "papers": papers,
    }


def write_results_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write a minimal results.tsv for downloader tests."""
    fieldnames = ["paper_id", "title", "url", "source_provider", "source_ids_json"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_tsv(path: Path) -> list[dict[str, str]]:
    """Read TSV rows into dictionaries."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


class TestSearchDownloadPDFs(unittest.TestCase):
    """Behavior tests for tools.search download-pdfs."""

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

    @staticmethod
    def _fake_download(
        *,
        pdf_url: str,
        target_path: Path,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> None:
        _ = timeout
        _ = retries
        if pdf_url.endswith("2401.01234.pdf"):
            raise RuntimeError("simulated download failure")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"%PDF-1.4\n%fake\n")

    def test_download_pdfs_resolves_direct_openreview_and_arxiv_urls(self) -> None:
        output_dir = self.root / "downloads"
        report_json = self.root / "downloads" / DEFAULT_PDF_REPORT_NAME

        with patch("tools.search._download_pdf_file", side_effect=self._fake_download):
            payload = run_download_pdfs(
                db_path=self.db_path,
                input_tsv=None,
                paper_ids=["D1", "D2"],
                output_dir=output_dir,
                report_json=report_json,
                overwrite=False,
            )

        self.assertEqual(payload["input_mode"], "paper_id")
        self.assertEqual(payload["requested_count"], 2)
        self.assertEqual(payload["downloaded_count"], 2)
        self.assertEqual(payload["failed_count"], 0)
        self.assertTrue(report_json.exists())
        self.assertTrue(Path(payload["failed_tsv"]).exists())
        self.assertEqual(read_tsv(Path(payload["failed_tsv"])), [])

        items = {item["paper_id"]: item for item in payload["items"]}
        self.assertEqual(items["D1"]["resolved_pdf_url"], "https://example.org/direct.pdf")
        self.assertEqual(items["D1"]["resolver"], "source_ids:cvf_pdf_url")
        self.assertEqual(items["D2"]["resolved_pdf_url"], "https://openreview.net/pdf?id=OR-D2")
        self.assertEqual(items["D2"]["resolver"], "derived:openreview_id")

        d1_path = Path(items["D1"]["file_path"])
        d2_path = Path(items["D2"]["file_path"])
        self.assertTrue(d1_path.exists())
        self.assertTrue(d2_path.exists())
        self.assertTrue(d1_path.read_bytes().startswith(b"%PDF-"))
        self.assertTrue(d2_path.read_bytes().startswith(b"%PDF-"))

    def test_download_pdfs_from_tsv_uses_default_output_dir_and_skips_existing(self) -> None:
        tsv_path = self.root / "query" / "results.tsv"
        write_results_tsv(
            tsv_path,
            [
                {
                    "paper_id": "D1",
                    "title": "Re-Basin via Implicit Sinkhorn Differentiation",
                    "url": "https://openaccess.thecvf.com/content/CVPR2023/html/Fake_Paper.html",
                    "source_provider": "cvf",
                    "source_ids_json": json.dumps({"cvf_pdf_url": "https://example.org/direct.pdf"}),
                },
                {
                    "paper_id": "D2",
                    "title": "Linear Mode Connectivity in Multitask and Continual Learning",
                    "url": "https://openreview.net/forum?id=OR-D2",
                    "source_provider": "openreview",
                    "source_ids_json": json.dumps({"openreview_id": "OR-D2"}),
                },
            ],
        )
        default_output_dir = tsv_path.parent / "pdfs"
        existing_path = default_output_dir / "D1__re_basin_via_implicit_sinkhorn_differentiation.pdf"
        existing_path.parent.mkdir(parents=True, exist_ok=True)
        existing_path.write_bytes(b"%PDF-1.4\n%existing\n")

        with patch("tools.search._download_pdf_file", side_effect=self._fake_download):
            payload = run_download_pdfs(
                db_path=self.db_path,
                input_tsv=tsv_path,
                paper_ids=["D1", "D2"],
                output_dir=None,
                report_json=None,
                overwrite=False,
            )

        self.assertEqual(payload["input_mode"], "tsv_filtered")
        self.assertEqual(payload["requested_count"], 2)
        self.assertEqual(payload["downloaded_count"], 1)
        self.assertEqual(payload["skipped_existing_count"], 1)
        self.assertEqual(payload["failed_count"], 0)
        self.assertEqual(payload["output_dir"], str(default_output_dir.resolve()))
        self.assertEqual(payload["report_json"], str((default_output_dir / DEFAULT_PDF_REPORT_NAME).resolve()))
        self.assertTrue((default_output_dir / DEFAULT_PDF_REPORT_NAME).exists())
        self.assertTrue(Path(payload["failed_tsv"]).exists())
        self.assertEqual(read_tsv(Path(payload["failed_tsv"])), [])

        items = {item["paper_id"]: item for item in payload["items"]}
        self.assertEqual(items["D1"]["status"], "skipped_existing")
        self.assertEqual(items["D2"]["status"], "downloaded")
        self.assertTrue(Path(items["D2"]["file_path"]).exists())

    def test_download_pdfs_reports_missing_ids_and_continues_after_failures(self) -> None:
        tsv_path = self.root / "query" / "results.tsv"
        write_results_tsv(
            tsv_path,
            [
                {
                    "paper_id": "D1",
                    "title": "Re-Basin via Implicit Sinkhorn Differentiation",
                    "url": "https://openaccess.thecvf.com/content/CVPR2023/html/Fake_Paper.html",
                    "source_provider": "cvf",
                    "source_ids_json": json.dumps({"cvf_pdf_url": "https://example.org/direct.pdf"}),
                },
                {
                    "paper_id": "D3",
                    "title": "Optimizing Mode Connectivity for Class Incremental Learning",
                    "url": "https://arxiv.org/abs/2401.01234",
                    "source_provider": "arxiv",
                    "source_ids_json": json.dumps({"arxiv_id": "2401.01234"}),
                },
            ],
        )

        with patch("tools.search._download_pdf_file", side_effect=self._fake_download):
            payload = run_download_pdfs(
                db_path=self.db_path,
                input_tsv=tsv_path,
                paper_ids=["D1", "D3", "D404"],
                output_dir=self.root / "downloads",
                report_json=self.root / "downloads" / "report.json",
                overwrite=False,
            )

        self.assertEqual(payload["requested_count"], 3)
        self.assertEqual(payload["downloaded_count"], 1)
        self.assertEqual(payload["failed_count"], 2)
        self.assertTrue(Path(payload["report_json"]).exists())
        self.assertTrue(Path(payload["failed_tsv"]).exists())

        items = {item["paper_id"]: item for item in payload["items"]}
        self.assertEqual(items["D1"]["status"], "downloaded")
        self.assertEqual(items["D3"]["status"], "failed")
        self.assertIn("simulated download failure", items["D3"]["error"])
        self.assertEqual(items["D3"]["resolved_pdf_url"], "https://arxiv.org/pdf/2401.01234.pdf")
        self.assertEqual(items["D3"]["resolver"], "derived:arxiv_id")
        self.assertEqual(items["D404"]["status"], "failed")
        self.assertIn("input TSV", items["D404"]["error"])

        failed_rows = {row["paper_id"]: row for row in read_tsv(Path(payload["failed_tsv"]))}
        self.assertEqual(set(failed_rows), {"D3", "D404"})
        self.assertIn("simulated download failure", failed_rows["D3"]["error"])
        self.assertIn("input TSV", failed_rows["D404"]["error"])


if __name__ == "__main__":
    unittest.main()
