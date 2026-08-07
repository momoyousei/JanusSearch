#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect ICML papers from the official ICML virtual JSON."""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("icml_collect")

ICML_BASE_URL = "https://icml.cc"
ICML_VIRTUAL_JSON_URL_TEMPLATE = (
    f"{ICML_BASE_URL}/static/virtual/data/icml-{{year}}-orals-posters.json"
)
ICML_VIRTUAL_PAPERS_URL_TEMPLATE = f"{ICML_BASE_URL}/virtual/{{year}}/papers.html"
DEFAULT_OUTPUT_ROOT = Path("archives/root_json")
DEFAULT_INDEX_ROOT = Path("artifacts")

DEFAULT_HEADERS = {
    "User-Agent": "JanusSearch/1.0 (mailto:janus@example.com)",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Connection": "close",
}
PRESENTATION_LEVEL_RANK = {"poster": 0, "oral": 1, "bestpaper": 2}


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


def normalize_spaces(text: str) -> str:
    """Collapse internal whitespace and decode common HTML entities."""
    return re.sub(r"\s+", " ", html.unescape(ensure_str(text))).strip()


def dedupe_strings(items: Iterable[str]) -> List[str]:
    """Deduplicate non-empty strings while preserving order."""
    seen: set[str] = set()
    result: List[str] = []
    for item in items:
        normalized = normalize_spaces(item)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def normalize_title_key(value: str) -> str:
    """Normalize a title for deterministic sorting and dedupe."""
    return re.sub(r"\s+", " ", normalize_spaces(value)).casefold()


def parse_years(raw: str) -> List[int]:
    """Parse year expression like '2026', '2025,2026', or '2021-2026'."""
    value = ensure_str(raw)
    if not value:
        raise ValueError("Years cannot be empty")
    if "," in value:
        years = [int(item.strip()) for item in value.split(",") if item.strip()]
        if not years:
            raise ValueError("No years parsed from comma-separated input")
        return years
    if "-" in value:
        start_text, end_text = value.split("-", maxsplit=1)
        start = int(start_text.strip())
        end = int(end_text.strip())
        if start > end:
            raise ValueError(f"Invalid year range: {value}")
        return list(range(start, end + 1))
    return [int(value)]


def fetch_json(url: str, timeout: float, retries: int, min_interval: float) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Fetch URL JSON with retry and return response headers."""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        if attempt > 1 and min_interval > 0:
            time.sleep(min_interval)
        request = Request(url, headers=DEFAULT_HEADERS)
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "ignore"))
                if not isinstance(payload, dict):
                    raise RuntimeError(f"Unexpected JSON payload type: {type(payload)}")
                headers = {key.lower(): value for key, value in response.headers.items()}
                return payload, headers
        except (
            HTTPError,
            URLError,
            TimeoutError,
            socket.timeout,
            ConnectionError,
            OSError,
            json.JSONDecodeError,
            RuntimeError,
        ) as err:
            last_err = err
            LOGGER.warning("Fetch failed (%s/%s) %s: %s", attempt, retries, url, err)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


def to_abs_url(value: str) -> str:
    """Convert an ICML-relative URL to an absolute URL."""
    raw = ensure_str(value)
    if not raw:
        return ""
    return urljoin(ICML_BASE_URL, raw)


def parse_author_fields(authors_raw: Any) -> Tuple[List[str], List[str]]:
    """Parse author names and institutions from ICML virtual JSON."""
    if not isinstance(authors_raw, list):
        return [], []
    authors: List[str] = []
    institutions: List[str] = []
    for author in authors_raw:
        if not isinstance(author, dict):
            continue
        name = normalize_spaces(ensure_str(author.get("fullname")))
        institution = normalize_spaces(ensure_str(author.get("institution")))
        if name:
            authors.append(name)
        if institution:
            institutions.append(institution)
    return dedupe_strings(authors), dedupe_strings(institutions)


def normalize_presentation_level(value: str) -> str:
    """Map ICML decision text to project presentation levels."""
    text = ensure_str(value).lower()
    if "best" in text and "paper" in text:
        return "bestpaper"
    if "oral" in text or "spotlight" in text:
        return "oral"
    return "poster"


def infer_track_from_sourceurl(sourceurl: str) -> Tuple[str, str, str]:
    """Infer project track fields from the OpenReview group URL."""
    parsed = urlparse(sourceurl)
    group_id = ensure_str((parse_qs(parsed.query).get("id") or [""])[0])
    lower = group_id.lower()
    if lower.endswith("/conference"):
        return "conference", "Conference", "main"
    if "position_paper_track" in lower:
        return "position_paper_track", "Position Paper Track", "other"
    segment = group_id.split("/")[-1] if group_id else "other"
    track = re.sub(r"[^a-z0-9]+", "_", segment.lower()).strip("_") or "other"
    return track, track.replace("_", " ").title(), "other"


def extract_openreview_id(url: str) -> str | None:
    """Extract OpenReview forum id from URL."""
    parsed = urlparse(ensure_str(url))
    if "openreview.net" not in parsed.netloc.lower():
        return None
    value = ensure_str((parse_qs(parsed.query).get("id") or [""])[0])
    return value or None


def build_quality_flags(
    authors: Sequence[str],
    abstract: str,
    institutions: Sequence[str],
    keywords: Sequence[str],
) -> List[str]:
    """Build quality flags for one ICML virtual record."""
    flags: List[str] = []
    if not authors:
        flags.append("missing_authors")
    if not normalize_spaces(abstract):
        flags.append("missing_abstract")
    if not institutions:
        flags.append("missing_institutions")
    if not keywords:
        flags.append("missing_keywords")
    return flags


def build_record(item: Dict[str, Any], year: int, collected_at: str) -> Dict[str, Any]:
    """Build one root_json paper record from ICML virtual JSON."""
    title = normalize_spaces(ensure_str(item.get("name")))
    authors, institutions = parse_author_fields(item.get("authors"))
    abstract = normalize_spaces(ensure_str(item.get("abstract")))
    keywords_raw = item.get("keywords")
    keywords = (
        dedupe_strings([ensure_str(value) for value in keywords_raw])
        if isinstance(keywords_raw, list)
        else []
    )
    decision = normalize_spaces(ensure_str(item.get("decision")))
    presentation_level = normalize_presentation_level(decision)
    sourceurl = normalize_spaces(ensure_str(item.get("sourceurl")))
    track, track_display_name, track_group = infer_track_from_sourceurl(sourceurl)

    virtual_url = to_abs_url(ensure_str(item.get("virtualsite_url")))
    paper_url = to_abs_url(ensure_str(item.get("paper_url")))
    paper_pdf_url = to_abs_url(ensure_str(item.get("paper_pdf_url")))
    openreview_id = extract_openreview_id(paper_url)

    source_ids: Dict[str, str] = {}
    for key in ("id", "uid", "sourceid"):
        value = ensure_str(item.get(key))
        if value:
            source_ids[f"icml_virtual_{key}"] = value
    if virtual_url:
        source_ids["icml_virtualsite_url"] = virtual_url
    if sourceurl:
        source_ids["icml_sourceurl"] = sourceurl
    if decision:
        source_ids["icml_decision"] = decision
    eventtype = normalize_spaces(ensure_str(item.get("eventtype") or item.get("event_type")))
    if eventtype:
        source_ids["icml_eventtype"] = eventtype
    if paper_url:
        source_ids["icml_paper_url"] = paper_url
    if paper_pdf_url:
        source_ids["icml_paper_pdf_url"] = paper_pdf_url
    if openreview_id:
        source_ids["openreview_id"] = openreview_id

    quality_flags = build_quality_flags(
        authors=authors,
        abstract=abstract,
        institutions=institutions,
        keywords=keywords,
    )
    record_status = (
        "placeholder"
        if "missing_authors" in quality_flags or "missing_abstract" in quality_flags
        else "resolved"
    )

    return {
        "paper_title": title,
        "authors": authors,
        "institutions": institutions,
        "abstract": abstract,
        "keywords": keywords,
        "presentation_level": presentation_level,
        "openalex_id": None,
        "openreview_id": openreview_id,
        "doi": None,
        "track": track,
        "track_display_name": track_display_name,
        "track_group": track_group,
        "title": title,
        "url": virtual_url or None,
        "external_url": paper_pdf_url or paper_url or None,
        "citation_count": None,
        "venue": "ICML",
        "year": year,
        "source_provider": "icml_virtual",
        "collected_at": collected_at,
        "source_ids": source_ids,
        "record_status": record_status,
        "quality_flags": quality_flags,
    }


def record_score(record: Dict[str, Any]) -> Tuple[int, int, int]:
    """Rank duplicate ICML virtual records by presentation and completeness."""
    level = normalize_spaces(ensure_str(record.get("presentation_level"))) or "poster"
    track_group = normalize_spaces(ensure_str(record.get("track_group")))
    completeness = 0
    if record.get("abstract"):
        completeness += 1
    if record.get("authors"):
        completeness += 1
    if record.get("institutions"):
        completeness += 1
    return (
        PRESENTATION_LEVEL_RANK.get(level, 0),
        1 if track_group == "main" else 0,
        completeness,
    )


def parse_records(
    payload: Dict[str, Any],
    year: int,
    collected_at: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Parse and title-deduplicate ICML virtual JSON records."""
    results = payload.get("results")
    if not isinstance(results, list):
        raise RuntimeError("ICML virtual payload has no results list")

    raw_decision_counts: Dict[str, int] = {}
    raw_eventtype_counts: Dict[str, int] = {}
    raw_sourceurl_counts: Dict[str, int] = {}
    by_title: Dict[str, Dict[str, Any]] = {}
    duplicate_title_entry_count = 0

    for item in results:
        if not isinstance(item, dict):
            continue
        decision = normalize_spaces(ensure_str(item.get("decision"))) or "unknown"
        raw_decision_counts[decision] = raw_decision_counts.get(decision, 0) + 1
        eventtype = normalize_spaces(ensure_str(item.get("eventtype") or item.get("event_type"))) or "unknown"
        raw_eventtype_counts[eventtype] = raw_eventtype_counts.get(eventtype, 0) + 1
        sourceurl = normalize_spaces(ensure_str(item.get("sourceurl"))) or "unknown"
        raw_sourceurl_counts[sourceurl] = raw_sourceurl_counts.get(sourceurl, 0) + 1

        record = build_record(item=item, year=year, collected_at=collected_at)
        title_key = normalize_title_key(ensure_str(record.get("paper_title")))
        if not title_key:
            continue
        existing = by_title.get(title_key)
        if existing is not None:
            duplicate_title_entry_count += 1
            if record_score(record) > record_score(existing):
                by_title[title_key] = record
            continue
        by_title[title_key] = record

    papers = list(by_title.values())
    papers.sort(key=lambda item: normalize_title_key(ensure_str(item.get("paper_title"))))
    return papers, {
        "raw_result_count": len(results),
        "deduplicated_title_count": len(papers),
        "duplicate_title_entry_count": duplicate_title_entry_count,
        "raw_decision_counts": raw_decision_counts,
        "raw_eventtype_counts": raw_eventtype_counts,
        "raw_sourceurl_counts": raw_sourceurl_counts,
    }


def count_field(items: Sequence[Dict[str, Any]], key: str, default: str) -> Dict[str, int]:
    """Count categorical field values."""
    counts: Dict[str, int] = {}
    for item in items:
        value = normalize_spaces(ensure_str(item.get(key))) or default
        counts[value] = counts.get(value, 0) + 1
    return {
        key_: counts[key_]
        for key_ in sorted(counts, key=lambda value: (-counts[value], value))
    }


def build_track_catalog(track_counts: Dict[str, int]) -> List[Dict[str, Any]]:
    """Build official track catalog for M1 alignment."""
    display_names = {
        "conference": ("Conference", "main"),
        "position_paper_track": ("Position Paper Track", "other"),
    }
    catalog: List[Dict[str, Any]] = []
    for track, count in track_counts.items():
        display_name, group = display_names.get(
            track,
            (track.replace("_", " ").title(), "other"),
        )
        catalog.append(
            {
                "track": track,
                "track_display_name": display_name,
                "track_group": group,
                "paper_count": count,
            }
        )
    return catalog


def build_payload(
    year: int,
    papers: Sequence[Dict[str, Any]],
    collected_at: str,
    data_url: str,
    headers: Dict[str, str],
    stats: Dict[str, Any],
) -> Dict[str, Any]:
    """Build root_json payload for ICML virtual data."""
    year_short = year % 100
    count = len(papers)
    track_counts = count_field(papers, key="track", default="conference")
    track_group_counts = count_field(papers, key="track_group", default="main")
    presentation_level_counts = count_field(
        papers,
        key="presentation_level",
        default="poster",
    )
    return {
        "query": {
            "target": f"ICML-{year_short:02d}",
            "venue_code": "ICML",
            "year": year,
            "provider": "icml_virtual",
            "api_key_used": False,
            "work_filter_strategy": f"official_virtual_json:icml-{year}-orals-posters",
            "source_year_count_estimate": count,
            "raw_result_count": stats.get("raw_result_count"),
            "duplicate_title_entry_count": stats.get("duplicate_title_entry_count"),
        },
        "source": {
            "provider": "icml_virtual",
            "openalex_source_id": None,
            "openreview_venue_id": f"ICML.cc/{year}/Conference",
            "display_name": "International Conference on Machine Learning",
            "source_type": "conference",
            "official_url": ICML_VIRTUAL_PAPERS_URL_TEMPLATE.format(year=year),
            "data_url": data_url,
            "data_last_modified": headers.get("last-modified"),
            "data_etag": headers.get("etag"),
        },
        "generated_at_utc": collected_at,
        "paper_count": count,
        "track_counts": track_counts,
        "track_group_counts": track_group_counts,
        "presentation_level_counts": presentation_level_counts,
        "official_tracks": {
            "source_url": data_url,
            "paper_count_official": count,
            "results_count": stats.get("raw_result_count"),
            "track_catalog": build_track_catalog(track_counts),
            "duplicate_title_entry_count": stats.get("duplicate_title_entry_count"),
            "raw_decision_counts": stats.get("raw_decision_counts", {}),
            "raw_eventtype_counts": stats.get("raw_eventtype_counts", {}),
            "raw_sourceurl_counts": stats.get("raw_sourceurl_counts", {}),
        },
        "papers": list(papers),
    }


def collect_one_year(
    year: int,
    output_root: Path,
    timeout: float,
    retries: int,
    min_interval: float,
) -> Dict[str, Any]:
    """Collect one ICML year from the official virtual JSON."""
    data_url = ICML_VIRTUAL_JSON_URL_TEMPLATE.format(year=year)
    LOGGER.info("Collecting ICML %s from virtual JSON: %s", year, data_url)
    collected_at = utc_now_iso()
    source_payload, headers = fetch_json(
        url=data_url,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
    )
    papers, stats = parse_records(
        payload=source_payload,
        year=year,
        collected_at=collected_at,
    )
    payload = build_payload(
        year=year,
        papers=papers,
        collected_at=collected_at,
        data_url=data_url,
        headers=headers,
        stats=stats,
    )

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"ICML-{year % 100:02d}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info(
        "ICML %s virtual collected: raw=%s unique=%s duplicate_entries=%s",
        year,
        stats.get("raw_result_count"),
        len(papers),
        stats.get("duplicate_title_entry_count"),
    )

    return {
        "venue": "ICML",
        "year": year,
        "official_url": ICML_VIRTUAL_PAPERS_URL_TEMPLATE.format(year=year),
        "data_url": data_url,
        "data_last_modified": headers.get("last-modified"),
        "data_etag": headers.get("etag"),
        "official_paper_count": len(papers),
        "collected_paper_count": len(papers),
        "raw_result_count": stats.get("raw_result_count"),
        "duplicate_title_entry_count": stats.get("duplicate_title_entry_count"),
        "track_counts": count_field(papers, key="track", default="conference"),
        "track_group_counts": count_field(papers, key="track_group", default="main"),
        "presentation_level_counts": count_field(
            papers,
            key="presentation_level",
            default="poster",
        ),
        "abstract_filled_count": sum(1 for paper in papers if paper.get("abstract")),
        "abstract_missing_count": sum(1 for paper in papers if not paper.get("abstract")),
        "authors_filled_count": sum(1 for paper in papers if paper.get("authors")),
        "authors_missing_count": sum(1 for paper in papers if not paper.get("authors")),
        "output_file": str(output_path),
        "generated_at_utc": collected_at,
        "provider": "icml_virtual",
    }


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="Collect ICML papers from official virtual JSON")
    parser.add_argument("--years", required=True, help="Year, comma list, or range, e.g. 2026 or 2021-2026")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Root output directory for root_json files")
    parser.add_argument("--index-root", default=str(DEFAULT_INDEX_ROOT), help="Root directory for collection report")
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout seconds")
    parser.add_argument("--retries", type=int, default=3, help="HTTP retry count")
    parser.add_argument("--min-interval", type=float, default=1.0, help="Sleep seconds between retry attempts")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logging level",
    )
    return parser


def main() -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    years = parse_years(args.years)
    output_root = Path(args.output_root)
    index_root = Path(args.index_root)
    collections_root = index_root / "collections"
    collections_root.mkdir(parents=True, exist_ok=True)

    summary: List[Dict[str, Any]] = []
    for year in years:
        summary.append(
            collect_one_year(
                year=year,
                output_root=output_root,
                timeout=args.timeout,
                retries=args.retries,
                min_interval=args.min_interval,
            )
        )

    total = sum(int(item["collected_paper_count"]) for item in summary)
    report = {
        "generated_at_utc": utc_now_iso(),
        "provider": "icml_virtual",
        "venue": "ICML",
        "years": years,
        "total_collected": total,
        "items": summary,
    }
    report_path = collections_root / "icml_collection_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Collection report written: %s", report_path)
    LOGGER.info("Total collected papers: %s", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
