#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for conference-specific collectors."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.aaai_collect import build_openreview_paper_record
from tools.acl_collect import fetch_text as fetch_acl_text
from tools.cvpr_collect import collect_one_year


class TestCollectors(unittest.TestCase):
    """Cover collector failure paths that previously escaped the test suite."""

    def test_openaccess_collection_returns_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            with patch("tools.cvpr_collect.fetch_text", return_value="<html></html>"):
                summary = collect_one_year(
                    venue="ICCV",
                    year=2025,
                    output_root=output_root,
                    timeout=1.0,
                    retries=1,
                    min_interval=0.0,
                    fetch_abstracts=False,
                    workers=1,
                    eccv_index_file=None,
                    source_mode="openaccess",
                )

            self.assertEqual(summary["provider"], "cvf_openaccess")
            self.assertTrue((output_root / "ICCV-25.json").exists())

    def test_acl_curl_fails_on_http_errors(self) -> None:
        error = subprocess.CalledProcessError(returncode=22, cmd=["curl"])
        with patch("tools.acl_collect.subprocess.run", side_effect=error) as run_mock:
            with self.assertRaises(RuntimeError):
                fetch_acl_text(
                    "https://aclanthology.org/missing",
                    timeout=1.0,
                    retries=2,
                    min_interval=0.0,
                )

        self.assertEqual(run_mock.call_count, 2)
        command = run_mock.call_args.args[0]
        self.assertIn("--fail-with-body", command)

    def test_aaai_openreview_pdf_uses_openreview_domain(self) -> None:
        note = {
            "id": "note-1",
            "forum": "forum-1",
            "content": {
                "title": {"value": "Test Paper"},
                "authors": {"value": ["Alice"]},
                "abstract": {"value": "Test abstract"},
                "pdf": {"value": "/pdf?id=note-1"},
            },
        }
        record = build_openreview_paper_record(
            note,
            year=2026,
            collected_at="2026-08-06T00:00:00+00:00",
        )

        expected = "https://openreview.net/pdf?id=note-1"
        self.assertEqual(record["external_url"], expected)
        self.assertEqual(record["source_ids"]["openreview_pdf_url"], expected)


if __name__ == "__main__":
    unittest.main()
