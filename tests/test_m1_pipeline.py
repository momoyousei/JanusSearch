#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for M1 pipeline helpers."""

from __future__ import annotations

import unittest

from tools.m1_pipeline import (
    canonicalize_doi,
    dedupe_records,
    is_placeholder_record,
    normalize_title,
    resolve_icml_pmlr_volume,
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


if __name__ == "__main__":
    unittest.main()
