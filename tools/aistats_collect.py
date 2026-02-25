#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect AISTATS papers (2021+) from official PMLR volumes."""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("aistats_collect")

PMLR_HOME_URL = "https://proceedings.mlr.press/"
PMLR_VOLUME_URL_TEMPLATE = "https://proceedings.mlr.press/{volume}/"

DEFAULT_OUTPUT_ROOT = Path("archives/root_json")
DEFAULT_INDEX_ROOT = Path("index")

DEFAULT_HEADERS = {
    "User-Agent": "JanusSearch/1.0 (mailto:janus@example.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "close",
}

VOLUME_LINE_RE = re.compile(
    r'<a href="v(?P<volume>\d+)"><b>Volume\s+\d+</b></a>\s*(?P<label>[^<]+)</li>',
    flags=re.I,
)
PAPER_BLOCK_RE = re.compile(r'<div class="paper">\s*(?P<body>.*?)\s*</div>', flags=re.S)
TITLE_RE = re.compile(r'<p class="title">\s*(?P<title>.*?)\s*</p>', flags=re.S | re.I)
AUTHORS_RE = re.compile(r'<span class="authors">\s*(?P<authors>.*?)\s*</span>', flags=re.S | re.I)
INFO_RE = re.compile(r'<span class="info">\s*<i>(?P<booktitle>.*?)</i>,\s*PMLR\s*(?P<pages>[^<]+)</span>', flags=re.S | re.I)
LINK_RE = re.compile(r'<a href="(?P<href>[^"]+)"[^>]*>\s*(?P<label>[^<]+)\s*</a>', flags=re.S | re.I)
ABSTRACT_RE = re.compile(r'<div id="abstract"[^>]*>\s*(?P<abstract>.*?)\s*</div>', flags=re.S | re.I)
META_TAG_RE = re.compile(r'<meta\s+[^>]*name="(?P<name>[^"]+)"[^>]*content="(?P<content>[^"]*)"', flags=re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")
DOI_LINE_RE = re.compile(r"\bdoi\s*=\s*[{\"]\s*(?P<doi>10\.[^}\"]+)", flags=re.I)


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


def strip_tags(value: str) -> str:
    """Remove HTML tags and decode entities."""
    text = ensure_str(value)
    text = text.replace("&nbsp;", " ")
    text = TAG_RE.sub(" ", text)
    return normalize_spaces(html.unescape(text))


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
    """Fetch URL text with retry."""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        if attempt > 1 and min_interval > 0:
            time.sleep(min_interval)
        request = Request(url, headers=DEFAULT_HEADERS)
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "ignore")
        except (HTTPError, URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as err:
            last_err = err
            LOGGER.warning("Fetch failed (%s/%s) %s: %s", attempt, retries, url, err)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


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


def normalize_doi(raw: str) -> str | None:
    """Normalize DOI text."""
    value = ensure_str(raw).lower()
    if not value:
        return None
    if value.startswith("https://doi.org/"):
        value = value[16:]
    elif value.startswith("http://doi.org/"):
        value = value[15:]
    elif value.startswith("doi:"):
        value = value[4:]
    value = normalize_spaces(value)
    if not value or not value.startswith("10."):
        return None
    return value


def resolve_year_volume_map(years: Sequence[int], timeout: float, retries: int, min_interval: float) -> Dict[int, str]:
    """Resolve AISTATS year -> PMLR volume slug from PMLR homepage."""
    html_text = fetch_text(
        url=PMLR_HOME_URL,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
    )
    mapping: Dict[int, str] = {}
    for match in VOLUME_LINE_RE.finditer(html_text):
        volume = f"v{ensure_str(match.group('volume'))}"
        label = normalize_spaces(strip_tags(match.group("label"))).lower()
        year_match = re.search(r"\b(20\d{2})\b", label)
        if not year_match:
            continue
        year = int(year_match.group(1))
        if year not in years:
            continue
        if "aistats" not in label:
            continue
        mapping[year] = volume
    return mapping


def parse_paper_stubs(volume_html: str, volume_url: str, year: int, volume: str) -> List[Dict[str, Any]]:
    """Parse paper stubs from one PMLR volume page."""
    stubs: List[Dict[str, Any]] = []
    for match in PAPER_BLOCK_RE.finditer(volume_html):
        body = ensure_str(match.group("body"))
        title_match = TITLE_RE.search(body)
        authors_match = AUTHORS_RE.search(body)
        info_match = INFO_RE.search(body)
        if not title_match:
            continue

        title = strip_tags(title_match.group("title"))
        authors_text = strip_tags(authors_match.group("authors")) if authors_match else ""
        authors = dedupe_preserve([strip_tags(item) for item in authors_text.split(",")])
        pages = strip_tags(info_match.group("pages")) if info_match else ""
        booktitle = strip_tags(info_match.group("booktitle")) if info_match else ""

        abs_url = ""
        pdf_url = ""
        for link_match in LINK_RE.finditer(body):
            href = ensure_str(link_match.group("href"))
            label = normalize_spaces(strip_tags(link_match.group("label"))).lower()
            if not href:
                continue
            full_url = urljoin(volume_url, href)
            if label == "abs":
                abs_url = full_url
            if "pdf" in label and not pdf_url:
                pdf_url = full_url

        paper_id = ""
        paper_id_match = re.search(r"/v\d+/([^/.]+)\.html$", abs_url)
        if paper_id_match:
            paper_id = ensure_str(paper_id_match.group(1))

        stubs.append(
            {
                "title": title,
                "authors": authors,
                "booktitle": booktitle,
                "pages": pages,
                "abs_url": abs_url,
                "pdf_url": pdf_url,
                "paper_id": paper_id,
                "volume": volume,
                "year": year,
            }
        )
    return stubs


def parse_meta_map(page_html: str) -> Dict[str, str]:
    """Parse <meta name=... content=...> tags into map."""
    meta: Dict[str, str] = {}
    for match in META_TAG_RE.finditer(page_html):
        name = normalize_spaces(ensure_str(match.group("name"))).lower()
        content = normalize_spaces(html.unescape(ensure_str(match.group("content"))))
        if name and content and name not in meta:
            meta[name] = content
    return meta


def fetch_paper_details(stub: Dict[str, Any], timeout: float, retries: int, min_interval: float) -> Dict[str, Any]:
    """Fetch abstract/doi for one paper stub."""
    abs_url = ensure_str(stub.get("abs_url"))
    if not abs_url:
        return {"abstract": "", "doi": None, "url": None, "external_url": stub.get("pdf_url")}

    page_html = fetch_text(url=abs_url, timeout=timeout, retries=retries, min_interval=min_interval)
    meta_map = parse_meta_map(page_html)
    abstract_match = ABSTRACT_RE.search(page_html)
    abstract = strip_tags(abstract_match.group("abstract")) if abstract_match else ""

    doi = normalize_doi(ensure_str(meta_map.get("citation_doi")))
    if not doi:
        doi_match = DOI_LINE_RE.search(page_html)
        if doi_match:
            doi = normalize_doi(ensure_str(doi_match.group("doi")))

    pdf_url = ensure_str(stub.get("pdf_url")) or ensure_str(meta_map.get("citation_pdf_url"))
    return {
        "abstract": abstract,
        "doi": doi,
        "url": abs_url,
        "external_url": pdf_url or abs_url,
    }


def build_quality_flags(authors: Sequence[str], abstract: str) -> List[str]:
    """Build quality flags for one paper."""
    flags: List[str] = []
    if not authors:
        flags.append("missing_authors")
    if not normalize_spaces(abstract):
        flags.append("missing_abstract")
    flags.append("missing_keywords")
    flags.append("missing_institutions")
    return flags


def build_paper_record(stub: Dict[str, Any], details: Dict[str, Any], collected_at: str) -> Dict[str, Any]:
    """Build one root_json paper record."""
    title = normalize_spaces(ensure_str(stub.get("title")))
    authors = [normalize_spaces(ensure_str(name)) for name in (stub.get("authors") or []) if normalize_spaces(ensure_str(name))]
    abstract = normalize_spaces(ensure_str(details.get("abstract")))
    doi = normalize_doi(ensure_str(details.get("doi")))
    url = normalize_spaces(ensure_str(details.get("url")))
    external_url = normalize_spaces(ensure_str(details.get("external_url")))
    volume = ensure_str(stub.get("volume"))
    pages = ensure_str(stub.get("pages"))
    paper_id = ensure_str(stub.get("paper_id"))

    quality_flags = build_quality_flags(authors=authors, abstract=abstract)
    source_ids: Dict[str, str] = {"pmlr_volume": volume}
    if paper_id:
        source_ids["pmlr_id"] = paper_id
    if url:
        source_ids["pmlr_abs_url"] = url
    if pages:
        source_ids["pmlr_pages"] = pages
    if doi:
        source_ids["doi"] = doi

    return {
        "paper_title": title,
        "authors": authors,
        "institutions": [],
        "abstract": abstract,
        "keywords": [],
        "presentation_level": "poster",
        "openalex_id": None,
        "doi": doi,
        "track": "main",
        "track_display_name": "Main",
        "track_group": "main",
        "title": title,
        "url": url or None,
        "external_url": external_url or None,
        "citation_count": None,
        "venue": "AISTATS",
        "year": int(stub.get("year")),
        "source_provider": "pmlr",
        "collected_at": collected_at,
        "source_ids": source_ids,
        "record_status": "placeholder" if ("missing_authors" in quality_flags or "missing_abstract" in quality_flags) else "resolved",
        "quality_flags": quality_flags,
    }


def count_field(items: Sequence[Dict[str, Any]], key: str, default: str) -> Dict[str, int]:
    """Count categorical field values."""
    counts: Dict[str, int] = {}
    for item in items:
        value = normalize_spaces(ensure_str(item.get(key))) or default
        counts[value] = counts.get(value, 0) + 1
    return counts


def build_payload(
    year: int,
    volume: str,
    volume_url: str,
    papers: Sequence[Dict[str, Any]],
    collected_at: str,
    official_count: int,
) -> Dict[str, Any]:
    """Build root_json payload for one year."""
    year_short = year % 100
    return {
        "query": {
            "target": f"AISTATS-{year_short:02d}",
            "venue_code": "AISTATS",
            "year": year,
            "provider": "pmlr",
            "api_key_used": False,
            "work_filter_strategy": f"official_pmlr_volume:{volume};conference:AISTATS;year:{year}",
            "source_year_count_estimate": official_count,
        },
        "source": {
            "provider": "pmlr",
            "openalex_source_id": None,
            "openreview_venue_id": None,
            "display_name": "International Conference on Artificial Intelligence and Statistics",
            "source_type": "conference",
            "official_url": volume_url,
        },
        "generated_at_utc": collected_at,
        "paper_count": len(papers),
        "track_counts": count_field(papers, key="track", default="main"),
        "track_group_counts": count_field(papers, key="track_group", default="main"),
        "presentation_level_counts": count_field(papers, key="presentation_level", default="poster"),
        "papers": list(papers),
    }


def collect_one_year(
    *,
    year: int,
    volume: str,
    output_root: Path,
    timeout: float,
    retries: int,
    min_interval: float,
    workers: int,
) -> Dict[str, Any]:
    """Collect one AISTATS year and write root_json."""
    volume_url = PMLR_VOLUME_URL_TEMPLATE.format(volume=volume)
    collected_at = utc_now_iso()
    LOGGER.info("AISTATS %s loading volume: %s", year, volume_url)
    volume_html = fetch_text(url=volume_url, timeout=timeout, retries=retries, min_interval=min_interval)
    stubs = parse_paper_stubs(volume_html=volume_html, volume_url=volume_url, year=year, volume=volume)
    official_count = len(stubs)
    LOGGER.info("AISTATS %s official papers from %s: %s", year, volume, official_count)

    papers: List[Dict[str, Any]] = []
    if stubs:
        worker_count = max(1, workers)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    fetch_paper_details,
                    stub,
                    timeout,
                    retries,
                    min_interval,
                ): stub
                for stub in stubs
            }
            done = 0
            for future in as_completed(futures):
                done += 1
                stub = futures[future]
                try:
                    details = future.result()
                except Exception as err:  # pragma: no cover - network variability
                    LOGGER.warning("AISTATS %s detail fetch failed: %s (%s)", year, stub.get("abs_url"), err)
                    details = {"abstract": "", "doi": None, "url": stub.get("abs_url"), "external_url": stub.get("pdf_url")}
                papers.append(build_paper_record(stub=stub, details=details, collected_at=collected_at))
                if done == len(stubs) or done % 50 == 0:
                    LOGGER.info("AISTATS %s detail progress: %s/%s", year, done, len(stubs))

    papers.sort(key=lambda item: normalize_spaces(ensure_str(item.get("title"))).lower())
    payload = build_payload(
        year=year,
        volume=volume,
        volume_url=volume_url,
        papers=papers,
        collected_at=collected_at,
        official_count=official_count,
    )

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"AISTATS-{year % 100:02d}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    missing_abstract_count = sum(
        1 for paper in papers if "missing_abstract" in (paper.get("quality_flags") or [])
    )
    LOGGER.info(
        "AISTATS %s done: official=%s collected=%s missing_abstract=%s",
        year,
        official_count,
        len(papers),
        missing_abstract_count,
    )
    return {
        "year": year,
        "volume": volume,
        "official_url": volume_url,
        "official_paper_count": official_count,
        "collected_paper_count": len(papers),
        "missing_vs_official": max(0, official_count - len(papers)),
        "missing_abstract_count": missing_abstract_count,
        "output_file": str(output_path),
        "generated_at_utc": collected_at,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="Collect AISTATS papers from official PMLR volumes")
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
    parser.add_argument("--retries", type=int, default=4, help="HTTP retry times")
    parser.add_argument(
        "--min-interval",
        type=float,
        default=0.2,
        help="Sleep seconds between retries",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=16,
        help="Parallel workers for paper-detail fetching (default: 16)",
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
    index_root.mkdir(parents=True, exist_ok=True)

    year_volume_map = resolve_year_volume_map(
        years=years,
        timeout=args.timeout,
        retries=args.retries,
        min_interval=args.min_interval,
    )
    missing_years = [year for year in years if year not in year_volume_map]
    if missing_years:
        raise RuntimeError(f"Failed to resolve AISTATS PMLR volumes for years: {missing_years}")

    summary: List[Dict[str, Any]] = []
    for year in years:
        volume = year_volume_map[year]
        summary.append(
            collect_one_year(
                year=year,
                volume=volume,
                output_root=output_root,
                timeout=args.timeout,
                retries=args.retries,
                min_interval=args.min_interval,
                workers=args.workers,
            )
        )

    total_official = sum(int(item["official_paper_count"]) for item in summary)
    total_collected = sum(int(item["collected_paper_count"]) for item in summary)
    report = {
        "generated_at_utc": utc_now_iso(),
        "provider": "pmlr",
        "venue": "AISTATS",
        "years": years,
        "year_volume_map": {str(year): year_volume_map[year] for year in years},
        "total_official": total_official,
        "total_collected": total_collected,
        "official_vs_collected_aligned": total_official == total_collected,
        "items": summary,
    }
    report_path = index_root / "aistats_collection_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Collection report written: %s", report_path)
    LOGGER.info("Total official papers: %s", total_official)
    LOGGER.info("Total collected papers: %s", total_collected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
