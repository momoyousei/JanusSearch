#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect CVF OpenAccess papers (CVPR/ICCV/ECCV)."""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("cvpr_collect")

CVF_BASE_URL = "https://openaccess.thecvf.com"
CVPR_VIRTUAL_BASE_URL = "https://cvpr.thecvf.com"
CVPR_VIRTUAL_JSON_URL_TEMPLATE = (
    f"{CVPR_VIRTUAL_BASE_URL}/static/virtual/data/cvpr-{{year}}-orals-posters.json"
)
CVPR_VIRTUAL_PAPERS_URL_TEMPLATE = (
    f"{CVPR_VIRTUAL_BASE_URL}/virtual/{{year}}/papers.html?day=all"
)
ECVA_BASE_URL = "https://www.ecva.net"
ECVA_PAPERS_URL = f"{ECVA_BASE_URL}/papers.php"
DEFAULT_OUTPUT_ROOT = Path("archives/root_json")
DEFAULT_INDEX_ROOT = Path("artifacts")
VENUE_SETTINGS: Dict[str, Dict[str, str]] = {
    "CVPR": {
        "display_name": "IEEE/CVF Conference on Computer Vision and Pattern Recognition",
        "official_event_name": "CVPR",
    },
    "ICCV": {
        "display_name": "IEEE/CVF International Conference on Computer Vision",
        "official_event_name": "ICCV",
    },
    "ECCV": {
        "display_name": "European Conference on Computer Vision",
        "official_event_name": "ECCV",
    },
}
PAPER_BLOCK_RE = re.compile(
    r'<dt class="ptitle"><br>\s*'
    r'<a href=(?:"(?P<html_dq>[^"]+)"|\'(?P<html_sq>[^\']+)\'|(?P<html_uq>[^ >]+))\s*>'
    r"\s*(?P<title>.*?)\s*</a>\s*</dt>\s*"
    r"<dd>\s*(?P<authors>.*?)\s*</dd>\s*"
    r"<dd>\s*(?P<links>.*?)\s*</dd>",
    re.S,
)
TAG_RE = re.compile(r"<[^>]+>")
ABSTRACT_RE = re.compile(r'<div id="abstract">\s*(.*?)\s*</div>', re.S)
DOI_RE = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)")
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
    """Collapse internal whitespace."""
    return re.sub(r"\s+", " ", ensure_str(text)).strip()


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
    """Fetch URL content with retry."""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        if attempt > 1 and min_interval > 0:
            time.sleep(min_interval)
        request = Request(
            url,
            headers={
                "User-Agent": "curl/8.7.1",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "ignore")
        except (HTTPError, URLError, TimeoutError) as err:
            last_err = err
            LOGGER.warning("Fetch failed (%s/%s) %s: %s", attempt, retries, url, err)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


def to_abs_url(url: str, base_url: str = CVF_BASE_URL) -> str:
    """Convert relative CVF path to absolute URL."""
    raw = ensure_str(url)
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw.startswith("//"):
        return f"https:{raw}"
    if raw.startswith("/"):
        return f"{base_url}{raw}"
    return f"{base_url}/{raw}"


def venue_provider(venue: str) -> str:
    """Return source provider by venue."""
    if venue == "ECCV":
        return "ecva_papers"
    return "cvf_openaccess"


def parse_arxiv_id(url: str) -> str | None:
    """Extract arXiv identifier from URL if present."""
    text = ensure_str(url)
    if not text:
        return None
    match = re.search(r"arxiv\.org/abs/([^/?#]+)", text)
    if not match:
        return None
    return ensure_str(match.group(1)) or None


def parse_doi(value: str) -> str | None:
    """Extract DOI from URL/text."""
    text = ensure_str(unquote(value))
    if not text:
        return None
    match = DOI_RE.search(text)
    if not match:
        return None
    return ensure_str(match.group(1).rstrip("].,;")) or None


def parse_bibtex_key(bibtex: str) -> str | None:
    """Extract bibtex key from @InProceedings{...}."""
    text = ensure_str(bibtex)
    if not text:
        return None
    match = re.search(r"@\w+\{([^,]+),", text)
    if not match:
        return None
    return ensure_str(match.group(1)) or None


def parse_bibtex_pages(bibtex: str) -> str | None:
    """Extract page range from bibtex block."""
    text = ensure_str(bibtex)
    if not text:
        return None
    match = re.search(r"pages\s*=\s*\{([^}]+)\}", text, re.I)
    if not match:
        return None
    return normalize_spaces(match.group(1)) or None


def extract_anchor_pairs(block: str) -> List[Tuple[str, str]]:
    """Extract (href, anchor_text) from a html block."""
    pairs: List[Tuple[str, str]] = []
    for match in re.finditer(
        r'<a[^>]*href\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([^\'" >]+))[^>]*>(.*?)</a>',
        block,
        re.S,
    ):
        href = ensure_str(match.group(1) or match.group(2) or match.group(3))
        text = strip_tags(match.group(4))
        if href:
            pairs.append((href, text))
    return pairs


def strip_tags(value: str) -> str:
    """Remove HTML tags and decode entities."""
    return normalize_spaces(html.unescape(TAG_RE.sub("", value)))


def extract_abstract(detail_html: str) -> str:
    """Extract abstract text from CVF paper detail page."""
    match = ABSTRACT_RE.search(detail_html)
    if not match:
        return ""
    return strip_tags(match.group(1))


def parse_one_paper(
    match: re.Match[str],
    venue: str,
    year: int,
    collected_at: str,
) -> Dict[str, Any]:
    """Parse one paper block."""
    html_path = ensure_str(
        match.group("html_dq") or match.group("html_sq") or match.group("html_uq")
    )
    title_raw = ensure_str(match.group("title"))
    authors_block = ensure_str(match.group("authors"))
    links_block = ensure_str(match.group("links"))
    base_url = ECVA_BASE_URL if venue == "ECCV" else CVF_BASE_URL
    provider = venue_provider(venue)
    source_prefix = "ecva" if venue == "ECCV" else "cvf"

    title = strip_tags(title_raw)
    authors = [
        normalize_spaces(html.unescape(item))
        for item in re.findall(r'name="query_author"\s+value="([^"]+)"', authors_block)
    ]
    if not authors:
        authors = [
            strip_tags(item)
            for item in re.findall(r"<a[^>]*>(.*?)</a>", authors_block, flags=re.S)
        ]
        authors = [item for item in authors if item]
    if not authors:
        authors = [normalize_spaces(item) for item in strip_tags(authors_block).split(",")]
        authors = [item for item in authors if item]

    link_pairs = extract_anchor_pairs(links_block)
    pdf_url = ""
    arxiv_url = ""
    doi_url = ""
    doi = None
    supplementary_url = ""
    for href, anchor_text in link_pairs:
        label = normalize_spaces(anchor_text).lower()
        if label == "pdf" and not pdf_url:
            pdf_url = to_abs_url(href, base_url=base_url)
        elif label == "arxiv" and not arxiv_url:
            arxiv_url = to_abs_url(href, base_url=base_url)
        elif label == "doi" and not doi_url:
            doi_url = to_abs_url(href, base_url=base_url)
            doi = parse_doi(doi_url)
        elif "supplementary" in label and not supplementary_url:
            supplementary_url = to_abs_url(href, base_url=base_url)

    bibtex_block = ""
    bibtex_match = re.search(
        r'<div class="bibref pre-white-space">(.*?)</div>',
        links_block,
        flags=re.S,
    )
    if bibtex_match:
        bibtex_block = strip_tags(bibtex_match.group(1))

    source_ids: Dict[str, str] = {f"{source_prefix}_paper_html": to_abs_url(html_path, base_url=base_url)}
    if pdf_url:
        source_ids[f"{source_prefix}_pdf_url"] = pdf_url
    if arxiv_url:
        source_ids[f"{source_prefix}_arxiv_url"] = arxiv_url
        arxiv_id = parse_arxiv_id(arxiv_url)
        if arxiv_id:
            source_ids["arxiv_id"] = arxiv_id
    if doi_url:
        source_ids[f"{source_prefix}_doi_url"] = doi_url
    if supplementary_url:
        source_ids[f"{source_prefix}_supp_url"] = supplementary_url
    bibtex_key = parse_bibtex_key(bibtex_block)
    if bibtex_key:
        source_ids[f"{source_prefix}_bibtex_key"] = bibtex_key
    pages = parse_bibtex_pages(bibtex_block)
    if pages:
        source_ids[f"{source_prefix}_pages"] = pages
    if not doi:
        for href, anchor_text in link_pairs:
            if normalize_spaces(anchor_text).lower() == "doi":
                doi = parse_doi(href)
                if doi:
                    break
    if not doi:
        doi = parse_doi(links_block)

    return {
        "paper_title": title,
        "authors": authors,
        "institutions": [],
        "abstract": "",
        "keywords": [],
        "presentation_level": "poster",
        "openalex_id": None,
        "doi": doi,
        "track": "main",
        "track_display_name": "Main",
        "track_group": "main",
        "title": title,
        "url": to_abs_url(html_path, base_url=base_url) or None,
        "external_url": pdf_url or None,
        "citation_count": None,
        "venue": venue,
        "year": year,
        "source_provider": provider,
        "collected_at": collected_at,
        "source_ids": source_ids,
        "record_status": "resolved",
        "quality_flags": ["missing_abstract", "missing_keywords", "missing_institutions"],
    }


def parse_papers(page_html: str, venue: str, year: int, collected_at: str) -> List[Dict[str, Any]]:
    """Extract all paper records from one CVF venue-year page."""
    papers: List[Dict[str, Any]] = []
    eccv_year_marker = f"papers/eccv_{year}/papers_eccv/html/"
    for match in PAPER_BLOCK_RE.finditer(page_html):
        if venue == "ECCV":
            html_path = ensure_str(
                match.group("html_dq") or match.group("html_sq") or match.group("html_uq")
            )
            if eccv_year_marker not in html_path.lower():
                continue
        paper = parse_one_paper(
            match=match,
            venue=venue,
            year=year,
            collected_at=collected_at,
        )
        if paper["paper_title"]:
            papers.append(paper)
    return papers


def normalize_title_key(value: str) -> str:
    """Normalize a title for deduplication."""
    normalized = re.sub(r"[^a-z0-9]+", " ", ensure_str(value).lower())
    return re.sub(r"\s+", " ", normalized).strip()


def dedupe_strings(items: Sequence[str]) -> List[str]:
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


def cvpr_virtual_abs_url(value: str) -> str:
    """Return absolute CVPR virtual URL."""
    raw = ensure_str(value)
    if not raw:
        return ""
    return urljoin(CVPR_VIRTUAL_BASE_URL, raw)


def normalize_virtual_presentation_level(value: str) -> str:
    """Map CVPR virtual decision text to project presentation levels."""
    text = ensure_str(value).lower()
    if "best" in text and "paper" in text:
        return "bestpaper"
    if "oral" in text or "spotlight" in text:
        return "oral"
    return "poster"


def build_virtual_quality_flags(
    authors: Sequence[str],
    abstract: str,
    institutions: Sequence[str],
    keywords: Sequence[str],
) -> List[str]:
    """Build quality flags for one CVPR virtual record."""
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


def parse_virtual_author_fields(authors_raw: Any) -> Tuple[List[str], List[str]]:
    """Parse author and institution fields from CVPR virtual JSON."""
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


def build_cvpr_virtual_record(
    item: Dict[str, Any],
    year: int,
    collected_at: str,
) -> Dict[str, Any]:
    """Build one root_json paper record from CVPR virtual JSON."""
    title = normalize_spaces(ensure_str(item.get("name")))
    authors, institutions = parse_virtual_author_fields(item.get("authors"))
    abstract = normalize_spaces(ensure_str(item.get("abstract")))
    keywords_raw = item.get("keywords")
    keywords = (
        dedupe_strings([ensure_str(value) for value in keywords_raw])
        if isinstance(keywords_raw, list)
        else []
    )
    decision = normalize_spaces(ensure_str(item.get("decision")))
    presentation_level = normalize_virtual_presentation_level(decision)

    virtual_url = cvpr_virtual_abs_url(ensure_str(item.get("virtualsite_url")))
    paper_url = cvpr_virtual_abs_url(ensure_str(item.get("paper_url")))
    paper_pdf_url = cvpr_virtual_abs_url(ensure_str(item.get("paper_pdf_url")))
    sourceurl = normalize_spaces(ensure_str(item.get("sourceurl")))

    source_ids: Dict[str, str] = {}
    for key in ("id", "uid", "sourceid"):
        value = ensure_str(item.get(key))
        if value:
            source_ids[f"cvpr_virtual_{key}"] = value
    if virtual_url:
        source_ids["cvpr_virtualsite_url"] = virtual_url
    if sourceurl:
        source_ids["cvpr_sourceurl"] = sourceurl
    if decision:
        source_ids["cvpr_decision"] = decision
    eventtype = normalize_spaces(ensure_str(item.get("eventtype") or item.get("event_type")))
    if eventtype:
        source_ids["cvpr_eventtype"] = eventtype
    if paper_url:
        source_ids["cvpr_paper_url"] = paper_url
    if paper_pdf_url:
        source_ids["cvpr_paper_pdf_url"] = paper_pdf_url

    quality_flags = build_virtual_quality_flags(
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
        "doi": None,
        "track": "conference",
        "track_display_name": "Conference",
        "track_group": "main",
        "title": title,
        "url": virtual_url or None,
        "external_url": paper_pdf_url or paper_url or None,
        "citation_count": None,
        "venue": "CVPR",
        "year": year,
        "source_provider": "cvpr_virtual",
        "collected_at": collected_at,
        "source_ids": source_ids,
        "record_status": record_status,
        "quality_flags": quality_flags,
    }


def cvpr_virtual_record_score(record: Dict[str, Any]) -> Tuple[int, int, int]:
    """Rank duplicate CVPR virtual records by presentation and completeness."""
    level = normalize_spaces(ensure_str(record.get("presentation_level"))) or "poster"
    source_ids = record.get("source_ids")
    eventtype = ""
    if isinstance(source_ids, dict):
        eventtype = normalize_spaces(ensure_str(source_ids.get("cvpr_eventtype"))).lower()
    is_oral_event = 1 if eventtype == "oral" else 0
    completeness = 0
    if record.get("abstract"):
        completeness += 1
    if record.get("authors"):
        completeness += 1
    if record.get("institutions"):
        completeness += 1
    return (PRESENTATION_LEVEL_RANK.get(level, 0), is_oral_event, completeness)


def parse_cvpr_virtual_records(
    payload: Dict[str, Any],
    year: int,
    collected_at: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Parse and title-deduplicate CVPR virtual JSON records."""
    results = payload.get("results")
    if not isinstance(results, list):
        raise RuntimeError("CVPR virtual payload has no results list")

    raw_decision_counts: Dict[str, int] = {}
    raw_eventtype_counts: Dict[str, int] = {}
    by_title: Dict[str, Dict[str, Any]] = {}
    duplicate_title_entry_count = 0

    for item in results:
        if not isinstance(item, dict):
            continue
        decision = normalize_spaces(ensure_str(item.get("decision"))) or "unknown"
        raw_decision_counts[decision] = raw_decision_counts.get(decision, 0) + 1
        eventtype = normalize_spaces(ensure_str(item.get("eventtype") or item.get("event_type")))
        eventtype = eventtype or "unknown"
        raw_eventtype_counts[eventtype] = raw_eventtype_counts.get(eventtype, 0) + 1

        record = build_cvpr_virtual_record(item=item, year=year, collected_at=collected_at)
        title_key = normalize_title_key(ensure_str(record.get("paper_title")))
        if not title_key:
            continue
        existing = by_title.get(title_key)
        if existing is not None:
            duplicate_title_entry_count += 1
            if cvpr_virtual_record_score(record) > cvpr_virtual_record_score(existing):
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
    }


def _fetch_paper_abstract(
    url: str,
    timeout: float,
    retries: int,
    min_interval: float,
) -> Tuple[str, str]:
    """Fetch abstract for one paper URL."""
    if not ensure_str(url):
        return "", "missing_url"
    try:
        detail_html = fetch_text(
            url=url,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
        )
    except Exception as err:  # pragma: no cover - network variability
        return "", str(err)
    abstract = extract_abstract(detail_html)
    if not abstract:
        return "", "missing_abstract_block"
    return abstract, ""


def enrich_abstracts(
    papers: List[Dict[str, Any]],
    timeout: float,
    retries: int,
    min_interval: float,
    workers: int,
) -> Tuple[int, int]:
    """Fill abstracts by fetching CVF paper detail pages."""
    if not papers:
        return 0, 0

    worker_count = max(1, workers)
    success = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _fetch_paper_abstract,
                ensure_str(paper.get("url")),
                timeout,
                retries,
                min_interval,
            ): idx
            for idx, paper in enumerate(papers)
        }
        for future in as_completed(futures):
            idx = futures[future]
            paper = papers[idx]
            abstract, err = future.result()
            if abstract:
                paper["abstract"] = abstract
                flags = [ensure_str(item) for item in paper.get("quality_flags", [])]
                paper["quality_flags"] = [item for item in flags if item != "missing_abstract"]
                success += 1
            else:
                if not err:
                    err = "unknown_error"
                failed += 1

    return success, failed


def build_payload(
    venue: str,
    year: int,
    papers: Sequence[Dict[str, Any]],
    collected_at: str,
) -> Dict[str, Any]:
    """Build root_json payload."""
    settings = VENUE_SETTINGS[venue]
    year_short = year % 100
    count = len(papers)
    provider = venue_provider(venue)
    if venue == "ECCV":
        official_url = ECVA_PAPERS_URL
        work_filter_strategy = f"official_ecva:eccv_{year}"
    else:
        official_url = f"{CVF_BASE_URL}/{venue}{year}?day=all"
        work_filter_strategy = f"official_openaccess:{venue}{year}?day=all"
    return {
        "query": {
            "target": f"{venue}-{year_short:02d}",
            "venue_code": venue,
            "year": year,
            "provider": provider,
            "api_key_used": False,
            "work_filter_strategy": work_filter_strategy,
            "source_year_count_estimate": None,
        },
        "source": {
            "provider": provider,
            "openalex_source_id": None,
            "openreview_venue_id": None,
            "display_name": settings["display_name"],
            "source_type": "conference",
            "official_url": official_url,
        },
        "generated_at_utc": collected_at,
        "paper_count": count,
        "track_counts": {"main": count},
        "track_group_counts": {"main": count},
        "presentation_level_counts": {"poster": count},
        "papers": list(papers),
    }


def count_field(items: Sequence[Dict[str, Any]], key: str, default: str) -> Dict[str, int]:
    """Count categorical field values."""
    counts: Dict[str, int] = {}
    for item in items:
        value = normalize_spaces(ensure_str(item.get(key))) or default
        counts[value] = counts.get(value, 0) + 1
    return counts


def build_cvpr_virtual_payload(
    year: int,
    papers: Sequence[Dict[str, Any]],
    collected_at: str,
    data_url: str,
    stats: Dict[str, Any],
) -> Dict[str, Any]:
    """Build root_json payload for CVPR virtual data."""
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
            "target": f"CVPR-{year_short:02d}",
            "venue_code": "CVPR",
            "year": year,
            "provider": "cvpr_virtual",
            "api_key_used": False,
            "work_filter_strategy": f"official_virtual_json:cvpr-{year}-orals-posters",
            "source_year_count_estimate": count,
            "raw_result_count": stats.get("raw_result_count"),
            "duplicate_title_entry_count": stats.get("duplicate_title_entry_count"),
        },
        "source": {
            "provider": "cvpr_virtual",
            "openalex_source_id": None,
            "openreview_venue_id": f"thecvf.com/CVPR/{year}/Conference",
            "display_name": VENUE_SETTINGS["CVPR"]["display_name"],
            "source_type": "conference",
            "official_url": CVPR_VIRTUAL_PAPERS_URL_TEMPLATE.format(year=year),
            "data_url": data_url,
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
            "track_catalog": [
                {
                    "track": "conference",
                    "track_display_name": "Conference",
                    "track_group": "main",
                    "paper_count": count,
                }
            ],
            "duplicate_title_entry_count": stats.get("duplicate_title_entry_count"),
            "raw_decision_counts": stats.get("raw_decision_counts", {}),
            "raw_eventtype_counts": stats.get("raw_eventtype_counts", {}),
        },
        "papers": list(papers),
    }


def collect_cvpr_virtual_year(
    year: int,
    output_root: Path,
    timeout: float,
    retries: int,
    min_interval: float,
) -> Dict[str, Any]:
    """Collect one CVPR year from the official virtual JSON."""
    data_url = CVPR_VIRTUAL_JSON_URL_TEMPLATE.format(year=year)
    LOGGER.info("Collecting CVPR %s from virtual JSON: %s", year, data_url)
    collected_at = utc_now_iso()
    payload_text = fetch_text(
        url=data_url,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
    )
    try:
        source_payload = json.loads(payload_text)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"Invalid CVPR virtual JSON from {data_url}: {err}") from err
    if not isinstance(source_payload, dict):
        raise RuntimeError(f"Unexpected CVPR virtual payload type: {type(source_payload)}")

    papers, stats = parse_cvpr_virtual_records(
        payload=source_payload,
        year=year,
        collected_at=collected_at,
    )
    payload = build_cvpr_virtual_payload(
        year=year,
        papers=papers,
        collected_at=collected_at,
        data_url=data_url,
        stats=stats,
    )

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"CVPR-{year % 100:02d}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info(
        "CVPR %s virtual collected: raw=%s unique=%s duplicate_entries=%s",
        year,
        stats.get("raw_result_count"),
        len(papers),
        stats.get("duplicate_title_entry_count"),
    )

    return {
        "venue": "CVPR",
        "year": year,
        "official_url": CVPR_VIRTUAL_PAPERS_URL_TEMPLATE.format(year=year),
        "data_url": data_url,
        "official_paper_count": len(papers),
        "collected_paper_count": len(papers),
        "raw_result_count": stats.get("raw_result_count"),
        "duplicate_title_entry_count": stats.get("duplicate_title_entry_count"),
        "abstract_filled_count": sum(1 for paper in papers if paper.get("abstract")),
        "abstract_missing_count": sum(1 for paper in papers if not paper.get("abstract")),
        "output_file": str(output_path),
        "generated_at_utc": collected_at,
        "provider": "cvpr_virtual",
    }


def collect_one_year(
    venue: str,
    year: int,
    output_root: Path,
    timeout: float,
    retries: int,
    min_interval: float,
    fetch_abstracts: bool,
    workers: int,
    eccv_index_file: str | None,
    source_mode: str,
) -> Dict[str, Any]:
    """Collect one year and write output JSON."""
    if venue == "CVPR" and source_mode == "virtual":
        return collect_cvpr_virtual_year(
            year=year,
            output_root=output_root,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
        )

    if venue == "ECCV":
        list_url = ECVA_PAPERS_URL
    else:
        list_url = f"{CVF_BASE_URL}/{venue}{year}?day=all"
    LOGGER.info("Collecting %s %s from %s", venue, year, list_url)
    collected_at = utc_now_iso()
    try:
        if venue == "ECCV" and ensure_str(eccv_index_file):
            page_html = Path(ensure_str(eccv_index_file)).read_text(encoding="utf-8", errors="ignore")
        else:
            page_html = fetch_text(url=list_url, timeout=timeout, retries=retries, min_interval=min_interval)
    except Exception:
        if venue == "CVPR" and source_mode == "auto":
            LOGGER.warning("OpenAccess collection failed for CVPR %s; falling back to virtual JSON", year)
            return collect_cvpr_virtual_year(
                year=year,
                output_root=output_root,
                timeout=timeout,
                retries=retries,
                min_interval=min_interval,
            )
        raise
    papers = parse_papers(page_html=page_html, venue=venue, year=year, collected_at=collected_at)
    if venue == "CVPR" and source_mode == "auto" and not papers:
        LOGGER.warning("OpenAccess returned 0 papers for CVPR %s; falling back to virtual JSON", year)
        return collect_cvpr_virtual_year(
            year=year,
            output_root=output_root,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
        )
    abstract_success = 0
    abstract_failed = 0
    if fetch_abstracts:
        LOGGER.info("Fetching abstracts for %s %s (%s papers)", venue, year, len(papers))
        abstract_success, abstract_failed = enrich_abstracts(
            papers=papers,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
            workers=workers,
        )
        LOGGER.info(
            "Abstract fetching %s %s done: success=%s failed=%s",
            venue,
            year,
            abstract_success,
            abstract_failed,
        )
    payload = build_payload(venue=venue, year=year, papers=papers, collected_at=collected_at)

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"{venue}-{year % 100:02d}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Written %s papers to %s", len(papers), output_path)

    return {
        "venue": venue,
        "year": year,
        "official_url": list_url,
        "official_paper_count": len(papers),
        "collected_paper_count": len(papers),
        "abstract_filled_count": abstract_success,
        "abstract_missing_count": abstract_failed,
        "output_file": str(output_path),
        "generated_at_utc": collected_at,
        "provider": provider,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="Collect CVF OpenAccess papers (CVPR/ICCV/ECCV)")
    parser.add_argument(
        "--venue",
        default="CVPR",
        choices=tuple(VENUE_SETTINGS.keys()),
        help="Venue code (default: CVPR)",
    )
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
    parser.add_argument("--timeout", type=float, default=45.0, help="HTTP timeout seconds")
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
        default=12,
        help="Parallel workers for abstract fetching (default: 12)",
    )
    parser.add_argument(
        "--no-fetch-abstracts",
        action="store_true",
        help="Only collect list page metadata, do not fetch detail-page abstracts",
    )
    parser.add_argument(
        "--source",
        default="auto",
        choices=("auto", "openaccess", "virtual"),
        help=(
            "Collection source. auto uses CVF/ECVA first and falls back to "
            "CVPR virtual JSON when OpenAccess is not published."
        ),
    )
    parser.add_argument(
        "--eccv-index-file",
        default="",
        help="Optional local ECVA papers.php snapshot path for ECCV collection",
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
    venue = ensure_str(args.venue).upper()
    if venue not in VENUE_SETTINGS:
        raise ValueError(f"Unsupported venue: {venue}")
    if args.source == "virtual" and venue != "CVPR":
        raise ValueError("--source virtual is only supported for CVPR")
    output_root = Path(args.output_root)
    index_root = Path(args.index_root)
    collections_root = index_root / "collections"
    collections_root.mkdir(parents=True, exist_ok=True)

    summary: List[Dict[str, Any]] = []
    for year in years:
        summary.append(
            collect_one_year(
                venue=venue,
                year=year,
                output_root=output_root,
                timeout=args.timeout,
                retries=args.retries,
                min_interval=args.min_interval,
                fetch_abstracts=not args.no_fetch_abstracts,
                workers=args.workers,
                eccv_index_file=ensure_str(args.eccv_index_file) or None,
                source_mode=args.source,
            )
        )

    total = sum(int(item["collected_paper_count"]) for item in summary)
    providers = sorted(
        {
            ensure_str(item.get("provider")) or venue_provider(venue)
            for item in summary
        }
    )
    report = {
        "generated_at_utc": utc_now_iso(),
        "provider": providers[0] if len(providers) == 1 else "mixed",
        "providers": providers,
        "venue": venue,
        "years": years,
        "total_collected": total,
        "items": summary,
    }
    report_path = collections_root / f"{venue.lower()}_collection_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Collection report written: %s", report_path)
    LOGGER.info("Total collected papers: %s", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
