#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect ACL papers (ACL + Findings-ACL) from ACL Anthology event pages."""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.parse import quote

LOGGER = logging.getLogger("acl_collect")

ACL_BASE_URL = "https://aclanthology.org"
EVENT_URL_TEMPLATE = f"{ACL_BASE_URL}/events/acl-{{year}}/"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
S2_PAPER_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"

DEFAULT_OUTPUT_ROOT = Path("archives/root_json")
DEFAULT_INDEX_ROOT = Path("artifacts")

ABS_BLOCK_RE = re.compile(
    r'<div class="card bg-light mb-2 mb-lg-3 collapse abstract-collapse" '
    r"id=abstract-(?P<abs_id>[^>]+)>"
    r'<div class="card-body p-3 small">(?P<abstract>.*?)</div></div>',
    re.S,
)
DETAIL_ABSTRACT_RE = re.compile(
    r'<div class="card-body acl-abstract"><h5 class=card-title>Abstract</h5><span>(?P<abstract>.*?)</span></div>',
    re.S,
)
TAG_RE = re.compile(r"<[^>]+>")


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
    """Collapse internal whitespace."""
    return re.sub(r"\s+", " ", ensure_str(text)).strip()


def normalize_title_for_match(text: str) -> str:
    """Normalize title text for fuzzy matching."""
    normalized = normalize_spaces(strip_tags(text)).lower()
    normalized = re.sub(r"[^a-z0-9 ]+", " ", normalized)
    return normalize_spaces(normalized)


def parse_years(raw: str) -> List[int]:
    """Parse year expression like '2021-2025' or '2021,2022,2023'."""
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


def fetch_text(url: str, timeout: float, retries: int, min_interval: float) -> str:
    """Fetch URL text with retry by curl."""
    last_err: Exception | None = None
    timeout_seconds = max(1, int(timeout))
    for attempt in range(1, retries + 1):
        if attempt > 1 and min_interval > 0:
            time.sleep(min_interval)
        cmd = [
            "curl",
            "-sS",
            "-L",
            "--fail-with-body",
            "--max-time",
            str(timeout_seconds),
            url,
        ]
        try:
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
            return result.stdout
        except subprocess.CalledProcessError as err:  # pragma: no cover - network variability
            last_err = err
            LOGGER.warning("Fetch failed (%s/%s) %s: %s", attempt, retries, url, err)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


def fetch_json(url: str, timeout: float, retries: int, min_interval: float) -> Dict[str, Any]:
    """Fetch JSON object from URL."""
    payload = fetch_text(url=url, timeout=timeout, retries=retries, min_interval=min_interval)
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"Invalid JSON from {url}: {err}") from err
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected JSON payload type from {url}: {type(result)}")
    return result


def strip_tags(value: str) -> str:
    """Remove HTML tags and decode entities."""
    text = ensure_str(value)
    text = re.sub(r"(?i)<br\\s*/?>", " ", text)
    text = TAG_RE.sub("", text)
    return normalize_spaces(html.unescape(text))


def dedupe_preserve(values: Iterable[str]) -> List[str]:
    """Deduplicate while preserving order."""
    seen: set[str] = set()
    out: List[str] = []
    for item in values:
        normalized = normalize_spaces(item)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    return out


def parse_abstract_map(page_html: str) -> Dict[str, str]:
    """Parse abstract map: anthology_id -> abstract."""
    abstract_map: Dict[str, str] = {}
    for match in ABS_BLOCK_RE.finditer(page_html):
        abs_id = ensure_str(match.group("abs_id"))
        paper_id = abs_id.replace("--", ".")
        abstract = strip_tags(match.group("abstract"))
        if not paper_id:
            continue
        abstract_map[paper_id] = abstract
    return abstract_map


def extract_track_series(paper_id: str) -> str:
    """Extract series from paper id, e.g., 2024.acl-long.1 -> acl-long."""
    parts = ensure_str(paper_id).split(".")
    if len(parts) < 3:
        return "acl"
    return ".".join(parts[1:-1]) or "acl"


def normalize_track(series: str) -> str:
    """Convert series to track slug."""
    return re.sub(r"[^a-z0-9]+", "_", ensure_str(series).lower()).strip("_") or "main"


def normalize_track_group(series: str) -> str:
    """Map ACL series to main/other track groups."""
    value = ensure_str(series).lower()
    if value in {"acl-long", "acl-short", "acl-main"}:
        return "main"
    return "other"


def build_quality_flags(authors: Sequence[str], abstract: str) -> List[str]:
    """Build quality flags for one record."""
    flags: List[str] = []
    if not authors:
        flags.append("missing_authors")
    if not normalize_spaces(abstract):
        flags.append("missing_abstract")
    flags.append("missing_keywords")
    flags.append("missing_institutions")
    return flags


def decode_openalex_abstract(inv_index: Dict[str, Any] | None) -> str:
    """Convert OpenAlex abstract_inverted_index to plain text."""
    if not isinstance(inv_index, dict) or not inv_index:
        return ""
    max_position = -1
    for positions in inv_index.values():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int) and pos > max_position:
                max_position = pos
    if max_position < 0:
        return ""
    tokens = [""] * (max_position + 1)
    for token, positions in inv_index.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if not isinstance(pos, int) or pos < 0 or pos > max_position:
                continue
            if not tokens[pos]:
                tokens[pos] = ensure_str(token)
    return normalize_spaces(" ".join(tokens))


def parse_event_records(
    page_html: str,
    year: int,
    collected_at: str,
    event_url: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """Parse ACL papers from one event page."""
    entry_re = re.compile(
        rf"<strong><a[^>]*href=/((?:{year}\.(?:acl|findings-acl)[^/]*))/>"
        r"\s*(.*?)\s*</a></strong><br>(.*?)</span></div>",
        re.S,
    )
    author_re = re.compile(r"<a href=/people/[^>]*>(.*?)</a>", re.S)
    abstract_map = parse_abstract_map(page_html)

    records: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    official_total = 0
    for match in entry_re.finditer(page_html):
        paper_id = ensure_str(match.group(1))
        if not paper_id or paper_id in seen_ids:
            continue
        seen_ids.add(paper_id)
        if paper_id.endswith(".0"):
            continue
        official_total += 1

        title = strip_tags(match.group(2))
        authors_html = ensure_str(match.group(3))
        authors = [strip_tags(item) for item in author_re.findall(authors_html)]
        if not authors:
            authors = [item for item in (strip_tags(part) for part in authors_html.split(",")) if item]
        authors = dedupe_preserve(authors)
        abstract = normalize_spaces(ensure_str(abstract_map.get(paper_id)))

        series = extract_track_series(paper_id)
        track = normalize_track(series)
        track_group = normalize_track_group(series)
        doi = f"10.18653/v1/{paper_id}"
        paper_url = f"{ACL_BASE_URL}/{paper_id}/"
        pdf_url = f"{ACL_BASE_URL}/{paper_id}.pdf"

        records.append(
            {
                "paper_title": title,
                "authors": authors,
                "institutions": [],
                "abstract": abstract,
                "keywords": [],
                "presentation_level": "poster",
                "openalex_id": None,
                "doi": doi,
                "track": track,
                "track_display_name": series,
                "track_group": track_group,
                "title": title,
                "url": paper_url,
                "external_url": pdf_url,
                "citation_count": None,
                "venue": "ACL",
                "year": year,
                "source_provider": "acl_anthology",
                "collected_at": collected_at,
                "source_ids": {
                    "acl_anthology_id": paper_id,
                    "acl_event_url": event_url,
                    "acl_series": series,
                },
                "record_status": "resolved",
                "quality_flags": build_quality_flags(authors=authors, abstract=abstract),
            }
        )

    return records, official_total


def summarize_counts(records: Sequence[Dict[str, Any]], key: str) -> Dict[str, int]:
    """Count frequency for one field."""
    out: Dict[str, int] = {}
    for record in records:
        value = normalize_spaces(ensure_str(record.get(key))) or "unknown"
        out[value] = out.get(value, 0) + 1
    return out


def build_payload(
    year: int,
    records: Sequence[Dict[str, Any]],
    collected_at: str,
    official_url: str,
) -> Dict[str, Any]:
    """Build root_json payload."""
    count = len(records)
    return {
        "query": {
            "target": f"ACL-{year % 100:02d}",
            "venue_code": "ACL",
            "year": year,
            "provider": "acl_anthology",
            "api_key_used": False,
            "work_filter_strategy": (
                f"official_acl_event:acl-{year};anthology_id_prefix:{year}.(acl|findings-acl)"
            ),
            "source_year_count_estimate": None,
        },
        "source": {
            "provider": "acl_anthology",
            "openalex_source_id": None,
            "openreview_venue_id": None,
            "display_name": "Annual Meeting of the Association for Computational Linguistics",
            "source_type": "conference",
            "official_url": official_url,
        },
        "generated_at_utc": collected_at,
        "paper_count": count,
        "track_counts": summarize_counts(records, "track"),
        "track_group_counts": summarize_counts(records, "track_group"),
        "presentation_level_counts": summarize_counts(records, "presentation_level"),
        "papers": list(records),
    }


def collect_one_year(
    year: int,
    output_root: Path,
    timeout: float,
    retries: int,
    min_interval: float,
    workers: int,
    title_threshold: float,
) -> Dict[str, Any]:
    """Collect ACL papers for one year and write output JSON."""
    event_url = EVENT_URL_TEMPLATE.format(year=year)
    LOGGER.info("Collecting ACL %s from %s", year, event_url)
    collected_at = utc_now_iso()
    event_html = fetch_text(
        url=event_url,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
    )
    records, official_count = parse_event_records(
        page_html=event_html,
        year=year,
        collected_at=collected_at,
        event_url=event_url,
    )
    detail_filled = enrich_missing_abstracts_from_details(
        records=records,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
        workers=workers,
    )
    openalex_filled = enrich_missing_abstracts_from_openalex(
        records=records,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
        workers=workers,
    )
    title_search_filled = enrich_missing_abstracts_from_title_search(
        records=records,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
        workers=workers,
        title_threshold=title_threshold,
    )
    payload = build_payload(
        year=year,
        records=records,
        collected_at=collected_at,
        official_url=event_url,
    )

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"ACL-{year % 100:02d}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info(
        "ACL %s written: official=%s collected=%s file=%s",
        year,
        official_count,
        len(records),
        output_path,
    )

    missing_abstract_count = sum(
        1 for record in records if "missing_abstract" in (record.get("quality_flags") or [])
    )
    return {
        "venue": "ACL",
        "year": year,
        "official_url": event_url,
        "official_paper_count": official_count,
        "collected_paper_count": len(records),
        "detail_abstract_filled_count": detail_filled,
        "openalex_abstract_filled_count": openalex_filled,
        "title_search_abstract_filled_count": title_search_filled,
        "missing_abstract_count": missing_abstract_count,
        "output_file": str(output_path),
        "generated_at_utc": collected_at,
    }


def _fetch_detail_abstract(
    url: str,
    timeout: float,
    retries: int,
    min_interval: float,
) -> str:
    """Fetch abstract from ACL paper detail page."""
    html_text = fetch_text(
        url=url,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
    )
    match = DETAIL_ABSTRACT_RE.search(html_text)
    if not match:
        return ""
    return strip_tags(match.group("abstract"))


def _fetch_openalex_abstract_by_doi(
    doi: str,
    timeout: float,
    retries: int,
    min_interval: float,
) -> str:
    """Fetch abstract from OpenAlex for one DOI."""
    normalized = ensure_str(doi)
    if not normalized:
        return ""
    url = f"{OPENALEX_WORKS_URL}?filter=doi:{quote(normalized, safe='')}&per-page=1"
    try:
        payload = fetch_json(
            url=url,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
        )
    except Exception:  # pragma: no cover - network variability
        return ""
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return ""
    first = results[0] if isinstance(results[0], dict) else {}
    return decode_openalex_abstract(first.get("abstract_inverted_index"))


def _fetch_title_fallback_abstract(
    title: str,
    timeout: float,
    retries: int,
    min_interval: float,
    title_threshold: float,
) -> Tuple[str, str]:
    """Fetch abstract by title search from OpenAlex/S2."""
    target = normalize_title_for_match(title)
    if not target:
        return "", ""

    best_abstract = ""
    best_source = ""
    best_score = 0.0

    openalex_url = f"{OPENALEX_WORKS_URL}?search={quote(title)}&per-page=10"
    try:
        openalex_payload = fetch_json(
            url=openalex_url,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
        )
    except Exception:  # pragma: no cover - network variability
        openalex_payload = {}
    for candidate in openalex_payload.get("results", []) if isinstance(openalex_payload, dict) else []:
        if not isinstance(candidate, dict):
            continue
        candidate_title = ensure_str(candidate.get("display_name") or candidate.get("title"))
        candidate_abstract = decode_openalex_abstract(candidate.get("abstract_inverted_index"))
        if not candidate_abstract:
            continue
        score = SequenceMatcher(
            None,
            target,
            normalize_title_for_match(candidate_title),
        ).ratio()
        if score > best_score:
            best_score = score
            best_abstract = candidate_abstract
            best_source = "openalex_title"

    s2_url = (
        f"{S2_PAPER_SEARCH_URL}?query={quote(title)}&limit=10"
        "&fields=title,abstract,externalIds"
    )
    try:
        s2_payload = fetch_json(
            url=s2_url,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
        )
    except Exception:  # pragma: no cover - network variability
        s2_payload = {}
    for candidate in s2_payload.get("data", []) if isinstance(s2_payload, dict) else []:
        if not isinstance(candidate, dict):
            continue
        candidate_title = ensure_str(candidate.get("title"))
        candidate_abstract = normalize_spaces(ensure_str(candidate.get("abstract")))
        if not candidate_abstract:
            continue
        score = SequenceMatcher(
            None,
            target,
            normalize_title_for_match(candidate_title),
        ).ratio()
        if score > best_score:
            best_score = score
            best_abstract = candidate_abstract
            best_source = "semantic_scholar_title"

    if best_abstract and best_score >= title_threshold:
        return best_abstract, best_source
    return "", ""


def enrich_missing_abstracts_from_details(
    records: List[Dict[str, Any]],
    timeout: float,
    retries: int,
    min_interval: float,
    workers: int,
) -> int:
    """Fill missing abstracts by querying ACL paper detail pages."""
    candidates = [
        (idx, record)
        for idx, record in enumerate(records)
        if "missing_abstract" in (record.get("quality_flags") or [])
    ]
    if not candidates:
        return 0
    LOGGER.info("ACL detail abstract fallback candidates: %s", len(candidates))

    worker_count = max(1, workers)
    updated = 0
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _fetch_detail_abstract,
                ensure_str(record.get("url")),
                timeout,
                retries,
                min_interval,
            ): idx
            for idx, record in candidates
        }
        for future in as_completed(futures):
            idx = futures[future]
            record = records[idx]
            try:
                abstract = normalize_spaces(ensure_str(future.result()))
            except Exception:  # pragma: no cover - network variability
                abstract = ""
            if not abstract:
                continue
            record["abstract"] = abstract
            flags = [ensure_str(item) for item in (record.get("quality_flags") or [])]
            record["quality_flags"] = [item for item in flags if item != "missing_abstract"]
            source_ids = record.get("source_ids") or {}
            source_ids.setdefault("acl_detail_abstract_fallback", ensure_str(record.get("url")))
            record["source_ids"] = source_ids
            updated += 1

    if updated:
        LOGGER.info("ACL detail abstract fallback filled: %s", updated)
    return updated


def enrich_missing_abstracts_from_openalex(
    records: List[Dict[str, Any]],
    timeout: float,
    retries: int,
    min_interval: float,
    workers: int,
) -> int:
    """Fill remaining missing abstracts from OpenAlex using DOI."""
    candidates = [
        (idx, record)
        for idx, record in enumerate(records)
        if "missing_abstract" in (record.get("quality_flags") or []) and ensure_str(record.get("doi"))
    ]
    if not candidates:
        return 0
    LOGGER.info("ACL OpenAlex abstract fallback candidates: %s", len(candidates))

    worker_count = max(1, workers)
    updated = 0
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _fetch_openalex_abstract_by_doi,
                ensure_str(record.get("doi")),
                timeout,
                retries,
                min_interval,
            ): idx
            for idx, record in candidates
        }
        for future in as_completed(futures):
            idx = futures[future]
            record = records[idx]
            try:
                abstract = normalize_spaces(ensure_str(future.result()))
            except Exception:  # pragma: no cover - network variability
                abstract = ""
            if not abstract:
                continue
            record["abstract"] = abstract
            flags = [ensure_str(item) for item in (record.get("quality_flags") or [])]
            record["quality_flags"] = [item for item in flags if item != "missing_abstract"]
            source_ids = record.get("source_ids") or {}
            source_ids.setdefault("openalex_abstract_fallback", ensure_str(record.get("doi")))
            record["source_ids"] = source_ids
            updated += 1

    if updated:
        LOGGER.info("ACL OpenAlex abstract fallback filled: %s", updated)
    return updated


def enrich_missing_abstracts_from_title_search(
    records: List[Dict[str, Any]],
    timeout: float,
    retries: int,
    min_interval: float,
    workers: int,
    title_threshold: float,
) -> int:
    """Fill remaining missing abstracts by title search (OpenAlex/S2)."""
    candidates = [
        (idx, record)
        for idx, record in enumerate(records)
        if "missing_abstract" in (record.get("quality_flags") or []) and ensure_str(record.get("paper_title"))
    ]
    if not candidates:
        return 0
    LOGGER.info("ACL title-search abstract fallback candidates: %s", len(candidates))

    worker_count = max(1, workers)
    updated = 0
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _fetch_title_fallback_abstract,
                ensure_str(record.get("paper_title")),
                timeout,
                retries,
                min_interval,
                title_threshold,
            ): idx
            for idx, record in candidates
        }
        for future in as_completed(futures):
            idx = futures[future]
            record = records[idx]
            try:
                abstract, source = future.result()
            except Exception:  # pragma: no cover - network variability
                abstract, source = "", ""
            abstract = normalize_spaces(ensure_str(abstract))
            source = ensure_str(source)
            if not abstract or not source:
                continue
            record["abstract"] = abstract
            flags = [ensure_str(item) for item in (record.get("quality_flags") or [])]
            record["quality_flags"] = [item for item in flags if item != "missing_abstract"]
            source_ids = record.get("source_ids") or {}
            source_ids.setdefault(f"{source}_fallback", ensure_str(record.get("paper_title")))
            record["source_ids"] = source_ids
            updated += 1

    if updated:
        LOGGER.info("ACL title-search abstract fallback filled: %s", updated)
    return updated


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="Collect ACL papers from ACL Anthology")
    parser.add_argument(
        "--years",
        default="2021-2025",
        help="Year range, e.g. 2021-2025 or 2021,2022,2023",
    )
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"Output directory (default: {DEFAULT_OUTPUT_ROOT})",
    )
    parser.add_argument(
        "--index-root",
        default=str(DEFAULT_INDEX_ROOT),
        help=f"Report directory (default: {DEFAULT_INDEX_ROOT})",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout seconds")
    parser.add_argument("--retries", type=int, default=3, help="HTTP retry times")
    parser.add_argument(
        "--min-interval",
        type=float,
        default=0.5,
        help="Sleep seconds between retries",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=16,
        help="Parallel workers for abstract fallback stages (default: 16)",
    )
    parser.add_argument(
        "--title-threshold",
        type=float,
        default=0.90,
        help="Title similarity threshold for title-search abstract fallback (default: 0.90)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Log level",
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
                workers=args.workers,
                title_threshold=args.title_threshold,
            )
        )

    total = sum(int(item["collected_paper_count"]) for item in summary)
    report = {
        "generated_at_utc": utc_now_iso(),
        "provider": "acl_anthology",
        "venue": "ACL",
        "years": years,
        "total_collected": total,
        "total_missing_abstract": sum(int(item["missing_abstract_count"]) for item in summary),
        "items": summary,
    }
    report_path = collections_root / "acl_collection_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Collection report written: %s", report_path)
    LOGGER.info("Total collected papers: %s", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
