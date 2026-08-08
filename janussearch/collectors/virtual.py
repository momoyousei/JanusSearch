#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect 2026 conference data from official virtual-event endpoints."""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

from janussearch.collectors.outcomes import write_collection_result
from janussearch.infrastructure.http import (
    HttpFetchError,
    decode_response_body,
    fetch_json,
    fetch_response,
)

LOGGER = logging.getLogger("janussearch.virtual_collect")

VENUE_HOSTS = {
    "AISTATS": "virtual.aistats.org",
    "ICLR": "iclr.cc",
    "ICML": "icml.cc",
    "NEURIPS": "neurips.cc",
    "CVPR": "cvpr.thecvf.com",
    "ECCV": "eccv.ecva.net",
}

OFFICIAL_CATALOG_EXPECTED_COUNTS = {
    ("AISTATS", 2026): 609,
    ("ICML", 2026): 6628,
}

class ApprovedPaginationIncomplete(RuntimeError):
    """A supported source returned a first page but forbade a later page."""

def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: Any) -> str:
    """Normalize one scalar string."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_title(value: Any) -> str:
    """Build a stable title key."""
    return normalize_text(value).casefold()


def fetch_text(url: str, *, timeout: float, retries: int) -> str:
    """Fetch one official HTML page with the shared retry policy."""
    response = fetch_response(url, timeout=timeout, retries=retries)
    return decode_response_body(response.body, response.headers).decode("utf-8", "replace")


def strip_html(value: Any) -> str:
    """Normalize one small trusted HTML fragment to plain text."""
    text = re.sub(r"<br\s*/?>", " ", str(value or ""), flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_text(html.unescape(text))


def parse_official_catalog(html_text: str, *, venue: str, year: int) -> list[dict[str, str]]:
    """Parse the complete official virtual paper directory."""
    pattern = re.compile(
        rf'<li>\s*<a\s+href="(?P<path>/virtual/{year}/poster/(?P<id>\d+))"[^>]*>'
        r"(?P<title>.*?)</a>\s*</li>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    host = VENUE_HOSTS[venue]
    records: dict[str, dict[str, str]] = {}
    for match in pattern.finditer(html_text):
        event_id = match.group("id")
        title = strip_html(match.group("title"))
        if not title:
            raise RuntimeError(f"Official catalog has empty title for event {event_id}")
        current = records.get(event_id)
        if current and normalize_title(current["title"]) != normalize_title(title):
            raise RuntimeError(f"Official catalog reuses event id {event_id} for two titles")
        records[event_id] = {
            "event_id": event_id,
            "title": title,
            "virtual_url": f"https://{host}{match.group('path')}",
        }
    return sorted(records.values(), key=lambda item: int(item["event_id"]))


def parse_official_detail(html_text: str) -> dict[str, Any]:
    """Parse title, authors, abstract and official links from a virtual detail page."""
    json_ld_match = re.search(
        r'<script\s+type="application/ld\+json">\s*(\{.*?\})\s*</script>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not json_ld_match:
        raise RuntimeError("Official virtual detail page has no CreativeWork JSON-LD")
    json_ld = json.loads(json_ld_match.group(1))
    authors = _names(json_ld.get("author"))
    title = normalize_text(json_ld.get("name"))
    abstract_match = re.search(
        r'<div\s+class="abstract-text-inner">(.*?)</div>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    abstract = strip_html(abstract_match.group(1)) if abstract_match else ""
    openreview_match = re.search(
        r'href="(https://openreview\.net/forum\?id=([A-Za-z0-9_-]+))"',
        html_text,
        flags=re.IGNORECASE,
    )
    event_type_match = re.search(
        r'<span\s+class="event-type-badge">(.*?)</span>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    keyword_match = re.search(
        r'<meta\s+name="keywords"\s+content="([^"]*)"',
        html_text,
        flags=re.IGNORECASE,
    )
    keywords = []
    if keyword_match:
        keywords = [
            normalize_text(value)
            for value in html.unescape(keyword_match.group(1)).split(",")
            if normalize_text(value)
        ]
    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "openreview_id": openreview_match.group(2) if openreview_match else "",
        "paper_url": openreview_match.group(1) if openreview_match else "",
        "event_type": strip_html(event_type_match.group(1)) if event_type_match else "Poster",
        "keywords": keywords,
    }


def _canonical_by_virtual_event(
    *, canonical_path: Path, venue: str
) -> dict[str, dict[str, Any]]:
    if not canonical_path.is_file():
        return {}
    payload = json.loads(canonical_path.read_text(encoding="utf-8"))
    key = f"{venue.lower()}_virtual_event_id"
    result: dict[str, dict[str, Any]] = {}
    for record in payload.get("papers", []):
        if not isinstance(record, dict):
            continue
        source_ids = record.get("source_ids")
        event_id = normalize_text(source_ids.get(key)) if isinstance(source_ids, dict) else ""
        if event_id:
            result[event_id] = record
    return result


def collect_official_catalog(
    venue: str,
    year: int,
    output: Path,
    *,
    timeout: float,
    retries: int,
    canonical_root: Path,
    workers: int = 20,
) -> dict[str, Any]:
    """Collect a complete official HTML catalog and only fetch missing details."""
    host = VENUE_HOSTS[venue]
    catalog_url = f"https://{host}/virtual/{year}/papers.html?filter=titles"
    catalog_html = fetch_text(catalog_url, timeout=timeout, retries=retries)
    stubs = parse_official_catalog(catalog_html, venue=venue, year=year)
    expected_count = OFFICIAL_CATALOG_EXPECTED_COUNTS.get((venue, year))
    if expected_count is not None and len(stubs) != expected_count:
        raise RuntimeError(
            f"Official catalog count changed for {venue} {year}: {len(stubs)} != {expected_count}"
        )

    canonical = _canonical_by_virtual_event(
        canonical_path=canonical_root / venue.lower() / f"{year}.json",
        venue=venue,
    )
    detail_by_id: dict[str, dict[str, Any]] = {}
    detail_stubs: list[dict[str, str]] = []
    reused_count = 0
    refreshed_count = 0
    retitle_mappings: list[dict[str, str]] = []
    for stub in stubs:
        old = canonical.get(stub["event_id"])
        old_complete = bool(
            old
            and normalize_text(old.get("abstract"))
            and bool(old.get("authors"))
            and normalize_text(old.get("record_status")).casefold() != "placeholder"
        )
        if old and normalize_title(old.get("title")) != normalize_title(stub["title"]):
            old_paper_id = normalize_text(old.get("paper_id"))
            if not old_paper_id:
                raise RuntimeError(
                    f"Canonical {venue} event {stub['event_id']} has no paper_id for retitle"
                )
            retitle_mappings.append(
                {
                    "source_event_id": stub["event_id"],
                    "old_paper_id": old_paper_id,
                    "old_title": normalize_text(old.get("title")),
                    "new_title": stub["title"],
                }
            )
        if old_complete:
            reused_count += 1
        else:
            # Existing placeholder records must be refreshed from the
            # authoritative detail page; reusing them would freeze the
            # missing abstract/author fields forever.
            if old:
                refreshed_count += 1
            detail_stubs.append(stub)

    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(fetch_text, stub["virtual_url"], timeout=timeout, retries=retries): stub
            for stub in detail_stubs
        }
        for future in as_completed(futures):
            stub = futures[future]
            try:
                detail = parse_official_detail(future.result())
                if normalize_title(detail.get("title")) != normalize_title(stub["title"]):
                    raise RuntimeError("detail title does not match catalog")
                detail_by_id[stub["event_id"]] = detail
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("%s detail fetch failed for %s: %s", venue, stub["event_id"], exc)
                failed.append(stub["event_id"])
    if failed:
        raise RuntimeError(
            f"Official catalog detail coverage incomplete for {venue} {year}: failed={len(failed)}"
        )

    collected_at = utc_now_iso()
    records: list[dict[str, Any]] = []
    for stub in stubs:
        event_id = stub["event_id"]
        old = canonical.get(event_id)
        old_complete = bool(
            old
            and normalize_text(old.get("abstract"))
            and bool(old.get("authors"))
            and normalize_text(old.get("record_status")).casefold() != "placeholder"
        )
        if old_complete:
            record = dict(old)
            record["title"] = stub["title"]
            record["paper_title"] = stub["title"]
            record["collected_at"] = collected_at
            record["source_provider"] = "official_virtual_catalog"
            source_ids = dict(record.get("source_ids") or {})
            source_ids[f"{venue.lower()}_virtual_event_id"] = event_id
            source_ids[f"{venue.lower()}_virtual_url"] = stub["virtual_url"]
            record["source_ids"] = source_ids
            records.append(record)
            continue
        detail = detail_by_id[event_id]
        item = {
            "id": event_id,
            "name": stub["title"],
            "authors": detail["authors"],
            "abstract": detail["abstract"],
            "keywords": detail["keywords"],
            "paper_url": detail["paper_url"],
            "openreview_id": detail["openreview_id"],
            "virtualsite_url": stub["virtual_url"],
            "eventtype": detail["event_type"],
            "sourceurl": (
                f"https://openreview.net/group?id=aistats.org/AISTATS/{year}/Conference"
                if venue == "AISTATS"
                else f"https://openreview.net/group?id={venue}.cc/{year}/Conference"
            ),
        }
        records.append(
            build_record(
                item,
                venue=venue,
                year=year,
                collected_at=collected_at,
                provider="official_virtual_catalog",
            )
        )

    records = dedupe_records(records)
    if len(records) != len(stubs):
        raise RuntimeError(
            f"Official catalog title uniqueness failed for {venue} {year}: {len(records)} != {len(stubs)}"
        )
    uniform_poster_policy = venue == "AISTATS" and year == 2026
    if uniform_poster_policy:
        for record in records:
            record["presentation_level"] = "poster"
    payload = {
        "query": {"venue_code": venue, "year": year, "provider": "official_virtual_catalog"},
        "source": {"provider": "official_virtual_catalog", "urls": [catalog_url]},
        "generated_at_utc": collected_at,
        "reconciliation": {
            "external_title_count": len(records),
            "retitle_mappings": retitle_mappings,
        },
        "group_coverage": {
            "expected_catalog_count": expected_count,
            "catalog_count": len(stubs),
            "canonical_records_reused": reused_count,
            "canonical_records_refreshed": refreshed_count,
            "retitle_mapping_count": len(retitle_mappings),
            "detail_pages_fetched": len(detail_stubs),
            "detail_pages_failed": len(failed),
            "detail_pages_missing_abstract": sum(
                not normalize_text(detail.get("abstract"))
                for detail in detail_by_id.values()
            ),
            "detail_pages_missing_authors": sum(
                not detail.get("authors") for detail in detail_by_id.values()
            ),
            "fallback_reason": (
                "official_virtual_catalog_uniform_poster_policy"
                if uniform_poster_policy
                else "official_virtual_catalog"
            ),
        },
        "papers": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sidecar = write_collection_result(
        output.parent,
        outcome="collected",
        venue=venue,
        year=year,
        sources=[catalog_url],
        reason="official_virtual_catalog_complete",
        metrics=payload["group_coverage"]
        | {
            "group_coverage": payload["group_coverage"],
            "paper_count": len(records),
        },
    )
    return {
        "outcome": "collected",
        "output": str(output),
        "sidecar": str(sidecar),
        "count": len(records),
    }


def _https_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
    return urlunparse(parsed)


def virtual_urls(venue: str, year: int) -> tuple[str, str]:
    """Return official events and abstracts endpoints."""
    host = VENUE_HOSTS[venue]
    slug = venue.lower()
    base = f"https://{host}/static/virtual/data/{slug}-{year}"
    return f"{base}-orals-posters.json", f"{base}-abstracts.json"


def fetch_complete_events(
    events_url: str,
    *,
    timeout: float,
    retries: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Follow same-origin pagination and reject declared-count shortfalls."""
    origin_host = (urlparse(events_url).hostname or "").lower()
    current_url = events_url
    seen_urls: set[str] = set()
    results: list[dict[str, Any]] = []
    declared_count: int | None = None
    while current_url:
        current_url = _https_url(current_url)
        if current_url in seen_urls:
            raise RuntimeError(f"Pagination loop detected at {current_url}")
        if (urlparse(current_url).hostname or "").lower() != origin_host:
            raise RuntimeError(f"Cross-origin pagination URL rejected: {current_url}")
        seen_urls.add(current_url)
        try:
            payload = fetch_json(current_url, timeout=timeout, retries=retries)
        except HttpFetchError as exc:
            if results and exc.status_code == 403:
                raise ApprovedPaginationIncomplete(
                    f"Pagination forbidden after {len(seen_urls) - 1} complete pages: {exc}"
                ) from exc
            raise
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise RuntimeError(f"Virtual endpoint has no results list: {current_url}")
        if declared_count is None and isinstance(payload.get("count"), int):
            declared_count = int(payload["count"])
        results.extend(item for item in payload["results"] if isinstance(item, dict))
        next_url = normalize_text(payload.get("next"))
        current_url = urljoin(current_url, next_url) if next_url else ""

    if declared_count is not None and declared_count != len(results):
        raise RuntimeError(
            f"Virtual source incomplete: declared={declared_count} fetched={len(results)}"
        )
    return results, {
        "declared_count": declared_count,
        "fetched_count": len(results),
        "pages": len(seen_urls),
    }


def merge_abstracts(
    events: Sequence[dict[str, Any]],
    abstracts_payload: Any,
) -> list[dict[str, Any]]:
    """Join the official abstract map by event id without replacing present text."""
    if not isinstance(abstracts_payload, dict):
        raise RuntimeError("Virtual abstracts payload is not an object")
    merged: list[dict[str, Any]] = []
    for event in events:
        item = dict(event)
        if not normalize_text(item.get("abstract")):
            item["abstract"] = normalize_text(abstracts_payload.get(str(item.get("id"))))
        merged.append(item)
    return merged


def _names(authors: Any) -> list[str]:
    result: list[str] = []
    if not isinstance(authors, list):
        return result
    for author in authors:
        name = (
            normalize_text(author.get("fullname") or author.get("name"))
            if isinstance(author, dict)
            else normalize_text(author)
        )
        if name and name.casefold() not in {value.casefold() for value in result}:
            result.append(name)
    return result


def _institutions(authors: Any, direct: Any = None) -> list[str]:
    result: list[str] = []
    if isinstance(direct, list):
        for institution in direct:
            value = normalize_text(institution)
            if value and value.casefold() not in {item.casefold() for item in result}:
                result.append(value)
    if not isinstance(authors, list):
        return result
    for author in authors:
        value = normalize_text(author.get("institution")) if isinstance(author, dict) else ""
        if value and value.casefold() not in {item.casefold() for item in result}:
            result.append(value)
    return result


def _openreview_id(item: Mapping[str, Any]) -> str:
    explicit = normalize_text(item.get("openreview_id"))
    if explicit:
        return explicit
    paper_url = normalize_text(item.get("paper_url") or item.get("openreview_url"))
    parsed = urlparse(paper_url)
    if "openreview.net" not in (parsed.hostname or "").lower():
        return ""
    return normalize_text((parse_qs(parsed.query).get("id") or [""])[0])


def _track(sourceurl: str) -> tuple[str, str, str]:
    """Map an official group URL to the canonical historical track contract.

    The historical ICML event feed uses a CMT source URL (or no source URL at
    all), while NeurIPS exposes several OpenReview groups in the same feed.
    Keeping this mapping here means the 404 abstract-endpoint fallback does
    not silently collapse adjunct tracks into the main conference.
    """
    lower = normalize_text(sourceurl).casefold()
    if "position_paper" in lower:
        return "position_paper_track", "Position Paper Track", "other"
    if "datasets_and_benchmarks" in lower:
        return "datasets_and_benchmarks_track", "Datasets and Benchmarks Track", "other"
    if "ml_reproducibility" in lower or "reproducibility_challenge" in lower:
        return "ml_reproducibility_challenge", "ML Reproducibility Challenge", "other"
    if "journal_track_tmlr" in lower or "tmlr" in lower:
        return "journal_track_tmlr", "Journal Track (TMLR)", "other"
    if "journal_track_jmlr" in lower or "jmlr" in lower:
        return "journal_track_jmlr", "Journal Track (JMLR)", "other"
    if "journal_track_rescience" in lower or "rescience" in lower:
        return "journal_track_rescience", "Journal Track (ReScience)", "other"
    if "journal_track_annals_of_statistics" in lower:
        return "journal_track_annals_of_statistics", "Journal Track (Annals of Statistics)", "other"
    if "journal_track" in lower:
        return "journal_track", "Journal Track", "other"
    if "conference" in lower:
        return "conference", "Conference", "main"
    if "cmt3.research.microsoft.com" in lower:
        return "main", "Main", "main"
    if not lower:
        # ICML 2021's official historical feed has no sourceurl and its
        # canonical corpus uses the historical ``main`` track.
        return "main", "Main", "main"
    return "other", "Other", "other"


def _presentation(decision: str, eventtype: str) -> str:
    text = f"{decision} {eventtype}".casefold()
    if "best" in text and "paper" in text:
        return "bestpaper"
    if "oral" in text or "spotlight" in text:
        return "oral"
    return "poster"


def build_record(
    item: Mapping[str, Any],
    *,
    venue: str,
    year: int,
    collected_at: str,
    provider: str,
) -> dict[str, Any]:
    """Transform one virtual event into the historical-input contract."""
    title = normalize_text(item.get("name") or item.get("title"))
    authors = _names(item.get("authors"))
    institutions = _institutions(item.get("authors"), item.get("institutions"))
    abstract = normalize_text(item.get("abstract"))
    keywords_raw = item.get("keywords")
    keywords = [normalize_text(value) for value in keywords_raw] if isinstance(keywords_raw, list) else []
    keywords = [value for value in keywords if value]
    sourceurl = normalize_text(item.get("sourceurl"))
    track, track_display, track_group = _track(sourceurl)
    openreview_id = _openreview_id(item)
    virtual_path = normalize_text(
        item.get("virtualsite_url") or item.get("virtual_url") or item.get("oral_url")
    )
    virtual_url = urljoin(f"https://{VENUE_HOSTS[venue]}", virtual_path) if virtual_path else ""
    paper_url = normalize_text(item.get("paper_url") or item.get("openreview_url"))
    pdf_url = normalize_text(item.get("paper_pdf_url") or item.get("pdf_url"))
    event_id = normalize_text(item.get("id"))
    source_ids: dict[str, str] = {}
    if event_id:
        source_ids[f"{venue.lower()}_virtual_event_id"] = event_id
    if sourceurl:
        source_ids[f"{venue.lower()}_sourceurl"] = sourceurl
    if openreview_id:
        source_ids["openreview_id"] = openreview_id
    flags: list[str] = []
    if not authors:
        flags.append("missing_authors")
    if not abstract:
        flags.append("missing_abstract")
    if not institutions:
        flags.append("missing_institutions")
    if not keywords:
        flags.append("missing_keywords")
    return {
        "paper_title": title,
        "title": title,
        "authors": authors,
        "institutions": institutions,
        "abstract": abstract,
        "keywords": keywords,
        "presentation_level": _presentation(
            normalize_text(item.get("decision")),
            normalize_text(item.get("eventtype") or item.get("event_type")),
        ),
        "track": track,
        "track_display_name": track_display,
        "track_group": track_group,
        "openreview_id": openreview_id or None,
        "doi": None,
        "url": virtual_url or paper_url or None,
        "external_url": pdf_url or paper_url or None,
        "venue": venue,
        "year": year,
        "source_provider": provider,
        "source_ids": source_ids,
        "record_status": "resolved" if authors and abstract else "placeholder",
        "quality_flags": flags,
        "collected_at": collected_at,
    }


def dedupe_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge repeated poster/oral presentations by normalized paper title."""
    unique: dict[str, dict[str, Any]] = {}
    level_rank = {"poster": 0, "oral": 1, "bestpaper": 2}
    for record in records:
        key = normalize_title(record.get("title"))
        if not key:
            raise RuntimeError("Virtual event has an empty paper title")
        current = unique.get(key)
        if current is None:
            unique[key] = record
            continue
        current_id = normalize_text(current.get("openreview_id"))
        new_id = normalize_text(record.get("openreview_id"))
        current_score = (
            bool(current_id and not current_id.startswith("2026-")),
            bool(current.get("abstract")),
            len(current.get("authors") or []),
        )
        new_score = (
            bool(new_id and not new_id.startswith("2026-")),
            bool(record.get("abstract")),
            len(record.get("authors") or []),
        )
        best_level = max(
            (normalize_text(current.get("presentation_level")), normalize_text(record.get("presentation_level"))),
            key=lambda value: level_rank.get(value, 0),
        )
        if new_score > current_score:
            unique[key] = record
        unique[key]["presentation_level"] = best_level
    return sorted(unique.values(), key=lambda item: normalize_title(item.get("title")))


def allowed_sourceurls(venue: str, year: int) -> set[str] | None:
    """Return conference-only OpenReview group URLs for one target."""
    if year < 2026 and venue in {"ICML", "NEURIPS"}:
        # Historical virtual feeds are the complete official event catalogs;
        # their sourceurl values include adjunct tracks which are part of the
        # existing corpus and must not be filtered out before deduplication.
        return None
    if venue == "ICLR":
        return {f"https://openreview.net/group?id=ICLR.cc/{year}/Conference"}
    if venue == "ICML":
        return {
            f"https://openreview.net/group?id=ICML.cc/{year}/Conference",
            f"https://openreview.net/group?id=ICML.cc/{year}/Position_Paper_Track",
        }
    if venue == "NEURIPS":
        return {f"https://openreview.net/group?id=NeurIPS.cc/{year}/Conference"}
    return None


def filter_allowed(
    venue: str, events: Sequence[dict[str, Any]], *, year: int = 2026
) -> list[dict[str, Any]]:
    """Filter adjunct publications out of conference corpora."""
    allowed = allowed_sourceurls(venue, year)
    if not allowed:
        return list(events)
    return [item for item in events if normalize_text(item.get("sourceurl")) in allowed]


def canonical_openreview_id(record: Mapping[str, Any]) -> str:
    """Read a canonical OpenReview id from either supported storage location."""
    direct = normalize_text(record.get("openreview_id"))
    source_ids = record.get("source_ids")
    nested = (
        normalize_text(source_ids.get("openreview_id"))
        if isinstance(source_ids, dict)
        else ""
    )
    return direct or nested


def collect_target(
    venue: str,
    year: int,
    output: Path,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    canonical_root: Path = Path("data/raw"),
) -> dict[str, Any]:
    """Collect one official virtual target or emit an explicit non-data outcome."""
    if venue not in VENUE_HOSTS:
        raise ValueError(f"Unsupported virtual venue: {venue}")
    events_url, abstracts_url = virtual_urls(venue, year)
    sources = [events_url, abstracts_url]
    output.parent.mkdir(parents=True, exist_ok=True)
    official_error: Exception | None = None
    historical_event_fallback = year < 2026 and venue in {"ICML", "NEURIPS"}
    provider = "official"
    page_metrics: dict[str, Any] = {}
    try:
        events, page_metrics = fetch_complete_events(events_url, timeout=timeout, retries=retries)
        if not events:
            sidecar = write_collection_result(
                output.parent,
                outcome="no_update",
                venue=venue,
                year=year,
                sources=sources,
                reason="official_virtual_source_has_zero_events",
                metrics=page_metrics,
            )
            return {"outcome": "no_update", "sidecar": str(sidecar), "count": 0}
        try:
            abstracts = fetch_json(abstracts_url, timeout=timeout, retries=retries)
        except HttpFetchError as exc:
            if not historical_event_fallback or exc.status_code != 404:
                raise
            # The historical event endpoint already carries authors and full
            # abstracts.  The separate abstract map was retired, so a 404 is
            # an approved source fallback rather than an incomplete catalog.
            official_error = exc
            provider = "official_historical_events"
        else:
            events = merge_abstracts(events, abstracts)
            provider = "official"
    except (ApprovedPaginationIncomplete, HttpFetchError, RuntimeError) as exc:
        official_error = exc
        if venue == "ICML" and year == 2026:
            try:
                return collect_official_catalog(
                    venue,
                    year,
                    output,
                    timeout=timeout,
                    retries=retries,
                    canonical_root=canonical_root,
                )
            except (HttpFetchError, RuntimeError, ValueError, OSError, json.JSONDecodeError) as catalog_error:
                sidecar = write_collection_result(
                    output.parent,
                    outcome="incomplete_source",
                    venue=venue,
                    year=year,
                    sources=[*sources, f"https://{VENUE_HOSTS[venue]}/virtual/{year}/papers.html"],
                    reason=f"official_json={official_error}; official_catalog={catalog_error}",
                    metrics=page_metrics,
                )
                raise RuntimeError(
                    f"Official ICML JSON and catalog sources are unusable; sidecar={sidecar}"
                ) from catalog_error
        else:
            sidecar = write_collection_result(
                output.parent,
                outcome="incomplete_source",
                venue=venue,
                year=year,
                sources=sources,
                reason=str(exc),
                metrics=page_metrics,
            )
            raise RuntimeError(f"Incomplete official virtual source; sidecar={sidecar}: {exc}") from exc

    filtered = filter_allowed(venue, events, year=year)
    collected_at = utc_now_iso()
    records = dedupe_records(
        build_record(
            item,
            venue=venue,
            year=year,
            collected_at=collected_at,
            provider=provider,
        )
        for item in filtered
    )
    if not records:
        sidecar = write_collection_result(
            output.parent,
            outcome="incomplete_source",
            venue=venue,
            year=year,
            sources=sources,
            reason="nonempty_source_became_empty_after_venue_filter",
            metrics={**page_metrics, "raw_events": len(events), "filtered_events": len(filtered)},
        )
        raise RuntimeError(f"Filtered virtual source is empty; sidecar={sidecar}")
    sourceurl_groups = sorted(
        {
            normalize_text(item.get("sourceurl")) or "<none>"
            for item in filtered
        }
    )
    payload = {
        "query": {"venue_code": venue, "year": year, "provider": provider},
        "source": {
            "provider": provider,
            "urls": sources,
            "official_error": str(official_error) if official_error else None,
        },
        "generated_at_utc": collected_at,
        "reconciliation": {
            "external_title_count": len(records),
            "raw_event_count": len(events),
            "filtered_event_count": len(filtered),
        },
        "group_coverage": {
            "expected_group_count": len(allowed_sourceurls(venue, year) or sourceurl_groups),
            "covered_group_count": len(sourceurl_groups),
            "groups": sourceurl_groups,
            "raw_event_count": len(events),
            "filtered_event_count": len(filtered),
            "unique_paper_count": len(records),
            "fallback_reason": (
                "historical_event_payload_used_after_abstract_endpoint_404"
                if provider == "official_historical_events"
                else "official_virtual_json"
            ),
        },
        "papers": records,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sidecar = write_collection_result(
        output.parent,
        outcome="collected",
        venue=venue,
        year=year,
        sources=sources,
        reason="official_virtual_complete",
        metrics={
            **page_metrics,
            "raw_events": len(events),
            "filtered_events": len(filtered),
            "paper_count": len(records),
            "source_provider": provider,
            "group_coverage": payload["group_coverage"],
        },
    )
    return {"outcome": "collected", "output": str(output), "sidecar": str(sidecar), "count": len(records)}


def parse_target(value: str) -> tuple[str, int]:
    """Parse VENUE-YEAR target syntax."""
    match = re.fullmatch(r"([A-Za-z0-9_]+)-(\d{4})", value.strip())
    if not match:
        raise ValueError(f"Invalid target: {value}")
    return match.group(1).upper(), int(match.group(2))


def main() -> int:
    """CLI entrypoint for registry target mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target")
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--canonical-root", default="data/raw")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    venue, year = parse_target(args.target)
    result = collect_target(
        venue,
        year,
        Path(args.output),
        timeout=args.timeout,
        retries=args.retries,
        canonical_root=Path(args.canonical_root),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
