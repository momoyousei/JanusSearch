#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for conference-specific collectors."""

from __future__ import annotations

import gzip
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from janussearch.collectors.virtual import (
    collect_target,
    dedupe_records,
    fetch_complete_events,
    merge_abstracts,
    pinned_paper_list,
    build_record,
    canonical_openreview_id,
)
from janussearch.infrastructure.http import HttpFetchError, decode_response_body, fetch_response
from tools.aaai_collect import build_openreview_paper_record
from tools.acl_collect import fetch_text as fetch_acl_text
from tools.cvpr_collect import collect_one_year
from tools.pvldb_collect import official_records, parse_next_data, title_key


class TestCollectors(unittest.TestCase):
    """Cover collector failure paths that previously escaped the test suite."""

    def test_openaccess_zero_result_writes_no_paper_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            with patch("tools.cvpr_collect.fetch_text", return_value="<html></html>"):
                with self.assertRaisesRegex(RuntimeError, "returned zero papers"):
                    collect_one_year(
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

            self.assertFalse((output_root / "ICCV-25.json").exists())
            sidecar = json.loads((output_root / ".janus-collection.json").read_text())
            self.assertEqual(sidecar["outcome"], "incomplete_source")

    def test_gzip_magic_is_decoded_without_content_encoding(self) -> None:
        payload = b"<html>AAAI-26 Technical Tracks 1</html>"
        self.assertEqual(decode_response_body(gzip.compress(payload), {}), payload)

    def test_http_403_is_distinct_from_empty_source(self) -> None:
        error = HTTPError(
            "https://example.test/data.json",
            403,
            "Forbidden",
            {},
            io.BytesIO(b"forbidden"),
        )
        with patch("janussearch.infrastructure.http.urlopen", side_effect=error):
            with self.assertRaises(HttpFetchError) as raised:
                fetch_response("https://example.test/data.json", retries=1)
        self.assertEqual(raised.exception.category, "http_forbidden")
        self.assertEqual(raised.exception.status_code, 403)

    def test_virtual_abstracts_are_joined_by_event_id(self) -> None:
        merged = merge_abstracts(
            [{"id": 7, "name": "Paper", "abstract": ""}],
            {"7": "Official abstract"},
        )
        self.assertEqual(merged[0]["abstract"], "Official abstract")

    def test_virtual_duplicate_presentation_keeps_submission_id_and_oral_level(self) -> None:
        poster = {
            "title": "Same paper",
            "openreview_id": "submission-1",
            "abstract": "Abstract",
            "authors": ["Alice"],
            "presentation_level": "poster",
            "source_ids": {"openreview_id": "submission-1"},
        }
        oral = {
            "title": "Same paper",
            "openreview_id": "2026-Oral--1",
            "abstract": "Abstract",
            "authors": ["Alice"],
            "presentation_level": "oral",
            "source_ids": {"openreview_id": "2026-Oral--1"},
        }
        records = dedupe_records([oral, poster])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["openreview_id"], "submission-1")
        self.assertEqual(records[0]["presentation_level"], "oral")

    def test_pinned_icml_payload_uses_papers_member(self) -> None:
        records = pinned_paper_list({"summary": {"count": 1}, "papers": [{"name": "P"}]})
        self.assertEqual(records, [{"name": "P"}])

    def test_pinned_icml_record_fields_are_adapted(self) -> None:
        record = build_record(
            {
                "id": "7",
                "title": "Pinned paper",
                "authors": ["Alice"],
                "institutions": ["Example University"],
                "abstract": "Abstract",
                "decision": "oral",
                "openreview_url": "https://openreview.net/forum?id=forum-7",
                "virtual_url": "https://icml.cc/virtual/2026/poster/7",
                "sourceurl": "https://openreview.net/group?id=ICML.cc/2026/Conference",
            },
            venue="ICML",
            year=2026,
            collected_at="2026-08-07T00:00:00+00:00",
            provider="icml_virtual_pinned_snapshot",
        )
        self.assertEqual(record["openreview_id"], "forum-7")
        self.assertEqual(record["institutions"], ["Example University"])
        self.assertEqual(record["presentation_level"], "oral")

    def test_canonical_openreview_id_accepts_source_ids(self) -> None:
        self.assertEqual(
            canonical_openreview_id({"openreview_id": None, "source_ids": {"openreview_id": "forum-1"}}),
            "forum-1",
        )

    def test_virtual_declared_count_shortfall_is_incomplete(self) -> None:
        payload = {"count": 2, "next": None, "results": [{"id": 1}]}
        with patch("janussearch.collectors.virtual.fetch_json", return_value=payload):
            with self.assertRaisesRegex(RuntimeError, "declared=2 fetched=1"):
                fetch_complete_events(
                    "https://iclr.cc/static/virtual/data/test.json",
                    timeout=1.0,
                    retries=1,
                )

    def test_virtual_zero_source_emits_no_update_without_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "NEURIPS-26.json"
            with patch(
                "janussearch.collectors.virtual.fetch_complete_events",
                return_value=([], {"declared_count": 0, "fetched_count": 0, "pages": 1}),
            ):
                result = collect_target("NEURIPS", 2026, output)
            self.assertEqual(result["outcome"], "no_update")
            self.assertFalse(output.exists())
            sidecar = json.loads((output.parent / ".janus-collection.json").read_text())
            self.assertEqual(sidecar["outcome"], "no_update")

    def test_icml_failed_pinned_fallback_writes_incomplete_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "ICML-26.json"
            with patch(
                "janussearch.collectors.virtual.fetch_complete_events",
                side_effect=RuntimeError("official pagination shortfall"),
            ), patch(
                "janussearch.collectors.virtual.load_icml_pinned_snapshot",
                side_effect=RuntimeError("pinned mismatch"),
            ):
                with self.assertRaisesRegex(RuntimeError, "sources are unusable"):
                    collect_target("ICML", 2026, output)
            self.assertFalse(output.exists())
            sidecar = json.loads((output.parent / ".janus-collection.json").read_text())
            self.assertEqual(sidecar["outcome"], "incomplete_source")

    def test_pvldb_next_data_excludes_front_matter(self) -> None:
        page_props = {
            "volumeSummaries": [
                {"Paper Title": "Front Matter", "Author Names": "Editors", "Abstract": ""},
                {
                    "Paper Title": "Paper One",
                    "Author Names": "Alice and Bob",
                    "Abstract": "A complete abstract.",
                    "Paper ID": "vol19/p1-one",
                },
            ],
            "groupedIssues": {
                "1": [
                    {"issue": 1, "title": "Front Matter", "authors": "Editors"},
                    {
                        "issue": 1,
                        "title": "Paper One",
                        "authors": "Alice and Bob",
                        "pdf": "https://example.test/p1.pdf",
                        "start_page": 1,
                        "end_page": 10,
                    },
                ]
            },
        }
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({"props": {"pageProps": page_props}})
            + "</script>"
        )
        parsed = parse_next_data(html)
        records = official_records(parsed, minimum_count=1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "Paper One")
        self.assertEqual(records[0]["authors"], ["Alice", "Bob"])

    def test_pvldb_title_key_ignores_trailing_publication_punctuation(self) -> None:
        self.assertEqual(title_key("Balancing the Blend:"), title_key("Balancing the Blend"))

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
