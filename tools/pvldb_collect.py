#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility entrypoint for the PVLDB collector."""

from janussearch.collectors.pvldb import (
    PVLDB_VOLUME_URL,
    collect_year,
    main,
    normalize_text,
    official_records,
    parse_next_data,
    split_authors,
    title_key,
)

__all__ = [
    "PVLDB_VOLUME_URL",
    "collect_year",
    "main",
    "normalize_text",
    "official_records",
    "parse_next_data",
    "split_authors",
    "title_key",
]

if __name__ == "__main__":
    raise SystemExit(main())
