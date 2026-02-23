#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect CVPR papers from official CVF OpenAccess pages."""

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
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("cvpr_collect")

CVF_BASE_URL = "https://openaccess.thecvf.com"
DEFAULT_OUTPUT_ROOT = Path("archives/root_json")
DEFAULT_INDEX_ROOT = Path("index")
PAPER_BLOCK_RE = re.compile(
    r'<dt class="ptitle"><br><a href="(?P<html>[^"]+)">(?P<title>.*?)</a></dt>\s*'
    r"<dd>(?P<authors>.*?)</dd>\s*"
    r"<dd>(?P<links>.*?)</dd>",
    re.S,
)
TAG_RE = re.compile(r"<[^>]+>")
ABSTRACT_RE = re.compile(r'<div id="abstract">\s*(.*?)\s*</div>', re.S)


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


def to_abs_url(url: str) -> str:
    """Convert relative CVF path to absolute URL."""
    raw = ensure_str(url)
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw.startswith("/"):
        return f"{CVF_BASE_URL}{raw}"
    return f"{CVF_BASE_URL}/{raw}"


def parse_arxiv_id(url: str) -> str | None:
    """Extract arXiv identifier from URL if present."""
    text = ensure_str(url)
    if not text:
        return None
    match = re.search(r"arxiv\.org/abs/([^/?#]+)", text)
    if not match:
        return None
    return ensure_str(match.group(1)) or None


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


def strip_tags(value: str) -> str:
    """Remove HTML tags and decode entities."""
    return normalize_spaces(html.unescape(TAG_RE.sub("", value)))


def extract_abstract(detail_html: str) -> str:
    """Extract abstract text from CVF paper detail page."""
    match = ABSTRACT_RE.search(detail_html)
    if not match:
        return ""
    return strip_tags(match.group(1))


def parse_one_paper(match: re.Match[str], year: int, collected_at: str) -> Dict[str, Any]:
    """Parse one paper block."""
    html_path = ensure_str(match.group("html"))
    title_raw = ensure_str(match.group("title"))
    authors_block = ensure_str(match.group("authors"))
    links_block = ensure_str(match.group("links"))

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

    link_pairs = re.findall(r'<a href="([^"]+)">([^<]+)</a>', links_block)
    pdf_url = ""
    arxiv_url = ""
    for href, anchor_text in link_pairs:
        label = normalize_spaces(anchor_text).lower()
        if label == "pdf" and not pdf_url:
            pdf_url = to_abs_url(href)
        elif label == "arxiv" and not arxiv_url:
            arxiv_url = to_abs_url(href)

    bibtex_block = ""
    bibtex_match = re.search(
        r'<div class="bibref pre-white-space">(.*?)</div>',
        links_block,
        flags=re.S,
    )
    if bibtex_match:
        bibtex_block = strip_tags(bibtex_match.group(1))

    source_ids: Dict[str, str] = {"cvf_paper_html": to_abs_url(html_path)}
    if pdf_url:
        source_ids["cvf_pdf_url"] = pdf_url
    if arxiv_url:
        source_ids["cvf_arxiv_url"] = arxiv_url
        arxiv_id = parse_arxiv_id(arxiv_url)
        if arxiv_id:
            source_ids["arxiv_id"] = arxiv_id
    bibtex_key = parse_bibtex_key(bibtex_block)
    if bibtex_key:
        source_ids["cvf_bibtex_key"] = bibtex_key
    pages = parse_bibtex_pages(bibtex_block)
    if pages:
        source_ids["cvf_pages"] = pages

    return {
        "paper_title": title,
        "authors": authors,
        "institutions": [],
        "abstract": "",
        "keywords": [],
        "presentation_level": "poster",
        "openalex_id": None,
        "doi": None,
        "track": "main",
        "track_display_name": "Main",
        "track_group": "main",
        "title": title,
        "url": to_abs_url(html_path) or None,
        "external_url": pdf_url or None,
        "citation_count": None,
        "venue": "CVPR",
        "year": year,
        "source_provider": "cvf_openaccess",
        "collected_at": collected_at,
        "source_ids": source_ids,
        "record_status": "resolved",
        "quality_flags": ["missing_abstract", "missing_keywords", "missing_institutions"],
    }


def parse_papers(page_html: str, year: int, collected_at: str) -> List[Dict[str, Any]]:
    """Extract all paper records from one CVPR year page."""
    papers: List[Dict[str, Any]] = []
    for match in PAPER_BLOCK_RE.finditer(page_html):
        paper = parse_one_paper(match=match, year=year, collected_at=collected_at)
        if paper["paper_title"]:
            papers.append(paper)
    return papers


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


def build_payload(year: int, papers: Sequence[Dict[str, Any]], collected_at: str) -> Dict[str, Any]:
    """Build root_json payload."""
    year_short = year % 100
    count = len(papers)
    return {
        "query": {
            "target": f"CVPR-{year_short:02d}",
            "venue_code": "CVPR",
            "year": year,
            "provider": "cvf_openaccess",
            "api_key_used": False,
            "work_filter_strategy": f"official_openaccess:CVPR{year}?day=all",
            "source_year_count_estimate": None,
        },
        "source": {
            "provider": "cvf_openaccess",
            "openalex_source_id": None,
            "openreview_venue_id": None,
            "display_name": "IEEE/CVF Conference on Computer Vision and Pattern Recognition",
            "source_type": "conference",
            "official_url": f"{CVF_BASE_URL}/CVPR{year}?day=all",
        },
        "generated_at_utc": collected_at,
        "paper_count": count,
        "track_counts": {"main": count},
        "track_group_counts": {"main": count},
        "presentation_level_counts": {"poster": count},
        "papers": list(papers),
    }


def collect_one_year(
    year: int,
    output_root: Path,
    timeout: float,
    retries: int,
    min_interval: float,
    fetch_abstracts: bool,
    workers: int,
) -> Dict[str, Any]:
    """Collect one year and write output JSON."""
    list_url = f"{CVF_BASE_URL}/CVPR{year}?day=all"
    LOGGER.info("Collecting CVPR %s from %s", year, list_url)
    collected_at = utc_now_iso()
    page_html = fetch_text(url=list_url, timeout=timeout, retries=retries, min_interval=min_interval)
    papers = parse_papers(page_html=page_html, year=year, collected_at=collected_at)
    abstract_success = 0
    abstract_failed = 0
    if fetch_abstracts:
        LOGGER.info("Fetching abstracts for CVPR %s (%s papers)", year, len(papers))
        abstract_success, abstract_failed = enrich_abstracts(
            papers=papers,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
            workers=workers,
        )
        LOGGER.info(
            "Abstract fetching CVPR %s done: success=%s failed=%s",
            year,
            abstract_success,
            abstract_failed,
        )
    payload = build_payload(year=year, papers=papers, collected_at=collected_at)

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"CVPR-{year % 100:02d}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Written %s papers to %s", len(papers), output_path)

    return {
        "year": year,
        "official_url": list_url,
        "official_paper_count": len(papers),
        "collected_paper_count": len(papers),
        "abstract_filled_count": abstract_success,
        "abstract_missing_count": abstract_failed,
        "output_file": str(output_path),
        "generated_at_utc": collected_at,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="Collect CVPR papers from CVF OpenAccess")
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

    summary: List[Dict[str, Any]] = []
    for year in years:
        summary.append(
            collect_one_year(
                year=year,
                output_root=output_root,
                timeout=args.timeout,
                retries=args.retries,
                min_interval=args.min_interval,
                fetch_abstracts=not args.no_fetch_abstracts,
                workers=args.workers,
            )
        )

    total = sum(int(item["collected_paper_count"]) for item in summary)
    report = {
        "generated_at_utc": utc_now_iso(),
        "provider": "cvf_openaccess",
        "venue": "CVPR",
        "years": years,
        "total_collected": total,
        "items": summary,
    }
    report_path = index_root / "cvpr_collection_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Collection report written: %s", report_path)
    LOGGER.info("Total collected papers: %s", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
