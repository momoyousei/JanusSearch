#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect IJCAI papers (2021+) from ijcai.org with DBLP/OpenReview/S2 supplements."""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import socket
import time
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

LOGGER = logging.getLogger("ijcai_collect")

IJCAI_BASE_URL = "https://www.ijcai.org"
IJCAI_PROCEEDINGS_URL_TEMPLATE = "https://www.ijcai.org/proceedings/{year}/"
DBLP_IJCAI_INDEX_URL = "https://dblp.org/db/conf/ijcai/"
DBLP_IJCAI_XML_URL_TEMPLATE = "https://dblp.org/db/conf/ijcai/ijcai{tag}.xml"
DBLP_REC_BASE = "https://dblp.org/rec/"
OPENREVIEW_API2_URL = "https://api2.openreview.net"
S2_BASE = "https://api.semanticscholar.org/graph/v1"
S2_FIELDS = "paperId,title,abstract,authors,citationCount,url,externalIds"

DEFAULT_OUTPUT_ROOT = Path("archives/root_json")
DEFAULT_INDEX_ROOT = Path("index")
DEFAULT_HEADERS = {
    "User-Agent": "JanusSearch/1.0 (mailto:janus@example.com)",
    "Accept": "application/json,text/html,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "close",
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


def normalize_spaces(text: str) -> str:
    """Collapse internal whitespace."""
    return re.sub(r"\s+", " ", ensure_str(text)).strip()


def normalize_title(value: str) -> str:
    """Normalize title text for matching."""
    text = normalize_spaces(html.unescape(value))
    if text.endswith("."):
        text = text[:-1].rstrip()
    return text


def normalize_doi(value: str | None) -> str | None:
    """Normalize DOI string."""
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
    normalized = normalize_spaces(raw).lower()
    return normalized or None


def normalize_title_key(value: str) -> str:
    """Build loose title key for cross-source matching."""
    text = normalize_title(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return normalize_spaces(text)


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
                deadline = time.monotonic() + max(8.0, timeout * 2.0)
                chunks: List[bytes] = []
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"read deadline exceeded for {url}")
                    block = response.read(64 * 1024)
                    if not block:
                        break
                    chunks.append(block)
                return b"".join(chunks).decode("utf-8", "ignore")
        except (HTTPError, URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as err:
            last_err = err
            LOGGER.warning("Fetch failed (%s/%s) %s: %s", attempt, retries, url, err)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


def fetch_json(url: str, timeout: float, retries: int, min_interval: float) -> Dict[str, Any]:
    """Fetch JSON object."""
    payload = fetch_text(url=url, timeout=timeout, retries=retries, min_interval=min_interval)
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"Invalid JSON from {url}: {err}") from err
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected JSON type from {url}: {type(result)}")
    return result


def strip_tags(value: str) -> str:
    """Strip HTML tags and normalize spaces."""
    return normalize_spaces(html.unescape(re.sub(r"<[^>]+>", " ", ensure_str(value))))


def split_authors(value: str) -> List[str]:
    """Split authors by comma and preserve order."""
    text = strip_tags(value)
    if not text:
        return []
    parts = [normalize_spaces(part) for part in text.split(",")]
    return [part for part in parts if part]


def dedupe_preserve(values: Iterable[str]) -> List[str]:
    """Deduplicate values preserving first appearance."""
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        item = normalize_spaces(value)
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def slugify_track(value: str, default: str = "main") -> str:
    """Convert track name to slug."""
    text = normalize_spaces(value).lower()
    if not text:
        return default
    slug = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return slug or default


def extract_doi_from_links(urls: Sequence[str]) -> str | None:
    """Extract DOI from URL-like fields."""
    for raw in urls:
        value = ensure_str(raw)
        if not value:
            continue
        match = re.search(r"doi\.org/(10\.[^/?#\s]+(?:/[^\s?#]+)?)", value, flags=re.I)
        if match:
            return normalize_doi(match.group(1))
        if value.lower().startswith("10."):
            return normalize_doi(value)
    return None


class MetaCollector(HTMLParser):
    """Collect <meta name=... content=...> tags."""

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
    values: List[str] = []
    for name in names:
        values.extend(meta.get(name.lower(), []))
    return dedupe_preserve(values)


SECTION_TITLE_RE = re.compile(
    r'<div class="section_title">\s*<h3>(?P<title>.*?)</h3>\s*</div>',
    re.I | re.S,
)
SUBSECTION_TITLE_RE = re.compile(
    r'<div class="subsection_title">\s*(?P<title>.*?)\s*</div>',
    re.I | re.S,
)
PAPER_BLOCK_RE = re.compile(
    r'<div id="paper(?P<paper_id>\d+)" class="paper_wrapper">\s*'
    r'<div class="title">(?P<title>.*?)</div>\s*'
    r'<div class="authors">(?P<authors>.*?)</div>\s*'
    r'<div class="details">\((?P<details>.*?)\)</div>\s*'
    r"</div>",
    re.I | re.S,
)
DETAIL_LINK_RE = re.compile(
    r'<a href="(?P<href>[^"]+)">\s*Details\s*</a>',
    re.I | re.S,
)
PDF_LINK_RE = re.compile(
    r'<a href="(?P<href>[^"]+\.pdf)">\s*PDF\s*</a>',
    re.I | re.S,
)
KEYWORD_TOPIC_RE = re.compile(
    r'<div class="topic">\s*(?P<topic>.*?)\s*</div>',
    re.I | re.S,
)
ABSTRACT_BLOCK_RE = re.compile(
    r'<div class="row">\s*'
    r'<div class="col-md-12">\s*(?P<abstract>.*?)\s*</div>\s*'
    r'<div class="col-md-12">\s*<div class="keywords">',
    re.I | re.S,
)


def parse_official_listing(year: int, listing_html: str) -> List[Dict[str, Any]]:
    """Parse ijcai.org proceedings listing to paper stubs."""
    section_positions: List[int] = []
    section_titles: List[str] = []
    for match in SECTION_TITLE_RE.finditer(listing_html):
        section_positions.append(match.start())
        section_titles.append(strip_tags(match.group("title")))

    subsection_positions: List[int] = []
    subsection_titles: List[str] = []
    for match in SUBSECTION_TITLE_RE.finditer(listing_html):
        subsection_positions.append(match.start())
        subsection_titles.append(strip_tags(match.group("title")))

    proceedings_url = IJCAI_PROCEEDINGS_URL_TEMPLATE.format(year=year)
    papers: List[Dict[str, Any]] = []
    for match in PAPER_BLOCK_RE.finditer(listing_html):
        pos = match.start()
        section_idx = bisect_right(section_positions, pos) - 1
        section_title = section_titles[section_idx] if section_idx >= 0 else "Main Track"
        subsection_idx = bisect_right(subsection_positions, pos) - 1
        subsection_title = ""
        if subsection_idx >= 0:
            subsection_pos = subsection_positions[subsection_idx]
            if section_idx < 0 or subsection_pos > section_positions[section_idx]:
                subsection_title = subsection_titles[subsection_idx]

        details_html = ensure_str(match.group("details"))
        detail_match = DETAIL_LINK_RE.search(details_html)
        pdf_match = PDF_LINK_RE.search(details_html)
        detail_href = ensure_str(detail_match.group("href")) if detail_match else ""
        pdf_href = ensure_str(pdf_match.group("href")) if pdf_match else ""

        paper_id = int(match.group("paper_id"))
        title = normalize_title(strip_tags(match.group("title")))
        authors = split_authors(match.group("authors"))
        detail_url = urljoin(proceedings_url, detail_href) if detail_href else (
            f"{IJCAI_BASE_URL}/proceedings/{year}/{paper_id}"
        )
        pdf_url = urljoin(proceedings_url, pdf_href) if pdf_href else ""

        papers.append(
            {
                "paper_id": paper_id,
                "title": title,
                "authors": authors,
                "section_title": section_title,
                "subsection_title": subsection_title,
                "detail_url": detail_url,
                "pdf_url": pdf_url,
            }
        )
    return papers


def parse_official_detail_page(detail_html: str) -> Dict[str, Any]:
    """Parse one IJCAI detail page."""
    parser = MetaCollector()
    parser.feed(detail_html)
    meta = parser.meta

    title = normalize_title(first_meta(meta, ("citation_title",)))
    authors = all_meta(meta, ("citation_author",))
    doi = normalize_doi(first_meta(meta, ("citation_doi",)))
    pdf_url = normalize_spaces(first_meta(meta, ("citation_pdf_url",)))
    first_page = normalize_spaces(first_meta(meta, ("citation_firstpage",)))
    last_page = normalize_spaces(first_meta(meta, ("citation_lastpage",)))
    pages = ""
    if first_page and last_page:
        pages = f"{first_page}-{last_page}"
    elif first_page:
        pages = first_page
    elif last_page:
        pages = last_page

    abstract = ""
    abstract_match = ABSTRACT_BLOCK_RE.search(detail_html)
    if abstract_match:
        abstract = strip_tags(abstract_match.group("abstract"))
    keywords = dedupe_preserve(strip_tags(topic) for topic in KEYWORD_TOPIC_RE.findall(detail_html))

    return {
        "title": title,
        "authors": authors,
        "doi": doi,
        "pdf_url": pdf_url,
        "pages": pages,
        "abstract": abstract,
        "keywords": keywords,
    }


def parse_dblp_xml_records(xml_text: str, year: int, xml_url: str) -> List[Dict[str, Any]]:
    """Parse inproceedings records from one DBLP XML."""
    root = ET.fromstring(xml_text)
    records: List[Dict[str, Any]] = []
    for node in root.findall(".//inproceedings"):
        node_year = normalize_spaces(ensure_str(node.findtext("year")))
        if node_year and node_year != str(year):
            continue

        key = normalize_spaces(ensure_str(node.attrib.get("key")))
        title = normalize_title("".join(node.find("title").itertext()) if node.find("title") is not None else "")
        if not title:
            continue
        authors = [
            normalize_spaces("".join(author.itertext()))
            for author in node.findall("author")
            if normalize_spaces("".join(author.itertext()))
        ]
        pages = normalize_spaces(ensure_str(node.findtext("pages")))
        ee_values = [
            normalize_spaces(ensure_str(ee.text))
            for ee in node.findall("ee")
            if normalize_spaces(ensure_str(ee.text))
        ]
        doi = extract_doi_from_links(ee_values)
        rec_url = f"{DBLP_REC_BASE}{key}" if key else ""
        records.append(
            {
                "dblp_key": key,
                "title": title,
                "title_key": normalize_title_key(title),
                "authors": authors,
                "doi": doi,
                "pages": pages,
                "ee_values": ee_values,
                "xml_url": xml_url,
                "rec_url": rec_url,
            }
        )
    return records


def resolve_dblp_year_tags(
    years: Sequence[int],
    timeout: float,
    retries: int,
    min_interval: float,
) -> Dict[int, List[str]]:
    """Resolve DBLP XML tags for IJCAI years.

    IJCAI 2021+ currently uses one XML stream per year (`ijcai{year}.xml`).
    To keep collection stable under flaky network conditions, we use deterministic
    yearly tags directly instead of querying the DBLP index page.
    """
    _ = (timeout, retries, min_interval)
    return {year: [str(year)] for year in years}


def parse_openreview_note(note: Dict[str, Any]) -> Dict[str, Any]:
    """Parse one OpenReview note into lightweight structure."""
    content = note.get("content")
    if not isinstance(content, dict):
        content = {}

    def note_value(key: str) -> Any:
        raw = content.get(key)
        if isinstance(raw, dict) and "value" in raw:
            return raw.get("value")
        return raw

    title = normalize_title(ensure_str(note_value("title")))
    authors_raw = note_value("authors")
    if isinstance(authors_raw, list):
        authors = [normalize_spaces(ensure_str(item)) for item in authors_raw if normalize_spaces(ensure_str(item))]
    else:
        authors = []
    abstract = normalize_spaces(ensure_str(note_value("abstract")))
    doi = normalize_doi(ensure_str(note_value("doi")))
    pdf = normalize_spaces(ensure_str(note_value("pdf")))
    if pdf.startswith("/"):
        pdf = urljoin("https://openreview.net", pdf)

    return {
        "note_id": normalize_spaces(ensure_str(note.get("id"))),
        "forum_id": normalize_spaces(ensure_str(note.get("forum"))),
        "title": title,
        "title_key": normalize_title_key(title),
        "authors": authors,
        "abstract": abstract,
        "doi": doi,
        "pdf_url": pdf,
    }


def fetch_openreview_count(
    year: int,
    timeout: float,
    retries: int,
    min_interval: float,
) -> int:
    """Fetch OpenReview count for one IJCAI year."""
    venue_label = f"IJCAI {year}"
    params = urlencode(
        {
            "content.venue": venue_label,
            "limit": 1,
            "offset": 0,
        }
    )
    url = f"{OPENREVIEW_API2_URL}/notes?{params}"
    payload = fetch_json(url=url, timeout=timeout, retries=retries, min_interval=min_interval)
    return int(payload.get("count") or 0)


def fetch_openreview_maps(
    year: int,
    timeout: float,
    retries: int,
    min_interval: float,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Fetch OpenReview notes for IJCAI year and build DOI/title maps."""
    venue_label = f"IJCAI {year}"
    offset = 0
    page_size = 1000
    by_doi: Dict[str, Dict[str, Any]] = {}
    by_title: Dict[str, Dict[str, Any]] = {}

    while True:
        params = urlencode(
            {
                "content.venue": venue_label,
                "limit": page_size,
                "offset": offset,
            }
        )
        url = f"{OPENREVIEW_API2_URL}/notes?{params}"
        payload = fetch_json(url=url, timeout=timeout, retries=retries, min_interval=min_interval)
        notes = payload.get("notes")
        if not isinstance(notes, list):
            notes = []
        if not notes:
            break
        for note in notes:
            if not isinstance(note, dict):
                continue
            parsed = parse_openreview_note(note)
            if parsed["doi"]:
                by_doi[parsed["doi"]] = parsed
            if parsed["title_key"]:
                by_title[parsed["title_key"]] = parsed
        offset += len(notes)
        if len(notes) < page_size:
            break
    return by_doi, by_title


class SemanticScholarClient:
    """Small Semantic Scholar client with retry and rate control."""

    def __init__(
        self,
        *,
        timeout: float,
        retries: int,
        min_interval_seconds: float,
    ) -> None:
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

    def _request_json(self, url: str) -> Dict[str, Any] | None:
        attempt = 0
        while True:
            attempt += 1
            self._wait_for_rate_limit()
            request = Request(url, headers=DEFAULT_HEADERS, method="GET")
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
                    wait = 3.0
                    retry_after = err.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        wait = float(retry_after)
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

    def lookup_by_doi(self, doi: str) -> Dict[str, Any] | None:
        """Lookup one paper by DOI."""
        paper_id = f"DOI:{doi}"
        url = f"{S2_BASE}/paper/{quote(paper_id, safe='')}?{urlencode({'fields': S2_FIELDS})}"
        return self._request_json(url)

    def search_by_title(self, title: str, limit: int = 3) -> Dict[str, Any] | None:
        """Search paper by title."""
        query = normalize_spaces(title)
        if not query:
            return None
        params = {"query": query, "limit": str(limit), "fields": S2_FIELDS}
        url = f"{S2_BASE}/paper/search?{urlencode(params)}"
        return self._request_json(url)


def parse_s2_result_by_title(local_title: str, payload: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """Pick best Semantic Scholar search result for local title."""
    if not payload or not isinstance(payload.get("data"), list):
        return None
    local_key = normalize_title_key(local_title)
    if not local_key:
        return None
    best_ratio = 0.0
    best_item: Dict[str, Any] | None = None
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        title = normalize_spaces(ensure_str(item.get("title")))
        ratio = 0.0
        candidate_key = normalize_title_key(title)
        if local_key and candidate_key:
            ratio = _simple_similarity(local_key, candidate_key)
        if ratio > best_ratio:
            best_ratio = ratio
            best_item = item
    if best_ratio >= 0.90:
        return best_item
    return None


def _simple_similarity(a: str, b: str) -> float:
    """Compute lightweight title similarity."""
    if not a or not b:
        return 0.0
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return 0.0
    inter = len(a_tokens & b_tokens)
    union = len(a_tokens | b_tokens)
    return inter / union if union else 0.0


def build_quality_flags(
    authors: Sequence[str],
    abstract: str,
    institutions: Sequence[str],
    keywords: Sequence[str],
) -> List[str]:
    """Build quality flag list."""
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
    source_year_count_estimate: int,
) -> Dict[str, Any]:
    """Build root_json payload."""
    year_short = year % 100
    return {
        "query": {
            "target": f"IJCAI-{year_short:02d}",
            "venue_code": "IJCAI",
            "year": year,
            "provider": "ijcai_official_openreview_s2",
            "api_key_used": False,
            "work_filter_strategy": f"official_ijcai_proceedings:year={year}",
            "source_year_count_estimate": source_year_count_estimate,
        },
        "source": {
            "provider": "ijcai_official_openreview_s2",
            "openalex_source_id": None,
            "openreview_venue_id": f"IJCAI {year}",
            "display_name": "International Joint Conference on Artificial Intelligence",
            "source_type": "conference",
            "official_url": IJCAI_PROCEEDINGS_URL_TEMPLATE.format(year=year),
        },
        "generated_at_utc": collected_at,
        "paper_count": len(papers),
        "track_counts": count_field(papers, key="track", default="main"),
        "track_group_counts": count_field(papers, key="track_group", default="main"),
        "presentation_level_counts": count_field(
            papers,
            key="presentation_level",
            default="poster",
        ),
        "papers": list(papers),
    }


def collect_one_year(
    *,
    year: int,
    dblp_tags: Sequence[str],
    output_root: Path,
    timeout: float,
    retries: int,
    min_interval: float,
    workers: int,
    use_openreview_supplement: bool,
    use_s2_supplement: bool,
    s2_min_interval: float,
) -> Dict[str, Any]:
    """Collect one IJCAI year and write root_json."""
    collected_at = utc_now_iso()
    proceedings_url = IJCAI_PROCEEDINGS_URL_TEMPLATE.format(year=year)

    listing_html = fetch_text(
        url=proceedings_url,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
    )
    official_entries = parse_official_listing(year=year, listing_html=listing_html)
    official_entry_count = len(official_entries)

    unique_official: Dict[int, Dict[str, Any]] = {}
    for item in official_entries:
        unique_official[int(item["paper_id"])] = item
    official_list = list(unique_official.values())
    official_unique_count = len(official_list)

    detail_failed_count = 0
    detail_done = 0
    detail_total = len(official_list)
    LOGGER.info("IJCAI %s detail fetch total=%s", year, detail_total)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                fetch_text,
                ensure_str(item.get("detail_url")),
                timeout,
                retries,
                min_interval,
            ): item
            for item in official_list
        }
        for future in as_completed(futures):
            item = futures[future]
            detail_done += 1
            detail: Dict[str, Any] | None = None
            try:
                detail_html = future.result()
                detail = parse_official_detail_page(detail_html=detail_html)
            except Exception as err:  # noqa: BLE001
                detail_failed_count += 1
                LOGGER.warning(
                    "IJCAI %s detail failed (paper=%s): %s",
                    year,
                    item.get("paper_id"),
                    err,
                )

            if detail:
                if detail.get("title"):
                    item["title"] = detail["title"]
                if detail.get("authors"):
                    item["authors"] = detail["authors"]
                if detail.get("doi"):
                    item["doi"] = detail["doi"]
                if detail.get("pdf_url"):
                    item["pdf_url"] = detail["pdf_url"]
                if detail.get("pages"):
                    item["pages"] = detail["pages"]
                item["abstract"] = normalize_spaces(ensure_str(detail.get("abstract")))
                item["keywords"] = list(detail.get("keywords", []))
            else:
                item["abstract"] = ""
                item["keywords"] = []

            if detail_done == detail_total or detail_done % 300 == 0:
                LOGGER.info(
                    "IJCAI %s detail progress: %s/%s (failed=%s)",
                    year,
                    detail_done,
                    detail_total,
                    detail_failed_count,
                )

    dblp_records_all: List[Dict[str, Any]] = []
    dblp_xml_urls: List[str] = []
    for tag in dblp_tags:
        xml_url = DBLP_IJCAI_XML_URL_TEMPLATE.format(tag=tag)
        dblp_xml_urls.append(xml_url)
        cache_path = Path(f"/tmp/dblp_ijcai_{tag}.xml")
        if cache_path.exists():
            xml_text = cache_path.read_text(encoding="utf-8", errors="ignore")
        else:
            xml_text = fetch_text(
                url=xml_url,
                timeout=timeout,
                retries=retries,
                min_interval=min_interval,
            )
            cache_path.write_text(xml_text, encoding="utf-8")
        dblp_records_all.extend(parse_dblp_xml_records(xml_text=xml_text, year=year, xml_url=xml_url))
    dblp_inproceedings_count = len(dblp_records_all)

    dblp_by_doi: Dict[str, Dict[str, Any]] = {}
    dblp_by_title: Dict[str, Dict[str, Any]] = {}
    for item in dblp_records_all:
        doi = normalize_doi(ensure_str(item.get("doi")))
        if doi:
            dblp_by_doi[doi] = item
        title_key = normalize_title_key(ensure_str(item.get("title")))
        if title_key:
            dblp_by_title[title_key] = item

    openreview_by_doi: Dict[str, Dict[str, Any]] = {}
    openreview_by_title: Dict[str, Dict[str, Any]] = {}
    openreview_count = 0
    if use_openreview_supplement:
        try:
            openreview_count = fetch_openreview_count(
                year=year,
                timeout=timeout,
                retries=retries,
                min_interval=min_interval,
            )

            need_openreview_records = any(
                not normalize_spaces(ensure_str(item.get("abstract")))
                for item in official_list
            )
            if need_openreview_records:
                openreview_by_doi, openreview_by_title = fetch_openreview_maps(
                    year=year,
                    timeout=timeout,
                    retries=retries,
                    min_interval=min_interval,
                )
        except Exception as err:  # noqa: BLE001
            LOGGER.warning("OpenReview supplement unavailable for IJCAI %s: %s", year, err)

    s2_client = SemanticScholarClient(
        timeout=timeout,
        retries=retries,
        min_interval_seconds=s2_min_interval,
    )

    papers: List[Dict[str, Any]] = []
    matched_dblp_count = 0
    openreview_enriched_count = 0
    s2_enriched_count = 0
    for item in official_list:
        paper_id = int(item.get("paper_id"))
        title = normalize_title(ensure_str(item.get("title")))
        title_key = normalize_title_key(title)
        doi = normalize_doi(ensure_str(item.get("doi")))
        authors = dedupe_preserve(item.get("authors", []))
        abstract = normalize_spaces(ensure_str(item.get("abstract")))
        keywords = dedupe_preserve(item.get("keywords", []))
        pages = normalize_spaces(ensure_str(item.get("pages")))
        detail_url = normalize_spaces(ensure_str(item.get("detail_url")))
        pdf_url = normalize_spaces(ensure_str(item.get("pdf_url")))
        section_title = normalize_spaces(ensure_str(item.get("section_title")))
        subsection_title = normalize_spaces(ensure_str(item.get("subsection_title")))

        dblp = None
        if doi and doi in dblp_by_doi:
            dblp = dblp_by_doi[doi]
        elif title_key and title_key in dblp_by_title:
            dblp = dblp_by_title[title_key]
        if dblp:
            matched_dblp_count += 1
            if not doi:
                doi = normalize_doi(ensure_str(dblp.get("doi")))
            if not authors:
                authors = dedupe_preserve(dblp.get("authors", []))
            if not pages:
                pages = normalize_spaces(ensure_str(dblp.get("pages")))

        openreview = None
        if doi and doi in openreview_by_doi:
            openreview = openreview_by_doi[doi]
        elif title_key and title_key in openreview_by_title:
            openreview = openreview_by_title[title_key]
        if openreview:
            patched = False
            if not abstract:
                abstract = normalize_spaces(ensure_str(openreview.get("abstract")))
                patched = bool(abstract)
            if not authors:
                authors = dedupe_preserve(openreview.get("authors", []))
            if not doi:
                doi = normalize_doi(ensure_str(openreview.get("doi")))
            if not pdf_url:
                pdf_url = normalize_spaces(ensure_str(openreview.get("pdf_url")))
            if patched:
                openreview_enriched_count += 1

        source_ids: Dict[str, str] = {
            "ijcai_paper_id": str(paper_id),
            "ijcai_detail_url": detail_url,
        }
        if pdf_url:
            source_ids["ijcai_pdf_url"] = pdf_url
        if section_title:
            source_ids["ijcai_section"] = section_title
        if subsection_title:
            source_ids["ijcai_subsection"] = subsection_title

        if dblp:
            if ensure_str(dblp.get("dblp_key")):
                source_ids["dblp_key"] = ensure_str(dblp.get("dblp_key"))
            if ensure_str(dblp.get("rec_url")):
                source_ids["dblp_rec_url"] = ensure_str(dblp.get("rec_url"))
            if ensure_str(dblp.get("xml_url")):
                source_ids["dblp_xml_url"] = ensure_str(dblp.get("xml_url"))
            if ensure_str(dblp.get("pages")) and "dblp_pages" not in source_ids:
                source_ids["dblp_pages"] = ensure_str(dblp.get("pages"))

        if openreview:
            if ensure_str(openreview.get("note_id")):
                source_ids["openreview_note_id"] = ensure_str(openreview.get("note_id"))
            if ensure_str(openreview.get("forum_id")):
                source_ids["openreview_forum_id"] = ensure_str(openreview.get("forum_id"))

        citation_count: int | None = None
        if use_s2_supplement and not abstract:
            s2_data: Dict[str, Any] | None = None
            if doi:
                s2_data = s2_client.lookup_by_doi(doi)
            if not s2_data:
                s2_search = s2_client.search_by_title(title)
                s2_data = parse_s2_result_by_title(local_title=title, payload=s2_search)
            if s2_data:
                s2_abstract = normalize_spaces(ensure_str(s2_data.get("abstract")))
                if s2_abstract and not abstract:
                    abstract = s2_abstract
                    s2_enriched_count += 1
                s2_paper_id = normalize_spaces(ensure_str(s2_data.get("paperId")))
                if s2_paper_id:
                    source_ids["semantic_scholar_paper_id"] = s2_paper_id
                external_ids = s2_data.get("externalIds")
                if isinstance(external_ids, dict):
                    s2_doi = normalize_doi(ensure_str(external_ids.get("DOI")))
                    if s2_doi and not doi:
                        doi = s2_doi
                c_count = s2_data.get("citationCount")
                if isinstance(c_count, int):
                    citation_count = c_count

        if doi:
            source_ids["doi"] = doi

        track_display_name = subsection_title or section_title or "Main"
        track_group = "main" if section_title.lower().startswith("main track") else slugify_track(
            section_title,
            default="main",
        )
        track = "main" if track_group == "main" else slugify_track(track_display_name, default=track_group)
        institutions: List[str] = []
        quality_flags = build_quality_flags(
            authors=authors,
            abstract=abstract,
            institutions=institutions,
            keywords=keywords,
        )

        external_url = f"https://doi.org/{doi}" if doi else (pdf_url or None)
        if detail_url:
            url = detail_url
        elif pdf_url:
            url = pdf_url
        else:
            url = None

        papers.append(
            {
                "paper_title": title,
                "authors": authors,
                "institutions": institutions,
                "abstract": abstract,
                "keywords": keywords,
                "presentation_level": "poster",
                "openalex_id": None,
                "doi": doi,
                "track": track,
                "track_display_name": track_display_name or "Main",
                "track_group": track_group,
                "title": title,
                "url": url,
                "external_url": external_url,
                "citation_count": citation_count,
                "venue": "IJCAI",
                "year": year,
                "source_provider": "ijcai_official_openreview_s2",
                "collected_at": collected_at,
                "source_ids": source_ids,
                "record_status": (
                    "placeholder"
                    if ("missing_authors" in quality_flags or "missing_abstract" in quality_flags)
                    else "resolved"
                ),
                "quality_flags": quality_flags,
            }
        )

    papers.sort(key=lambda paper: normalize_title_key(ensure_str(paper.get("title"))))
    missing_abstract_count = sum(
        1 for paper in papers if "missing_abstract" in (paper.get("quality_flags") or [])
    )
    payload = build_payload(
        year=year,
        papers=papers,
        collected_at=collected_at,
        source_year_count_estimate=official_unique_count,
    )

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"IJCAI-{year % 100:02d}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    LOGGER.info(
        "IJCAI %s collected: official=%s dblp=%s collected=%s missing_abstract=%s",
        year,
        official_unique_count,
        dblp_inproceedings_count,
        len(papers),
        missing_abstract_count,
    )
    return {
        "year": year,
        "official_listing_url": proceedings_url,
        "official_entry_count": official_entry_count,
        "official_unique_count": official_unique_count,
        "dblp_tags": list(dblp_tags),
        "dblp_xml_urls": dblp_xml_urls,
        "dblp_inproceedings_count": dblp_inproceedings_count,
        "official_minus_dblp_delta": official_unique_count - dblp_inproceedings_count,
        "openreview_count": openreview_count,
        "collected_paper_count": len(papers),
        "detail_fetch_failed_count": detail_failed_count,
        "matched_dblp_count": matched_dblp_count,
        "openreview_enriched_count": openreview_enriched_count,
        "s2_enriched_count": s2_enriched_count,
        "missing_abstract_count": missing_abstract_count,
        "output_file": str(output_path),
        "generated_at_utc": collected_at,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(
        description="Collect IJCAI papers from ijcai.org with DBLP/OpenReview/S2 supplements"
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
        default=20,
        help="Parallel workers for IJCAI detail page fetching (default: 20)",
    )
    parser.add_argument(
        "--no-openreview-supplement",
        action="store_true",
        help="Disable OpenReview supplement for missing fields",
    )
    parser.add_argument(
        "--no-semantic-scholar-supplement",
        action="store_true",
        help="Disable Semantic Scholar supplement for missing abstracts",
    )
    parser.add_argument(
        "--s2-min-interval",
        type=float,
        default=2.0,
        help="Minimum interval between Semantic Scholar requests (default: 2.0)",
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

    dblp_year_tags = resolve_dblp_year_tags(
        years=years,
        timeout=args.timeout,
        retries=args.retries,
        min_interval=args.min_interval,
    )

    summary: List[Dict[str, Any]] = []
    for year in years:
        tags = dblp_year_tags.get(year, [str(year)])
        LOGGER.info("Collecting IJCAI %s with DBLP tags: %s", year, tags)
        summary.append(
            collect_one_year(
                year=year,
                dblp_tags=tags,
                output_root=output_root,
                timeout=args.timeout,
                retries=args.retries,
                min_interval=args.min_interval,
                workers=args.workers,
                use_openreview_supplement=not args.no_openreview_supplement,
                use_s2_supplement=not args.no_semantic_scholar_supplement,
                s2_min_interval=args.s2_min_interval,
            )
        )

    total_official_unique = sum(int(item["official_unique_count"]) for item in summary)
    total_dblp_inproceedings = sum(int(item["dblp_inproceedings_count"]) for item in summary)
    total_collected = sum(int(item["collected_paper_count"]) for item in summary)
    total_missing_abstract = sum(int(item["missing_abstract_count"]) for item in summary)

    report = {
        "generated_at_utc": utc_now_iso(),
        "provider": "ijcai_official_openreview_s2",
        "venue": "IJCAI",
        "years": years,
        "total_official_unique": total_official_unique,
        "total_dblp_inproceedings": total_dblp_inproceedings,
        "official_minus_dblp_delta": total_official_unique - total_dblp_inproceedings,
        "total_collected": total_collected,
        "official_vs_collected_aligned": total_official_unique == total_collected,
        "total_missing_abstract": total_missing_abstract,
        "items": summary,
    }
    report_path = index_root / "ijcai_collection_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Collection report written: %s", report_path)
    LOGGER.info("Total official unique papers: %s", total_official_unique)
    LOGGER.info("Total DBLP inproceedings: %s", total_dblp_inproceedings)
    LOGGER.info("Total collected papers: %s", total_collected)
    LOGGER.info("Total missing abstracts: %s", total_missing_abstract)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
