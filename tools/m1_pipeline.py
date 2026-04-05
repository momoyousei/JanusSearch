#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1 data pipeline: inventory, normalize, backfill, and validate venue-year files."""

from __future__ import annotations

import argparse
import copy
import hashlib
import html as html_lib
import json
import logging
import os
import re
import socket
import shutil
import time
import xml.etree.ElementTree as et
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("m1_pipeline")

DEFAULT_INPUT_GLOB = "archives/root_json/*-*.json"
DEFAULT_CANONICAL_ROOT = Path("data/raw")
DEFAULT_BACKUP_ROOT = Path("backups/raw")
DEFAULT_INDEX_ROOT = Path("artifacts")
DEFAULT_AUTHORS_THRESHOLD = 90.0
DEFAULT_ABSTRACT_THRESHOLD = 85.0
PRESENTATION_LEVELS = ("poster", "oral", "bestpaper")
FIELD_PROVENANCE_FIELDS = (
    "abstract",
    "authors",
    "url",
    "track_group",
    "presentation_level",
)
FIELD_PROVENANCE_VALUES = {
    "official",
    "venue_special",
    "s2",
    "arxiv",
    "papers_cool",
    "manual",
}
DERIVED_QUALITY_FLAGS = {
    "missing_title",
    "missing_authors",
    "missing_abstract",
    "missing_keywords",
    "missing_institutions",
    "placeholder_external_only",
}
PAPERS_COOL_BASE = "https://papers.cool"
PAPERS_COOL_SUPPORTED_VENUES = {"ACL", "AAAI"}
PAPERS_COOL_ALLOWED_DOMAINS = {
    "ACL": {"aclanthology.org"},
    "AAAI": {"ojs.aaai.org"},
}
PAPERS_COOL_DEFAULT_POLICY = "full_fields"
PAPERS_COOL_POLICY_CHOICES = (PAPERS_COOL_DEFAULT_POLICY,)
PAPERS_COOL_FALLBACK_FLAG = "aggregator_fallback_papers_cool"

S2_BASE = "https://api.semanticscholar.org/graph/v1"
S2_FIELDS = "paperId,title,abstract,authors,citationCount,url,externalIds"
ARXIV_BASE = "https://export.arxiv.org/api/query"
ARXIV_DEFAULT_MIN_INTERVAL_SECONDS = 3.0
ARXIV_DEFAULT_USER_AGENT = "JanusSearch/0.1"
PMLR_BASE = "https://proceedings.mlr.press"
_ARXIV_LAST_REQUEST_AT = 0.0
_NEURIPS_OFFICIAL_STATS_CACHE: Dict[str, Dict[str, Any]] = {}
_PMLR_INDEX_CACHE: Dict[str, Dict[str, str]] = {}
_PMLR_ABSTRACT_CACHE: Dict[str, Optional[str]] = {}
_PAPERS_COOL_VENUE_CACHE: Dict[str, Dict[str, Dict[str, Any]]] = {}
_ICML_PMLR_VOLUMES: Dict[int, str] = {
    2021: "v139",
}


@dataclass
class FileContext:
    """Resolved context for one venue-year source file."""

    path: Path
    venue: str
    year: int
    provider: str
    generated_at: str
    target: str


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def ensure_str(value: Any) -> str:
    """Return stripped string representation."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def should_skip_neurips_source_fetch() -> bool:
    """Whether to skip remote NeurIPS official catalog fetch."""
    flag = ensure_str(os.getenv("JANUS_SKIP_NEURIPS_SOURCE_FETCH")).lower()
    return flag in {"1", "true", "yes", "on"}


def empty_neurips_catalog() -> Dict[str, Any]:
    """Return empty NeurIPS official catalog shape."""
    return {
        "presentation_counts": {},
        "track_counts": {},
        "paper_count_unique": 0,
        "presentation_counts_raw": {},
        "track_counts_raw": {},
        "title_map": {},
    }


def parse_neurips_catalog_payload(payload: Any) -> Dict[str, Any]:
    """Parse NeurIPS official payload into normalized catalog."""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return empty_neurips_catalog()

    presentation_counts_raw: Dict[str, int] = {}
    track_counts_raw: Dict[str, int] = {}
    title_map: Dict[str, Dict[str, str]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        paper_title = ensure_str(item.get("name"))
        title_key = normalize_title(paper_title)
        if not title_key:
            continue
        decision = ensure_str(item.get("decision"))
        level = normalize_presentation_level(decision)
        presentation_counts_raw[level] = presentation_counts_raw.get(level, 0) + 1

        sourceurl = ensure_str(item.get("sourceurl"))
        track = _extract_group_slug_from_sourceurl(sourceurl)
        track_counts_raw[track] = track_counts_raw.get(track, 0) + 1
        paper_url = ensure_str(item.get("paper_url"))
        title_map[title_key] = {
            "title": paper_title,
            "presentation_level": level,
            "track": track,
            "sourceurl": sourceurl,
            "paper_url": paper_url,
        }

    presentation_counts_unique: Dict[str, int] = {}
    track_counts_unique: Dict[str, int] = {}
    for entry in title_map.values():
        level = normalize_presentation_level(entry.get("presentation_level"))
        track = normalize_track_value(entry.get("track"))
        presentation_counts_unique[level] = presentation_counts_unique.get(level, 0) + 1
        track_counts_unique[track] = track_counts_unique.get(track, 0) + 1

    presentation_counts = {
        k: presentation_counts_unique[k]
        for k in sorted(
            presentation_counts_unique,
            key=lambda key: (-presentation_counts_unique[key], key),
        )
    }
    track_counts = {
        k: track_counts_unique[k]
        for k in sorted(track_counts_unique, key=lambda key: (-track_counts_unique[key], key))
    }
    return {
        "presentation_counts": presentation_counts,
        "track_counts": track_counts,
        "paper_count_unique": len(title_map),
        "presentation_counts_raw": {
            k: presentation_counts_raw[k]
            for k in sorted(
                presentation_counts_raw,
                key=lambda key: (-presentation_counts_raw[key], key),
            )
        },
        "track_counts_raw": {
            k: track_counts_raw[k]
            for k in sorted(track_counts_raw, key=lambda key: (-track_counts_raw[key], key))
        },
        "title_map": title_map,
    }


def unique_preserve_order(values: Iterable[str]) -> List[str]:
    """Deduplicate a string list while preserving first appearance order."""
    seen: set[str] = set()
    out: List[str] = []
    for raw in values:
        value = ensure_str(raw)
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def to_string_list(value: Any) -> List[str]:
    """Normalize arbitrary value into a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
                continue
            if isinstance(item, dict):
                for key in ("display_name", "name", "text", "value"):
                    candidate = item.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        out.append(candidate)
                        break
                continue
            out.append(str(item))
        return unique_preserve_order(out)
    return [str(value)]


def normalize_title(text: str) -> str:
    """Normalize title for matching and deduplication."""
    cleaned = re.sub(r"\s+", " ", ensure_str(text)).strip().lower()
    return re.sub(r"[^a-z0-9]+", "", cleaned)


def normalize_slug(text: str) -> str:
    """Normalize value to lower snake-ish identifier."""
    cleaned = ensure_str(text).lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned)
    return cleaned.strip("_")


def normalize_track_value(value: Any) -> str:
    """Normalize track value to machine-friendly slug."""
    track = normalize_slug(ensure_str(value))
    if not track:
        return "other"
    aliases = {
        "main_track": "conference",
        "conference_main_track": "conference",
        "datasets_benchmarks": "datasets_and_benchmarks_track",
        "datasets_and_benchmarks": "datasets_and_benchmarks_track",
        "position_paper": "position_paper_track",
        "ml_reproducibility": "ml_reproducibility_challenge",
    }
    if track in aliases:
        return aliases[track]
    if track in {"main_track", "conference_main_track"}:
        return "conference"
    return track


def normalize_track_group(value: Any, track: str) -> str:
    """Normalize track group to main/other."""
    group = normalize_slug(ensure_str(value))
    if group in {"main", "other"}:
        return group
    if track in {"conference", "main"}:
        return "main"
    return "other"


def normalize_presentation_level(value: Any) -> str:
    """Normalize presentation level into poster/oral/bestpaper."""
    text = ensure_str(value).lower()
    if not text:
        return "poster"
    if "best" in text and "paper" in text:
        return "bestpaper"
    if "oral" in text or "spotlight" in text:
        return "oral"
    if "poster" in text:
        return "poster"
    if text in PRESENTATION_LEVELS:
        return text
    return "poster"


def map_source_provider_to_provenance(source_provider: Any) -> str:
    """Map provider-specific labels into the compact provenance taxonomy."""
    provider = normalize_slug(source_provider)
    if provider in {
        "acl_anthology",
        "aaai_ojs",
        "openreview",
        "official",
    }:
        return "official"
    if provider in {"semantic_scholar", "semantic_scholar_api", "s2"}:
        return "s2"
    if provider == "arxiv":
        return "arxiv"
    if provider == "manual":
        return "manual"
    if provider == "papers_cool":
        return "papers_cool"
    return "venue_special"


def field_has_value(record: Dict[str, Any], field: str) -> bool:
    """Check whether one provenance-tracked field is meaningfully present."""
    if field == "authors":
        return bool(to_string_list(record.get(field)))
    return not is_missing_text(record.get(field))


def normalize_field_provenance(record: Dict[str, Any]) -> Dict[str, str]:
    """Normalize and backfill field-level provenance."""
    existing = record.get("field_provenance")
    normalized: Dict[str, str] = {}
    if isinstance(existing, dict):
        for key, value in existing.items():
            field = ensure_str(key)
            provenance = normalize_slug(value)
            if field in FIELD_PROVENANCE_FIELDS and provenance in FIELD_PROVENANCE_VALUES:
                normalized[field] = provenance

    default_provenance = map_source_provider_to_provenance(record.get("source_provider"))
    for field in FIELD_PROVENANCE_FIELDS:
        if field not in normalized and field_has_value(record, field):
            normalized[field] = default_provenance
    return normalized


def set_field_provenance(record: Dict[str, Any], fields: Sequence[str], provenance: str) -> None:
    """Assign provenance to populated fields."""
    normalized_provenance = normalize_slug(provenance)
    if normalized_provenance not in FIELD_PROVENANCE_VALUES:
        raise ValueError(f"Unsupported field provenance: {provenance}")
    field_provenance = normalize_field_provenance(record)
    for field in fields:
        if field in FIELD_PROVENANCE_FIELDS and field_has_value(record, field):
            field_provenance[field] = normalized_provenance
    record["field_provenance"] = field_provenance


def normalize_quality_flags(record: Dict[str, Any]) -> List[str]:
    """Merge derived quality flags with stable custom markers."""
    existing = [
        ensure_str(item)
        for item in to_string_list(record.get("quality_flags"))
        if ensure_str(item)
    ]
    custom_flags = [item for item in existing if item not in DERIVED_QUALITY_FLAGS]
    return unique_preserve_order([*quality_flags_for(record), *custom_flags])


def canonicalize_doi(value: Any) -> Optional[str]:
    """Convert DOI variants into canonical bare DOI string."""
    doi = ensure_str(value)
    if not doi:
        return None
    lowered = doi.lower()
    prefixes = ("https://doi.org/", "http://doi.org/", "doi:")
    for prefix in prefixes:
        if lowered.startswith(prefix):
            doi = doi[len(prefix) :]
            break
    doi = doi.strip()
    return doi or None


def parse_venue_year_from_filename(path: Path) -> Tuple[str, int]:
    """Extract venue and year from file name like NeurIPS-25.json."""
    match = re.match(r"^([A-Za-z0-9_]+)-(\d{2,4})\.json$", path.name)
    if not match:
        raise ValueError(f"Unrecognized source file name: {path.name}")
    venue = match.group(1)
    year_raw = int(match.group(2))
    if year_raw < 100:
        year_raw += 2000
    return venue.upper(), year_raw


def infer_context(payload: Dict[str, Any], path: Path) -> FileContext:
    """Build context for one source payload."""
    fallback_venue, fallback_year = parse_venue_year_from_filename(path)
    query = payload.get("query", {})
    source = payload.get("source", {})

    venue = ensure_str(query.get("venue_code")) or fallback_venue
    year_value = query.get("year")
    if isinstance(year_value, int):
        year = year_value
    else:
        year = fallback_year

    provider = (
        ensure_str(query.get("provider"))
        or ensure_str(source.get("provider"))
        or "unknown"
    )
    generated_at = ensure_str(payload.get("generated_at_utc")) or utc_now_iso()
    target = ensure_str(query.get("target")) or f"{venue}-{year}"
    return FileContext(
        path=path,
        venue=venue,
        year=year,
        provider=provider,
        generated_at=generated_at,
        target=target,
    )


def build_paper_id(record: Dict[str, Any], venue: str, year: int) -> str:
    """Build deterministic paper id using venue/year/title and fallback IDs."""
    title = ensure_str(record.get("paper_title") or record.get("title"))
    key = normalize_title(title)
    if not key:
        key = (
            canonicalize_doi(record.get("doi"))
            or ensure_str(record.get("openreview_id"))
            or ensure_str(record.get("openalex_id"))
            or ensure_str(record.get("external_url"))
            or "missing-title"
        )
    digest = hashlib.sha1(f"{venue}:{year}:{key}".encode("utf-8")).hexdigest()
    return f"S2-{digest[:16]}"


def is_missing_text(value: Any) -> bool:
    """Check whether text field is empty."""
    return not ensure_str(value)


def is_placeholder_record(record: Dict[str, Any]) -> bool:
    """Infer whether a record is an externally reconciled placeholder."""
    if bool(record.get("external_only")):
        return True
    authors = to_string_list(record.get("authors"))
    abstract_missing = is_missing_text(record.get("abstract"))
    has_anchor = bool(
        canonicalize_doi(record.get("doi"))
        or ensure_str(record.get("openreview_id"))
        or ensure_str(record.get("openalex_id"))
    )
    if not authors and abstract_missing and not has_anchor and ensure_str(
        record.get("external_url")
    ):
        return True
    return False


def quality_flags_for(record: Dict[str, Any]) -> List[str]:
    """Build issue flags for one normalized record."""
    flags: List[str] = []
    if is_missing_text(record.get("paper_title")) and is_missing_text(record.get("title")):
        flags.append("missing_title")
    if not to_string_list(record.get("authors")):
        flags.append("missing_authors")
    if is_missing_text(record.get("abstract")):
        flags.append("missing_abstract")
    if not to_string_list(record.get("keywords")):
        flags.append("missing_keywords")
    if not to_string_list(record.get("institutions")):
        flags.append("missing_institutions")
    if is_placeholder_record(record):
        flags.append("placeholder_external_only")
    return flags


def record_score(record: Dict[str, Any]) -> int:
    """Score a record for winner selection during deduplication."""
    score = 0
    if not is_missing_text(record.get("paper_title")):
        score += 6
    if to_string_list(record.get("authors")):
        score += 6
    if not is_missing_text(record.get("abstract")):
        score += 6
    if to_string_list(record.get("keywords")):
        score += 2
    if to_string_list(record.get("institutions")):
        score += 2
    if canonicalize_doi(record.get("doi")):
        score += 2
    if ensure_str(record.get("openreview_id")):
        score += 2
    if ensure_str(record.get("openalex_id")):
        score += 2
    if not is_placeholder_record(record):
        score += 2
    if ensure_str(record.get("record_status")).lower() == "repaired":
        score += 1
    return score


def merge_record_group(group: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge duplicate records into a single best-effort record."""
    ordered = sorted(group, key=record_score, reverse=True)
    merged: Dict[str, Any] = copy.deepcopy(ordered[0])
    merged.setdefault("source_ids", {})
    if not isinstance(merged["source_ids"], dict):
        merged["source_ids"] = {}
    merged.setdefault("field_provenance", {})
    if not isinstance(merged["field_provenance"], dict):
        merged["field_provenance"] = {}

    for current in ordered[1:]:
        for list_key in ("authors", "institutions", "keywords", "quality_flags"):
            existing = to_string_list(merged.get(list_key))
            incoming = to_string_list(current.get(list_key))
            merged[list_key] = unique_preserve_order([*existing, *incoming])

        for scalar_key in (
            "paper_title",
            "title",
            "abstract",
            "doi",
            "url",
            "openalex_id",
            "openreview_id",
            "semantic_scholar_paper_id",
            "presentation_level",
            "track",
            "track_display_name",
            "track_group",
            "external_url",
            "citation_count",
        ):
            if is_missing_text(merged.get(scalar_key)):
                incoming_value = current.get(scalar_key)
                if not is_missing_text(incoming_value):
                    merged[scalar_key] = incoming_value

        if bool(merged.get("external_only")) and not bool(current.get("external_only")):
            merged["external_only"] = False

        incoming_source_ids = current.get("source_ids", {})
        if isinstance(incoming_source_ids, dict):
            for source_key, source_value in incoming_source_ids.items():
                if source_key not in merged["source_ids"] and source_value:
                    merged["source_ids"][source_key] = source_value

        incoming_field_provenance = current.get("field_provenance", {})
        if isinstance(incoming_field_provenance, dict):
            for field, provenance in incoming_field_provenance.items():
                if field not in merged["field_provenance"] and provenance:
                    merged["field_provenance"][field] = provenance

    return merged


def transform_record(raw: Dict[str, Any], context: FileContext) -> Dict[str, Any]:
    """Normalize one record into canonical-enriched shape."""
    record: Dict[str, Any] = copy.deepcopy(raw)
    title = ensure_str(record.get("paper_title") or record.get("title"))
    record["paper_title"] = title
    record["title"] = title

    record["authors"] = unique_preserve_order(to_string_list(record.get("authors")))
    record["institutions"] = unique_preserve_order(
        to_string_list(record.get("institutions"))
    )
    record["keywords"] = unique_preserve_order(to_string_list(record.get("keywords")))

    abstract = ensure_str(record.get("abstract"))
    record["abstract"] = abstract

    doi = canonicalize_doi(record.get("doi"))
    record["doi"] = doi

    url = ensure_str(record.get("url")) or ensure_str(record.get("external_url"))
    record["url"] = url or None

    citation_count = record.get("citation_count")
    if citation_count is None:
        citation_count = record.get("citationCount")
    if isinstance(citation_count, str) and citation_count.isdigit():
        citation_count = int(citation_count)
    if not isinstance(citation_count, int):
        citation_count = None
    record["citation_count"] = citation_count

    track = normalize_track_value(record.get("track"))
    record["track"] = track
    record["track_group"] = normalize_track_group(record.get("track_group"), track)
    track_display_name = ensure_str(record.get("track_display_name"))
    if not track_display_name:
        track_display_name = track.replace("_", " ").title()
    record["track_display_name"] = track_display_name

    record["presentation_level"] = normalize_presentation_level(
        record.get("presentation_level")
    )

    record["venue"] = context.venue
    record["year"] = context.year
    record["source_provider"] = ensure_str(record.get("source_provider")) or context.provider
    record["collected_at"] = ensure_str(record.get("collected_at")) or context.generated_at

    source_ids = record.get("source_ids")
    if not isinstance(source_ids, dict):
        source_ids = {}
    if record.get("openalex_id"):
        source_ids.setdefault("openalex_id", record["openalex_id"])
    if record.get("openreview_id"):
        source_ids.setdefault("openreview_id", record["openreview_id"])
    if doi:
        source_ids.setdefault("doi", doi)
    if record.get("semantic_scholar_paper_id"):
        source_ids.setdefault("semantic_scholar_paper_id", record["semantic_scholar_paper_id"])
    record["source_ids"] = source_ids
    record["field_provenance"] = normalize_field_provenance(record)

    record["paper_id"] = ensure_str(record.get("paper_id")) or build_paper_id(
        record=record,
        venue=context.venue,
        year=context.year,
    )

    placeholder = is_placeholder_record(record)
    status = ensure_str(record.get("record_status")).lower()
    if placeholder:
        record["record_status"] = "placeholder"
    elif status == "repaired":
        record["record_status"] = "repaired"
    else:
        record["record_status"] = "resolved"

    record["quality_flags"] = normalize_quality_flags(record)
    return record


def dedupe_records(records: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Deduplicate records by normalized title."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    untitled_count = 0
    for record in records:
        title_key = normalize_title(ensure_str(record.get("paper_title")))
        if not title_key:
            fallback = (
                canonicalize_doi(record.get("doi"))
                or ensure_str(record.get("openreview_id"))
                or ensure_str(record.get("openalex_id"))
            )
            if fallback:
                title_key = f"__id__{fallback}"
            else:
                untitled_count += 1
                title_key = f"__untitled__{untitled_count}"
        groups.setdefault(title_key, []).append(record)

    deduped: List[Dict[str, Any]] = []
    duplicates_removed = 0
    for key in sorted(groups):
        group = groups[key]
        if len(group) == 1:
            deduped.append(group[0])
            continue
        merged = merge_record_group(group)
        duplicates_removed += len(group) - 1
        deduped.append(merged)
    return deduped, duplicates_removed


def count_records_by(records: Sequence[Dict[str, Any]], key: str) -> Dict[str, int]:
    """Count records by normalized key."""
    counts: Dict[str, int] = {}
    for record in records:
        if key == "track":
            normalized = normalize_track_value(record.get("track"))
        elif key == "track_group":
            normalized = normalize_track_group(record.get("track_group"), normalize_track_value(record.get("track")))
        elif key == "presentation_level":
            normalized = normalize_presentation_level(record.get("presentation_level"))
        else:
            normalized = normalize_slug(record.get(key))
            if not normalized:
                normalized = "unknown"
        counts[normalized] = counts.get(normalized, 0) + 1
    return {
        k: counts[k]
        for k in sorted(counts.keys(), key=lambda item: (-counts[item], item))
    }


def compute_metrics(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute per-file quality metrics."""
    total = len(records)
    resolved_records = [r for r in records if ensure_str(r.get("record_status")) != "placeholder"]
    placeholder_total = total - len(resolved_records)

    def coverage(items: Sequence[Dict[str, Any]], key: str) -> float:
        if not items:
            return 0.0
        if key == "abstract":
            hits = sum(1 for item in items if not is_missing_text(item.get("abstract")))
        elif key == "authors":
            hits = sum(1 for item in items if bool(to_string_list(item.get("authors"))))
        else:
            hits = sum(1 for item in items if item.get(key) not in (None, "", [], {}))
        return round(hits * 100.0 / len(items), 2)

    title_keys = [normalize_title(ensure_str(item.get("paper_title"))) for item in records]
    counter: Dict[str, int] = {}
    duplicates = 0
    for key in title_keys:
        if not key:
            continue
        counter[key] = counter.get(key, 0) + 1
        if counter[key] > 1:
            duplicates += 1

    return {
        "total": total,
        "resolved_total": len(resolved_records),
        "placeholder_total": placeholder_total,
        "full_authors_coverage": coverage(records, "authors"),
        "full_abstract_coverage": coverage(records, "abstract"),
        "resolved_authors_coverage": coverage(resolved_records, "authors"),
        "resolved_abstract_coverage": coverage(resolved_records, "abstract"),
        "duplicate_title_count": duplicates,
    }


def normalize_count_map(raw: Any) -> Dict[str, int]:
    """Normalize arbitrary count mapping to slug->int."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, int] = {}
    for key, value in raw.items():
        normalized_key = normalize_slug(key)
        if not normalized_key:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            out[normalized_key] = value
            continue
        if isinstance(value, str) and value.strip().isdigit():
            out[normalized_key] = int(value.strip())
    return out


def diff_count_maps(actual: Dict[str, int], official: Dict[str, int]) -> Dict[str, int]:
    """Compute actual-official difference map."""
    diff: Dict[str, int] = {}
    for key in sorted(set(actual) | set(official)):
        diff[key] = int(actual.get(key, 0)) - int(official.get(key, 0))
    return diff


def _extract_group_slug_from_sourceurl(sourceurl: str) -> str:
    """Extract normalized track slug from NeurIPS official source URL."""
    parsed = urlparse(sourceurl)
    query_values = parse_qs(parsed.query)
    group_id = ensure_str((query_values.get("id") or [""])[0])
    working = group_id
    if "/group?id=" in sourceurl:
        working = sourceurl.split("/group?id=", maxsplit=1)[1]
    working = working.strip()
    if not working:
        return "other"
    lower = working.lower()
    if lower.endswith("/conference"):
        return "conference"
    if "datasets_and_benchmarks_track" in lower:
        return "datasets_and_benchmarks_track"
    if "position_paper_track" in lower:
        return "position_paper_track"
    if "ml_reproducibility_challenge" in lower:
        return "ml_reproducibility_challenge"
    if "journal_track_tmlr" in lower:
        return "journal_track_tmlr"
    if "journal_track_jmlr" in lower:
        return "journal_track_jmlr"
    if "journal_track_rescience" in lower:
        return "journal_track_rescience"
    if "journal_track_annals_of_statistics" in lower:
        return "journal_track_annals_of_statistics"
    if "journal_track" in lower:
        return "journal_track"
    segment = working.split("/")[-1] if "/" in working else working
    return normalize_track_value(segment)


def fetch_neurips_official_catalog(
    source_url: str, timeout: float = 300.0, retries: int = 3
) -> Dict[str, Any]:
    """Fetch official NeurIPS catalog with track/presentation counts and title map."""
    if should_skip_neurips_source_fetch():
        return empty_neurips_catalog()
    if not source_url:
        return empty_neurips_catalog()

    cache_dir = ensure_str(os.getenv("JANUS_NEURIPS_OFFICIAL_CACHE_DIR"))
    if cache_dir:
        cache_path = Path(cache_dir) / Path(urlparse(source_url).path).name
        if cache_path.is_file():
            try:
                with cache_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                LOGGER.info("Using cached NeurIPS official catalog: %s", cache_path)
                return parse_neurips_catalog_payload(payload)
            except (OSError, json.JSONDecodeError) as err:
                LOGGER.warning("Failed local NeurIPS cache load (%s): %s", cache_path, err)
    for attempt in range(1, retries + 1):
        try:
            LOGGER.info(
                "Fetching NeurIPS official catalog (attempt %s/%s): %s",
                attempt,
                retries,
                source_url,
            )
            request = Request(
                source_url,
                headers={"Accept": "application/json", "User-Agent": ARXIV_DEFAULT_USER_AGENT},
                method="GET",
            )
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", errors="ignore"))
            return parse_neurips_catalog_payload(payload)
        except (HTTPError, URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as err:
            if attempt == retries:
                LOGGER.warning("Failed official NeurIPS stats fetch: %s", err)
                return empty_neurips_catalog()
            wait = min(10.0, 1.5 * (2 ** (attempt - 1)))
            time.sleep(wait)
    return empty_neurips_catalog()


def extract_official_baseline(
    payload: Dict[str, Any],
    context: FileContext,
    cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Extract official baseline counts if available."""
    official_tracks = payload.get("official_tracks")
    reconciliation = payload.get("reconciliation")

    baseline: Dict[str, Any] = {
        "source": "none",
        "paper_count_official": None,
        "track_counts_official": {},
        "presentation_counts_official": {},
    }

    if isinstance(official_tracks, dict):
        baseline["source"] = "official_tracks"
        paper_count_official = official_tracks.get("paper_count_official")
        if isinstance(paper_count_official, int):
            baseline["paper_count_official"] = paper_count_official

        track_catalog = official_tracks.get("track_catalog")
        if isinstance(track_catalog, list):
            counts: Dict[str, int] = {}
            for item in track_catalog:
                if not isinstance(item, dict):
                    continue
                track = normalize_track_value(item.get("track"))
                paper_count = item.get("paper_count")
                if isinstance(paper_count, int):
                    counts[track] = counts.get(track, 0) + paper_count
            baseline["track_counts_official"] = counts

        source_url = ensure_str(official_tracks.get("source_url"))
        if source_url:
            cache_key = f"{context.venue}-{context.year}"
            cached = cache.get(cache_key) if cache else None
            if cached is None:
                catalog = fetch_neurips_official_catalog(source_url=source_url)
                if cache is not None:
                    cache[cache_key] = catalog
            else:
                catalog = cached

            presentation_counts = catalog.get("presentation_counts", {})
            fetched_track_counts = catalog.get("track_counts", {})
            paper_count_unique = catalog.get("paper_count_unique")

            if isinstance(paper_count_unique, int) and paper_count_unique > 0:
                baseline["paper_count_official"] = paper_count_unique

            if presentation_counts:
                baseline["presentation_counts_official"] = presentation_counts
            # Prefer unique-title official track baseline.
            if fetched_track_counts:
                baseline["track_counts_official"] = fetched_track_counts

        return baseline

    if isinstance(reconciliation, dict):
        baseline["source"] = "reconciliation"
        external_count = reconciliation.get("external_title_count")
        if isinstance(external_count, int):
            baseline["paper_count_official"] = external_count
        baseline["track_counts_official"] = normalize_count_map(
            reconciliation.get("external_track_counts")
        )
        baseline["presentation_counts_official"] = normalize_count_map(
            reconciliation.get("external_presentation_counts")
        )
        return baseline

    return baseline


def evaluate_alignment(
    *,
    payload: Dict[str, Any],
    records: Sequence[Dict[str, Any]],
    context: FileContext,
    official_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Evaluate official alignment for paper, track, and presentation counts."""
    actual_paper_count = len(records)
    actual_track_counts = count_records_by(records, "track")
    actual_presentation_counts = count_records_by(records, "presentation_level")
    baseline = extract_official_baseline(payload=payload, context=context, cache=official_cache)

    official_paper_count = baseline.get("paper_count_official")
    official_track_counts = baseline.get("track_counts_official") or {}
    official_presentation_counts = baseline.get("presentation_counts_official") or {}

    paper_aligned: Optional[bool]
    paper_diff: Optional[int]
    if isinstance(official_paper_count, int):
        paper_diff = actual_paper_count - official_paper_count
        paper_aligned = paper_diff == 0
    else:
        paper_diff = None
        paper_aligned = None

    track_diff = diff_count_maps(actual_track_counts, official_track_counts) if official_track_counts else {}
    track_aligned = None if not official_track_counts else all(value == 0 for value in track_diff.values())

    presentation_diff = (
        diff_count_maps(actual_presentation_counts, official_presentation_counts)
        if official_presentation_counts
        else {}
    )
    presentation_aligned = (
        None
        if not official_presentation_counts
        else all(value == 0 for value in presentation_diff.values())
    )

    return {
        "baseline_source": baseline.get("source"),
        "paper_count": {
            "actual": actual_paper_count,
            "official": official_paper_count,
            "diff": paper_diff,
            "aligned": paper_aligned,
        },
        "track_counts": {
            "actual": actual_track_counts,
            "official": official_track_counts,
            "diff": track_diff,
            "aligned": track_aligned,
        },
        "presentation_level_counts": {
            "actual": actual_presentation_counts,
            "official": official_presentation_counts,
            "diff": presentation_diff,
            "aligned": presentation_aligned,
        },
    }


def apply_neurips_official_catalog(
    records: List[Dict[str, Any]],
    context: FileContext,
    official_tracks_payload: Any,
) -> Dict[str, int]:
    """Apply official NeurIPS title->track/presentation mapping and add missing placeholders."""
    if should_skip_neurips_source_fetch():
        return {"updated_records": 0, "added_placeholders": 0, "removed_unofficial": 0}
    if not isinstance(official_tracks_payload, dict):
        return {"updated_records": 0, "added_placeholders": 0, "removed_unofficial": 0}

    source_url = ensure_str(official_tracks_payload.get("source_url"))
    if not source_url:
        return {"updated_records": 0, "added_placeholders": 0, "removed_unofficial": 0}

    cached = _NEURIPS_OFFICIAL_STATS_CACHE.get(source_url)
    if cached is None:
        cached = fetch_neurips_official_catalog(source_url=source_url)
        _NEURIPS_OFFICIAL_STATS_CACHE[source_url] = cached

    title_map = cached.get("title_map", {})
    if not isinstance(title_map, dict) or not title_map:
        return {"updated_records": 0, "added_placeholders": 0, "removed_unofficial": 0}

    by_title: Dict[str, Dict[str, Any]] = {}
    for record in records:
        key = normalize_title(ensure_str(record.get("paper_title")))
        if key and key not in by_title:
            by_title[key] = record

    updated_records = 0
    added_placeholders = 0
    for title_key, official in title_map.items():
        if not isinstance(official, dict):
            continue
        official_track = normalize_track_value(official.get("track"))
        official_level = normalize_presentation_level(official.get("presentation_level"))
        official_title = ensure_str(official.get("title"))
        official_url = ensure_str(official.get("paper_url"))
        official_sourceurl = ensure_str(official.get("sourceurl"))

        existing = by_title.get(title_key)
        if existing is not None:
            changed = False
            if normalize_track_value(existing.get("track")) != official_track:
                existing["track"] = official_track
                existing["track_group"] = normalize_track_group(existing.get("track_group"), official_track)
                existing["track_display_name"] = official_track.replace("_", " ").title()
                changed = True
            if normalize_presentation_level(existing.get("presentation_level")) != official_level:
                existing["presentation_level"] = official_level
                changed = True
            if official_sourceurl and is_missing_text(existing.get("official_track_source_url")):
                existing["official_track_source_url"] = official_sourceurl
                changed = True
            if official_url and is_missing_text(existing.get("url")):
                existing["url"] = official_url
                changed = True
            if changed:
                existing["quality_flags"] = normalize_quality_flags(existing)
                updated_records += 1
            continue

        placeholder = {
            "paper_title": official_title,
            "title": official_title,
            "authors": [],
            "institutions": [],
            "abstract": "",
            "keywords": [],
            "presentation_level": official_level,
            "openalex_id": None,
            "doi": None,
            "openreview_id": None,
            "semantic_scholar_paper_id": None,
            "track": official_track,
            "track_display_name": official_track.replace("_", " ").title(),
            "track_group": normalize_track_group(None, official_track),
            "external_only": True,
            "external_url": official_url or None,
            "url": official_url or None,
            "official_track_source_url": official_sourceurl or None,
            "source_provider": context.provider,
            "venue": context.venue,
            "year": context.year,
            "citation_count": None,
            "source_ids": {},
            "collected_at": context.generated_at,
            "record_status": "placeholder",
        }
        placeholder["paper_id"] = build_paper_id(
            record=placeholder, venue=context.venue, year=context.year
        )
        placeholder["quality_flags"] = normalize_quality_flags(placeholder)
        records.append(placeholder)
        by_title[title_key] = placeholder
        added_placeholders += 1

    allowed_titles = set(title_map.keys())
    filtered_records = [
        record
        for record in records
        if normalize_title(ensure_str(record.get("paper_title"))) in allowed_titles
    ]
    removed_unofficial = len(records) - len(filtered_records)
    if removed_unofficial > 0:
        records[:] = filtered_records

    return {
        "updated_records": updated_records,
        "added_placeholders": added_placeholders,
        "removed_unofficial": removed_unofficial,
    }


def build_canonical_payload(
    context: FileContext, records: Sequence[Dict[str, Any]], metrics: Dict[str, Any]
) -> Dict[str, Any]:
    """Build canonical data/raw payload."""
    canonical_records: List[Dict[str, Any]] = []
    for record in records:
        canonical_records.append(
            {
                "paper_id": record.get("paper_id"),
                "title": record.get("title"),
                "authors": record.get("authors", []),
                "venue": context.venue,
                "year": context.year,
                "abstract": record.get("abstract", ""),
                "doi": record.get("doi"),
                "url": record.get("url"),
                "citation_count": record.get("citation_count"),
                "source_provider": record.get("source_provider"),
                "source_ids": record.get("source_ids", {}),
                "field_provenance": record.get("field_provenance", {}),
                "keywords": record.get("keywords", []),
                "track": record.get("track"),
                "track_display_name": record.get("track_display_name"),
                "track_group": record.get("track_group"),
                "presentation_level": record.get("presentation_level"),
                "institutions": record.get("institutions", []),
                "record_status": record.get("record_status"),
                "quality_flags": record.get("quality_flags", []),
                "collected_at": record.get("collected_at"),
            }
        )

    return {
        "venue": context.venue,
        "year": context.year,
        "collected_at": context.generated_at,
        "source": context.provider,
        "count": len(canonical_records),
        "metrics": metrics,
        "papers": canonical_records,
    }


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON payload with utf-8 formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def backup_file(source_path: Path, backup_root: Path, run_id: str) -> Path:
    """Create backup copy before modifying source file."""
    target_dir = backup_root / run_id
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_path = target_dir / source_path.name
    if not backup_path.exists():
        shutil.copy2(source_path, backup_path)
    return backup_path


def find_input_files(input_glob: str) -> List[Path]:
    """Discover source json files from current directory."""
    files = [
        path
        for path in sorted(Path.cwd().glob(input_glob))
        if path.is_file() and path.name.lower().endswith(".json")
    ]
    if not files:
        raise FileNotFoundError(f"No files found with pattern: {input_glob}")
    return files


def load_payload(path: Path) -> Dict[str, Any]:
    """Load one top-level source payload."""
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Top-level JSON must be object: {path}")
    return payload


def normalize_payload(
    payload: Dict[str, Any], context: FileContext
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], int]:
    """Normalize and deduplicate one source payload."""
    raw_records = payload.get("papers")
    if not isinstance(raw_records, list):
        raise ValueError(f"Payload has invalid papers list: {context.path}")

    transformed = [transform_record(record, context) for record in raw_records if isinstance(record, dict)]
    deduped, duplicates_removed = dedupe_records(transformed)

    # Recompute final status and ids after dedupe merge.
    finalized: List[Dict[str, Any]] = []
    for record in deduped:
        normalized = transform_record(record, context)
        normalized["paper_id"] = build_paper_id(
            record=normalized,
            venue=context.venue,
            year=context.year,
        )
        finalized.append(normalized)

    neurips_alignment_stats = {
        "updated_records": 0,
        "added_placeholders": 0,
        "removed_unofficial": 0,
    }
    official_cache: Dict[str, Dict[str, Any]] = {}
    if context.venue == "NEURIPS":
        official_tracks_payload = payload.get("official_tracks")
        source_url = ""
        if isinstance(official_tracks_payload, dict):
            source_url = ensure_str(official_tracks_payload.get("source_url"))
        if source_url:
            cached_catalog = _NEURIPS_OFFICIAL_STATS_CACHE.get(source_url)
            if cached_catalog is None:
                cached_catalog = fetch_neurips_official_catalog(source_url=source_url)
                _NEURIPS_OFFICIAL_STATS_CACHE[source_url] = cached_catalog
            official_cache[f"{context.venue}-{context.year}"] = cached_catalog
        neurips_alignment_stats = apply_neurips_official_catalog(
            records=finalized,
            context=context,
            official_tracks_payload=payload.get("official_tracks"),
        )

    finalized.sort(key=lambda item: normalize_title(ensure_str(item.get("paper_title"))))

    metrics = compute_metrics(finalized)
    track_counts = count_records_by(finalized, "track")
    track_group_counts = count_records_by(finalized, "track_group")
    presentation_level_counts = count_records_by(finalized, "presentation_level")

    root_payload = copy.deepcopy(payload)
    root_payload["paper_count"] = len(finalized)
    root_payload["track_counts"] = track_counts
    root_payload["track_group_counts"] = track_group_counts
    root_payload["presentation_level_counts"] = presentation_level_counts
    root_payload["papers"] = finalized
    alignment_snapshot = evaluate_alignment(
        payload=root_payload,
        records=finalized,
        context=context,
        official_cache=official_cache,
    )
    root_payload["m1"] = {
        "normalized_at_utc": utc_now_iso(),
        "venue": context.venue,
        "year": context.year,
        "duplicates_removed": duplicates_removed,
        "official_neurips_alignment": neurips_alignment_stats,
        "metrics": metrics,
        "alignment": alignment_snapshot,
        "counts": {
            "paper_count": len(finalized),
            "track_counts": track_counts,
            "track_group_counts": track_group_counts,
            "presentation_level_counts": presentation_level_counts,
        },
    }
    canonical_payload = build_canonical_payload(context=context, records=finalized, metrics=metrics)
    return root_payload, canonical_payload, metrics, duplicates_removed


def render_stats_markdown(items: Sequence[Dict[str, Any]], summary: Dict[str, Any]) -> str:
    """Create markdown summary for M1 validation run."""
    lines: List[str] = [
        "# M1 Quality Stats",
        "",
        f"Generated at: `{summary['generated_at_utc']}`",
        "",
        "| File | Total | Resolved | Placeholder | Dup | Resolved Authors % | Resolved Abstract % | Alignment | Gate |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for item in items:
        metrics = item["metrics"]
        gate = "PASS" if item.get("gate_pass") else "FAIL"
        alignment = "PASS" if item.get("alignment_pass", True) else "FAIL"
        lines.append(
            "| "
            + " | ".join(
                [
                    item["file"],
                    str(metrics["total"]),
                    str(metrics["resolved_total"]),
                    str(metrics["placeholder_total"]),
                    str(metrics["duplicate_title_count"]),
                    f"{metrics['resolved_authors_coverage']:.2f}",
                    f"{metrics['resolved_abstract_coverage']:.2f}",
                    alignment,
                    gate,
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Summary",
            "",
            f"- Files: {summary['file_count']}",
            f"- Total records: {summary['total_records']}",
            f"- Resolved records: {summary['resolved_records']}",
            f"- Placeholder records: {summary['placeholder_records']}",
            f"- Duplicate titles: {summary['duplicate_titles']}",
            f"- Alignment pass files: {summary.get('alignment_pass_files', 0)}",
            f"- Alignment fail files: {summary.get('alignment_fail_files', 0)}",
            f"- Gate pass files: {summary['gate_pass_files']}",
            f"- Gate fail files: {summary['gate_fail_files']}",
        ]
    )
    return "\n".join(lines) + "\n"


class SemanticScholarClient:
    """Small Semantic Scholar client with retry and rate control."""

    def __init__(
        self,
        api_key: Optional[str],
        timeout: float,
        retries: int,
        min_interval_seconds: float,
    ) -> None:
        self.api_key = ensure_str(api_key) or None
        self.timeout = timeout
        self.retries = retries
        self.min_interval_seconds = min_interval_seconds
        self._last_request_at = 0.0

    def _wait_for_rate_limit(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)

    def _request_json(
        self, url: str, method: str = "GET", body: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        payload = None
        headers = {"Accept": "application/json"}
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["x-api-key"] = self.api_key

        attempt = 0
        while True:
            attempt += 1
            self._wait_for_rate_limit()
            request = Request(url, data=payload, headers=headers, method=method)
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    self._last_request_at = time.monotonic()
                    data = response.read().decode("utf-8", errors="ignore")
                    return json.loads(data) if data else None
            except HTTPError as err:
                self._last_request_at = time.monotonic()
                if err.code == 404:
                    return None
                if err.code == 429 and attempt <= self.retries:
                    retry_after = err.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else 3.0
                    LOGGER.warning("S2 rate-limited, sleeping %.1fs", wait)
                    time.sleep(wait)
                    continue
                if 500 <= err.code < 600 and attempt <= self.retries:
                    wait = min(12.0, 1.5 * (2 ** (attempt - 1)))
                    LOGGER.warning("S2 server error %s, retry in %.1fs", err.code, wait)
                    time.sleep(wait)
                    continue
                body_text = err.read().decode("utf-8", errors="ignore")
                LOGGER.warning("S2 request failed (%s): %s", err.code, body_text[:180])
                return None
            except (URLError, TimeoutError, socket.timeout, json.JSONDecodeError) as err:
                if attempt > self.retries:
                    LOGGER.warning("S2 request failed after retries: %s", err)
                    return None
                wait = min(12.0, 1.5 * (2 ** (attempt - 1)))
                LOGGER.warning("S2 transient error, retry in %.1fs (%s)", wait, err)
                time.sleep(wait)

    def lookup_by_doi(self, doi: str) -> Optional[Dict[str, Any]]:
        """Query paper by DOI."""
        paper_id = f"DOI:{doi}"
        url = f"{S2_BASE}/paper/{quote(paper_id, safe='')}?{urlencode({'fields': S2_FIELDS})}"
        return self._request_json(url=url, method="GET")

    def search_by_title(self, title: str, limit: int = 3) -> Optional[Dict[str, Any]]:
        """Search papers by title, returns full payload."""
        query = ensure_str(title)
        if not query:
            return None
        params = {"query": query, "limit": str(limit), "fields": S2_FIELDS}
        url = f"{S2_BASE}/paper/search?{urlencode(params)}"
        return self._request_json(url=url, method="GET")


def fetch_text_with_retry(
    url: str,
    *,
    timeout: float,
    retries: int,
    headers: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Fetch URL text with retry and transient error handling."""
    request_headers = {"Accept": "text/html,application/json", "User-Agent": "JanusSearch/0.1"}
    if headers:
        request_headers.update(headers)
    for attempt in range(1, retries + 1):
        try:
            request = Request(url, headers=request_headers, method="GET")
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="ignore")
        except HTTPError as err:
            if err.code == 404:
                return None
            if attempt < retries and (err.code == 429 or 500 <= err.code < 600):
                wait = min(12.0, 1.4 * (2 ** (attempt - 1)))
                time.sleep(wait)
                continue
            LOGGER.warning("HTTP fetch failed (%s): %s", err.code, url)
            return None
        except (URLError, TimeoutError, socket.timeout) as err:
            if attempt >= retries:
                LOGGER.warning("URL fetch failed after retries (%s): %s", url, err)
                return None
            wait = min(12.0, 1.4 * (2 ** (attempt - 1)))
            time.sleep(wait)
    return None


def build_papers_cool_venue_url(venue: str, year: int) -> str:
    """Build venue landing page URL for papers.cool."""
    return f"{PAPERS_COOL_BASE}/venue/{ensure_str(venue).upper()}.{int(year)}"


def strip_html_text(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", ensure_str(text)))
    return re.sub(r"\s+", " ", plain).strip()


def parse_papers_cool_venue_html(html_text: str, venue: str) -> Dict[str, Dict[str, Any]]:
    """Parse one papers.cool venue page into normalized title index."""
    entries: Dict[str, Dict[str, Any]] = {}
    blocks = re.findall(
        r'(<div id="[^"]+" class="panel paper"[^>]*>.*?<hr id="fold-[^"]+"[^>]*></hr>\s*</div>)',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for block in blocks:
        title_match = re.search(
            r'<a id="title-[^"]+" class="title-link[^"]*" href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        official_match = re.search(
            r'<h2 class="title">\s*<a href="([^"]+)" target="_blank" title="[^"]+">',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not title_match or not official_match:
            continue

        title = strip_html_text(title_match.group(2))
        title_key = normalize_title(title)
        official_url = ensure_str(official_match.group(1))
        if not title_key or not official_url:
            continue

        page_url = ensure_str(title_match.group(1))
        if page_url.startswith("/"):
            page_url = f"{PAPERS_COOL_BASE}{page_url}"

        pdf_match = re.search(
            r'<a id="pdf-[^"]+"[^>]* data="([^"]+)"',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        authors_match = re.search(
            r'<p id="authors-[^"]+" class="metainfo authors[^"]*">.*?</p>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        summary_match = re.search(
            r'<p id="summary-[^"]+" class="summary[^"]*">(.*?)</p>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        subject_match = re.search(
            r'<p id="subjects-[^"]+" class="metainfo subjects">.*?<a[^>]*>(.*?)</a>',
            block,
            flags=re.IGNORECASE | re.DOTALL,
        )

        authors: List[str] = []
        if authors_match:
            authors = unique_preserve_order(
                strip_html_text(name)
                for name in re.findall(
                    r'<a class="author[^"]*"[^>]*>(.*?)</a>',
                    authors_match.group(0),
                    flags=re.IGNORECASE | re.DOTALL,
                )
            )

        entries[title_key] = {
            "venue": ensure_str(venue).upper(),
            "title": title,
            "title_key": title_key,
            "page_url": page_url,
            "official_url": official_url,
            "pdf_url": ensure_str(pdf_match.group(1)) if pdf_match else "",
            "authors": authors,
            "abstract": strip_html_text(summary_match.group(1)) if summary_match else "",
            "subject": strip_html_text(subject_match.group(1)) if subject_match else "",
        }
    return entries


def load_papers_cool_venue_index(
    *, venue: str, year: int, timeout: float, retries: int
) -> Dict[str, Dict[str, Any]]:
    """Fetch and cache one papers.cool venue page."""
    venue_key = f"{ensure_str(venue).upper()}-{int(year)}"
    cached = _PAPERS_COOL_VENUE_CACHE.get(venue_key)
    if cached is not None:
        return cached

    html_text = fetch_text_with_retry(
        build_papers_cool_venue_url(venue=venue, year=year),
        timeout=timeout,
        retries=retries,
    )
    if not html_text:
        _PAPERS_COOL_VENUE_CACHE[venue_key] = {}
        return {}

    parsed = parse_papers_cool_venue_html(html_text=html_text, venue=venue)
    _PAPERS_COOL_VENUE_CACHE[venue_key] = parsed
    return parsed


def resolve_papers_cool_entry_by_title(
    *, venue: str, year: int, title: str, timeout: float, retries: int
) -> Optional[Dict[str, Any]]:
    """Resolve one papers.cool paper by strict title match."""
    venue_code = ensure_str(venue).upper()
    if venue_code not in PAPERS_COOL_SUPPORTED_VENUES:
        return None

    title_key = normalize_title(title)
    if not title_key:
        return None

    entries = load_papers_cool_venue_index(
        venue=venue_code,
        year=year,
        timeout=timeout,
        retries=retries,
    )
    if not entries:
        return None
    if title_key in entries:
        return entries[title_key]

    best_entry: Optional[Dict[str, Any]] = None
    best_ratio = 0.0
    for candidate_key, entry in entries.items():
        ratio = SequenceMatcher(None, title_key, candidate_key).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_entry = entry
    if best_ratio >= 0.997:
        return best_entry
    return None


def papers_cool_subject_mapping(
    *, venue: str, subject: str
) -> Optional[Tuple[str, str]]:
    """Map papers.cool Subject text to stable track_group/presentation_level."""
    venue_code = ensure_str(venue).upper()
    normalized = ensure_str(subject).lower()
    if not normalized:
        return None
    if venue_code == "ACL":
        if "findings" in normalized:
            return ("other", "poster")
        if normalized.startswith("acl."):
            return ("main", "poster")
        return None
    if venue_code == "AAAI" and normalized.startswith("aaai."):
        return ("main", "poster")
    return None


def is_allowed_papers_cool_official_url(venue: str, url: str) -> bool:
    """Validate that the resolved official URL stays within the venue allowlist."""
    allowed = PAPERS_COOL_ALLOWED_DOMAINS.get(ensure_str(venue).upper(), set())
    hostname = (urlparse(ensure_str(url)).hostname or "").lower()
    if not hostname or not allowed:
        return False
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed)


def backfill_from_papers_cool(
    *,
    record: Dict[str, Any],
    venue: str,
    year: int,
    timeout: float,
    retries: int,
    policy: str,
) -> Dict[str, Any]:
    """Backfill missing fields from papers.cool without changing primary provider."""
    result = {
        "matched": False,
        "updated": False,
        "updated_fields": [],
        "rejected_reason": "",
    }
    if normalize_slug(policy) != PAPERS_COOL_DEFAULT_POLICY:
        result["rejected_reason"] = "unsupported_policy"
        return result

    venue_code = ensure_str(venue).upper()
    if venue_code not in PAPERS_COOL_SUPPORTED_VENUES:
        result["rejected_reason"] = "unsupported_venue"
        return result

    title = ensure_str(record.get("paper_title") or record.get("title"))
    if not title:
        result["rejected_reason"] = "missing_title"
        return result

    entry = resolve_papers_cool_entry_by_title(
        venue=venue_code,
        year=year,
        title=title,
        timeout=timeout,
        retries=retries,
    )
    if not entry:
        result["rejected_reason"] = "no_match"
        return result

    official_url = ensure_str(entry.get("official_url"))
    if not is_allowed_papers_cool_official_url(venue_code, official_url):
        result["rejected_reason"] = "invalid_official_url"
        return result

    result["matched"] = True
    updated_fields: List[str] = []
    if is_missing_text(record.get("url")) and official_url:
        record["url"] = official_url
        updated_fields.append("url")

    abstract = ensure_str(entry.get("abstract"))
    if is_missing_text(record.get("abstract")) and abstract:
        record["abstract"] = abstract
        updated_fields.append("abstract")

    authors = unique_preserve_order(entry.get("authors", []))
    if not to_string_list(record.get("authors")) and authors:
        record["authors"] = authors
        updated_fields.append("authors")

    subject_mapping = papers_cool_subject_mapping(
        venue=venue_code,
        subject=ensure_str(entry.get("subject")),
    )
    if subject_mapping:
        track_group, presentation_level = subject_mapping
        if not ensure_str(record.get("track")) and ensure_str(record.get("track_group")) in {"", "other"}:
            record["track_group"] = track_group
            updated_fields.append("track_group")
        if not ensure_str(record.get("track")) and ensure_str(record.get("presentation_level")) in {"", "poster"}:
            record["presentation_level"] = presentation_level
            updated_fields.append("presentation_level")

    if updated_fields:
        source_ids = record.get("source_ids")
        if not isinstance(source_ids, dict):
            source_ids = {}
        source_ids["papers_cool_page_url"] = ensure_str(entry.get("page_url"))
        source_ids["papers_cool_official_url"] = official_url
        if ensure_str(entry.get("pdf_url")):
            source_ids["papers_cool_pdf_url"] = ensure_str(entry.get("pdf_url"))
        if ensure_str(entry.get("subject")):
            source_ids["papers_cool_subject"] = ensure_str(entry.get("subject"))
        record["source_ids"] = source_ids
        set_field_provenance(record, updated_fields, "papers_cool")
        quality_flags = [
            ensure_str(item)
            for item in to_string_list(record.get("quality_flags"))
            if ensure_str(item)
        ]
        if PAPERS_COOL_FALLBACK_FLAG not in quality_flags:
            quality_flags.append(PAPERS_COOL_FALLBACK_FLAG)
        record["quality_flags"] = quality_flags
        result["updated"] = True
        result["updated_fields"] = updated_fields
        return result

    result["rejected_reason"] = "no_missing_fields_updated"
    return result


def resolve_icml_pmlr_volume(year: int) -> Optional[str]:
    """Resolve ICML year to PMLR volume slug."""
    return _ICML_PMLR_VOLUMES.get(year)


def load_pmlr_title_index(volume: str, timeout: float, retries: int) -> Dict[str, str]:
    """Build normalized-title -> PMLR abstract page URL index for one volume."""
    cached = _PMLR_INDEX_CACHE.get(volume)
    if cached is not None:
        return cached

    index_url = f"{PMLR_BASE}/{volume}/"
    html_text = fetch_text_with_retry(index_url, timeout=timeout, retries=retries)
    if not html_text:
        _PMLR_INDEX_CACHE[volume] = {}
        return {}

    mapping: Dict[str, str] = {}
    blocks = re.findall(r'<div class="paper">(.*?)</div>', html_text, flags=re.IGNORECASE | re.DOTALL)
    for block in blocks:
        title_match = re.search(r'<p class="title">(.*?)</p>', block, flags=re.IGNORECASE | re.DOTALL)
        link_match = re.search(r'\[<a href="([^"]+)">abs</a>\]', block, flags=re.IGNORECASE)
        if not title_match or not link_match:
            continue
        raw_title = html_lib.unescape(re.sub(r"<[^>]+>", " ", title_match.group(1)))
        clean_title = re.sub(r"\s+", " ", raw_title).strip()
        normalized = normalize_title(clean_title)
        if not normalized:
            continue
        abs_url = ensure_str(link_match.group(1))
        if abs_url.startswith("http://") or abs_url.startswith("https://"):
            mapping[normalized] = abs_url
        elif abs_url.startswith("/"):
            mapping[normalized] = f"{PMLR_BASE}{abs_url}"
        else:
            mapping[normalized] = f"{PMLR_BASE}/{volume}/{abs_url}"

    _PMLR_INDEX_CACHE[volume] = mapping
    return mapping


def resolve_pmlr_abs_url_by_title(
    *,
    venue: str,
    year: int,
    title: str,
    timeout: float,
    retries: int,
) -> Optional[str]:
    """Resolve PMLR abstract-page URL by title for ICML records."""
    if ensure_str(venue).upper() != "ICML":
        return None
    volume = resolve_icml_pmlr_volume(year)
    if not volume:
        return None
    mapping = load_pmlr_title_index(volume=volume, timeout=timeout, retries=retries)
    if not mapping:
        return None

    normalized = normalize_title(title)
    if normalized in mapping:
        return mapping[normalized]

    if not normalized:
        return None
    best_url: Optional[str] = None
    best_ratio = 0.0
    for candidate_norm, candidate_url in mapping.items():
        ratio = SequenceMatcher(None, normalized, candidate_norm).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_url = candidate_url
    if best_ratio >= 0.992:
        return best_url
    return None


def fetch_pmlr_abstract(abs_url: str, timeout: float, retries: int) -> Optional[str]:
    """Fetch abstract text from one PMLR abstract page."""
    cached = _PMLR_ABSTRACT_CACHE.get(abs_url)
    if cached is not None:
        return cached

    html_text = fetch_text_with_retry(abs_url, timeout=timeout, retries=retries)
    if not html_text:
        _PMLR_ABSTRACT_CACHE[abs_url] = None
        return None

    match = re.search(
        r'<div[^>]*id="abstract"[^>]*>(.*?)</div>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        _PMLR_ABSTRACT_CACHE[abs_url] = None
        return None

    abstract = html_lib.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
    abstract = re.sub(r"\s+", " ", abstract).strip()
    _PMLR_ABSTRACT_CACHE[abs_url] = abstract or None
    return _PMLR_ABSTRACT_CACHE[abs_url]


def backfill_from_pmlr(
    *,
    record: Dict[str, Any],
    venue: str,
    year: int,
    timeout: float,
    retries: int,
) -> bool:
    """Backfill abstract from PMLR for ICML papers when available."""
    if not is_missing_text(record.get("abstract")):
        return False
    title = ensure_str(record.get("paper_title") or record.get("title"))
    if not title:
        return False
    abs_url = resolve_pmlr_abs_url_by_title(
        venue=venue,
        year=year,
        title=title,
        timeout=timeout,
        retries=retries,
    )
    if not abs_url:
        return False
    abstract = fetch_pmlr_abstract(abs_url=abs_url, timeout=timeout, retries=retries)
    if not abstract:
        return False
    record["abstract"] = abstract
    source_ids = record.get("source_ids", {})
    if not isinstance(source_ids, dict):
        source_ids = {}
    source_ids.setdefault("pmlr_abs_url", abs_url)
    record["source_ids"] = source_ids
    set_field_provenance(record, ["abstract"], "venue_special")
    return True


def arxiv_wait_for_rate_limit(min_interval_seconds: float = ARXIV_DEFAULT_MIN_INTERVAL_SECONDS) -> None:
    """Throttle arXiv requests to avoid 429."""
    global _ARXIV_LAST_REQUEST_AT
    elapsed = time.monotonic() - _ARXIV_LAST_REQUEST_AT
    if elapsed < min_interval_seconds:
        time.sleep(min_interval_seconds - elapsed)


def build_arxiv_headers() -> Dict[str, str]:
    """Build arXiv headers with explicit User-Agent."""
    mailto = ensure_str(os.environ.get("JANUSSEARCH_EMAIL"))
    user_agent = ARXIV_DEFAULT_USER_AGENT
    if mailto:
        user_agent = f"{ARXIV_DEFAULT_USER_AGENT} (mailto:{mailto})"
    return {"Accept": "application/atom+xml", "User-Agent": user_agent}


def titles_match(local_title: str, remote_title: str) -> bool:
    """Strict title check to reduce false-positive backfills."""
    local_norm = normalize_title(local_title)
    remote_norm = normalize_title(remote_title)
    if not local_norm or not remote_norm:
        return False
    if local_norm == remote_norm:
        return True
    ratio = SequenceMatcher(None, local_norm, remote_norm).ratio()
    return ratio >= 0.965


def parse_s2_result_by_title(
    local_title: str, payload: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Pick best Semantic Scholar search result for local title."""
    if not payload or not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    candidates = [item for item in data if isinstance(item, dict)]
    if not candidates:
        return None
    for candidate in candidates:
        title = ensure_str(candidate.get("title"))
        if titles_match(local_title, title):
            return candidate
    return None


def parse_arxiv_id(record: Dict[str, Any], s2_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """Extract arXiv ID from existing fields."""
    candidates: List[str] = []
    doi = canonicalize_doi(record.get("doi"))
    if doi:
        candidates.append(doi)
    for key in ("url", "external_url"):
        value = ensure_str(record.get(key))
        if value:
            candidates.append(value)
    if s2_data and isinstance(s2_data.get("externalIds"), dict):
        external_ids = s2_data["externalIds"]
        arxiv_id = ensure_str(external_ids.get("ArXiv"))
        if arxiv_id:
            return arxiv_id
        doi_from_s2 = canonicalize_doi(external_ids.get("DOI"))
        if doi_from_s2:
            candidates.append(doi_from_s2)

    pattern = re.compile(r"(\d{4}\.\d{4,5}(?:v\d+)?)")
    for candidate in candidates:
        if "arxiv" not in candidate.lower():
            continue
        match = pattern.search(candidate)
        if match:
            return match.group(1)
    for candidate in candidates:
        if "arxiv.org/abs/" in candidate.lower():
            parsed = urlparse(candidate)
            identifier = parsed.path.rsplit("/", maxsplit=1)[-1]
            if identifier:
                return identifier
    return None


def fetch_arxiv_abstract(arxiv_id: str, timeout: float, retries: int) -> Optional[str]:
    """Fetch abstract from arXiv Atom API for one paper."""
    params = urlencode({"id_list": arxiv_id, "max_results": "1"})
    url = f"{ARXIV_BASE}?{params}"

    for attempt in range(1, retries + 1):
        try:
            arxiv_wait_for_rate_limit()
            request = Request(url, headers=build_arxiv_headers(), method="GET")
            with urlopen(request, timeout=timeout) as response:
                global _ARXIV_LAST_REQUEST_AT
                _ARXIV_LAST_REQUEST_AT = time.monotonic()
                xml_body = response.read().decode("utf-8", errors="ignore")
            root = et.fromstring(xml_body)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            summary = root.findtext("atom:entry/atom:summary", default="", namespaces=ns)
            abstract = re.sub(r"\s+", " ", ensure_str(summary)).strip()
            return abstract or None
        except (HTTPError, URLError, TimeoutError, socket.timeout, et.ParseError) as err:
            if attempt == retries:
                LOGGER.warning("arXiv fetch failed for %s: %s", arxiv_id, err)
                return None
            wait = min(10.0, 1.2 * (2 ** (attempt - 1)))
            time.sleep(wait)
    return None


def search_arxiv_abstract_by_title(
    title: str, timeout: float, retries: int, max_results: int = 3
) -> Optional[str]:
    """Search arXiv by title and return best-matching abstract."""
    title_query = ensure_str(title)
    if not title_query:
        return None

    params = urlencode(
        {
            "search_query": f'ti:"{title_query}"',
            "start": "0",
            "max_results": str(max_results),
        }
    )
    url = f"{ARXIV_BASE}?{params}"

    for attempt in range(1, retries + 1):
        try:
            arxiv_wait_for_rate_limit()
            request = Request(url, headers=build_arxiv_headers(), method="GET")
            with urlopen(request, timeout=timeout) as response:
                global _ARXIV_LAST_REQUEST_AT
                _ARXIV_LAST_REQUEST_AT = time.monotonic()
                xml_body = response.read().decode("utf-8", errors="ignore")
            root = et.fromstring(xml_body)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            best_abstract: Optional[str] = None
            best_ratio = 0.0

            for entry in root.findall("atom:entry", namespaces=ns):
                candidate_title = ensure_str(entry.findtext("atom:title", default="", namespaces=ns))
                candidate_summary = ensure_str(
                    entry.findtext("atom:summary", default="", namespaces=ns)
                )
                if not candidate_title or not candidate_summary:
                    continue
                if titles_match(title_query, candidate_title):
                    return re.sub(r"\s+", " ", candidate_summary).strip()
                ratio = SequenceMatcher(
                    None, normalize_title(title_query), normalize_title(candidate_title)
                ).ratio()
                if ratio > best_ratio and ratio >= 0.86:
                    best_ratio = ratio
                    best_abstract = re.sub(r"\s+", " ", candidate_summary).strip()
            return best_abstract
        except (HTTPError, URLError, TimeoutError, socket.timeout, et.ParseError) as err:
            if attempt == retries:
                LOGGER.warning("arXiv title search failed for %s: %s", title_query[:80], err)
                return None
            wait = min(10.0, 1.2 * (2 ** (attempt - 1)))
            time.sleep(wait)
    return None


def apply_s2_data(record: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    """Apply Semantic Scholar data into one record."""
    changed = False
    abstract = ensure_str(payload.get("abstract"))
    if is_missing_text(record.get("abstract")) and abstract:
        record["abstract"] = abstract
        set_field_provenance(record, ["abstract"], "s2")
        changed = True

    authors = payload.get("authors")
    if isinstance(authors, list):
        extracted = []
        for author in authors:
            if isinstance(author, dict):
                name = ensure_str(author.get("name"))
                if name:
                    extracted.append(name)
        if extracted and not to_string_list(record.get("authors")):
            record["authors"] = unique_preserve_order(extracted)
            set_field_provenance(record, ["authors"], "s2")
            changed = True

    citation = payload.get("citationCount")
    if isinstance(citation, int):
        if record.get("citation_count") is None or citation > int(record.get("citation_count", 0)):
            record["citation_count"] = citation
            changed = True

    url = ensure_str(payload.get("url"))
    if url and is_missing_text(record.get("url")):
        record["url"] = url
        set_field_provenance(record, ["url"], "s2")
        changed = True

    paper_id = ensure_str(payload.get("paperId"))
    if paper_id and ensure_str(record.get("semantic_scholar_paper_id")) != paper_id:
        record["semantic_scholar_paper_id"] = paper_id
        changed = True

    external_ids = payload.get("externalIds")
    if isinstance(external_ids, dict):
        external_doi = canonicalize_doi(external_ids.get("DOI"))
        if external_doi and not canonicalize_doi(record.get("doi")):
            record["doi"] = external_doi
            changed = True

    if changed:
        source_ids = record.get("source_ids", {})
        if not isinstance(source_ids, dict):
            source_ids = {}
        if paper_id:
            source_ids["semantic_scholar_paper_id"] = paper_id
        if record.get("doi"):
            source_ids["doi"] = record.get("doi")
        record["source_ids"] = source_ids
    return changed


def backfill_records(
    records: List[Dict[str, Any]],
    *,
    venue: str,
    year: int,
    client: SemanticScholarClient,
    timeout: float,
    retries: int,
    max_records: int,
    enable_arxiv_title: bool,
    enable_papers_cool: bool,
    papers_cool_policy: str,
) -> Dict[str, Any]:
    """Backfill missing abstracts for resolved records."""
    candidates = [
        record
        for record in records
        if ensure_str(record.get("record_status")) != "placeholder"
        and is_missing_text(record.get("abstract"))
    ]
    if max_records > 0:
        candidates = candidates[:max_records]

    stats = {
        "candidates": len(candidates),
        "updated_records": 0,
        "pmlr_hits": 0,
        "s2_doi_hits": 0,
        "s2_title_hits": 0,
        "arxiv_hits": 0,
        "arxiv_title_hits": 0,
        "papers_cool_hits": 0,
        "papers_cool_rejected": 0,
        "papers_cool_fields_filled": 0,
        "failed_records": 0,
    }

    for index, record in enumerate(candidates, start=1):
        title = ensure_str(record.get("paper_title"))
        LOGGER.info("Backfill %d/%d: %s", index, len(candidates), title[:120])

        updated = False
        s2_data: Optional[Dict[str, Any]] = None

        if backfill_from_pmlr(
            record=record,
            venue=venue,
            year=year,
            timeout=timeout,
            retries=retries,
        ):
            updated = True
            stats["pmlr_hits"] += 1

        doi = canonicalize_doi(record.get("doi"))
        if doi and is_missing_text(record.get("abstract")):
            s2_data = client.lookup_by_doi(doi)
            if s2_data and titles_match(title, ensure_str(s2_data.get("title"))):
                if apply_s2_data(record, s2_data):
                    updated = True
                stats["s2_doi_hits"] += 1
            else:
                s2_data = None

        if not s2_data and is_missing_text(record.get("abstract")):
            s2_search = client.search_by_title(title)
            candidate = parse_s2_result_by_title(local_title=title, payload=s2_search)
            if candidate:
                if apply_s2_data(record, candidate):
                    updated = True
                s2_data = candidate
                stats["s2_title_hits"] += 1

        if is_missing_text(record.get("abstract")):
            arxiv_id = parse_arxiv_id(record, s2_data)
            if arxiv_id:
                arxiv_abstract = fetch_arxiv_abstract(
                    arxiv_id=arxiv_id,
                    timeout=timeout,
                    retries=retries,
                )
                if arxiv_abstract:
                    record["abstract"] = arxiv_abstract
                    set_field_provenance(record, ["abstract"], "arxiv")
                    updated = True
                    stats["arxiv_hits"] += 1

        if enable_arxiv_title and is_missing_text(record.get("abstract")):
            arxiv_title_abstract = search_arxiv_abstract_by_title(
                title=title,
                timeout=timeout,
                retries=retries,
            )
            if arxiv_title_abstract:
                record["abstract"] = arxiv_title_abstract
                set_field_provenance(record, ["abstract"], "arxiv")
                updated = True
                stats["arxiv_title_hits"] += 1

        if enable_papers_cool and (
            is_missing_text(record.get("abstract"))
            or is_missing_text(record.get("url"))
            or not to_string_list(record.get("authors"))
        ):
            papers_cool_result = backfill_from_papers_cool(
                record=record,
                venue=venue,
                year=year,
                timeout=timeout,
                retries=retries,
                policy=papers_cool_policy,
            )
            if papers_cool_result["updated"]:
                updated = True
                stats["papers_cool_hits"] += 1
                stats["papers_cool_fields_filled"] += len(papers_cool_result["updated_fields"])
            elif papers_cool_result["rejected_reason"]:
                stats["papers_cool_rejected"] += 1

        if updated:
            record["record_status"] = "repaired"
            record["quality_flags"] = normalize_quality_flags(record)
            stats["updated_records"] += 1
        else:
            stats["failed_records"] += 1

    return stats


def run_inventory(input_glob: str, report_path: Path) -> Dict[str, Any]:
    """Run inventory step without mutation."""
    files = find_input_files(input_glob)
    file_items: List[Dict[str, Any]] = []
    summary = {
        "generated_at_utc": utc_now_iso(),
        "file_count": 0,
        "total_records": 0,
        "resolved_records": 0,
        "placeholder_records": 0,
        "duplicate_titles": 0,
    }

    for file_path in files:
        payload = load_payload(file_path)
        context = infer_context(payload, file_path)
        root_payload, _canonical_payload, metrics, duplicates_removed = normalize_payload(
            payload=payload,
            context=context,
        )
        del root_payload
        file_item = {
            "file": file_path.name,
            "venue": context.venue,
            "year": context.year,
            "provider": context.provider,
            "duplicates_if_normalized": duplicates_removed,
            "metrics": metrics,
        }
        file_items.append(file_item)

        summary["file_count"] += 1
        summary["total_records"] += metrics["total"]
        summary["resolved_records"] += metrics["resolved_total"]
        summary["placeholder_records"] += metrics["placeholder_total"]
        summary["duplicate_titles"] += metrics["duplicate_title_count"]

    report = {"summary": summary, "files": file_items}
    write_json(report_path, report)
    LOGGER.info("Inventory report written: %s", report_path)
    return report


def run_normalize(
    input_glob: str,
    canonical_root: Path,
    backup_root: Path,
    report_path: Path,
    write_back: bool,
) -> Dict[str, Any]:
    """Run normalization and writing of root + canonical files."""
    files = find_input_files(input_glob)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    file_items: List[Dict[str, Any]] = []
    summary = {
        "generated_at_utc": utc_now_iso(),
        "file_count": 0,
        "records_after_normalize": 0,
        "duplicates_removed": 0,
        "backups_created": 0,
    }

    for file_path in files:
        payload = load_payload(file_path)
        context = infer_context(payload, file_path)
        root_payload, canonical_payload, metrics, duplicates_removed = normalize_payload(
            payload=payload,
            context=context,
        )

        canonical_path = canonical_root / normalize_slug(context.venue) / f"{context.year}.json"
        write_json(canonical_path, canonical_payload)

        backup_path = None
        if write_back:
            backup_path = backup_file(file_path, backup_root=backup_root, run_id=run_id)
            write_json(file_path, root_payload)
            summary["backups_created"] += 1

        file_items.append(
            {
                "file": file_path.name,
                "venue": context.venue,
                "year": context.year,
                "duplicates_removed": duplicates_removed,
                "canonical_path": str(canonical_path),
                "backup_path": str(backup_path) if backup_path else None,
                "metrics": metrics,
            }
        )

        summary["file_count"] += 1
        summary["records_after_normalize"] += metrics["total"]
        summary["duplicates_removed"] += duplicates_removed

        LOGGER.info(
            "Normalized %s: total=%s duplicates_removed=%s",
            file_path.name,
            metrics["total"],
            duplicates_removed,
        )

    report = {"summary": summary, "files": file_items}
    write_json(report_path, report)
    LOGGER.info("Normalize report written: %s", report_path)
    return report


def run_backfill(
    input_glob: str,
    canonical_root: Path,
    backup_root: Path,
    report_path: Path,
    write_back: bool,
    timeout: float,
    retries: int,
    max_records_per_file: int,
    min_interval: float,
    enable_arxiv_title: bool,
    enable_papers_cool: bool,
    papers_cool_policy: str,
) -> Dict[str, Any]:
    """Run conference-special + API + fallback backfill for missing fields."""
    files = find_input_files(input_glob)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    api_key = ensure_str(os.environ.get("SEMANTIC_SCHOLAR_API_KEY")) or None
    client = SemanticScholarClient(
        api_key=api_key,
        timeout=timeout,
        retries=retries,
        min_interval_seconds=min_interval,
    )

    summary = {
        "generated_at_utc": utc_now_iso(),
        "api_key_used": bool(api_key),
        "file_count": 0,
        "updated_records": 0,
        "candidates": 0,
        "failed_records": 0,
        "papers_cool_hits": 0,
        "papers_cool_rejected": 0,
        "papers_cool_fields_filled": 0,
    }
    file_items: List[Dict[str, Any]] = []

    for file_path in files:
        payload = load_payload(file_path)
        context = infer_context(payload, file_path)
        root_payload, canonical_payload, _metrics_before, _duplicates_removed = normalize_payload(
            payload=payload,
            context=context,
        )
        records = root_payload["papers"]
        stats = backfill_records(
            records=records,
            venue=context.venue,
            year=context.year,
            client=client,
            timeout=timeout,
            retries=retries,
            max_records=max_records_per_file,
            enable_arxiv_title=enable_arxiv_title,
            enable_papers_cool=enable_papers_cool,
            papers_cool_policy=papers_cool_policy,
        )

        # Re-run normalization phase to ensure status and metrics are coherent.
        root_payload["papers"] = records
        root_payload["paper_count"] = len(records)
        root_payload["generated_at_utc"] = ensure_str(root_payload.get("generated_at_utc")) or utc_now_iso()
        root_payload, canonical_payload, metrics_after, duplicates_removed = normalize_payload(
            payload=root_payload,
            context=context,
        )

        canonical_path = canonical_root / normalize_slug(context.venue) / f"{context.year}.json"
        write_json(canonical_path, canonical_payload)
        backup_path = None
        if write_back:
            backup_path = backup_file(file_path, backup_root=backup_root, run_id=run_id)
            write_json(file_path, root_payload)

        item = {
            "file": file_path.name,
            "venue": context.venue,
            "year": context.year,
            "stats": stats,
            "duplicates_removed": duplicates_removed,
            "metrics_after": metrics_after,
            "canonical_path": str(canonical_path),
            "backup_path": str(backup_path) if backup_path else None,
        }
        file_items.append(item)

        summary["file_count"] += 1
        summary["updated_records"] += stats["updated_records"]
        summary["candidates"] += stats["candidates"]
        summary["failed_records"] += stats["failed_records"]
        summary["papers_cool_hits"] += stats["papers_cool_hits"]
        summary["papers_cool_rejected"] += stats["papers_cool_rejected"]
        summary["papers_cool_fields_filled"] += stats["papers_cool_fields_filled"]

        LOGGER.info(
            "Backfill %s: candidates=%s updated=%s failed=%s papers_cool_hits=%s",
            file_path.name,
            stats["candidates"],
            stats["updated_records"],
            stats["failed_records"],
            stats["papers_cool_hits"],
        )

    report = {"summary": summary, "files": file_items}
    write_json(report_path, report)
    LOGGER.info("Backfill report written: %s", report_path)
    return report


def run_validate(
    input_glob: str,
    report_path: Path,
    stats_md_path: Path,
    threshold_authors: float,
    threshold_abstract: float,
    enforce_official_alignment: bool,
) -> Tuple[Dict[str, Any], bool]:
    """Run quality checks and produce validation report."""
    files = find_input_files(input_glob)
    file_items: List[Dict[str, Any]] = []
    summary = {
        "generated_at_utc": utc_now_iso(),
        "file_count": 0,
        "total_records": 0,
        "resolved_records": 0,
        "placeholder_records": 0,
        "duplicate_titles": 0,
        "gate_pass_files": 0,
        "gate_fail_files": 0,
        "threshold_authors": threshold_authors,
        "threshold_abstract": threshold_abstract,
        "enforce_official_alignment": enforce_official_alignment,
        "alignment_pass_files": 0,
        "alignment_fail_files": 0,
    }
    all_pass = True
    official_cache: Dict[str, Dict[str, Any]] = {}

    for file_path in files:
        payload = load_payload(file_path)
        context = infer_context(payload, file_path)
        root_payload, _canonical_payload, metrics, _duplicates_removed = normalize_payload(
            payload=payload,
            context=context,
        )
        alignment = evaluate_alignment(
            payload=root_payload,
            records=root_payload["papers"],
            context=context,
            official_cache=official_cache,
        )
        issues: List[str] = []
        gate_pass = True
        if metrics["duplicate_title_count"] != 0:
            gate_pass = False
            issues.append(f"duplicate_title_count={metrics['duplicate_title_count']}")
        if metrics["resolved_authors_coverage"] < threshold_authors:
            gate_pass = False
            issues.append(
                f"resolved_authors_coverage={metrics['resolved_authors_coverage']:.2f}< {threshold_authors:.2f}"
            )
        if metrics["resolved_abstract_coverage"] < threshold_abstract:
            gate_pass = False
            issues.append(
                f"resolved_abstract_coverage={metrics['resolved_abstract_coverage']:.2f}< {threshold_abstract:.2f}"
            )
        if enforce_official_alignment:
            paper_alignment = alignment["paper_count"]["aligned"]
            track_alignment = alignment["track_counts"]["aligned"]
            level_alignment = alignment["presentation_level_counts"]["aligned"]

            if paper_alignment is False:
                gate_pass = False
                issues.append(
                    "official_paper_count_mismatch="
                    f"{alignment['paper_count']['actual']} vs {alignment['paper_count']['official']}"
                )
            if track_alignment is False:
                gate_pass = False
                issues.append(
                    "official_track_counts_mismatch="
                    f"{alignment['track_counts']['diff']}"
                )
            if level_alignment is False:
                gate_pass = False
                issues.append(
                    "official_presentation_counts_mismatch="
                    f"{alignment['presentation_level_counts']['diff']}"
                )

        alignment_checks = [
            alignment["paper_count"]["aligned"],
            alignment["track_counts"]["aligned"],
            alignment["presentation_level_counts"]["aligned"],
        ]
        alignment_pass = all(value is not False for value in alignment_checks)
        if alignment_pass:
            summary["alignment_pass_files"] += 1
        else:
            summary["alignment_fail_files"] += 1

        if gate_pass:
            summary["gate_pass_files"] += 1
        else:
            summary["gate_fail_files"] += 1
            all_pass = False

        file_items.append(
            {
                "file": file_path.name,
                "venue": context.venue,
                "year": context.year,
                "metrics": metrics,
                "alignment": alignment,
                "alignment_pass": alignment_pass,
                "gate_pass": gate_pass,
                "issues": issues,
            }
        )

        summary["file_count"] += 1
        summary["total_records"] += metrics["total"]
        summary["resolved_records"] += metrics["resolved_total"]
        summary["placeholder_records"] += metrics["placeholder_total"]
        summary["duplicate_titles"] += metrics["duplicate_title_count"]

    report = {"summary": summary, "files": file_items}
    write_json(report_path, report)
    stats_md_path.parent.mkdir(parents=True, exist_ok=True)
    stats_md_path.write_text(render_stats_markdown(file_items, summary), encoding="utf-8")
    LOGGER.info("Validation report written: %s", report_path)
    LOGGER.info("Stats markdown written: %s", stats_md_path)
    return report, all_pass


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="M1 data pipeline")
    parser.add_argument(
        "--input-glob",
        default=DEFAULT_INPUT_GLOB,
        help=f"Input file glob (default: {DEFAULT_INPUT_GLOB})",
    )
    parser.add_argument(
        "--canonical-root",
        default=str(DEFAULT_CANONICAL_ROOT),
        help=f"Canonical output root (default: {DEFAULT_CANONICAL_ROOT})",
    )
    parser.add_argument(
        "--backup-root",
        default=str(DEFAULT_BACKUP_ROOT),
        help=f"Backup root for write-back files (default: {DEFAULT_BACKUP_ROOT})",
    )
    parser.add_argument(
        "--index-root",
        default=str(DEFAULT_INDEX_ROOT),
        help=f"Artifacts/report root (default: {DEFAULT_INDEX_ROOT})",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Log level",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("inventory", help="Inspect and report current data quality")

    normalize = subparsers.add_parser("normalize", help="Normalize and deduplicate files")
    normalize.add_argument(
        "--no-write-back",
        action="store_true",
        help="Do not write root files, only canonical outputs",
    )

    backfill = subparsers.add_parser("backfill", help="Backfill missing abstracts")
    backfill.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds")
    backfill.add_argument("--retries", type=int, default=3, help="Retry count")
    backfill.add_argument(
        "--max-records-per-file",
        type=int,
        default=0,
        help="Cap backfill records per file, 0 for unlimited",
    )
    backfill.add_argument(
        "--min-interval",
        type=float,
        default=3.0,
        help="Minimum interval between S2 requests in seconds",
    )
    backfill.add_argument(
        "--no-write-back",
        action="store_true",
        help="Do not write root files, only canonical outputs",
    )
    backfill.add_argument(
        "--enable-arxiv-title",
        action="store_true",
        help="Enable arXiv title-based fallback search (off by default).",
    )
    backfill.add_argument(
        "--enable-papers-cool",
        action="store_true",
        help="Enable papers.cool fallback for ACL/AAAI missing fields (off by default).",
    )
    backfill.add_argument(
        "--papers-cool-policy",
        default=PAPERS_COOL_DEFAULT_POLICY,
        choices=PAPERS_COOL_POLICY_CHOICES,
        help=f"papers.cool fallback policy (default: {PAPERS_COOL_DEFAULT_POLICY}).",
    )

    validate = subparsers.add_parser("validate", help="Run validation gates")
    validate.add_argument(
        "--threshold-authors",
        type=float,
        default=DEFAULT_AUTHORS_THRESHOLD,
        help=f"Resolved authors coverage threshold (default: {DEFAULT_AUTHORS_THRESHOLD})",
    )
    validate.add_argument(
        "--threshold-abstract",
        type=float,
        default=DEFAULT_ABSTRACT_THRESHOLD,
        help=f"Resolved abstract coverage threshold (default: {DEFAULT_ABSTRACT_THRESHOLD})",
    )
    validate.add_argument(
        "--skip-official-alignment",
        action="store_true",
        help="Skip official count alignment checks in validation.",
    )

    run = subparsers.add_parser("run", help="Run inventory -> normalize -> backfill -> validate")
    run.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds")
    run.add_argument("--retries", type=int, default=3, help="Retry count")
    run.add_argument(
        "--max-records-per-file",
        type=int,
        default=0,
        help="Cap backfill records per file, 0 for unlimited",
    )
    run.add_argument(
        "--min-interval",
        type=float,
        default=3.0,
        help="Minimum interval between S2 requests in seconds",
    )
    run.add_argument(
        "--threshold-authors",
        type=float,
        default=DEFAULT_AUTHORS_THRESHOLD,
        help=f"Resolved authors coverage threshold (default: {DEFAULT_AUTHORS_THRESHOLD})",
    )
    run.add_argument(
        "--threshold-abstract",
        type=float,
        default=DEFAULT_ABSTRACT_THRESHOLD,
        help=f"Resolved abstract coverage threshold (default: {DEFAULT_ABSTRACT_THRESHOLD})",
    )
    run.add_argument(
        "--enable-arxiv-title",
        action="store_true",
        help="Enable arXiv title-based fallback search (off by default).",
    )
    run.add_argument(
        "--enable-papers-cool",
        action="store_true",
        help="Enable papers.cool fallback for ACL/AAAI missing fields (off by default).",
    )
    run.add_argument(
        "--papers-cool-policy",
        default=PAPERS_COOL_DEFAULT_POLICY,
        choices=PAPERS_COOL_POLICY_CHOICES,
        help=f"papers.cool fallback policy (default: {PAPERS_COOL_DEFAULT_POLICY}).",
    )
    run.add_argument(
        "--skip-official-alignment",
        action="store_true",
        help="Skip official count alignment checks in validation.",
    )
    return parser


def main() -> int:
    """CLI entrypoint."""
    parser = build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    canonical_root = Path(args.canonical_root)
    backup_root = Path(args.backup_root)
    index_root = Path(args.index_root)
    m1_root = index_root / "m1"
    m1_root.mkdir(parents=True, exist_ok=True)

    inventory_report = m1_root / "inventory.json"
    normalize_report = m1_root / "normalize_report.json"
    backfill_report = m1_root / "backfill_report.json"
    validate_report = m1_root / "quality_report.json"
    stats_md = m1_root / "stats.md"

    if args.command == "inventory":
        run_inventory(args.input_glob, inventory_report)
        return 0

    if args.command == "normalize":
        run_normalize(
            input_glob=args.input_glob,
            canonical_root=canonical_root,
            backup_root=backup_root,
            report_path=normalize_report,
            write_back=not args.no_write_back,
        )
        return 0

    if args.command == "backfill":
        run_backfill(
            input_glob=args.input_glob,
            canonical_root=canonical_root,
            backup_root=backup_root,
            report_path=backfill_report,
            write_back=not args.no_write_back,
            timeout=args.timeout,
            retries=args.retries,
            max_records_per_file=args.max_records_per_file,
            min_interval=args.min_interval,
            enable_arxiv_title=args.enable_arxiv_title,
            enable_papers_cool=args.enable_papers_cool,
            papers_cool_policy=args.papers_cool_policy,
        )
        return 0

    if args.command == "validate":
        _report, all_pass = run_validate(
            input_glob=args.input_glob,
            report_path=validate_report,
            stats_md_path=stats_md,
            threshold_authors=args.threshold_authors,
            threshold_abstract=args.threshold_abstract,
            enforce_official_alignment=not args.skip_official_alignment,
        )
        return 0 if all_pass else 1

    if args.command == "run":
        run_inventory(args.input_glob, inventory_report)
        run_normalize(
            input_glob=args.input_glob,
            canonical_root=canonical_root,
            backup_root=backup_root,
            report_path=normalize_report,
            write_back=True,
        )
        run_backfill(
            input_glob=args.input_glob,
            canonical_root=canonical_root,
            backup_root=backup_root,
            report_path=backfill_report,
            write_back=True,
            timeout=args.timeout,
            retries=args.retries,
            max_records_per_file=args.max_records_per_file,
            min_interval=args.min_interval,
            enable_arxiv_title=args.enable_arxiv_title,
            enable_papers_cool=args.enable_papers_cool,
            papers_cool_policy=args.papers_cool_policy,
        )
        _report, all_pass = run_validate(
            input_glob=args.input_glob,
            report_path=validate_report,
            stats_md_path=stats_md,
            threshold_authors=args.threshold_authors,
            threshold_abstract=args.threshold_abstract,
            enforce_official_alignment=not args.skip_official_alignment,
        )
        return 0 if all_pass else 1

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
