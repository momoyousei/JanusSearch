#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect AAAI papers (Technical Tracks) from official AAAI OJS pages."""

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
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("aaai_collect")

AAAI_BASE_URL = "https://ojs.aaai.org"
ARCHIVE_URL = f"{AAAI_BASE_URL}/index.php/AAAI/issue/archive"
OPENREVIEW_BASE_URL = "https://openreview.net"
OPENREVIEW_API2_URL = "https://api2.openreview.net"
DEFAULT_OUTPUT_ROOT = Path("archives/root_json")
DEFAULT_INDEX_ROOT = Path("index")

ISSUE_LINK_RE = re.compile(
    r'<a class="title" href="(?P<url>[^"]+/issue/view/\d+)">\s*(?P<title>[^<]+?)\s*</a>',
    re.S,
)
NEXT_PAGE_RE = re.compile(r'<a class="next" href="(?P<url>[^"]+)">Next</a>', re.S)
TECHNICAL_ISSUE_RE = re.compile(r"^AAAI-(?P<yy>\d{2})\s+Technical Tracks\s+(?P<seq>\d+)$")

ARTICLE_ENTRY_RE = re.compile(r'<div class="obj_article_summary">(?P<body>.*?)</div>\s*</li>', re.S)
ARTICLE_ANCHOR_RE = re.compile(
    r'<a id="article-(?P<id>\d+)" href="(?P<url>[^"]+/article/view/\d+)">\s*(?P<title>.*?)\s*</a>',
    re.S,
)
ARTICLE_AUTHORS_RE = re.compile(r'<div class="authors">\s*(?P<authors>.*?)\s*</div>', re.S)
ARTICLE_PAGES_RE = re.compile(r'<div class="pages">\s*(?P<pages>.*?)\s*</div>', re.S)
ARTICLE_PDF_RE = re.compile(r'<a class="obj_galley_link pdf" href="(?P<pdf>[^"]+)"', re.S)
ARTICLE_ID_RE = re.compile(r"/article/(?:view|download)/(?P<id>\d+)")
TAG_RE = re.compile(r"<[^>]+>")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


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


def normalize_spaces(value: str) -> str:
    """Collapse internal whitespace."""
    return re.sub(r"\s+", " ", ensure_str(value)).strip()


def parse_years(raw: str) -> List[int]:
    """Parse year expression like '2021-2026' or '2021,2022,2023'."""
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
        request = Request(url, headers=DEFAULT_HEADERS)
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "ignore")
        except (HTTPError, URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as err:
            last_err = err
            LOGGER.warning("Fetch failed (%s/%s) %s: %s", attempt, retries, url, err)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


def fetch_json(url: str, timeout: float, retries: int, min_interval: float) -> Dict[str, Any]:
    """Fetch JSON object from URL with retry."""
    text = fetch_text(url=url, timeout=timeout, retries=retries, min_interval=min_interval)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"Invalid JSON from {url}: {err}") from err
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected JSON payload type from {url}: {type(payload)}")
    return payload


def to_abs_url(value: str) -> str:
    """Return absolute URL."""
    raw = ensure_str(value)
    if not raw:
        return ""
    return urljoin(AAAI_BASE_URL, raw)


def split_authors(value: str) -> List[str]:
    """Split author text from issue listing."""
    text = normalize_spaces(strip_tags(value))
    if not text:
        return []
    return [item for item in (normalize_spaces(part) for part in text.split(",")) if item]


def strip_tags(value: str) -> str:
    """Remove HTML tags and decode entities."""
    return normalize_spaces(html.unescape(TAG_RE.sub("", ensure_str(value))))


def normalize_doi(value: str | None) -> str | None:
    """Normalize DOI representation."""
    raw = ensure_str(value)
    if not raw:
        return None
    lowered = raw.lower()
    if lowered.startswith("https://doi.org/"):
        raw = raw[16:]
    elif lowered.startswith("http://doi.org/"):
        raw = raw[15:]
    elif lowered.startswith("doi:"):
        raw = raw[4:]
    normalized = ensure_str(raw)
    return normalized or None


def slugify_track(value: str) -> str:
    """Convert track name to slug."""
    text = normalize_spaces(value).lower()
    if not text:
        return "main"
    slug = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return slug or "main"


def parse_article_id(url: str) -> str:
    """Extract article id from article URL."""
    match = ARTICLE_ID_RE.search(ensure_str(url))
    if not match:
        return ""
    return ensure_str(match.group("id"))


def dedupe_preserve(items: Iterable[str]) -> List[str]:
    """Deduplicate list while preserving order."""
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


class MetaCollector(HTMLParser):
    """Collect <meta name=... content=...> tags from article HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.meta: Dict[str, List[str]] = {}

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        attrs_map = {ensure_str(key).lower(): value for key, value in attrs}
        name = ensure_str(attrs_map.get("name")).lower()
        content = attrs_map.get("content")
        if not name or content is None:
            return
        self.meta.setdefault(name, []).append(ensure_str(content))


def first_meta(meta: Dict[str, List[str]], names: Sequence[str]) -> str:
    """Return first non-empty meta content by candidate names."""
    for name in names:
        values = meta.get(name.lower(), [])
        for value in values:
            normalized = normalize_spaces(value)
            if normalized:
                return normalized
    return ""


def all_meta(meta: Dict[str, List[str]], names: Sequence[str]) -> List[str]:
    """Return all non-empty meta values by candidate names."""
    result: List[str] = []
    for name in names:
        result.extend(meta.get(name.lower(), []))
    return dedupe_preserve(result)


def parse_archive_issues(archive_html: str) -> List[Dict[str, Any]]:
    """Parse issue links from archive page."""
    issues: List[Dict[str, Any]] = []
    for match in ISSUE_LINK_RE.finditer(archive_html):
        title = normalize_spaces(html.unescape(match.group("title")))
        url = to_abs_url(match.group("url"))
        parsed = TECHNICAL_ISSUE_RE.match(title)
        year: int | None = None
        seq: int | None = None
        if parsed:
            year = 2000 + int(parsed.group("yy"))
            seq = int(parsed.group("seq"))
        issues.append(
            {
                "title": title,
                "url": url,
                "year": year,
                "sequence": seq,
                "issue_id": ensure_str(url.rsplit("/", maxsplit=1)[-1]),
            }
        )
    return issues


def iter_archive_pages(timeout: float, retries: int, min_interval: float) -> List[Dict[str, Any]]:
    """Iterate paginated archive pages and aggregate issues."""
    issues: List[Dict[str, Any]] = []
    seen_pages: set[str] = set()
    seen_issues: set[str] = set()
    page_url = ARCHIVE_URL
    page_index = 0

    while page_url and page_url not in seen_pages:
        page_index += 1
        seen_pages.add(page_url)
        LOGGER.info("Loading archive page %s: %s", page_index, page_url)
        archive_html = fetch_text(
            url=page_url,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
        )
        for item in parse_archive_issues(archive_html):
            if item["url"] in seen_issues:
                continue
            seen_issues.add(item["url"])
            issues.append(item)

        next_match = NEXT_PAGE_RE.search(archive_html)
        page_url = to_abs_url(next_match.group("url")) if next_match else ""

    return issues


def parse_issue_articles(issue_html: str, issue_url: str, issue_title: str) -> List[Dict[str, Any]]:
    """Parse article summaries from one issue page."""
    records: List[Dict[str, Any]] = []
    for match in ARTICLE_ENTRY_RE.finditer(issue_html):
        body = match.group("body")
        anchor = ARTICLE_ANCHOR_RE.search(body)
        if not anchor:
            continue
        article_url = to_abs_url(anchor.group("url"))
        article_id = ensure_str(anchor.group("id")) or parse_article_id(article_url)
        title = strip_tags(anchor.group("title"))
        authors_match = ARTICLE_AUTHORS_RE.search(body)
        pages_match = ARTICLE_PAGES_RE.search(body)
        pdf_match = ARTICLE_PDF_RE.search(body)
        authors = split_authors(authors_match.group("authors")) if authors_match else []
        pages = strip_tags(pages_match.group("pages")) if pages_match else ""
        pdf_url = to_abs_url(pdf_match.group("pdf")) if pdf_match else ""
        records.append(
            {
                "article_id": article_id,
                "article_url": article_url,
                "title": title,
                "authors_from_issue": authors,
                "pages_from_issue": pages,
                "pdf_from_issue": pdf_url,
                "issue_url": issue_url,
                "issue_title": issue_title,
            }
        )

    return records


def parse_article_meta(article_html: str) -> Dict[str, Any]:
    """Parse key metadata from article page."""
    collector = MetaCollector()
    collector.feed(article_html)
    meta = collector.meta

    title = first_meta(meta, ("citation_title", "dc.title"))
    abstract = first_meta(meta, ("dc.description",))
    authors = all_meta(meta, ("citation_author", "dc.creator.personalname"))
    institutions = all_meta(meta, ("citation_author_institution",))
    doi = normalize_doi(first_meta(meta, ("citation_doi", "dc.identifier.doi")))
    pdf_url = first_meta(meta, ("citation_pdf_url",))
    article_url = first_meta(meta, ("citation_abstract_html_url", "dc.identifier.uri"))
    pages = first_meta(meta, ("dc.identifier.pagenumber",))
    track_display_name = first_meta(meta, ("dc.type.articletype",))

    return {
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "institutions": institutions,
        "doi": doi,
        "pdf_url": to_abs_url(pdf_url),
        "article_url": to_abs_url(article_url),
        "pages": pages,
        "track_display_name": track_display_name,
        "meta_article_id": first_meta(meta, ("dc.identifier",)),
    }


def build_quality_flags(
    *,
    authors: Sequence[str],
    abstract: str,
    institutions: Sequence[str],
    detail_failed: bool,
) -> List[str]:
    """Build quality flags for one paper."""
    flags: List[str] = []
    if not authors:
        flags.append("missing_authors")
    if not normalize_spaces(abstract):
        flags.append("missing_abstract")
    if not institutions:
        flags.append("missing_institutions")
    flags.append("missing_keywords")
    if detail_failed:
        flags.append("detail_fetch_failed")
    return flags


def build_paper_record(
    listing: Dict[str, Any],
    year: int,
    collected_at: str,
    detail: Dict[str, Any] | None,
    detail_error: str,
) -> Dict[str, Any]:
    """Build one paper record."""
    detail_failed = detail is None
    title = normalize_spaces(
        detail["title"] if detail and detail.get("title") else ensure_str(listing.get("title"))
    )
    authors = detail["authors"] if detail and detail.get("authors") else listing["authors_from_issue"]
    institutions = detail["institutions"] if detail and detail.get("institutions") else []
    abstract = detail["abstract"] if detail and detail.get("abstract") else ""
    doi = detail["doi"] if detail else None
    article_url = (
        detail["article_url"]
        if detail and detail.get("article_url")
        else ensure_str(listing.get("article_url"))
    )
    pdf_url = (
        detail["pdf_url"] if detail and detail.get("pdf_url") else ensure_str(listing.get("pdf_from_issue"))
    )
    pages = detail["pages"] if detail and detail.get("pages") else ensure_str(listing.get("pages_from_issue"))
    track_display_name = (
        detail["track_display_name"]
        if detail and detail.get("track_display_name")
        else "AAAI Technical Track"
    )
    track_slug = slugify_track(track_display_name)
    track_group = "main" if "technical track" in track_display_name.lower() else "other"
    if track_slug == "main":
        track_group = "main"

    article_id = (
        ensure_str(detail.get("meta_article_id"))
        if detail
        else ensure_str(listing.get("article_id"))
    )
    if not article_id:
        article_id = parse_article_id(article_url) or ensure_str(listing.get("article_id"))

    quality_flags = build_quality_flags(
        authors=authors,
        abstract=abstract,
        institutions=institutions,
        detail_failed=detail_failed,
    )

    source_ids: Dict[str, str] = {
        "aaai_article_url": ensure_str(article_url),
        "aaai_issue_url": ensure_str(listing.get("issue_url")),
        "aaai_issue_title": ensure_str(listing.get("issue_title")),
    }
    if article_id:
        source_ids["aaai_article_id"] = article_id
    if pdf_url:
        source_ids["aaai_pdf_url"] = ensure_str(pdf_url)
    if pages:
        source_ids["aaai_pages"] = ensure_str(pages)
    if detail_failed and detail_error:
        source_ids["detail_fetch_error"] = ensure_str(detail_error)

    return {
        "paper_title": title,
        "authors": list(authors),
        "institutions": list(institutions),
        "abstract": normalize_spaces(abstract),
        "keywords": [],
        "presentation_level": "poster",
        "openalex_id": None,
        "doi": doi,
        "track": track_slug,
        "track_display_name": track_display_name or "Main",
        "track_group": track_group,
        "title": title,
        "url": article_url or None,
        "external_url": pdf_url or None,
        "citation_count": None,
        "venue": "AAAI",
        "year": year,
        "source_provider": "aaai_ojs",
        "collected_at": collected_at,
        "source_ids": source_ids,
        "record_status": "placeholder" if detail_failed else "resolved",
        "quality_flags": quality_flags,
    }


def fetch_and_parse_article(
    listing: Dict[str, Any],
    timeout: float,
    retries: int,
    min_interval: float,
) -> Tuple[Dict[str, Any] | None, str]:
    """Fetch and parse one article page."""
    article_url = ensure_str(listing.get("article_url"))
    if not article_url:
        return None, "missing_article_url"
    try:
        article_html = fetch_text(
            url=article_url,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
        )
        return parse_article_meta(article_html), ""
    except Exception as err:  # pragma: no cover - network variability
        return None, ensure_str(err)


def count_field(items: Sequence[Dict[str, Any]], key: str, default: str) -> Dict[str, int]:
    """Count categorical field values."""
    counts: Dict[str, int] = {}
    for item in items:
        value = normalize_spaces(ensure_str(item.get(key))) or default
        counts[value] = counts.get(value, 0) + 1
    return counts


def build_payload(
    year: int,
    papers: Sequence[Dict[str, Any]],
    collected_at: str,
    source_year_count_estimate: int | None,
) -> Dict[str, Any]:
    """Build root_json payload."""
    year_short = year % 100
    paper_count = len(papers)
    return {
        "query": {
            "target": f"AAAI-{year_short:02d}",
            "venue_code": "AAAI",
            "year": year,
            "provider": "aaai_ojs",
            "api_key_used": False,
            "work_filter_strategy": f"official_ojs_archive:AAAI-{year_short:02d} Technical Tracks *",
            "source_year_count_estimate": source_year_count_estimate,
        },
        "source": {
            "provider": "aaai_ojs",
            "openalex_source_id": None,
            "openreview_venue_id": None,
            "display_name": "Proceedings of the AAAI Conference on Artificial Intelligence",
            "source_type": "conference",
            "official_url": ARCHIVE_URL,
        },
        "generated_at_utc": collected_at,
        "paper_count": paper_count,
        "track_counts": count_field(papers, key="track", default="main"),
        "track_group_counts": count_field(papers, key="track_group", default="main"),
        "presentation_level_counts": count_field(
            papers, key="presentation_level", default="poster"
        ),
        "papers": list(papers),
    }


def note_value(content: Dict[str, Any], key: str) -> Any:
    """Read OpenReview content value that may be wrapped in {'value': ...}."""
    raw = content.get(key)
    if isinstance(raw, dict) and "value" in raw:
        return raw.get("value")
    return raw


def normalize_list_str(values: Any) -> List[str]:
    """Normalize list-like field to string list."""
    if isinstance(values, str):
        normalized = normalize_spaces(values)
        return [normalized] if normalized else []
    if not isinstance(values, list):
        return []
    items: List[str] = []
    for value in values:
        text = normalize_spaces(ensure_str(value))
        if text:
            items.append(text)
    return items


def parse_submission_count(text: str) -> int | None:
    """Extract submission count from announcement text if present."""
    body = ensure_str(text)
    if not body:
        return None
    patterns = (
        r"receiving\s+([0-9][0-9,]*)\s+submissions",
        r"received\s+([0-9][0-9,]*)\s+submissions",
    )
    for pattern in patterns:
        match = re.search(pattern, body, flags=re.I)
        if not match:
            continue
        digits = match.group(1).replace(",", "")
        try:
            return int(digits)
        except ValueError:
            return None
    return None


def fetch_openreview_reference_note(
    year: int,
    timeout: float,
    retries: int,
    min_interval: float,
) -> Dict[str, Any] | None:
    """Fetch OpenReview announcement note used as supplemental reference."""
    query = quote(f"OpenReview Hosts Record-Breaking AAAI {year} Conference")
    url = f"{OPENREVIEW_API2_URL}/notes/search?query={query}&limit=5&offset=0"
    data = fetch_json(url=url, timeout=timeout, retries=retries, min_interval=min_interval)
    for note in data.get("notes", []):
        if not isinstance(note, dict):
            continue
        content = note.get("content", {})
        if not isinstance(content, dict):
            continue
        title = ensure_str(note_value(content, "title"))
        article = ensure_str(note_value(content, "article"))
        if f"AAAI {year}" not in title and f"AAAI {year}" not in article:
            continue
        note_id = ensure_str(note.get("id"))
        if not note_id:
            continue
        return {
            "note_id": note_id,
            "title": title,
            "url": f"{OPENREVIEW_BASE_URL}/forum?id={note_id}",
            "submission_count_reported": parse_submission_count(article),
        }
    return None


def build_openreview_paper_record(
    note: Dict[str, Any],
    year: int,
    collected_at: str,
) -> Dict[str, Any]:
    """Build one paper record from OpenReview note."""
    content = note.get("content", {})
    if not isinstance(content, dict):
        content = {}

    note_id = ensure_str(note.get("id"))
    forum_id = ensure_str(note.get("forum")) or note_id
    title = normalize_spaces(ensure_str(note_value(content, "title")))
    authors = normalize_list_str(note_value(content, "authors"))
    abstract = normalize_spaces(ensure_str(note_value(content, "abstract")))
    keywords = normalize_list_str(note_value(content, "keywords"))
    doi = normalize_doi(ensure_str(note_value(content, "doi")))
    venue_label = normalize_spaces(ensure_str(note_value(content, "venue")))
    venue_id = normalize_spaces(ensure_str(note_value(content, "venueid")))
    pdf_path = ensure_str(note_value(content, "pdf"))
    pdf_url = to_abs_url(pdf_path) if pdf_path.startswith("/") else ensure_str(pdf_path)
    forum_url = f"{OPENREVIEW_BASE_URL}/forum?id={forum_id or note_id}" if (forum_id or note_id) else ""

    presentation_level = "poster"
    lowered_venue = venue_label.lower()
    if "oral" in lowered_venue or "spotlight" in lowered_venue:
        presentation_level = "oral"

    quality_flags: List[str] = []
    if not authors:
        quality_flags.append("missing_authors")
    if not abstract:
        quality_flags.append("missing_abstract")
    if not keywords:
        quality_flags.append("missing_keywords")
    quality_flags.append("missing_institutions")

    source_ids: Dict[str, str] = {}
    if note_id:
        source_ids["openreview_note_id"] = note_id
    if forum_id:
        source_ids["openreview_forum_id"] = forum_id
    if venue_label:
        source_ids["openreview_venue"] = venue_label
    if venue_id:
        source_ids["openreview_venueid"] = venue_id
    invitations = note.get("invitations")
    if isinstance(invitations, list) and invitations:
        source_ids["openreview_invitation"] = ensure_str(invitations[0])
    if pdf_url:
        source_ids["openreview_pdf_url"] = pdf_url

    return {
        "paper_title": title,
        "authors": authors,
        "institutions": [],
        "abstract": abstract,
        "keywords": keywords,
        "presentation_level": presentation_level,
        "openalex_id": None,
        "doi": doi,
        "track": "main",
        "track_display_name": "Main",
        "track_group": "main",
        "title": title,
        "url": forum_url or None,
        "external_url": pdf_url or None,
        "citation_count": None,
        "venue": "AAAI",
        "year": year,
        "source_provider": "openreview",
        "collected_at": collected_at,
        "source_ids": source_ids,
        "record_status": "placeholder" if ("missing_authors" in quality_flags or "missing_abstract" in quality_flags) else "resolved",
        "quality_flags": quality_flags,
    }


def build_openreview_payload(
    year: int,
    papers: Sequence[Dict[str, Any]],
    collected_at: str,
    venue_labels: Sequence[str],
    reference_note: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """Build root_json payload for OpenReview fallback data."""
    year_short = year % 100
    query: Dict[str, Any] = {
        "target": f"AAAI-{year_short:02d}",
        "venue_code": "AAAI",
        "year": year,
        "provider": "openreview",
        "api_key_used": False,
        "work_filter_strategy": f"openreview:content.venue in {list(venue_labels)}",
        "source_year_count_estimate": len(papers),
    }
    if reference_note:
        query["reference_note_url"] = reference_note.get("url")
        if reference_note.get("submission_count_reported") is not None:
            query["submission_count_reported"] = int(reference_note["submission_count_reported"])

    return {
        "query": query,
        "source": {
            "provider": "openreview",
            "openalex_source_id": None,
            "openreview_venue_id": f"AAAI {year}",
            "display_name": "OpenReview (AAAI early listing)",
            "source_type": "conference",
            "official_url": f"{OPENREVIEW_BASE_URL}/search?query=AAAI%20{year}",
        },
        "generated_at_utc": collected_at,
        "paper_count": len(papers),
        "track_counts": count_field(papers, key="track", default="main"),
        "track_group_counts": count_field(papers, key="track_group", default="main"),
        "presentation_level_counts": count_field(
            papers, key="presentation_level", default="poster"
        ),
        "papers": list(papers),
    }


def collect_openreview_year(
    *,
    year: int,
    output_root: Path,
    timeout: float,
    retries: int,
    min_interval: float,
) -> Dict[str, Any]:
    """Collect one AAAI year from OpenReview fallback source."""
    collected_at = utc_now_iso()
    year_short = year % 100
    venue_labels = [
        f"AAAI {year}",
        f"AAAI {year} Oral",
        f"AAAI {year} Spotlight",
        f"AAAI {year} Poster",
    ]

    note_map: Dict[str, Dict[str, Any]] = {}
    venue_counts: Dict[str, int] = {}
    page_size = 1000
    for venue_label in venue_labels:
        offset = 0
        total_for_label = 0
        while True:
            params = urlencode(
                {
                    "content.venue": venue_label,
                    "limit": page_size,
                    "offset": offset,
                }
            )
            url = f"{OPENREVIEW_API2_URL}/notes?{params}"
            data = fetch_json(url=url, timeout=timeout, retries=retries, min_interval=min_interval)
            notes = data.get("notes", [])
            if not isinstance(notes, list):
                notes = []
            total_for_label = int(data.get("count") or total_for_label)
            for note in notes:
                if not isinstance(note, dict):
                    continue
                note_id = ensure_str(note.get("id"))
                if not note_id:
                    continue
                note_map[note_id] = note
            if not notes:
                break
            offset += len(notes)
            if len(notes) < page_size:
                break
        venue_counts[venue_label] = total_for_label

    papers = [
        build_openreview_paper_record(note=note, year=year, collected_at=collected_at)
        for note in note_map.values()
    ]
    papers.sort(key=lambda item: normalize_spaces(ensure_str(item.get("title"))).lower())

    reference_note = fetch_openreview_reference_note(
        year=year,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
    )
    payload = build_openreview_payload(
        year=year,
        papers=papers,
        collected_at=collected_at,
        venue_labels=venue_labels,
        reference_note=reference_note,
    )

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"AAAI-{year_short:02d}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    LOGGER.info(
        "AAAI %s collected from OpenReview fallback: collected=%s",
        year,
        len(papers),
    )
    return {
        "year": year,
        "issue_count": 0,
        "official_article_entry_count": len(papers),
        "official_unique_article_count": len(papers),
        "collected_paper_count": len(papers),
        "missing_vs_official_unique": 0,
        "detail_fetch_failed_count": sum(
            1 for paper in papers if paper.get("record_status") == "placeholder"
        ),
        "output_file": str(output_path),
        "generated_at_utc": collected_at,
        "issues": [],
        "provider": "openreview",
        "openreview_venue_counts": venue_counts,
        "reference_note": reference_note,
    }


def collect_one_year(
    *,
    year: int,
    issues: Sequence[Dict[str, Any]],
    output_root: Path,
    timeout: float,
    retries: int,
    min_interval: float,
    workers: int,
    fetch_details: bool,
) -> Dict[str, Any]:
    """Collect one AAAI year and write output JSON."""
    collected_at = utc_now_iso()
    year_short = year % 100
    issue_reports: List[Dict[str, Any]] = []
    year_listings: List[Dict[str, Any]] = []

    total_issues = len(issues)
    for index, issue in enumerate(issues, start=1):
        issue_url = ensure_str(issue.get("url"))
        issue_title = ensure_str(issue.get("title"))
        issue_seq = issue.get("sequence")
        LOGGER.info("AAAI %s issue %s/%s: %s", year, index, total_issues, issue_title)
        issue_html = fetch_text(
            url=issue_url,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
        )
        listings = parse_issue_articles(
            issue_html=issue_html,
            issue_url=issue_url,
            issue_title=issue_title,
        )
        for item in listings:
            item["issue_sequence"] = issue_seq
        year_listings.extend(listings)
        issue_reports.append(
            {
                "issue_id": ensure_str(issue.get("issue_id")),
                "issue_title": issue_title,
                "issue_url": issue_url,
                "official_article_count": len(listings),
            }
        )

    official_entry_count = len(year_listings)
    unique_by_article: Dict[str, Dict[str, Any]] = {}
    for listing in year_listings:
        key = ensure_str(listing.get("article_id")) or ensure_str(listing.get("article_url"))
        if not key:
            key = f"fallback::{len(unique_by_article)}::{ensure_str(listing.get('title'))}"
        unique_by_article[key] = listing
    unique_listings = list(unique_by_article.values())
    unique_official_count = len(unique_listings)

    papers: List[Dict[str, Any]] = []
    detail_failed_count = 0
    if fetch_details and unique_listings:
        worker_count = max(1, workers)
        detail_total = len(unique_listings)
        detail_done = 0
        LOGGER.info("AAAI %s fetching article details: %s items", year, detail_total)
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    fetch_and_parse_article,
                    listing,
                    timeout,
                    retries,
                    min_interval,
                ): listing
                for listing in unique_listings
            }
            for future in as_completed(futures):
                listing = futures[future]
                detail, err = future.result()
                detail_done += 1
                if detail is None:
                    detail_failed_count += 1
                if detail_done == detail_total or detail_done % 500 == 0:
                    LOGGER.info(
                        "AAAI %s detail progress: %s/%s (failed=%s)",
                        year,
                        detail_done,
                        detail_total,
                        detail_failed_count,
                    )
                papers.append(
                    build_paper_record(
                        listing=listing,
                        year=year,
                        collected_at=collected_at,
                        detail=detail,
                        detail_error=err,
                    )
                )
    else:
        for listing in unique_listings:
            papers.append(
                build_paper_record(
                    listing=listing,
                    year=year,
                    collected_at=collected_at,
                    detail=None,
                    detail_error="detail_fetch_disabled",
                )
            )
        detail_failed_count = len(unique_listings)

    papers.sort(key=lambda item: normalize_spaces(ensure_str(item.get("title"))).lower())
    payload = build_payload(
        year=year,
        papers=papers,
        collected_at=collected_at,
        source_year_count_estimate=unique_official_count,
    )

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"AAAI-{year_short:02d}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info(
        "AAAI %s collected: official_entries=%s unique=%s collected=%s",
        year,
        official_entry_count,
        unique_official_count,
        len(papers),
    )

    return {
        "year": year,
        "issue_count": len(issues),
        "official_article_entry_count": official_entry_count,
        "official_unique_article_count": unique_official_count,
        "collected_paper_count": len(papers),
        "missing_vs_official_unique": max(0, unique_official_count - len(papers)),
        "detail_fetch_failed_count": detail_failed_count,
        "output_file": str(output_path),
        "generated_at_utc": collected_at,
        "issues": issue_reports,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(
        description="Collect AAAI Technical Track papers from official AAAI OJS pages"
    )
    parser.add_argument(
        "--years",
        default="2021-2026",
        help="Year range, e.g. 2021-2026 or 2021,2022,2023",
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
        default=0.4,
        help="Sleep seconds between retries",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=20,
        help="Parallel workers for article detail fetching (default: 20)",
    )
    parser.add_argument(
        "--no-fetch-details",
        action="store_true",
        help="Do not fetch article detail pages (metadata will be listing-only placeholders)",
    )
    parser.add_argument(
        "--no-openreview-fallback",
        action="store_true",
        help="Disable OpenReview fallback when OJS technical-track issues are unavailable",
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

    all_issues = iter_archive_pages(
        timeout=args.timeout,
        retries=args.retries,
        min_interval=args.min_interval,
    )
    issue_map: Dict[int, List[Dict[str, Any]]] = {year: [] for year in years}
    for issue in all_issues:
        issue_year = issue.get("year")
        if issue_year in issue_map:
            issue_map[issue_year].append(issue)
    for year in years:
        issue_map[year].sort(key=lambda item: int(item.get("sequence") or 0))

    summary: List[Dict[str, Any]] = []
    missing_years: List[int] = []
    used_openreview_fallback = False
    for year in years:
        year_issues = issue_map.get(year, [])
        if not year_issues:
            LOGGER.warning("No AAAI Technical Track issues found for year %s", year)
            if not args.no_openreview_fallback:
                LOGGER.info("Falling back to OpenReview collection for AAAI %s", year)
                fallback_summary = collect_openreview_year(
                    year=year,
                    output_root=output_root,
                    timeout=args.timeout,
                    retries=args.retries,
                    min_interval=args.min_interval,
                )
                summary.append(fallback_summary)
                used_openreview_fallback = True
                if int(fallback_summary.get("collected_paper_count") or 0) == 0:
                    missing_years.append(year)
                continue

            missing_years.append(year)
            collected_at = utc_now_iso()
            payload = build_payload(
                year=year,
                papers=[],
                collected_at=collected_at,
                source_year_count_estimate=0,
            )
            output_root.mkdir(parents=True, exist_ok=True)
            output_path = output_root / f"AAAI-{year % 100:02d}.json"
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            summary.append(
                {
                    "year": year,
                    "issue_count": 0,
                    "official_article_entry_count": 0,
                    "official_unique_article_count": 0,
                    "collected_paper_count": 0,
                    "missing_vs_official_unique": 0,
                    "detail_fetch_failed_count": 0,
                    "output_file": str(output_path),
                    "generated_at_utc": collected_at,
                    "issues": [],
                    "note": "no_technical_track_issue_found_in_archive",
                }
            )
            continue

        LOGGER.info("Collecting AAAI %s from %s issues", year, len(year_issues))
        summary.append(
            collect_one_year(
                year=year,
                issues=year_issues,
                output_root=output_root,
                timeout=args.timeout,
                retries=args.retries,
                min_interval=args.min_interval,
                workers=args.workers,
                fetch_details=not args.no_fetch_details,
            )
        )

    total_collected = sum(int(item["collected_paper_count"]) for item in summary)
    total_official_unique = sum(int(item["official_unique_article_count"]) for item in summary)
    report = {
        "generated_at_utc": utc_now_iso(),
        "provider": "aaai_ojs_with_openreview_fallback" if used_openreview_fallback else "aaai_ojs",
        "venue": "AAAI",
        "years": years,
        "missing_years": missing_years,
        "total_official_unique": total_official_unique,
        "total_collected": total_collected,
        "official_vs_collected_aligned": total_collected == total_official_unique,
        "items": summary,
    }
    report_path = index_root / "aaai_collection_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Collection report written: %s", report_path)
    LOGGER.info("Total official unique papers: %s", total_official_unique)
    LOGGER.info("Total collected papers: %s", total_collected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
