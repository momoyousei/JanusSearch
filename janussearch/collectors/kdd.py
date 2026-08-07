#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect KDD papers (2021+) from DBLP official streams and OpenAlex abstracts."""

from __future__ import annotations

import argparse
import hashlib
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
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from janussearch.collectors.outcomes import write_collection_result
from janussearch.infrastructure.http import HttpFetchError, fetch_response

LOGGER = logging.getLogger("kdd_collect")

DBLP_KDD_INDEX_URL = "https://dblp.org/db/conf/kdd/"
DBLP_KDD_XML_URL_TEMPLATE = "https://dblp.org/db/conf/kdd/kdd{tag}.xml"
DBLP_REC_BASE = "https://dblp.org/rec/"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
CROSSREF_WORKS_URL = "https://api.crossref.org/works/"

KDD_2026_OPENREVIEW_GROUPS: Tuple[str, ...] = (
    "KDD.org/2026/ADS_Track_August",
    "KDD.org/2026/ADS_Track_Cycle_2",
    "KDD.org/2026/AI4Sciences_Track_February",
    "KDD.org/2026/Blue_Sky_Ideas_Track",
    "KDD.org/2026/Cup",
    "KDD.org/2026/Datasets_and_Benchmark_Track_August",
    "KDD.org/2026/Datasets_and_Benchmark_Track_Cycle_2",
    "KDD.org/2026/Research_Track_August",
    "KDD.org/2026/Research_Track_Cycle_2",
    "KDD.org/2026/Workshop/Agent4IR",
    "KDD.org/2026/Workshop/Agentic_AI_Evaluation_and_Trustworthiness",
    "KDD.org/2026/Workshop/AgenticSE",
    "KDD.org/2026/Workshop/AI_Agents",
    "KDD.org/2026/Workshop/AIDataSci",
    "KDD.org/2026/Workshop/epiDAMIK",
    "KDD.org/2026/Workshop/FedKDD-FedMAS",
    "KDD.org/2026/Workshop/GALOP",
    "KDD.org/2026/Workshop/GMLLM",
    "KDD.org/2026/Workshop/Integrity",
    "KDD.org/2026/Workshop/PILA",
    "KDD.org/2026/Workshop/RelSciFM",
    "KDD.org/2026/Workshop/RespMultimodal",
    "KDD.org/2026/Workshop/SciSoc_Agents_and_LLMs",
    "KDD.org/2026/Workshop/SeT-LLM",
    "KDD.org/2026/Workshop/TensorKDD",
)

# ACM Digital Library export snapshots captured from the official KDD '26
# V.1/V.2 proceeding pages.  The collector deliberately reads only these
# official snapshots; it does not fall back to OpenReview or third-party
# mirrors for KDD 2026.
KDD_2026_ACM_SNAPSHOT_ROOT = Path("archives/root_json")
KDD_2026_ACM_V1_TRACK_MAP = "KDD-26-ACM-V1-track-map.json"
KDD_2026_ACM_SPECS: Tuple[Dict[str, Any], ...] = (
    {
        "file": "KDD-26-ACM-V1.bib",
        "volume": "V.1",
        "proceedings_doi": "10.1145/3770854",
        "section": "from_track_map",
        "display": "V.1 (Research/ADS/Data & Benchmark)",
        "track_group": "main",
        "presentation_level": "poster",
        "expected_count": 256,
    },
    {
        "file": "KDD-26-ACM-V2-research_track.bib",
        "volume": "V.2",
        "proceedings_doi": "10.1145/3770855",
        "section": "research_track",
        "display": "Research Track",
        "track_group": "main",
        "presentation_level": "poster",
        "expected_count": 599,
    },
    {
        "file": "KDD-26-ACM-V2-ads_track.bib",
        "volume": "V.2",
        "proceedings_doi": "10.1145/3770855",
        "section": "ads_track",
        "display": "ADS Track",
        "track_group": "main",
        "presentation_level": "poster",
        "expected_count": 141,
    },
    {
        "file": "KDD-26-ACM-V2-data_benchmark_track.bib",
        "volume": "V.2",
        "proceedings_doi": "10.1145/3770855",
        "section": "data_benchmark_track",
        "display": "Data & Benchmark Track",
        "track_group": "main",
        "presentation_level": "poster",
        "expected_count": 152,
    },
    {
        "file": "KDD-26-ACM-V2-ai_for_sciences_track.bib",
        "volume": "V.2",
        "proceedings_doi": "10.1145/3770855",
        "section": "ai_for_sciences_track",
        "display": "AI for Sciences Track",
        "track_group": "main",
        "presentation_level": "poster",
        "expected_count": 237,
    },
    {
        "file": "KDD-26-ACM-V2-blue_sky_ideas_track.bib",
        "volume": "V.2",
        "proceedings_doi": "10.1145/3770855",
        "section": "blue_sky_ideas_track",
        "display": "Blue Sky Ideas Track",
        "track_group": "main",
        "presentation_level": "poster",
        "expected_count": 17,
    },
    {
        "file": "KDD-26-ACM-V2-ads_invited_talks.bib",
        "volume": "V.2",
        "proceedings_doi": "10.1145/3770855",
        "section": "ads_invited_talks",
        "display": "ADS Invited Talks",
        "track_group": "program",
        "presentation_level": "not_applicable",
        "expected_count": 2,
    },
    {
        "file": "KDD-26-ACM-V2-special_day_talks.bib",
        "volume": "V.2",
        "proceedings_doi": "10.1145/3770855",
        "section": "special_day_talks",
        "display": "Special Day Talks",
        "track_group": "program",
        "presentation_level": "not_applicable",
        "expected_count": 3,
    },
    {
        "file": "KDD-26-ACM-V2-panel.bib",
        "volume": "V.2",
        "proceedings_doi": "10.1145/3770855",
        "section": "panel",
        "display": "Panel",
        "track_group": "program",
        "presentation_level": "not_applicable",
        "expected_count": 1,
    },
    {
        "file": "KDD-26-ACM-V2-hands_on_tutorials.bib",
        "volume": "V.2",
        "proceedings_doi": "10.1145/3770855",
        "section": "hands_on_tutorials",
        "display": "Hands-on Tutorials",
        "track_group": "program",
        "presentation_level": "not_applicable",
        "expected_count": 9,
    },
    {
        "file": "KDD-26-ACM-V2-lecture_style_tutorials.bib",
        "volume": "V.2",
        "proceedings_doi": "10.1145/3770855",
        "section": "lecture_style_tutorials",
        "display": "Lecture-Style Tutorials",
        "track_group": "program",
        "presentation_level": "not_applicable",
        "expected_count": 23,
    },
    {
        "file": "KDD-26-ACM-V2-workshop_summaries.bib",
        "volume": "V.2",
        "proceedings_doi": "10.1145/3770855",
        "section": "workshop_summaries",
        "display": "Workshop Summaries",
        "track_group": "program",
        "presentation_level": "not_applicable",
        "expected_count": 30,
    },
    {
        "file": "KDD-26-ACM-V2-late_additions.bib",
        "volume": "V.2",
        "proceedings_doi": "10.1145/3770855",
        "section": "late_additions",
        "display": "Late Additions",
        "track_group": "program",
        "presentation_level": "not_applicable",
        "expected_count": 1,
    },
)

DEFAULT_OUTPUT_ROOT = Path("archives/root_json")
DEFAULT_INDEX_ROOT = Path("artifacts")

DEFAULT_HEADERS = {
    "User-Agent": "JanusSearch/1.0 (mailto:janus@example.com)",
    "Accept": "application/json,text/html,application/xml;q=0.9,*/*;q=0.8",
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


def _bibtex_entries(text: str) -> List[str]:
    """Split a BibTeX export into balanced entry strings."""
    entries: List[str] = []
    for match in re.finditer(r"@(?:inproceedings|conference|article)\s*\{", text, re.I):
        start = match.start()
        opening = text.find("{", match.start(), match.end())
        depth = 0
        for index in range(opening, len(text)):
            char = text[index]
            # ACM exports use brace-delimited values.  Count every brace;
            # escaped quotes such as {\"o} must not enter a quoted mode.
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    entries.append(text[start : index + 1])
                    break
        else:
            raise RuntimeError("Unbalanced BibTeX entry")
    return entries


def _bibtex_value(text: str, start: int) -> Tuple[str, int]:
    """Read one BibTeX value and return value plus next cursor position."""
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text):
        return "", start
    if text[start] == "{":
        depth = 1
        index = start + 1
        while index < len(text) and depth:
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        if depth:
            raise RuntimeError("Unbalanced BibTeX field value")
        return text[start + 1 : index - 1], index
    if text[start] == '"':
        index = start + 1
        escaped = False
        chars: List[str] = []
        while index < len(text):
            char = text[index]
            if escaped:
                chars.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                return "".join(chars), index + 1
            else:
                chars.append(char)
            index += 1
        raise RuntimeError("Unbalanced quoted BibTeX field value")
    index = start
    while index < len(text) and text[index] not in ",\n":
        index += 1
    return text[start:index], index


def _bibtex_fields(entry: str) -> Tuple[str, Dict[str, str]]:
    """Parse BibTeX key and fields without relying on a third-party parser."""
    opening = entry.find("{")
    if opening < 0 or not entry.endswith("}"):
        raise RuntimeError("Malformed BibTeX entry")
    body = entry[opening + 1 : -1]
    depth = 0
    comma_index = -1
    for index, char in enumerate(body):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == "," and depth == 0:
            comma_index = index
            break
    if comma_index < 0:
        return normalize_spaces(body), {}
    key = normalize_spaces(body[:comma_index])
    fields: Dict[str, str] = {}
    cursor = comma_index + 1
    while cursor < len(body):
        while cursor < len(body) and (body[cursor].isspace() or body[cursor] == ","):
            cursor += 1
        name_match = re.match(r"([A-Za-z][A-Za-z0-9_-]*)\s*=", body[cursor:])
        if not name_match:
            break
        name = name_match.group(1).lower()
        cursor += name_match.end()
        value, cursor = _bibtex_value(body, cursor)
        fields[name] = normalize_spaces(
            html.unescape(value).replace("~", " ").replace("\\&", "&")
        )
    return key, fields


def parse_acm_bibtex(text: str, *, source_file: str) -> List[Dict[str, Any]]:
    """Parse one official ACM BibTeX export into normalized metadata."""
    records: List[Dict[str, Any]] = []
    for entry in _bibtex_entries(text):
        key, fields = _bibtex_fields(entry)
        doi = normalize_doi(fields.get("doi") or key)
        title = normalize_spaces(fields.get("title", "")).strip("{}")
        if not doi or not title:
            raise RuntimeError(f"ACM BibTeX entry missing DOI/title: {source_file}")
        author_text = fields.get("author", "").replace("{", "").replace("}", "")
        authors = [
            normalize_spaces(author)
            for author in re.split(r"\s+and\s+", author_text, flags=re.I)
            if normalize_spaces(author)
        ]
        abstract = fields.get("abstract", "").replace("{", "").replace("}", "")
        abstract = normalize_spaces(re.sub(r"\\[A-Za-z]+\s*", "", abstract))
        abstract = (
            abstract.replace(r"\%", "%")
            .replace(r"\&", "&")
            .replace(r"\_", "_")
            .replace(r"\#", "#")
        )
        keywords = [
            normalize_spaces(item)
            for item in re.split(r"[,;]", fields.get("keywords", ""))
            if normalize_spaces(item)
        ]
        records.append(
            {
                "bib_key": key,
                "doi": doi,
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "keywords": keywords,
                "pages": normalize_spaces(fields.get("pages", "")),
                "year": normalize_spaces(fields.get("year", "")),
                "category": normalize_spaces(fields.get("keywords", "")),
                "source_file": source_file,
            }
        )
    if not records:
        raise RuntimeError(f"ACM BibTeX snapshot has no entries: {source_file}")
    return records


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


def resolve_kdd_year_tags(
    years: Sequence[int],
    timeout: float,
    retries: int,
    min_interval: float,
) -> Dict[int, List[str]]:
    """Resolve DBLP XML tags (e.g. 2025-1, 2025-2) per requested year."""
    index_html = fetch_text(
        url=DBLP_KDD_INDEX_URL,
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
    )
    all_tags = set(
        re.findall(
            r'href="https://dblp\.org/db/conf/kdd/kdd(20\d{2}(?:-\d+)?)\.html"',
            index_html,
        )
    )

    result: Dict[int, List[str]] = {}
    for year in years:
        year_text = str(year)
        tags = [tag for tag in all_tags if tag == year_text or tag.startswith(f"{year_text}-")]
        if not tags:
            # KDD 2025+ may be split into multiple DBLP parts.
            if year >= 2025:
                tags = [f"{year_text}-1", f"{year_text}-2"]
            else:
                tags = [year_text]

        def sort_key(tag: str) -> Tuple[int, int]:
            if "-" not in tag:
                return (0, 0)
            suffix = ensure_str(tag.split("-", maxsplit=1)[1])
            if suffix.isdigit():
                return (1, int(suffix))
            return (1, 999999)

        result[year] = sorted(tags, key=sort_key)
    return result


def parse_dblp_xml_records(xml_text: str, xml_url: str, year: int) -> List[Dict[str, Any]]:
    """Parse inproceedings records from one DBLP XML."""
    root = ET.fromstring(xml_text)
    records: List[Dict[str, Any]] = []
    for node in root.findall(".//inproceedings"):
        key = ensure_str(node.attrib.get("key"))
        title = normalize_title(element_text(node.find("title")))
        authors = [element_text(author) for author in node.findall("author")]
        authors = [author for author in authors if author]
        ee_values = [ensure_str(ee.text) for ee in node.findall("ee") if ensure_str(ee.text)]
        doi = extract_doi_from_ee(ee_values)
        pages = element_text(node.find("pages"))

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
                "ee_values": ee_values,
                "xml_url": xml_url,
                "rec_url": rec_url,
                "year": year,
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
                chunk,
                timeout,
                retries,
                min_interval,
            ): chunk
            for chunk in chunks
        }
        done = 0
        for future in as_completed(futures):
            done += 1
            data = future.result()
            mapping.update(data)
            if done == len(chunks) or done % 20 == 0:
                LOGGER.info("OpenAlex batch progress: %s/%s", done, len(chunks))
    return mapping


TAG_RE = re.compile(r"<[^>]+>")


def parse_crossref_abstract(raw_abstract: str) -> str:
    """Parse Crossref abstract (may contain JATS tags)."""
    text = ensure_str(raw_abstract)
    if not text:
        return ""
    cleaned = html.unescape(TAG_RE.sub(" ", text))
    return normalize_spaces(cleaned)


def fetch_crossref_abstract(
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
                doi,
                timeout,
                retries,
                min_interval,
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
                result[normalize_doi(doi)] = abstract
            if done == len(unique_dois) or done % 25 == 0:
                LOGGER.info("Crossref abstract progress: %s/%s", done, len(unique_dois))
    return result


def build_quality_flags(authors: Sequence[str], abstract: str, institutions: Sequence[str], keywords: Sequence[str], openalex_miss: bool) -> List[str]:
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
    record: Dict[str, Any],
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
    if doi:
        source_ids["doi"] = doi
    if openalex_id:
        source_ids["openalex_id"] = openalex_id

    return {
        "paper_title": title,
        "authors": authors,
        "institutions": institutions,
        "abstract": abstract,
        "keywords": keywords,
        "presentation_level": "poster",
        "openalex_id": openalex_id or None,
        "doi": doi,
        "track": "main",
        "track_display_name": "Main",
        "track_group": "main",
        "title": title,
        "url": url or None,
        "external_url": ensure_str(external_url) or None,
        "citation_count": citation_count if isinstance(citation_count, int) else None,
        "venue": "KDD",
        "year": year,
        "source_provider": "dblp_openalex",
        "collected_at": collected_at,
        "source_ids": source_ids,
        "record_status": "placeholder" if ("missing_authors" in quality_flags or "missing_abstract" in quality_flags) else "resolved",
        "quality_flags": quality_flags,
    }


def patch_missing_abstracts_with_crossref(
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
    year: int,
    papers: Sequence[Dict[str, Any]],
    collected_at: str,
    source_year_count_estimate: int,
) -> Dict[str, Any]:
    """Build root_json payload for one year."""
    year_short = year % 100
    return {
        "query": {
            "target": f"KDD-{year_short:02d}",
            "venue_code": "KDD",
            "year": year,
            "provider": "dblp_openalex",
            "api_key_used": False,
            "work_filter_strategy": f"official_dblp_stream:conf/kdd year={year}",
            "source_year_count_estimate": source_year_count_estimate,
        },
        "source": {
            "provider": "dblp_openalex",
            "openalex_source_id": None,
            "openreview_venue_id": None,
            "display_name": "ACM SIGKDD Conference on Knowledge Discovery and Data Mining",
            "source_type": "conference",
            "official_url": f"{DBLP_KDD_INDEX_URL}kdd{year}.html",
        },
        "generated_at_utc": collected_at,
        "paper_count": len(papers),
        "track_counts": count_field(papers, key="track", default="main"),
        "track_group_counts": count_field(papers, key="track_group", default="main"),
        "presentation_level_counts": count_field(papers, key="presentation_level", default="poster"),
        "papers": list(papers),
    }


def complete_failed_group_coverage(
    coverage: Sequence[Dict[str, Any]], *, reason: str
) -> List[Dict[str, Any]]:
    """Return an auditable 25-row manifest after an early group failure."""
    completed = [dict(item) for item in coverage]
    seen = {ensure_str(item.get("group_id")) for item in completed}
    completed.extend(
        {
            "group_id": group,
            "status": "not_checked_due_prior_group_failure",
            "accepted_count": 0,
            "reason": reason,
        }
        for group in KDD_2026_OPENREVIEW_GROUPS
        if group not in seen
    )
    return completed


def _sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of one official snapshot."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_acm_2026(
    *,
    output_root: Path,
    snapshot_root: Path = KDD_2026_ACM_SNAPSHOT_ROOT,
) -> Dict[str, Any]:
    """Collect all requested KDD 2026 ACM V.1/V.2 proceeding columns.

    The ACM pages are protected from ordinary HTTP clients, so the official
    browser exports are retained as immutable run inputs.  Missing files,
    changed section counts, unknown V.1 DOI mappings, duplicate DOIs, and
    title conflicts all fail closed and write an incomplete-source sidecar.
    """
    official_urls = [
        "https://dl.acm.org/doi/proceedings/10.1145/3770854",
        "https://dl.acm.org/doi/proceedings/10.1145/3770855",
    ]
    collected_at = utc_now_iso()
    source_hashes: Dict[str, str] = {}
    section_counts: Dict[str, int] = {}
    section_expected: Dict[str, int] = {}
    section_files: Dict[str, str] = {}
    section_volumes: Dict[str, str] = {}
    section_display: Dict[str, str] = {}
    section_records: List[Dict[str, Any]] = []
    try:
        map_path = snapshot_root / KDD_2026_ACM_V1_TRACK_MAP
        if not map_path.is_file():
            raise RuntimeError(f"missing official ACM V.1 track map: {map_path}")
        v1_map_payload = json.loads(map_path.read_text(encoding="utf-8"))
        v1_map = v1_map_payload.get("records")
        if not isinstance(v1_map, dict) or len(v1_map) != 256:
            raise RuntimeError("official ACM V.1 track map must contain exactly 256 records")

        for spec in KDD_2026_ACM_SPECS:
            path = snapshot_root / ensure_str(spec["file"])
            if not path.is_file():
                raise RuntimeError(f"missing official ACM snapshot: {path}")
            source_hashes[ensure_str(spec["file"])] = _sha256_file(path)
            parsed = parse_acm_bibtex(
                path.read_text(encoding="utf-8"), source_file=ensure_str(spec["file"])
            )
            if spec["section"] == "from_track_map":
                for parsed_record in parsed:
                    doi = ensure_str(parsed_record["doi"])
                    mapping = v1_map.get(doi)
                    if not isinstance(mapping, dict):
                        raise RuntimeError(f"V.1 DOI has no official track mapping: {doi}")
                    base_section = normalize_spaces(ensure_str(mapping.get("section")))
                    section = f"v1_{base_section}"
                    display = f"V.1 {normalize_spaces(ensure_str(mapping.get('display')))}"
                    section_records.append(
                        {
                            **parsed_record,
                            "volume": spec["volume"],
                            "proceedings_doi": spec["proceedings_doi"],
                            "section": section,
                            "display": display,
                            "track_group": spec["track_group"],
                            "presentation_level": spec["presentation_level"],
                        }
                    )
                    section_counts[section] = section_counts.get(section, 0) + 1
                    section_files[section] = ensure_str(spec["file"])
                    section_volumes[section] = ensure_str(spec["volume"])
                    section_display[section] = display
                for section, expected in (
                    ("v1_research_track", 183),
                    ("v1_ads_track", 44),
                    ("v1_data_benchmark_track", 29),
                ):
                    section_expected[section] = expected
            else:
                section = ensure_str(spec["section"])
                section_expected[section] = int(spec["expected_count"])
                section_files[section] = ensure_str(spec["file"])
                section_volumes[section] = ensure_str(spec["volume"])
                section_display[section] = ensure_str(spec["display"])
                section_counts[section] = len(parsed)
                section_records.extend(
                    {
                        **parsed_record,
                        "volume": spec["volume"],
                        "proceedings_doi": spec["proceedings_doi"],
                        "section": section,
                        "display": spec["display"],
                        "track_group": spec["track_group"],
                        "presentation_level": spec["presentation_level"],
                    }
                    for parsed_record in parsed
                )
        if set(section_counts) != set(section_expected):
            raise RuntimeError(
                "ACM section manifest mismatch: "
                f"actual={sorted(section_counts)} expected={sorted(section_expected)}"
            )
        for section, expected in section_expected.items():
            actual = section_counts.get(section, 0)
            if actual != expected:
                raise RuntimeError(
                    f"ACM section count changed for {section}: {actual} != {expected}"
                )

        seen_doi: Dict[str, str] = {}
        seen_title: Dict[str, str] = {}
        papers: List[Dict[str, Any]] = []
        for item in section_records:
            doi = normalize_doi(ensure_str(item.get("doi")))
            title = normalize_spaces(ensure_str(item.get("title")))
            if not doi or not title:
                raise RuntimeError("ACM record missing DOI/title")
            previous_title = seen_doi.get(doi)
            if previous_title is not None and previous_title != title:
                raise RuntimeError(f"ACM DOI maps to multiple titles: {doi}")
            title_key = title.casefold()
            previous_doi = seen_title.get(title_key)
            if previous_doi is not None and previous_doi != doi:
                raise RuntimeError(f"ACM duplicate title maps to two DOIs: {title}")
            if doi in seen_doi:
                continue
            seen_doi[doi] = title
            seen_title[title_key] = doi
            authors = [
                normalize_spaces(ensure_str(author))
                for author in item.get("authors", [])
                if normalize_spaces(ensure_str(author))
            ]
            abstract = normalize_spaces(ensure_str(item.get("abstract")))
            keywords = [
                normalize_spaces(ensure_str(keyword))
                for keyword in item.get("keywords", [])
                if normalize_spaces(ensure_str(keyword))
            ]
            quality_flags: List[str] = []
            if not authors:
                quality_flags.append("missing_authors")
            if not abstract:
                quality_flags.append("missing_abstract")
            source_file = ensure_str(item.get("source_file"))
            section = ensure_str(item.get("section"))
            source_ids = {
                "doi": doi,
                "acm_bib_key": ensure_str(item.get("bib_key")),
                "acm_proceedings_doi": ensure_str(item.get("proceedings_doi")),
                "acm_volume": ensure_str(item.get("volume")),
                "acm_section": section,
                "acm_snapshot": source_file,
            }
            pages = normalize_spaces(ensure_str(item.get("pages")))
            if pages:
                source_ids["acm_pages"] = pages
            papers.append(
                {
                    "paper_title": title,
                    "title": title,
                    "authors": authors,
                    "institutions": [],
                    "abstract": abstract,
                    "keywords": keywords,
                    "presentation_level": ensure_str(item.get("presentation_level"))
                    or "poster",
                    "openalex_id": None,
                    "doi": doi,
                    "track": section,
                    "track_display_name": ensure_str(item.get("display")),
                    "track_group": ensure_str(item.get("track_group")) or "main",
                    "url": f"https://dl.acm.org/doi/{doi}",
                    "external_url": f"https://doi.org/{doi}",
                    "citation_count": None,
                    "venue": "KDD",
                    "year": 2026,
                    "source_provider": "kdd_2026_official_acm",
                    "collected_at": collected_at,
                    "source_ids": source_ids,
                    "record_status": "resolved" if authors and abstract else "placeholder",
                    "quality_flags": quality_flags,
                }
            )
        papers.sort(key=lambda item: normalize_spaces(ensure_str(item.get("title"))).casefold())
        groups = [
            {
                "section": section,
                "display": section_display[section],
                "volume": section_volumes[section],
                "source_file": section_files[section],
                "expected_count": section_expected[section],
                "raw_count": section_counts[section],
                "unique_count": sum(
                    1 for paper in papers if ensure_str(paper.get("track")) == section
                ),
                "status": "covered",
            }
            for section in section_expected
        ]
        raw_count = len(section_records)
        payload = build_payload(
            year=2026,
            papers=papers,
            collected_at=collected_at,
            source_year_count_estimate=len(papers),
        )
        payload["query"] = {
            "target": "KDD-26",
            "venue_code": "KDD",
            "year": 2026,
            "provider": "kdd_2026_official_acm",
            "api_key_used": False,
            "work_filter_strategy": "official_acm_v1_v2_all_columns_except_external_workshop_papers",
            "source_year_count_estimate": len(papers),
        }
        payload["source"] = {
            "provider": "kdd_2026_official_acm",
            "display_name": "Proceedings of the 32nd ACM SIGKDD Conference on Knowledge Discovery and Data Mining",
            "source_type": "conference",
            "official_url": official_urls[0],
            "urls": official_urls,
            "snapshot_hashes": source_hashes,
        }
        payload["group_coverage"] = {
            "expected_group_count": len(section_expected),
            "covered_group_count": len(groups),
            "groups": groups,
            "raw_track_count": raw_count,
            "unique_track_count": len(papers),
            "volume_counts": {
                "V.1": sum(1 for item in section_records if item.get("volume") == "V.1"),
                "V.2": sum(1 for item in section_records if item.get("volume") == "V.2"),
            },
            "fallback_reason": "official_acm_proceedings_chrome_export",
            "external_workshop_scope": "ignored_by_user_request",
        }
        output_root.mkdir(parents=True, exist_ok=True)
        output_path = output_root / "KDD-26.json"
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        sidecar = write_collection_result(
            output_root,
            outcome="collected",
            venue="KDD",
            year=2026,
            sources=official_urls,
            reason="official_acm_proceedings_v1_v2_snapshot_complete",
            metrics={
                "group_coverage": payload["group_coverage"],
                "paper_count": len(papers),
                "raw_count": raw_count,
                "unique_count": len(papers),
                "authors_coverage": round(
                    sum(1 for paper in papers if paper.get("authors")) / max(1, len(papers)) * 100,
                    2,
                ),
                "abstract_coverage": round(
                    sum(1 for paper in papers if paper.get("abstract")) / max(1, len(papers)) * 100,
                    2,
                ),
                "snapshot_hashes": source_hashes,
            },
        )
        return {
            "year": 2026,
            "provider": "kdd_2026_official_acm",
            "official_entry_count": raw_count,
            "official_unique_count": len(papers),
            "collected_paper_count": len(papers),
            "missing_vs_official_unique": 0,
            "missing_openalex_count": 0,
            "missing_abstract_count": sum(
                1 for paper in papers if "missing_abstract" in (paper.get("quality_flags") or [])
            ),
            "crossref_patched_count": 0,
            "group_coverage": payload["group_coverage"],
            "output_file": str(output_path),
            "sidecar": str(sidecar),
            "generated_at_utc": collected_at,
        }
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        sidecar = write_collection_result(
            output_root,
            outcome="incomplete_source",
            venue="KDD",
            year=2026,
            sources=official_urls,
            reason=str(exc),
            metrics={
                "expected_section_count": len(KDD_2026_ACM_SPECS) + 2,
                "section_counts": section_counts,
                "section_expected": section_expected,
                "snapshot_hashes": source_hashes,
            },
        )
        raise RuntimeError(f"KDD 2026 ACM snapshot gate failed; sidecar={sidecar}: {exc}") from exc


def collect_openreview_2026(
    *,
    output_root: Path,
    timeout: float,
    retries: int,
) -> Dict[str, Any]:
    """Collect the exact 25 KDD-owned 2026 content groups from OpenReview."""
    from janussearch.collectors.generic import (
        fetch_openreview_notes_for_venue,
        openreview_note_to_record,
    )

    if len(KDD_2026_OPENREVIEW_GROUPS) != 25:
        raise RuntimeError("KDD 2026 group manifest must contain exactly 25 groups")
    collected_at = utc_now_iso()
    group_coverage: List[Dict[str, Any]] = []
    notes_by_stable_id: Dict[str, Dict[str, Any]] = {}
    memberships: Dict[str, List[str]] = {}
    sources = [f"https://openreview.net/group?id={group}" for group in KDD_2026_OPENREVIEW_GROUPS]
    probe_group = KDD_2026_OPENREVIEW_GROUPS[0]
    probe_url = "https://api2.openreview.net/notes?" + urlencode(
        {"content.venueid": probe_group, "limit": "1", "details": "replies"}
    )
    try:
        fetch_response(probe_url, timeout=timeout, retries=1)
    except HttpFetchError as exc:
        if exc.status_code != 403:
            raise
        group_coverage = [
            {
                "group_id": group,
                "status": "forbidden" if group == probe_group else "not_checked_due_api_403",
                "accepted_count": 0,
                "reason": str(exc) if group == probe_group else "official_api_probe_forbidden",
            }
            for group in KDD_2026_OPENREVIEW_GROUPS
        ]
        sidecar = write_collection_result(
            output_root,
            outcome="incomplete_source",
            venue="KDD",
            year=2026,
            sources=[*sources, probe_url],
            reason="official_openreview_api_forbidden_and_ui_has_no_public_accept_lists",
            metrics={
                "expected_group_count": len(KDD_2026_OPENREVIEW_GROUPS),
                "covered_group_count": 0,
                "group_coverage": group_coverage,
                "fallback_reason": "no_same_event_official_accepted_mirror",
            },
        )
        raise RuntimeError(
            f"KDD 2026 official OpenReview API is forbidden; sidecar={sidecar}"
        ) from exc
    for group in KDD_2026_OPENREVIEW_GROUPS:
        try:
            notes, meta = fetch_openreview_notes_for_venue(
                group,
                timeout=timeout,
                retries=retries,
                show_progress=False,
            )
        except RuntimeError as exc:
            group_coverage.append(
                {
                    "group_id": group,
                    "status": "forbidden" if "403" in str(exc) or "Forbidden" in str(exc) else "failed",
                    "accepted_count": 0,
                    "reason": str(exc),
                }
            )
            group_coverage = complete_failed_group_coverage(
                group_coverage, reason=f"prior_group_failed:{group}"
            )
            sidecar = write_collection_result(
                output_root,
                outcome="incomplete_source",
                venue="KDD",
                year=2026,
                sources=sources,
                reason=f"unverifiable_official_openreview_group:{group}",
                metrics={
                    "expected_group_count": len(KDD_2026_OPENREVIEW_GROUPS),
                    "covered_group_count": sum(
                        1 for item in group_coverage if item.get("status") == "covered"
                    ),
                    "group_coverage": group_coverage,
                    "fallback_reason": "openreview_api_unavailable_and_no_same_event_official_mirror",
                },
            )
            raise RuntimeError(
                f"KDD 2026 official group is not verifiable: {group}; sidecar={sidecar}"
            ) from exc
        if not notes:
            group_coverage.append(
                {
                    "group_id": group,
                    "status": "empty",
                    "accepted_count": 0,
                    "query_meta": meta,
                }
            )
            group_coverage = complete_failed_group_coverage(
                group_coverage, reason=f"prior_group_empty:{group}"
            )
            sidecar = write_collection_result(
                output_root,
                outcome="incomplete_source",
                venue="KDD",
                year=2026,
                sources=sources,
                reason=f"official_openreview_group_has_no_public_accepted_records:{group}",
                metrics={
                    "expected_group_count": len(KDD_2026_OPENREVIEW_GROUPS),
                    "covered_group_count": sum(
                        1 for item in group_coverage if item.get("status") == "covered"
                    ),
                    "group_coverage": group_coverage,
                    "fallback_reason": "no_same_event_official_accepted_mirror",
                },
            )
            raise RuntimeError(
                f"KDD 2026 group has no public accepted records: {group}; sidecar={sidecar}"
            )
        group_coverage.append(
            {
                "group_id": group,
                "status": "covered",
                "accepted_count": len(notes),
                "query_meta": meta,
            }
        )
        for note in notes:
            stable_id = normalize_spaces(
                ensure_str(note.get("forum") or note.get("id"))
            )
            if not stable_id:
                raise RuntimeError(f"KDD OpenReview note in {group} has no stable id")
            previous = notes_by_stable_id.get(stable_id)
            if previous is not None:
                previous_title = normalize_title(
                    ensure_str((previous.get("content") or {}).get("title"))
                )
                current_title = normalize_title(
                    ensure_str((note.get("content") or {}).get("title"))
                )
                if previous_title != current_title:
                    raise RuntimeError(
                        f"KDD stable id {stable_id} maps to multiple titles"
                    )
            else:
                notes_by_stable_id[stable_id] = note
            memberships.setdefault(stable_id, []).append(group)

    papers: List[Dict[str, Any]] = []
    seen_titles: Dict[str, str] = {}
    for stable_id, note in notes_by_stable_id.items():
        base = openreview_note_to_record(note, "poster", {})
        title = normalize_title(ensure_str(base.get("paper_title")))
        if not title:
            raise RuntimeError(f"KDD OpenReview note {stable_id} has no title")
        title_key = title.casefold()
        previous_id = seen_titles.get(title_key)
        if previous_id and previous_id != stable_id:
            raise RuntimeError(
                f"KDD duplicate title maps to two stable ids: {title}"
            )
        seen_titles[title_key] = stable_id
        groups = sorted(set(memberships[stable_id]))
        primary_group = groups[0]
        group_suffix = primary_group.split("KDD.org/2026/", 1)[-1]
        track = re.sub(r"[^a-z0-9]+", "_", group_suffix.lower()).strip("_")
        authors = [ensure_str(value) for value in base.get("authors", []) if ensure_str(value)]
        abstract = normalize_spaces(ensure_str(base.get("abstract")))
        keywords = [ensure_str(value) for value in base.get("keywords", []) if ensure_str(value)]
        institutions = [
            ensure_str(value) for value in base.get("institutions", []) if ensure_str(value)
        ]
        flags: List[str] = []
        if not authors:
            flags.append("missing_authors")
        if not abstract:
            flags.append("missing_abstract")
        if not institutions:
            flags.append("missing_institutions")
        if not keywords:
            flags.append("missing_keywords")
        papers.append(
            {
                **base,
                "paper_title": title,
                "title": title,
                "openreview_id": stable_id,
                "track": track,
                "track_display_name": group_suffix.replace("_", " ").replace("/", " / "),
                "track_group": "workshop" if "/Workshop/" in primary_group else "main",
                "url": f"https://openreview.net/forum?id={stable_id}",
                "external_url": None,
                "citation_count": None,
                "venue": "KDD",
                "year": 2026,
                "source_provider": "kdd_2026_official_openreview",
                "collected_at": collected_at,
                "source_ids": {
                    "openreview_forum_id": stable_id,
                    "kdd_group_ids": ",".join(groups),
                },
                "record_status": "resolved" if authors and abstract else "placeholder",
                "quality_flags": flags,
            }
        )
    papers.sort(key=lambda item: normalize_title(ensure_str(item.get("title"))).casefold())
    payload = build_payload(
        year=2026,
        papers=papers,
        collected_at=collected_at,
        source_year_count_estimate=len(papers),
    )
    payload["query"]["provider"] = "kdd_2026_official_openreview"
    payload["query"]["work_filter_strategy"] = "exact_25_kdd_owned_openreview_groups"
    payload["source"] = {
        "provider": "kdd_2026_official_openreview",
        "openreview_venue_id": "KDD.org/2026",
        "display_name": "KDD 2026",
        "source_type": "conference",
        "official_url": "https://openreview.net/group?id=KDD.org/2026",
        "urls": sources,
    }
    payload["group_coverage"] = {
        "expected_group_count": len(KDD_2026_OPENREVIEW_GROUPS),
        "covered_group_count": len(group_coverage),
        "groups": group_coverage,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / "KDD-26.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "year": 2026,
        "dblp_parts": [],
        "dblp_xml_urls": [],
        "official_entry_count": sum(item["accepted_count"] for item in group_coverage),
        "official_unique_count": len(papers),
        "collected_paper_count": len(papers),
        "missing_vs_official_unique": 0,
        "missing_openalex_count": 0,
        "missing_abstract_count": sum(
            1 for paper in papers if "missing_abstract" in (paper.get("quality_flags") or [])
        ),
        "crossref_patched_count": 0,
        "group_coverage": payload["group_coverage"],
        "output_file": str(output_path),
        "generated_at_utc": collected_at,
    }


def collect_one_year(
    *,
    year: int,
    tags: Sequence[str],
    output_root: Path,
    timeout: float,
    retries: int,
    min_interval: float,
    openalex_chunk_size: int,
    openalex_workers: int,
    crossref_workers: int,
) -> Dict[str, Any]:
    """Collect one KDD year and write root_json."""
    if year == 2026:
        return collect_acm_2026(
            output_root=output_root,
        )
    collected_at = utc_now_iso()
    all_records: List[Dict[str, Any]] = []
    xml_urls: List[str] = []
    for tag in tags:
        xml_url = DBLP_KDD_XML_URL_TEMPLATE.format(tag=tag)
        LOGGER.info("KDD %s loading DBLP part: %s", year, xml_url)
        xml_text = fetch_text(url=xml_url, timeout=timeout, retries=retries, min_interval=min_interval)
        part_records = parse_dblp_xml_records(xml_text=xml_text, xml_url=xml_url, year=year)
        LOGGER.info("KDD %s DBLP part %s records=%s", year, tag, len(part_records))
        all_records.extend(part_records)
        xml_urls.append(xml_url)

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
        paper = build_paper_record(
            record=record,
            year=year,
            collected_at=collected_at,
            openalex=openalex,
        )
        papers.append(paper)

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
        year=year,
        papers=papers,
        collected_at=collected_at,
        source_year_count_estimate=official_unique_count,
    )

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"KDD-{year % 100:02d}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    LOGGER.info(
        "KDD %s collected: official_entries=%s unique=%s collected=%s missing_openalex=%s missing_abstract=%s",
        year,
        official_entry_count,
        official_unique_count,
        len(papers),
        missing_openalex_count,
        missing_abstract_count,
    )
    return {
        "year": year,
        "dblp_parts": list(tags),
        "dblp_xml_urls": xml_urls,
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
    parser = argparse.ArgumentParser(description="Collect KDD papers from DBLP + OpenAlex")
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
    parser.add_argument("--retries", type=int, default=5, help="HTTP retry times")
    parser.add_argument(
        "--min-interval",
        type=float,
        default=0.4,
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

    years = parse_years(args.years)
    output_root = Path(args.output_root)
    index_root = Path(args.index_root)
    collections_root = index_root / "collections"
    collections_root.mkdir(parents=True, exist_ok=True)

    dblp_years = [year for year in years if year != 2026]
    year_tags = (
        resolve_kdd_year_tags(
            years=dblp_years,
            timeout=args.timeout,
            retries=args.retries,
            min_interval=args.min_interval,
        )
        if dblp_years
        else {}
    )
    summary: List[Dict[str, Any]] = []
    for year in years:
        tags = year_tags.get(year, [str(year)])
        LOGGER.info("Collecting KDD %s via DBLP tags: %s", year, tags)
        summary.append(
            collect_one_year(
                year=year,
                tags=tags,
                output_root=output_root,
                timeout=args.timeout,
                retries=args.retries,
                min_interval=args.min_interval,
                openalex_chunk_size=args.openalex_chunk_size,
                openalex_workers=args.openalex_workers,
                crossref_workers=args.crossref_workers,
            )
        )

    total_official_unique = sum(int(item["official_unique_count"]) for item in summary)
    total_collected = sum(int(item["collected_paper_count"]) for item in summary)
    report_provider = "kdd_2026_official_acm" if years == [2026] else "dblp_openalex"
    report = {
        "generated_at_utc": utc_now_iso(),
        "provider": report_provider,
        "venue": "KDD",
        "years": years,
        "total_official_unique": total_official_unique,
        "total_collected": total_collected,
        "official_vs_collected_aligned": total_official_unique == total_collected,
        "items": summary,
    }
    report_path = collections_root / "kdd_collection_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Collection report written: %s", report_path)
    LOGGER.info("Total official unique papers: %s", total_official_unique)
    LOGGER.info("Total collected papers: %s", total_collected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
