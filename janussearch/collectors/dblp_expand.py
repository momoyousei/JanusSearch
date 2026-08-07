#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect selected venues from DBLP with OpenAlex/Crossref abstract fallback."""

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
from http.client import IncompleteRead
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

LOGGER = logging.getLogger("dblp_expand_collect")

DBLP_BASE_URLS = ("https://dblp.uni-trier.de", "https://dblp.org")
DBLP_REC_BASE = "https://dblp.org/rec/"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
CROSSREF_WORKS_URL = "https://api.crossref.org/works/"

DEFAULT_OUTPUT_ROOT = Path("archives/root_json")
DEFAULT_INDEX_ROOT = Path("artifacts")

DEFAULT_HEADERS = {
    "User-Agent": "JanusSearch/1.0 (mailto:janus@example.com)",
    "Accept": "application/json,text/html,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "close",
}

TAG_RE = re.compile(r"<[^>]+>")

VENUE_CONFIG: Dict[str, Dict[str, Any]] = {
    "ICDE": {
        "mode": "conf",
        "series": "icde",
        "display_name": "IEEE International Conference on Data Engineering",
        "source_type": "conference",
        "presentation_level": "poster",
        "track_display_name": "Main",
    },
    "VLDB": {
        "mode": "pvldb",
        "series": "pvldb",
        "display_name": "International Conference on Very Large Data Bases",
        "source_type": "conference",
        "presentation_level": "poster",
        "track_display_name": "Main",
        "volume_year_offset": 2007,  # year 2021 -> volume 14
    },
    "SIGIR": {
        "mode": "conf",
        "series": "sigir",
        "display_name": "International ACM SIGIR Conference on Research and Development in Information Retrieval",
        "source_type": "conference",
        "presentation_level": "poster",
        "track_display_name": "Main",
    },
    "ACMMM": {
        "mode": "conf",
        "series": "mm",
        "display_name": "ACM Multimedia",
        "source_type": "conference",
        "presentation_level": "poster",
        "track_display_name": "Main",
    },
    "WWW": {
        "mode": "conf",
        "series": "www",
        "display_name": "The Web Conference",
        "source_type": "conference",
        "presentation_level": "poster",
        "track_display_name": "Main",
    },
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


def parse_venues(raw: str) -> List[str]:
    """Parse venue list from comma-separated string."""
    names = [normalize_spaces(item).upper() for item in ensure_str(raw).split(",")]
    venues = [name for name in names if name]
    if not venues:
        raise ValueError("Venues cannot be empty")
    invalid = [name for name in venues if name not in VENUE_CONFIG]
    if invalid:
        raise ValueError(f"Unsupported venues: {invalid}; allowed={sorted(VENUE_CONFIG)}")
    return venues


def build_dblp_url_candidates(path: str) -> List[str]:
    """Build DBLP URL candidates from path."""
    normalized = "/" + ensure_str(path).lstrip("/")
    return [f"{base}{normalized}" for base in DBLP_BASE_URLS]


def fetch_text_from_candidates(
    *,
    urls: Sequence[str],
    timeout: float,
    retries: int,
    min_interval: float,
) -> Tuple[str, str]:
    """Fetch text from candidate URLs with retries; returns (payload, used_url)."""
    if not urls:
        raise ValueError("No URL candidates provided")
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        for url in urls:
            request = Request(url, headers=DEFAULT_HEADERS)
            try:
                with urlopen(request, timeout=timeout) as response:
                    payload = response.read().decode("utf-8", "ignore")
                    return payload, url
            except (HTTPError, URLError, TimeoutError, socket.timeout, ConnectionError, OSError, IncompleteRead) as err:
                last_err = err
                LOGGER.warning(
                    "Fetch failed (%s/%s) %s: %s",
                    attempt,
                    retries,
                    url,
                    err,
                )
        if attempt < retries and min_interval > 0:
            time.sleep(min_interval)
    raise RuntimeError(f"Failed to fetch {urls} after {retries} attempts: {last_err}")


def fetch_text(url: str, timeout: float, retries: int, min_interval: float) -> str:
    """Fetch one URL text with retry."""
    payload, _ = fetch_text_from_candidates(
        urls=[url],
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
    )
    return payload


def fetch_json(url: str, timeout: float, retries: int, min_interval: float) -> Dict[str, Any]:
    """Fetch JSON object from URL."""
    payload = fetch_text(url=url, timeout=timeout, retries=retries, min_interval=min_interval)
    try:
        result = json.loads(payload)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"Invalid JSON from {url}: {err}") from err
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected JSON type from {url}: {type(result)}")
    return result


def normalize_title(value: str) -> str:
    """Normalize title content from DBLP XML."""
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


def extract_doi_from_ee(ee_values: Sequence[str]) -> str | None:
    """Extract DOI from DBLP <ee> links."""
    for value in ee_values:
        text = ensure_str(value)
        if not text:
            continue
        match = re.search(r"doi\.org/(10\.[^/?#\s]+(?:/[^\s?#]+)?)", text, flags=re.I)
        if match:
            return normalize_doi(match.group(1))
        if text.lower().startswith("10."):
            return normalize_doi(text)
    return None


def element_text(element: ET.Element | None) -> str:
    """Return flattened text from an XML element."""
    if element is None:
        return ""
    return normalize_spaces("".join(element.itertext()))


def resolve_conf_year_tags(
    *,
    series: str,
    year: int,
    timeout: float,
    retries: int,
    min_interval: float,
) -> Tuple[List[str], str]:
    """Resolve DBLP conf tags for one year, e.g. ['2025'] or ['2025-1','2025-2']."""
    index_path = f"/db/conf/{series}/"
    index_html, used_index_url = fetch_text_from_candidates(
        urls=build_dblp_url_candidates(index_path),
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
    )
    all_tags = set(
        re.findall(
            rf"{re.escape(series)}(20\d{{2}}(?:-\d+)?)\.html",
            index_html,
        )
    )
    year_text = str(year)
    tags = [tag for tag in all_tags if tag == year_text or tag.startswith(f"{year_text}-")]
    if not tags:
        tags = [year_text]

    def sort_key(tag: str) -> Tuple[int, int]:
        if "-" not in tag:
            return (0, 0)
        suffix = ensure_str(tag.split("-", maxsplit=1)[1])
        if suffix.isdigit():
            return (1, int(suffix))
        return (1, 999999)

    return sorted(tags, key=sort_key), used_index_url


def resolve_year_inputs(
    *,
    venue: str,
    config: Dict[str, Any],
    year: int,
    timeout: float,
    retries: int,
    min_interval: float,
) -> Tuple[List[str], str, str]:
    """Resolve xml paths and official URL for one venue-year."""
    mode = ensure_str(config.get("mode"))
    if mode == "conf":
        series = ensure_str(config.get("series"))
        tags, used_index_url = resolve_conf_year_tags(
            series=series,
            year=year,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
        )
        xml_paths = [f"/db/conf/{series}/{series}{tag}.xml" for tag in tags]
        filter_strategy = f"official_dblp_stream:conf/{series} year={year} tags={tags}"
        return xml_paths, used_index_url, filter_strategy

    if mode == "pvldb":
        offset = int(config.get("volume_year_offset", 2007))
        volume = year - offset
        if volume <= 0:
            raise ValueError(f"Invalid VLDB year-volume mapping: year={year}, offset={offset}")
        xml_paths = [f"/db/journals/pvldb/pvldb{volume}.xml"]
        official_url = f"{DBLP_BASE_URLS[0]}/db/journals/pvldb/pvldb{volume}.html"
        filter_strategy = f"official_dblp_stream:journals/pvldb volume={volume} year={year}"
        return xml_paths, official_url, filter_strategy

    raise ValueError(f"Unsupported mode={mode} for venue={venue}")


def parse_dblp_xml_records(
    *,
    xml_text: str,
    xml_url: str,
    venue: str,
    year: int,
    mode: str,
) -> List[Dict[str, Any]]:
    """Parse records from one DBLP XML payload."""
    root = ET.fromstring(xml_text)
    node_tag = "inproceedings" if mode == "conf" else "article"
    records: List[Dict[str, Any]] = []
    for node in root.findall(f".//{node_tag}"):
        key = ensure_str(node.attrib.get("key"))
        title = normalize_title(element_text(node.find("title")))
        entry_year_text = element_text(node.find("year"))
        entry_year = int(entry_year_text) if entry_year_text.isdigit() else None
        if entry_year is not None and entry_year != year:
            continue
        authors = [element_text(author) for author in node.findall("author")]
        authors = [author for author in authors if author]
        ee_values = [ensure_str(ee.text) for ee in node.findall("ee") if ensure_str(ee.text)]
        doi = extract_doi_from_ee(ee_values)
        pages = element_text(node.find("pages"))
        issue = element_text(node.find("number"))
        entry_volume = element_text(node.find("volume"))
        journal = element_text(node.find("journal"))
        booktitle = element_text(node.find("booktitle"))

        if not title:
            continue
        rec_url = f"{DBLP_REC_BASE}{key}" if key else ""
        records.append(
            {
                "dblp_key": key,
                "title": title,
                "authors": authors,
                "doi": doi,
                "pages": pages,
                "issue": issue,
                "entry_volume": entry_volume,
                "journal": journal,
                "booktitle": booktitle,
                "ee_values": ee_values,
                "xml_url": xml_url,
                "rec_url": rec_url,
                "year": year,
                "venue": venue,
            }
        )
    return records


def dedupe_records(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate records by DOI or normalized title."""
    unique: Dict[str, Dict[str, Any]] = {}
    for record in records:
        doi = ensure_str(record.get("doi")).lower()
        if doi:
            key = f"doi::{doi}"
        else:
            title = normalize_spaces(ensure_str(record.get("title"))).lower()
            key = f"title::{title}"
        unique[key] = record
    return list(unique.values())


def decode_openalex_abstract(inv_index: Dict[str, Any] | None) -> str:
    """Convert OpenAlex inverted index to plain text."""
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
            tokens[pos] = ensure_str(token)
    return normalize_spaces(" ".join(token for token in tokens if token))


def parse_openalex_work(work: Dict[str, Any]) -> Dict[str, Any]:
    """Parse one OpenAlex work object."""
    institutions: List[str] = []
    authorships = work.get("authorships")
    if isinstance(authorships, list):
        for authorship in authorships:
            if not isinstance(authorship, dict):
                continue
            ins_list = authorship.get("institutions")
            if not isinstance(ins_list, list):
                continue
            for ins in ins_list:
                if not isinstance(ins, dict):
                    continue
                name = normalize_spaces(ensure_str(ins.get("display_name")))
                if name and name not in institutions:
                    institutions.append(name)

    landing_page_url = ""
    primary_location = work.get("primary_location")
    if isinstance(primary_location, dict):
        landing_page_url = normalize_spaces(ensure_str(primary_location.get("landing_page_url")))

    concepts = work.get("concepts")
    keywords: List[str] = []
    if isinstance(concepts, list):
        for concept in concepts:
            if not isinstance(concept, dict):
                continue
            score = concept.get("score")
            if isinstance(score, (int, float)) and score < 0.35:
                continue
            name = normalize_spaces(ensure_str(concept.get("display_name")))
            if name and name not in keywords:
                keywords.append(name)
            if len(keywords) >= 8:
                break

    return {
        "openalex_id": normalize_spaces(ensure_str(work.get("id"))),
        "doi": normalize_doi(ensure_str(work.get("doi"))),
        "abstract": decode_openalex_abstract(work.get("abstract_inverted_index")),
        "institutions": institutions,
        "landing_page_url": landing_page_url,
        "cited_by_count": work.get("cited_by_count"),
        "keywords": keywords,
    }


def fetch_openalex_chunk(
    *,
    dois: Sequence[str],
    timeout: float,
    retries: int,
    min_interval: float,
) -> Dict[str, Dict[str, Any]]:
    """Fetch OpenAlex metadata for one DOI chunk."""
    if not dois:
        return {}
    expression = "|".join(f"https://doi.org/{doi}" for doi in dois)
    query = quote(expression, safe="|:/")
    url = f"{OPENALEX_WORKS_URL}?filter=doi:{query}&per-page=200"
    payload = fetch_json(url=url, timeout=timeout, retries=retries, min_interval=min_interval)
    results = payload.get("results")
    if not isinstance(results, list):
        return {}
    mapping: Dict[str, Dict[str, Any]] = {}
    for work in results:
        if not isinstance(work, dict):
            continue
        parsed = parse_openalex_work(work)
        doi = ensure_str(parsed.get("doi")).lower()
        if not doi:
            continue
        mapping[doi] = parsed
    return mapping


def chunked(items: Sequence[str], size: int) -> Iterable[List[str]]:
    """Yield list chunks."""
    chunk_size = max(1, int(size))
    for idx in range(0, len(items), chunk_size):
        yield list(items[idx : idx + chunk_size])


def fetch_openalex_map(
    *,
    dois: Sequence[str],
    timeout: float,
    retries: int,
    min_interval: float,
    chunk_size: int,
    workers: int,
) -> Dict[str, Dict[str, Any]]:
    """Fetch OpenAlex metadata map for DOI list."""
    unique_dois = sorted({normalize_doi(doi) for doi in dois if normalize_doi(doi)})
    if not unique_dois:
        return {}
    mapping: Dict[str, Dict[str, Any]] = {}
    chunks = list(chunked(unique_dois, chunk_size))
    worker_count = max(1, workers)
    LOGGER.info("OpenAlex DOI batches: %s chunks (size=%s)", len(chunks), chunk_size)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                fetch_openalex_chunk,
                dois=chunk,
                timeout=timeout,
                retries=retries,
                min_interval=min_interval,
            ): chunk
            for chunk in chunks
        }
        done = 0
        for future in as_completed(futures):
            done += 1
            try:
                mapping.update(future.result())
            except Exception:
                pass
            if done == len(chunks) or done % 20 == 0:
                LOGGER.info("OpenAlex batch progress: %s/%s", done, len(chunks))
    return mapping


def parse_crossref_abstract(raw_abstract: str) -> str:
    """Parse Crossref abstract (may contain JATS tags)."""
    text = ensure_str(raw_abstract)
    if not text:
        return ""
    cleaned = html.unescape(TAG_RE.sub(" ", text))
    return normalize_spaces(cleaned)


def fetch_crossref_abstract(
    *,
    doi: str,
    timeout: float,
    retries: int,
    min_interval: float,
) -> str:
    """Fetch abstract for one DOI from Crossref."""
    normalized = normalize_doi(doi)
    if not normalized:
        return ""
    url = f"{CROSSREF_WORKS_URL}{quote(normalized, safe='')}"
    try:
        payload = fetch_json(url=url, timeout=timeout, retries=retries, min_interval=min_interval)
    except Exception:
        return ""
    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    abstract = parse_crossref_abstract(ensure_str(message.get("abstract")))
    return abstract


def fetch_crossref_map(
    *,
    dois: Sequence[str],
    timeout: float,
    retries: int,
    min_interval: float,
    workers: int,
) -> Dict[str, str]:
    """Fetch Crossref abstracts for DOI list."""
    unique_dois = sorted({normalize_doi(doi) for doi in dois if normalize_doi(doi)})
    if not unique_dois:
        return {}
    result: Dict[str, str] = {}
    worker_count = max(1, workers)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                fetch_crossref_abstract,
                doi=doi,
                timeout=timeout,
                retries=retries,
                min_interval=min_interval,
            ): doi
            for doi in unique_dois
        }
        done = 0
        for future in as_completed(futures):
            done += 1
            doi = futures[future]
            try:
                raw = future.result()
            except Exception:
                raw = ""
            abstract = normalize_spaces(ensure_str(raw))
            if abstract:
                normalized = normalize_doi(doi)
                if normalized:
                    result[normalized] = abstract
            if done == len(unique_dois) or done % 25 == 0:
                LOGGER.info("Crossref abstract progress: %s/%s", done, len(unique_dois))
    return result


def build_quality_flags(
    *,
    authors: Sequence[str],
    abstract: str,
    institutions: Sequence[str],
    keywords: Sequence[str],
    openalex_miss: bool,
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
    if openalex_miss:
        flags.append("missing_openalex_match")
    return flags


def build_paper_record(
    *,
    record: Dict[str, Any],
    venue: str,
    config: Dict[str, Any],
    year: int,
    collected_at: str,
    openalex: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """Build one root_json paper record."""
    doi = normalize_doi(ensure_str(record.get("doi")))
    title = normalize_spaces(ensure_str(record.get("title")))
    authors = [normalize_spaces(ensure_str(name)) for name in record.get("authors", []) if normalize_spaces(ensure_str(name))]
    institutions = list(openalex.get("institutions", [])) if openalex else []
    abstract = normalize_spaces(ensure_str(openalex.get("abstract"))) if openalex else ""
    keywords = list(openalex.get("keywords", [])) if openalex else []
    citation_count = openalex.get("cited_by_count") if openalex else None
    landing_page_url = normalize_spaces(ensure_str(openalex.get("landing_page_url"))) if openalex else ""
    openalex_id = normalize_spaces(ensure_str(openalex.get("openalex_id"))) if openalex else ""

    url = landing_page_url or ensure_str(record.get("rec_url"))
    external_url = f"https://doi.org/{doi}" if doi else (record.get("ee_values") or [None])[0]
    openalex_miss = openalex is None and doi is not None
    quality_flags = build_quality_flags(
        authors=authors,
        abstract=abstract,
        institutions=institutions,
        keywords=keywords,
        openalex_miss=openalex_miss,
    )

    source_ids: Dict[str, str] = {}
    if ensure_str(record.get("dblp_key")):
        source_ids["dblp_key"] = ensure_str(record.get("dblp_key"))
    if ensure_str(record.get("rec_url")):
        source_ids["dblp_rec_url"] = ensure_str(record.get("rec_url"))
    if ensure_str(record.get("xml_url")):
        source_ids["dblp_xml_url"] = ensure_str(record.get("xml_url"))
    if ensure_str(record.get("pages")):
        source_ids["dblp_pages"] = ensure_str(record.get("pages"))
    if ensure_str(record.get("issue")):
        source_ids["dblp_issue"] = ensure_str(record.get("issue"))
    if ensure_str(record.get("entry_volume")):
        source_ids["dblp_volume"] = ensure_str(record.get("entry_volume"))
    if doi:
        source_ids["doi"] = doi
    if openalex_id:
        source_ids["openalex_id"] = openalex_id

    presentation_level = ensure_str(config.get("presentation_level")) or "poster"
    track_display_name = ensure_str(config.get("track_display_name")) or "Main"
    return {
        "paper_title": title,
        "authors": authors,
        "institutions": institutions,
        "abstract": abstract,
        "keywords": keywords,
        "presentation_level": presentation_level,
        "openalex_id": openalex_id or None,
        "doi": doi,
        "track": "main",
        "track_display_name": track_display_name,
        "track_group": "main",
        "title": title,
        "url": url or None,
        "external_url": ensure_str(external_url) or None,
        "citation_count": citation_count if isinstance(citation_count, int) else None,
        "venue": venue,
        "year": year,
        "source_provider": "dblp_openalex",
        "collected_at": collected_at,
        "source_ids": source_ids,
        "record_status": "placeholder" if ("missing_authors" in quality_flags or "missing_abstract" in quality_flags) else "resolved",
        "quality_flags": quality_flags,
    }


def patch_missing_abstracts_with_crossref(
    *,
    papers: List[Dict[str, Any]],
    timeout: float,
    retries: int,
    min_interval: float,
    workers: int,
) -> int:
    """Patch missing abstracts using Crossref fallback."""
    targets: List[str] = []
    for paper in papers:
        flags = paper.get("quality_flags", [])
        doi = normalize_doi(ensure_str(paper.get("doi")))
        if "missing_abstract" in flags and doi:
            targets.append(doi)
    if not targets:
        return 0

    LOGGER.info("Crossref fallback candidates: %s", len(targets))
    crossref_map = fetch_crossref_map(
        dois=targets,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
        workers=workers,
    )

    patched = 0
    for paper in papers:
        flags = [ensure_str(flag) for flag in paper.get("quality_flags", []) if ensure_str(flag)]
        if "missing_abstract" not in flags:
            continue
        doi = normalize_doi(ensure_str(paper.get("doi")))
        abstract = normalize_spaces(ensure_str(crossref_map.get(doi)))
        if not abstract:
            continue
        paper["abstract"] = abstract
        flags = [flag for flag in flags if flag != "missing_abstract"]
        paper["quality_flags"] = flags
        if paper.get("record_status") == "placeholder" and "missing_authors" not in flags:
            paper["record_status"] = "resolved"
        patched += 1
    return patched


def count_field(items: Sequence[Dict[str, Any]], key: str, default: str) -> Dict[str, int]:
    """Count categorical field values."""
    counts: Dict[str, int] = {}
    for item in items:
        value = normalize_spaces(ensure_str(item.get(key))) or default
        counts[value] = counts.get(value, 0) + 1
    return counts


def build_payload(
    *,
    venue: str,
    config: Dict[str, Any],
    year: int,
    papers: Sequence[Dict[str, Any]],
    collected_at: str,
    source_year_count_estimate: int,
    official_url: str,
    work_filter_strategy: str,
) -> Dict[str, Any]:
    """Build root_json payload for one venue-year."""
    year_short = year % 100
    return {
        "query": {
            "target": f"{venue}-{year_short:02d}",
            "venue_code": venue,
            "year": year,
            "provider": "dblp_openalex",
            "api_key_used": False,
            "work_filter_strategy": work_filter_strategy,
            "source_year_count_estimate": source_year_count_estimate,
        },
        "source": {
            "provider": "dblp_openalex",
            "openalex_source_id": None,
            "openreview_venue_id": None,
            "display_name": ensure_str(config.get("display_name")),
            "source_type": ensure_str(config.get("source_type")) or "conference",
            "official_url": official_url,
        },
        "generated_at_utc": collected_at,
        "paper_count": len(papers),
        "track_counts": count_field(papers, key="track", default="main"),
        "track_group_counts": count_field(papers, key="track_group", default="main"),
        "presentation_level_counts": count_field(
            papers,
            key="presentation_level",
            default=ensure_str(config.get("presentation_level")) or "poster",
        ),
        "papers": list(papers),
    }


def collect_one_year(
    *,
    venue: str,
    config: Dict[str, Any],
    year: int,
    output_root: Path,
    timeout: float,
    retries: int,
    min_interval: float,
    openalex_chunk_size: int,
    openalex_workers: int,
    crossref_workers: int,
) -> Dict[str, Any]:
    """Collect one venue-year and write root_json."""
    collected_at = utc_now_iso()
    xml_paths, official_url, filter_strategy = resolve_year_inputs(
        venue=venue,
        config=config,
        year=year,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
    )
    mode = ensure_str(config.get("mode"))
    all_records: List[Dict[str, Any]] = []
    used_xml_urls: List[str] = []
    for xml_path in xml_paths:
        xml_urls = build_dblp_url_candidates(xml_path)
        LOGGER.info("%s %s loading DBLP XML candidates: %s", venue, year, xml_urls)
        xml_text, used_xml_url = fetch_text_from_candidates(
            urls=xml_urls,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
        )
        part_records = parse_dblp_xml_records(
            xml_text=xml_text,
            xml_url=used_xml_url,
            venue=venue,
            year=year,
            mode=mode,
        )
        LOGGER.info("%s %s parsed records from %s: %s", venue, year, used_xml_url, len(part_records))
        all_records.extend(part_records)
        used_xml_urls.append(used_xml_url)

    official_entry_count = len(all_records)
    unique_records = dedupe_records(all_records)
    official_unique_count = len(unique_records)

    doi_list = [ensure_str(record.get("doi")) for record in unique_records if ensure_str(record.get("doi"))]
    openalex_map = fetch_openalex_map(
        dois=doi_list,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
        chunk_size=openalex_chunk_size,
        workers=openalex_workers,
    )

    papers: List[Dict[str, Any]] = []
    missing_openalex_count = 0
    for record in unique_records:
        doi = normalize_doi(ensure_str(record.get("doi")))
        openalex = openalex_map.get(doi, None) if doi else None
        if doi and openalex is None:
            missing_openalex_count += 1
        papers.append(
            build_paper_record(
                record=record,
                venue=venue,
                config=config,
                year=year,
                collected_at=collected_at,
                openalex=openalex,
            )
        )

    crossref_patched_count = patch_missing_abstracts_with_crossref(
        papers=papers,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
        workers=crossref_workers,
    )
    missing_abstract_count = sum(
        1 for paper in papers if "missing_abstract" in (paper.get("quality_flags") or [])
    )

    papers.sort(key=lambda item: normalize_spaces(ensure_str(item.get("title"))).lower())
    payload = build_payload(
        venue=venue,
        config=config,
        year=year,
        papers=papers,
        collected_at=collected_at,
        source_year_count_estimate=official_unique_count,
        official_url=official_url,
        work_filter_strategy=filter_strategy,
    )

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"{venue}-{year % 100:02d}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    LOGGER.info(
        "%s %s collected: official_entries=%s unique=%s collected=%s missing_openalex=%s missing_abstract=%s",
        venue,
        year,
        official_entry_count,
        official_unique_count,
        len(papers),
        missing_openalex_count,
        missing_abstract_count,
    )
    return {
        "venue": venue,
        "year": year,
        "dblp_xml_urls": used_xml_urls,
        "official_url": official_url,
        "official_entry_count": official_entry_count,
        "official_unique_count": official_unique_count,
        "collected_paper_count": len(papers),
        "missing_vs_official_unique": max(0, official_unique_count - len(papers)),
        "missing_openalex_count": missing_openalex_count,
        "missing_abstract_count": missing_abstract_count,
        "crossref_patched_count": crossref_patched_count,
        "output_file": str(output_path),
        "generated_at_utc": collected_at,
    }


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="Collect multiple DBLP venues into root_json files")
    parser.add_argument(
        "--venues",
        default="ICDE,VLDB,SIGIR,ACMMM,WWW",
        help="Comma-separated venue codes, e.g. ICDE,VLDB,SIGIR,ACMMM,WWW",
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
    parser.add_argument("--retries", type=int, default=6, help="HTTP retry times")
    parser.add_argument(
        "--min-interval",
        type=float,
        default=0.6,
        help="Sleep seconds between retries",
    )
    parser.add_argument(
        "--openalex-chunk-size",
        type=int,
        default=20,
        help="OpenAlex DOI batch size (default: 20)",
    )
    parser.add_argument(
        "--openalex-workers",
        type=int,
        default=8,
        help="Parallel workers for OpenAlex batches (default: 8)",
    )
    parser.add_argument(
        "--crossref-workers",
        type=int,
        default=6,
        help="Parallel workers for Crossref abstract fallback (default: 6)",
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

    venues = parse_venues(args.venues)
    years = parse_years(args.years)
    output_root = Path(args.output_root)
    index_root = Path(args.index_root)
    collections_root = index_root / "collections"
    collections_root.mkdir(parents=True, exist_ok=True)

    combined_items: List[Dict[str, Any]] = []
    per_venue_reports: Dict[str, Dict[str, Any]] = {}
    for venue in venues:
        config = VENUE_CONFIG[venue]
        items: List[Dict[str, Any]] = []
        for year in years:
            if venue == "ICDE" and year == 2026:
                from janussearch.collectors.icde import collect_2026

                items.append(
                    collect_2026(
                        output_root=output_root,
                        timeout=args.timeout,
                        retries=args.retries,
                    )
                )
                continue
            items.append(
                collect_one_year(
                    venue=venue,
                    config=config,
                    year=year,
                    output_root=output_root,
                    timeout=args.timeout,
                    retries=args.retries,
                    min_interval=args.min_interval,
                    openalex_chunk_size=args.openalex_chunk_size,
                    openalex_workers=args.openalex_workers,
                    crossref_workers=args.crossref_workers,
                )
            )
        total_official_unique = sum(int(item["official_unique_count"]) for item in items)
        total_collected = sum(int(item["collected_paper_count"]) for item in items)
        venue_report = {
            "generated_at_utc": utc_now_iso(),
            "provider": "dblp_openalex",
            "venue": venue,
            "years": years,
            "total_official_unique": total_official_unique,
            "total_collected": total_collected,
            "official_vs_collected_aligned": total_official_unique == total_collected,
            "items": items,
        }
        venue_report_path = collections_root / f"{venue.lower()}_collection_report.json"
        venue_report_path.write_text(json.dumps(venue_report, ensure_ascii=False, indent=2), encoding="utf-8")
        LOGGER.info("Venue report written: %s", venue_report_path)
        per_venue_reports[venue] = venue_report
        combined_items.extend(items)

    combined_report = {
        "generated_at_utc": utc_now_iso(),
        "provider": "dblp_openalex",
        "venues": venues,
        "years": years,
        "total_official_unique": sum(int(item["official_unique_count"]) for item in combined_items),
        "total_collected": sum(int(item["collected_paper_count"]) for item in combined_items),
        "per_venue_summary": {
            venue: {
                "total_official_unique": report["total_official_unique"],
                "total_collected": report["total_collected"],
                "official_vs_collected_aligned": report["official_vs_collected_aligned"],
            }
            for venue, report in per_venue_reports.items()
        },
        "items": combined_items,
    }
    combined_report_path = collections_root / "dblp_expand_collection_report.json"
    combined_report_path.write_text(json.dumps(combined_report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Combined report written: %s", combined_report_path)
    LOGGER.info(
        "Combined totals: official_unique=%s collected=%s",
        combined_report["total_official_unique"],
        combined_report["total_collected"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
