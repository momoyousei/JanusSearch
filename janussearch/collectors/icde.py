#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect the complete ICDE 2026 program and official workshop proceedings."""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from janussearch.collectors.outcomes import write_collection_result
from janussearch.infrastructure.http import fetch_response

LOGGER = logging.getLogger("icde_collect")

ICDE_2026_BASE_URL = "https://icde2026.github.io/"
ICDE_2026_WORKSHOP_XML_URL = "https://dblp.org/db/conf/icde/icde2026w.xml"
ICDE_2026_WORKSHOP_PROCEEDINGS_DOI = "10.1109/ICDEW71238.2026"

ICDE_2026_TRACK_PAGES: Tuple[Tuple[str, str, str, str, int], ...] = (
    ("accepted-papers.html", "research", "Research", "main", 261),
    ("ia-papers.html", "industry", "Industry and Application", "main", 37),
    ("demo-papers.html", "demo", "Demonstration", "adjunct", 20),
    ("tkde-papers.html", "tkde_poster", "TKDE Poster", "adjunct", 15),
    ("deft-papers.html", "deft", "Data Engineering Future Technologies", "adjunct", 6),
    ("lightning-talks.html", "lightning", "Lightning Talk", "adjunct", 14),
    ("phd-papers.html", "phd", "PhD Symposium", "adjunct", 4),
)

TRACK_PRIORITY = {
    "research": 0,
    "industry": 1,
    "demo": 2,
    "tkde_poster": 3,
    "deft": 4,
    "phd": 5,
    "lightning": 6,
    "workshop": 7,
}


def utc_now_iso() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: Any) -> str:
    """Normalize a scalar text field."""
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def normalize_title(value: Any) -> str:
    """Build an exact-title reconciliation key."""
    text = normalize_text(value)
    if text.endswith("."):
        text = text[:-1].rstrip()
    return text


def unique(values: Iterable[str]) -> List[str]:
    """Deduplicate non-empty strings while preserving order."""
    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        item = normalize_text(value)
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def fetch_text(url: str, *, timeout: float, retries: int) -> str:
    """Fetch UTF-8 text with the shared retry policy."""
    return fetch_response(url, timeout=timeout, retries=retries).text()


class AcceptedPaperParser(HTMLParser):
    """Parse the repeated paper cards used by all seven official ICDE pages."""

    def __init__(self) -> None:
        super().__init__()
        self.records: List[Dict[str, Any]] = []
        self.current: Dict[str, Any] | None = None
        self.item_depth = 0
        self.capture: Tuple[str, int, List[str]] | None = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, str | None]]) -> None:
        attrs_map = {key: value or "" for key, value in attrs}
        classes = set(attrs_map.get("class", "").split())
        if self.current is None:
            if tag == "li" and "paper-item" in classes:
                self.current = {
                    "number": "",
                    "title": "",
                    "authors": [],
                    "institutions": [],
                }
                self.item_depth = 1
            return

        self.item_depth += 1
        field = ""
        if "number-column" in classes:
            field = "number"
        elif "title" in classes:
            field = "title"
        elif "author-name" in classes:
            field = "author"
        elif "affiliation" in classes:
            field = "institution"
        if field:
            if self.capture is not None:
                raise RuntimeError(f"Nested ICDE capture fields are not supported: {field}")
            self.capture = (field, self.item_depth, [])

    def handle_data(self, data: str) -> None:
        if self.capture is not None:
            self.capture[2].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        if self.capture is not None and self.capture[1] == self.item_depth:
            field, _, chunks = self.capture
            value = normalize_text(" ".join(chunks))
            if field == "number":
                self.current["number"] = value
            elif field == "title":
                self.current["title"] = normalize_title(value)
            elif field == "author":
                self.current["authors"].append(value.rstrip("* "))
            elif field == "institution":
                self.current["institutions"].append(value.strip("() "))
            self.capture = None
        if self.item_depth == 1 and tag == "li":
            self.current["authors"] = unique(self.current["authors"])
            self.current["institutions"] = unique(self.current["institutions"])
            if not self.current["title"]:
                raise RuntimeError("ICDE official paper card has no title")
            self.records.append(self.current)
            self.current = None
            self.item_depth = 0
            self.capture = None
            return
        self.item_depth -= 1


def parse_accepted_page(page_html: str) -> List[Dict[str, Any]]:
    """Parse one official ICDE accepted-paper page."""
    parser = AcceptedPaperParser()
    parser.feed(page_html)
    parser.close()
    if parser.current is not None:
        raise RuntimeError("ICDE accepted-paper page ended inside a paper card")
    return parser.records


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return normalize_text("".join(element.itertext()))


def parse_workshop_proceedings(xml_text: str) -> List[Dict[str, Any]]:
    """Parse the 2026 ICDEW table of contents and require official IEEE DOIs."""
    root = ET.fromstring(xml_text)
    records: List[Dict[str, Any]] = []
    for node in root.findall(".//inproceedings"):
        title = normalize_title(_element_text(node.find("title")))
        authors = unique(_element_text(author) for author in node.findall("author"))
        ee_values = unique(_element_text(value) for value in node.findall("ee"))
        doi = ""
        for value in ee_values:
            parsed = urlparse(value)
            candidate = parsed.path.lstrip("/") if "doi.org" in parsed.netloc else value
            if candidate.casefold().startswith(f"{ICDE_2026_WORKSHOP_PROCEEDINGS_DOI}.".casefold()):
                doi = candidate.casefold()
                break
        if not title or not authors or not doi:
            raise RuntimeError("ICDEW 2026 record lacks title, authors, or official IEEE DOI")
        records.append(
            {
                "number": str(len(records) + 1),
                "title": title,
                "authors": authors,
                "institutions": [],
                "doi": doi,
                "dblp_key": normalize_text(node.attrib.get("key")),
                "pages": _element_text(node.find("pages")),
                "url": f"https://doi.org/{doi}",
            }
        )
    return records


def _quality_flags(record: Dict[str, Any]) -> List[str]:
    flags: List[str] = []
    if not record.get("authors"):
        flags.append("missing_authors")
    if not normalize_text(record.get("abstract")):
        flags.append("missing_abstract")
    if not record.get("institutions"):
        flags.append("missing_institutions")
    if not record.get("keywords"):
        flags.append("missing_keywords")
    return flags


def _build_record(
    raw: Dict[str, Any],
    *,
    track: str,
    track_display: str,
    track_group: str,
    source_url: str,
    collected_at: str,
) -> Dict[str, Any]:
    title = normalize_title(raw.get("title"))
    doi = normalize_text(raw.get("doi")) or None
    source_key = normalize_text(raw.get("dblp_key")) or f"{track}:{raw.get('number')}"
    source_ids: Dict[str, str] = {
        "icde_source_key": source_key,
        "icde_track": track,
    }
    if doi:
        source_ids["doi"] = doi
    if raw.get("dblp_key"):
        source_ids["dblp_key"] = normalize_text(raw.get("dblp_key"))
    if raw.get("pages"):
        source_ids["pages"] = normalize_text(raw.get("pages"))
    record = {
        "paper_title": title,
        "title": title,
        "authors": unique(raw.get("authors") or []),
        "institutions": unique(raw.get("institutions") or []),
        "abstract": "",
        "keywords": [],
        "presentation_level": "oral" if track in {"research", "industry", "deft", "phd", "workshop"} else "poster",
        "openalex_id": None,
        "doi": doi,
        "track": track,
        "track_display_name": track_display,
        "track_group": track_group,
        "track_memberships": [track],
        "url": normalize_text(raw.get("url")) or source_url,
        "external_url": normalize_text(raw.get("url")) or None,
        "citation_count": None,
        "venue": "ICDE",
        "year": 2026,
        "source_provider": "icde_2026_official_program",
        "collected_at": collected_at,
        "source_ids": source_ids,
        "record_status": "resolved" if raw.get("authors") else "placeholder",
    }
    record["quality_flags"] = _quality_flags(record)
    return record


def dedupe_tracks(
    records: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Deduplicate exact titles and retain every original track membership."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        key = normalize_title(record.get("title")).casefold()
        if not key:
            raise RuntimeError("ICDE source record has an empty title")
        grouped.setdefault(key, []).append(record)

    output: List[Dict[str, Any]] = []
    mappings: List[Dict[str, Any]] = []
    for key in sorted(grouped):
        group = sorted(
            grouped[key], key=lambda item: TRACK_PRIORITY.get(normalize_text(item.get("track")), 999)
        )
        retained = dict(group[0])
        retained["source_ids"] = dict(retained.get("source_ids") or {})
        memberships = unique(normalize_text(item.get("track")) for item in group)
        retained["track_memberships"] = memberships
        retained["source_ids"]["icde_track_memberships"] = json.dumps(
            memberships, ensure_ascii=False, separators=(",", ":")
        )
        for duplicate in group[1:]:
            duplicate_ids = duplicate.get("source_ids") or {}
            for source_name in ("doi", "dblp_key", "pages"):
                value = normalize_text(duplicate_ids.get(source_name))
                if value and not normalize_text(retained["source_ids"].get(source_name)):
                    retained["source_ids"][source_name] = value
                    if source_name == "doi" and not retained.get("doi"):
                        retained["doi"] = value
                        retained["external_url"] = f"https://doi.org/{value}"
            mappings.append(
                {
                    "title": retained["title"],
                    "retained_track": retained["track"],
                    "retained_source_key": retained["source_ids"].get("icde_source_key"),
                    "duplicate_track": duplicate.get("track"),
                    "duplicate_source_key": duplicate_ids.get("icde_source_key"),
                    "match_basis": "exact_title",
                }
            )
        retained["quality_flags"] = _quality_flags(retained)
        output.append(retained)
    return output, mappings


def count_field(records: Sequence[Dict[str, Any]], key: str) -> Dict[str, int]:
    """Count normalized categorical values."""
    counts: Dict[str, int] = {}
    for record in records:
        value = normalize_text(record.get(key)) or "unknown"
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def collect_2026(
    *,
    output_root: Path,
    timeout: float,
    retries: int,
) -> Dict[str, Any]:
    """Collect all official ICDE 2026 tracks and reconcile the five duplicates."""
    collected_at = utc_now_iso()
    raw_records: List[Dict[str, Any]] = []
    raw_track_counts: Dict[str, int] = {}
    sources: List[str] = []
    group_coverage: List[Dict[str, Any]] = []
    for page, track, track_display, track_group, expected_count in ICDE_2026_TRACK_PAGES:
        url = f"{ICDE_2026_BASE_URL}{page}"
        sources.append(url)
        parsed = parse_accepted_page(fetch_text(url, timeout=timeout, retries=retries))
        if len(parsed) != expected_count:
            raise RuntimeError(
                f"ICDE 2026 {track} count changed: {len(parsed)} != {expected_count}"
            )
        raw_track_counts[track] = len(parsed)
        group_coverage.append(
            {"group": track, "url": url, "status": "covered", "raw_count": len(parsed)}
        )
        raw_records.extend(
            _build_record(
                item,
                track=track,
                track_display=track_display,
                track_group=track_group,
                source_url=url,
                collected_at=collected_at,
            )
            for item in parsed
        )

    sources.append(ICDE_2026_WORKSHOP_XML_URL)
    workshops = parse_workshop_proceedings(
        fetch_text(ICDE_2026_WORKSHOP_XML_URL, timeout=timeout, retries=retries)
    )
    if len(workshops) != 34:
        raise RuntimeError(f"ICDEW 2026 proceedings count changed: {len(workshops)} != 34")
    raw_track_counts["workshop"] = len(workshops)
    group_coverage.append(
        {
            "group": "workshop",
            "url": ICDE_2026_WORKSHOP_XML_URL,
            "official_proceedings_doi": ICDE_2026_WORKSHOP_PROCEEDINGS_DOI,
            "status": "covered",
            "raw_count": len(workshops),
        }
    )
    raw_records.extend(
        _build_record(
            item,
            track="workshop",
            track_display="Workshop Proceedings",
            track_group="workshop",
            source_url=f"https://doi.org/{ICDE_2026_WORKSHOP_PROCEEDINGS_DOI}",
            collected_at=collected_at,
        )
        for item in workshops
    )

    if len(raw_records) != 391:
        raise RuntimeError(f"ICDE 2026 raw count changed: {len(raw_records)} != 391")
    papers, dedupe_mappings = dedupe_tracks(raw_records)
    if len(papers) != 386 or len(dedupe_mappings) != 5:
        raise RuntimeError(
            f"ICDE 2026 dedupe changed: unique={len(papers)} mappings={len(dedupe_mappings)}"
        )
    payload = {
        "query": {
            "target": "ICDE-26",
            "venue_code": "ICDE",
            "year": 2026,
            "provider": "icde_2026_official_program_and_proceedings",
            "api_key_used": False,
            "work_filter_strategy": "all_official_tracks_plus_icdew_proceedings",
            "source_year_count_estimate": 386,
        },
        "source": {
            "provider": "icde_2026_official_program_and_proceedings",
            "display_name": "42nd IEEE International Conference on Data Engineering",
            "source_type": "conference",
            "official_url": ICDE_2026_BASE_URL,
            "urls": sources,
        },
        "generated_at_utc": collected_at,
        "paper_count": len(papers),
        "raw_track_counts": raw_track_counts,
        "unique_track_counts": count_field(papers, "track"),
        "track_counts": count_field(papers, "track"),
        "track_group_counts": count_field(papers, "track_group"),
        "presentation_level_counts": count_field(papers, "presentation_level"),
        "dedupe_mappings": dedupe_mappings,
        "group_coverage": {
            "expected_group_count": 8,
            "covered_group_count": len(group_coverage),
            "groups": group_coverage,
            "raw_record_count": len(raw_records),
            "unique_record_count": len(papers),
        },
        "held_records": [],
        "papers": papers,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / "ICDE-26.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sidecar = write_collection_result(
        output_root,
        outcome="collected",
        venue="ICDE",
        year=2026,
        sources=sources,
        reason="official_program_and_proceedings_complete",
        metrics={
            "group_coverage": payload["group_coverage"],
            **payload["group_coverage"],
            "raw_track_counts": raw_track_counts,
            "unique_track_counts": payload["unique_track_counts"],
            "dedupe_mapping_count": len(dedupe_mappings),
        },
    )
    missing_abstract_count = sum(
        1 for paper in papers if "missing_abstract" in (paper.get("quality_flags") or [])
    )
    return {
        "year": 2026,
        "official_entry_count": len(raw_records),
        "official_unique_count": len(papers),
        "collected_paper_count": len(papers),
        "missing_vs_official_unique": 0,
        "missing_openalex_count": 0,
        "missing_abstract_count": missing_abstract_count,
        "crossref_patched_count": 0,
        "dedupe_mapping_count": len(dedupe_mappings),
        "group_coverage": payload["group_coverage"],
        "output_file": str(output_path),
        "sidecar": str(sidecar),
        "generated_at_utc": collected_at,
    }
