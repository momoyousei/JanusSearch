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
    ApprovedPaginationIncomplete,
    collect_official_catalog,
    collect_target,
    dedupe_records,
    fetch_complete_events,
    merge_abstracts,
    build_record,
    canonical_openreview_id,
    parse_official_catalog,
    parse_official_detail,
)
from janussearch.collectors.icde import (
    _build_record as build_icde_record,
    dedupe_tracks as dedupe_icde_tracks,
    parse_accepted_page as parse_icde_accepted_page,
    parse_workshop_proceedings as parse_icde_workshop_proceedings,
)
from janussearch.collectors.ijcai import parse_accepted_2026_cards
from janussearch.collectors.kdd import (
    KDD_2026_ACM_SPECS,
    KDD_2026_OPENREVIEW_GROUPS,
    collect_acm_2026,
    collect_openreview_2026,
    parse_acm_bibtex,
)
from janussearch.infrastructure.http import HttpFetchError, decode_response_body, fetch_response
from janussearch.collectors.outcomes import read_collection_result, write_collection_result
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

    def test_collection_sidecar_v2_validates_scope_and_file_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            paper_file = root / "ACL-25.json"
            paper_file.write_text(json.dumps({"papers": []}), encoding="utf-8")
            path = write_collection_result(
                root,
                outcome="collected",
                venue="ACL",
                years=[2025],
                sources=["https://aclanthology.org/events/acl-2025/"],
                reason="fixture",
            )
            payload = read_collection_result(
                root, expected_venue="ACL", expected_years=[2025]
            )
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["years"], [2025])
            self.assertFalse(path.with_suffix(".json.tmp").exists())
            with self.assertRaisesRegex(ValueError, "year scope mismatch"):
                read_collection_result(root, expected_venue="ACL", expected_years=[2026])
            paper_file.write_text(json.dumps({"papers": [1]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "file hash mismatch"):
                read_collection_result(root, expected_venue="ACL", expected_years=[2025])

    def test_collection_sidecar_rejects_year_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "concrete years"):
                write_collection_result(
                    Path(temp_dir),
                    outcome="no_update",
                    venue="AAAI",
                    year=0,
                    sources=["fixture"],
                    reason="fixture",
                )

    def test_collection_sidecar_keeps_incomplete_multi_year_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_collection_result(
                root,
                outcome="collected",
                venue="AAAI",
                year=2025,
                sources=["fixture-2025"],
                reason="complete",
            )
            write_collection_result(
                root,
                outcome="incomplete_source",
                venue="AAAI",
                year=2026,
                sources=["fixture-2026"],
                reason="incomplete",
            )
            payload = read_collection_result(
                root, expected_venue="AAAI", expected_years=[2025, 2026]
            )
            self.assertEqual(payload["outcome"], "incomplete_source")

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

    def test_official_virtual_record_fields_are_adapted(self) -> None:
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
            provider="official_virtual_catalog",
        )
        self.assertEqual(record["openreview_id"], "forum-7")
        self.assertEqual(record["institutions"], ["Example University"])
        self.assertEqual(record["presentation_level"], "oral")

    def test_virtual_empty_path_does_not_replace_paper_url_with_host(self) -> None:
        record = build_record(
            {
                "id": "8",
                "title": "Paper with direct URL",
                "authors": ["Alice"],
                "abstract": "Abstract",
                "paper_url": "https://openreview.net/forum?id=forum-8",
            },
            venue="ICLR",
            year=2026,
            collected_at="2026-08-07T00:00:00+00:00",
            provider="official",
        )
        self.assertEqual(record["url"], "https://openreview.net/forum?id=forum-8")

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

    def test_virtual_second_page_403_is_approved_pagination_incomplete(self) -> None:
        first = {
            "count": 2,
            "next": "https://icml.cc/static/virtual/data/page-2.json",
            "results": [{"id": 1}],
        }
        forbidden = HttpFetchError(
            url="https://icml.cc/static/virtual/data/page-2.json",
            category="http_forbidden",
            message="Forbidden",
            status_code=403,
        )
        with patch(
            "janussearch.collectors.virtual.fetch_json",
            side_effect=[first, forbidden],
        ):
            with self.assertRaises(ApprovedPaginationIncomplete):
                fetch_complete_events(
                    "https://icml.cc/static/virtual/data/page-1.json",
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

    def test_official_catalog_and_detail_parsers(self) -> None:
        catalog = """
        <ul>
          <li><a href="/virtual/2026/poster/7">Paper Seven</a></li>
          <li><a href="/virtual/2026/poster/8">Paper Eight</a></li>
        </ul>
        """
        stubs = parse_official_catalog(catalog, venue="ICML", year=2026)
        self.assertEqual([item["event_id"] for item in stubs], ["7", "8"])
        detail = parse_official_detail(
            """
            <script type="application/ld+json">{
              "name":"Paper Seven","author":[{"name":"Alice"},{"name":"Bob"}]
            }</script>
            <div class="abstract-text-inner">Official abstract.</div>
            <a href="https://openreview.net/forum?id=forum-7">OpenReview</a>
            <span class="event-type-badge">Oral</span>
            """
        )
        self.assertEqual(detail["authors"], ["Alice", "Bob"])
        self.assertEqual(detail["abstract"], "Official abstract.")
        self.assertEqual(detail["openreview_id"], "forum-7")
        self.assertEqual(detail["event_type"], "Oral")

    def test_official_catalog_audits_retitle_by_stable_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            canonical = root / "raw" / "icml" / "2026.json"
            canonical.parent.mkdir(parents=True)
            canonical.write_text(
                json.dumps(
                    {
                        "papers": [
                            {
                                "paper_id": "S2-stable",
                                "title": "Submission Title",
                                "paper_title": "Submission Title",
                                "authors": ["Alice"],
                                "abstract": "Canonical abstract.",
                                "source_ids": {
                                    "icml_virtual_event_id": "7",
                                    "openreview_id": "forum-7",
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = """
            <li><a href="/virtual/2026/poster/7">Final Title</a></li>
            <li><a href="/virtual/2026/poster/8">New Paper</a></li>
            """
            detail = """
            <script type="application/ld+json">{
              "name":"New Paper","author":[{"name":"Bob"}]
            }</script>
            <div class="abstract-text-inner">New abstract.</div>
            <a href="https://openreview.net/forum?id=forum-8">OpenReview</a>
            <span class="event-type-badge">Poster</span>
            """

            def fake_fetch(url: str, **_: object) -> str:
                return catalog if "papers.html" in url else detail

            output = root / "collected" / "ICML-26.json"
            with patch.dict(
                "janussearch.collectors.virtual.OFFICIAL_CATALOG_EXPECTED_COUNTS",
                {("ICML", 2026): 2},
                clear=True,
            ), patch("janussearch.collectors.virtual.fetch_text", side_effect=fake_fetch):
                collect_official_catalog(
                    "ICML", 2026, output,
                    timeout=1.0, retries=1, canonical_root=root / "raw", workers=1,
                )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["group_coverage"]["canonical_records_reused"], 1)
            self.assertEqual(payload["group_coverage"]["detail_pages_fetched"], 1)
            self.assertEqual(payload["group_coverage"]["retitle_mapping_count"], 1)
            self.assertEqual(
                payload["reconciliation"]["retitle_mappings"],
                [{
                    "source_event_id": "7",
                    "old_paper_id": "S2-stable",
                    "old_title": "Submission Title",
                    "new_title": "Final Title",
                }],
            )
            retitled = next(item for item in payload["papers"] if item["title"] == "Final Title")
            self.assertEqual(retitled["paper_id"], "S2-stable")
            self.assertEqual(retitled["source_ids"]["openreview_id"], "forum-7")

    def test_aistats_catalog_applies_uniform_poster_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog = '<li><a href="/virtual/2026/poster/7">Paper Seven</a></li>'
            detail = """
            <script type="application/ld+json">{
              "name":"Paper Seven","author":[{"name":"Alice"}]
            }</script>
            <div class="abstract-text-inner">Abstract.</div>
            <span class="event-type-badge">Poster</span>
            """

            def fake_fetch(url: str, **_: object) -> str:
                return catalog if "papers.html" in url else detail

            with patch.dict(
                "janussearch.collectors.virtual.OFFICIAL_CATALOG_EXPECTED_COUNTS",
                {("AISTATS", 2026): 1},
                clear=True,
            ), patch("janussearch.collectors.virtual.fetch_text", side_effect=fake_fetch):
                output = root / "AISTATS-26.json"
                result = collect_official_catalog(
                    "AISTATS", 2026, output,
                    timeout=1.0, retries=1, canonical_root=root / "raw", workers=1,
                )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["count"], 1)
            self.assertEqual(payload["papers"][0]["presentation_level"], "poster")
            self.assertEqual(
                payload["group_coverage"]["fallback_reason"],
                "official_virtual_catalog_uniform_poster_policy",
            )

    def test_icml_2026_removal_policy_matches_official_delta(self) -> None:
        policy = json.loads(
            Path("config/reconciliation/2026-venue-refresh.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(policy["targets"]["icml/2026.json"]["allowed_removals"]),
            {
                "S2-178163db226f5513",
                "S2-f0e6e120212f6ecf",
                "S2-092d850c9ce53a02",
                "S2-dceb3acc221bfca5",
                "S2-37def76fb074e4b1",
            },
        )

    def test_icml_failed_official_catalog_fallback_writes_incomplete_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "ICML-26.json"
            with patch(
                "janussearch.collectors.virtual.fetch_complete_events",
                side_effect=ApprovedPaginationIncomplete("official pagination shortfall"),
            ), patch(
                "janussearch.collectors.virtual.collect_official_catalog",
                side_effect=RuntimeError("catalog mismatch"),
            ):
                with self.assertRaisesRegex(RuntimeError, "sources are unusable"):
                    collect_target("ICML", 2026, output)

    def test_icml_first_page_failure_uses_official_catalog_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "ICML-26.json"
            error = HttpFetchError(
                url="https://icml.cc/first.json",
                category="http_forbidden",
                message="Forbidden",
                status_code=403,
            )
            with patch(
                "janussearch.collectors.virtual.fetch_complete_events",
                side_effect=error,
            ), patch(
                "janussearch.collectors.virtual.collect_official_catalog",
                return_value={"outcome": "collected", "count": 6628},
            ) as fallback:
                result = collect_target("ICML", 2026, output)
            fallback.assert_called_once()
            self.assertEqual(result["count"], 6628)

    def test_ijcai_2026_parser_isolates_title_placeholder(self) -> None:
        cards = parse_accepted_2026_cards(
            """
            <li class="ij-paper"><span class="ij-pid">#1</span>
              <h3 class="ij-ptitle">A Complete Paper</h3>
              <span class="ij-author">Alice</span>
              <div class="ij-abstract">Abstract.</div>
            </li>
            <li class="ij-paper"><span class="ij-pid">#EC2</span>
              <h3 class="ij-ptitle">Title TBD</h3>
              <span class="ij-author">Bob</span>
              <div class="ij-abstract">Not ready yet</div>
            </li>
            """
        )
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[1]["paper_id"], "EC2")
        self.assertEqual(cards[1]["title"], "Title TBD")

    def test_icde_track_dedupe_keeps_all_memberships_and_official_doi(self) -> None:
        official = parse_icde_accepted_page(
            """
            <li class="paper-item"><div class="number-column">01</div>
              <div class="title">Same Paper</div>
              <span class="author-name">Alice*</span><span class="affiliation">(Example U)</span>
            </li>
            """
        )
        workshop = parse_icde_workshop_proceedings(
            """
            <dblp><inproceedings key="conf/icde/Test26">
              <author>Alice</author><title>Same Paper.</title><pages>1-2</pages>
              <ee>https://doi.org/10.1109/ICDEW71238.2026.00001</ee>
            </inproceedings></dblp>
            """
        )
        records = [
            build_icde_record(
                official[0], track="phd", track_display="PhD", track_group="adjunct",
                source_url="https://icde2026.github.io/phd-papers.html", collected_at="now"
            ),
            build_icde_record(
                workshop[0], track="workshop", track_display="Workshop", track_group="workshop",
                source_url="https://doi.org/10.1109/ICDEW71238.2026", collected_at="now"
            ),
        ]
        unique_records, mappings = dedupe_icde_tracks(records)
        self.assertEqual(len(unique_records), 1)
        self.assertEqual(unique_records[0]["track"], "phd")
        self.assertEqual(unique_records[0]["track_memberships"], ["phd", "workshop"])
        self.assertEqual(
            unique_records[0]["doi"], "10.1109/icdew71238.2026.00001"
        )
        self.assertEqual(len(mappings), 1)

    def test_kdd_2026_manifest_contains_exact_25_content_groups(self) -> None:
        self.assertEqual(len(KDD_2026_OPENREVIEW_GROUPS), 25)
        self.assertEqual(len(set(KDD_2026_OPENREVIEW_GROUPS)), 25)
        self.assertEqual(
            sum("/Workshop/" in group for group in KDD_2026_OPENREVIEW_GROUPS),
            16,
        )
        self.assertNotIn("KDD.org/2026/Workshop_Proposal", KDD_2026_OPENREVIEW_GROUPS)

    def test_kdd_acm_bibtex_parser_handles_escaped_latex_quotes(self) -> None:
        bib = r'''@inproceedings{10.1145/1.2,
author = {Schl{\"o}tterer, J{\"o}rg and Doe, Jane},
title = {A {T}itle},
abstract = {An abstract with 25.27\% and {\texttimes} markup.},
keywords = {machine learning, data mining},
year = {2026}
}'''
        records = parse_acm_bibtex(bib, source_file="fixture.bib")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["doi"], "10.1145/1.2")
        self.assertEqual(len(records[0]["authors"]), 2)
        self.assertIn("25.27%", records[0]["abstract"])

    def test_kdd_acm_snapshot_manifest_covers_v1_v2_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = collect_acm_2026(output_root=Path(temp_dir))
        self.assertEqual(len(KDD_2026_ACM_SPECS), 13)
        self.assertEqual(result["official_entry_count"], 1471)
        self.assertEqual(result["official_unique_count"], 1471)
        self.assertEqual(result["group_coverage"]["covered_group_count"], 15)
        self.assertEqual(result["group_coverage"]["volume_counts"], {"V.1": 256, "V.2": 1215})

    def test_kdd_2026_group_failure_audits_all_25_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            with patch("janussearch.collectors.kdd.fetch_response"), patch(
                "janussearch.collectors.generic.fetch_openreview_notes_for_venue",
                side_effect=RuntimeError("HTTP Error 403: Forbidden"),
            ):
                with self.assertRaisesRegex(RuntimeError, "not verifiable"):
                    collect_openreview_2026(
                        output_root=output_root, timeout=1.0, retries=1
                    )
            sidecar = json.loads(
                (output_root / ".janus-collection.json").read_text(encoding="utf-8")
            )
            coverage = sidecar["metrics"]["group_coverage"]
            self.assertEqual(len(coverage), 25)
            self.assertEqual(coverage[0]["status"], "forbidden")
            self.assertTrue(
                all(
                    item["status"] == "not_checked_due_prior_group_failure"
                    for item in coverage[1:]
                )
            )

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
