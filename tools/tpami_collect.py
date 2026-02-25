#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect TPAMI papers (2021+) from DBLP with OpenAlex/Crossref abstract fill."""

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
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

LOGGER = logging.getLogger("tpami_collect")

DBLP_TPAMI_INDEX_URL = "https://dblp.org/db/journals/pami/"
DBLP_TPAMI_XML_URL_TEMPLATE = "https://dblp.org/db/journals/pami/pami{volume}.xml"
DBLP_REC_BASE = "https://dblp.org/rec/"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
CROSSREF_WORKS_URL = "https://api.crossref.org/works/"

DEFAULT_OUTPUT_ROOT = Path("archives/root_json")
DEFAULT_INDEX_ROOT = Path("index")

TPAMI_VOLUME_YEAR_OFFSET = 1978

DEFAULT_HEADERS = {
    "User-Agent": "JanusSearch/1.0 (mailto:janus@example.com)",
    "Accept": "application/json,text/html,application/xml;q=0.9,*/*;q=0.8",
}

TAG_RE = re.compile(r"<[^>]+>")
EDITORIAL_PREFIXES = ("editorial", "guest editorial")


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


def is_editorial_title(title: str) -> bool:
    """Return whether title indicates editorial content."""
    lowered = normalize_spaces(title).lower()
    return any(lowered.startswith(prefix) for prefix in EDITORIAL_PREFIXES)


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


def resolve_tpami_volumes(
    years: Sequence[int],
    timeout: float,
    retries: int,
    min_interval: float,
) -> Dict[int, int]:
    """Resolve DBLP TPAMI volume number from year and verify availability."""
    resolved: Dict[int, int] = {}
    for year in years:
        volume = year - TPAMI_VOLUME_YEAR_OFFSET
        if volume <= 0:
            raise ValueError(f"Invalid TPAMI year: {year}")
        xml_url = DBLP_TPAMI_XML_URL_TEMPLATE.format(volume=volume)
        xml_text = fetch_text(
            url=xml_url,
            timeout=timeout,
            retries=retries,
            min_interval=min_interval,
        )
        root = ET.fromstring(xml_text)
        years_in_xml = {
            int(ensure_str(year_node.text))
            for year_node in root.findall(".//year")
            if ensure_str(year_node.text).isdigit()
        }
        if year not in years_in_xml:
            # Soft warning: DBLP occasionally has carry-over entries.
            LOGGER.warning(
                "TPAMI volume-year mismatch: year=%s volume=%s xml_years=%s",
                year,
                volume,
                sorted(years_in_xml),
            )
        resolved[year] = volume
    return resolved


def parse_tpami_xml_records(xml_text: str, xml_url: str, year: int, volume: int) -> List[Dict[str, Any]]:
    """Parse article records for one TPAMI year from one DBLP XML."""
    root = ET.fromstring(xml_text)
    records: List[Dict[str, Any]] = []
    for node in root.findall(".//article"):
        key = ensure_str(node.attrib.get("key"))
        title = normalize_title(element_text(node.find("title")))
        if is_editorial_title(title):
            continue
        authors = [element_text(author) for author in node.findall("author")]
        authors = [author for author in authors if author]
        ee_values = [ensure_str(ee.text) for ee in node.findall("ee") if ensure_str(ee.text)]
        doi = extract_doi_from_ee(ee_values)
        pages = element_text(node.find("pages"))
        issue_number = element_text(node.find("number"))
        entry_year_text = element_text(node.find("year"))
        entry_volume = element_text(node.find("volume"))
        journal = element_text(node.find("journal"))

        if not title:
            continue
        if entry_year_text.isdigit() and int(entry_year_text) != year:
            continue
        rec_url = f"{DBLP_REC_BASE}{key}" if key else ""
        records.append(
            {
                "dblp_key": key,
                "title": title,
                "authors": authors,
                "doi": doi,
                "pages": pages,
                "number": issue_number,
                "entry_volume": entry_volume,
                "journal": journal,
                "ee_values": ee_values,
                "xml_url": xml_url,
                "rec_url": rec_url,
                "year": year,
                "volume": volume,
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
            try:
                data = future.result()
            except Exception:
                data = {}
            mapping.update(data)
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
                normalized = normalize_doi(doi)
                if normalized:
                    result[normalized] = abstract
            if done == len(unique_dois) or done % 25 == 0:
                LOGGER.info("Crossref abstract progress: %s/%s", done, len(unique_dois))
    return result


def build_quality_flags(
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
    first_ee = (record.get("ee_values") or [None])[0]
    external_url = f"https://doi.org/{doi}" if doi else first_ee
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
    if ensure_str(record.get("number")):
        source_ids["dblp_issue_number"] = ensure_str(record.get("number"))
    if ensure_str(record.get("entry_volume")):
        source_ids["dblp_volume"] = ensure_str(record.get("entry_volume"))
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
        "presentation_level": "journal",
        "openalex_id": openalex_id or None,
        "doi": doi,
        "track": "main",
        "track_display_name": "Journal",
        "track_group": "main",
        "title": title,
        "url": url or None,
        "external_url": ensure_str(external_url) or None,
        "citation_count": citation_count if isinstance(citation_count, int) else None,
        "venue": "TPAMI",
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
    volume: int,
    papers: Sequence[Dict[str, Any]],
    collected_at: str,
    source_year_count_estimate: int,
) -> Dict[str, Any]:
    """Build root_json payload for one year."""
    year_short = year % 100
    return {
        "query": {
            "target": f"TPAMI-{year_short:02d}",
            "venue_code": "TPAMI",
            "year": year,
            "provider": "dblp_openalex",
            "api_key_used": False,
            "work_filter_strategy": f"official_dblp_stream:journals/pami volume={volume} year={year}",
            "source_year_count_estimate": source_year_count_estimate,
        },
        "source": {
            "provider": "dblp_openalex",
            "openalex_source_id": None,
            "openreview_venue_id": None,
            "display_name": "IEEE Transactions on Pattern Analysis and Machine Intelligence",
            "source_type": "journal",
            "official_url": f"{DBLP_TPAMI_INDEX_URL}pami{volume}.html",
        },
        "generated_at_utc": collected_at,
        "paper_count": len(papers),
        "track_counts": count_field(papers, key="track", default="main"),
        "track_group_counts": count_field(papers, key="track_group", default="main"),
        "presentation_level_counts": count_field(papers, key="presentation_level", default="journal"),
        "papers": list(papers),
    }


def collect_one_year(
    *,
    year: int,
    volume: int,
    output_root: Path,
    timeout: float,
    retries: int,
    min_interval: float,
    openalex_chunk_size: int,
    openalex_workers: int,
    crossref_workers: int,
) -> Dict[str, Any]:
    """Collect one TPAMI year and write root_json."""
    collected_at = utc_now_iso()
    xml_url = DBLP_TPAMI_XML_URL_TEMPLATE.format(volume=volume)
    LOGGER.info("TPAMI %s loading DBLP volume: %s", year, xml_url)
    xml_text = fetch_text(url=xml_url, timeout=timeout, retries=retries, min_interval=min_interval)
    all_records = parse_tpami_xml_records(xml_text=xml_text, xml_url=xml_url, year=year, volume=volume)
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
        volume=volume,
        papers=papers,
        collected_at=collected_at,
        source_year_count_estimate=official_unique_count,
    )

    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"TPAMI-{year % 100:02d}.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    LOGGER.info(
        "TPAMI %s collected: volume=%s official_entries=%s unique=%s collected=%s missing_openalex=%s missing_abstract=%s",
        year,
        volume,
        official_entry_count,
        official_unique_count,
        len(papers),
        missing_openalex_count,
        missing_abstract_count,
    )
    return {
        "year": year,
        "volume": volume,
        "dblp_xml_url": xml_url,
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
    parser = argparse.ArgumentParser(description="Collect TPAMI papers from DBLP + OpenAlex")
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
    index_root.mkdir(parents=True, exist_ok=True)

    year_volume_map = resolve_tpami_volumes(
        years=years,
        timeout=args.timeout,
        retries=args.retries,
        min_interval=args.min_interval,
    )
    summary: List[Dict[str, Any]] = []
    for year in years:
        volume = year_volume_map[year]
        LOGGER.info("Collecting TPAMI %s via volume=%s", year, volume)
        summary.append(
            collect_one_year(
                year=year,
                volume=volume,
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
    report = {
        "generated_at_utc": utc_now_iso(),
        "provider": "dblp_openalex",
        "venue": "TPAMI",
        "years": years,
        "year_volume_map": {str(year): year_volume_map[year] for year in years},
        "total_official_unique": total_official_unique,
        "total_collected": total_collected,
        "official_vs_collected_aligned": total_official_unique == total_collected,
        "items": summary,
    }
    report_path = index_root / "tpami_collection_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("Collection report written: %s", report_path)
    LOGGER.info("Total official unique papers: %s", total_official_unique)
    LOGGER.info("Total collected papers: %s", total_collected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
