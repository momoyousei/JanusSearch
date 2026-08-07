#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect 2026 conference data from official virtual-event endpoints."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

from janussearch.collectors.outcomes import write_collection_result
from janussearch.infrastructure.http import HttpFetchError, fetch_json

LOGGER = logging.getLogger("janussearch.virtual_collect")

PINNED_ICML_COMMIT = "2cf625b555c51e61086a3b009c59d47e768466cf"
PINNED_ICML_SHA256 = "73b6c52566255c85761977cc3f423739ef54deebc1befa7b8b79eb9f5cf3ac1a"
PINNED_ICML_URL = (
    "https://raw.githubusercontent.com/gisbi-kim/icml2026-explorer/"
    f"{PINNED_ICML_COMMIT}/output/icml2026_papers.json"
)

VENUE_HOSTS = {
    "ICLR": "iclr.cc",
    "ICML": "icml.cc",
    "NEURIPS": "neurips.cc",
    "CVPR": "cvpr.thecvf.com",
    "ECCV": "eccv.ecva.net",
}

def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def normalize_text(value: Any) -> str:
    """Normalize one scalar string."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_title(value: Any) -> str:
    """Build a stable title key."""
    return normalize_text(value).casefold()


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
        payload = fetch_json(current_url, timeout=timeout, retries=retries)
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
        name = normalize_text(author.get("fullname")) if isinstance(author, dict) else normalize_text(author)
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
    if sourceurl.endswith("/Position_Paper_Track"):
        return "position_paper_track", "Position Paper Track", "other"
    return "conference", "Conference", "main"


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
    virtual_url = urljoin(
        f"https://{VENUE_HOSTS[venue]}",
        normalize_text(item.get("virtualsite_url") or item.get("virtual_url") or item.get("oral_url")),
    )
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


def pinned_paper_list(payload: Any) -> list[dict[str, Any]]:
    """Extract papers from the pinned repository's versioned payload shape."""
    records = payload.get("papers") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise RuntimeError("Pinned ICML snapshot has no papers list")
    return [item for item in records if isinstance(item, dict)]


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


def load_icml_pinned_snapshot(
    *, timeout: float, retries: int, canonical_path: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load the one approved ICML snapshot and verify its identity and subset rule."""
    from janussearch.infrastructure.http import decode_response_body, fetch_response

    response = fetch_response(PINNED_ICML_URL, timeout=timeout, retries=retries)
    snapshot_bytes = decode_response_body(response.body, response.headers)
    digest = hashlib.sha256(snapshot_bytes).hexdigest()
    if digest != PINNED_ICML_SHA256:
        raise RuntimeError(f"Pinned ICML snapshot SHA-256 mismatch: {digest}")
    payload = json.loads(snapshot_bytes.decode("utf-8"))
    filtered = filter_allowed("ICML", pinned_paper_list(payload), year=2026)
    snapshot_ids = {_openreview_id(item) for item in filtered}
    if "" in snapshot_ids:
        raise RuntimeError("Pinned ICML snapshot contains a record without OpenReview id")
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    canonical_ids = {
        canonical_openreview_id(record)
        for record in canonical.get("papers", [])
        if isinstance(record, dict) and canonical_openreview_id(record)
    }
    third_party_only = sorted(snapshot_ids - canonical_ids)
    if third_party_only:
        raise RuntimeError(f"Pinned ICML snapshot has {len(third_party_only)} non-canonical ids")
    if len(snapshot_ids) != 6559:
        raise RuntimeError(f"Pinned ICML filtered count changed: {len(snapshot_ids)} != 6559")
    return filtered, {
        "commit": PINNED_ICML_COMMIT,
        "sha256": digest,
        "filtered_count": len(filtered),
        "unique_openreview_ids": len(snapshot_ids),
        "third_party_only_ids": len(third_party_only),
    }


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
        abstracts = fetch_json(abstracts_url, timeout=timeout, retries=retries)
        events = merge_abstracts(events, abstracts)
        provider = "official"
    except (HttpFetchError, RuntimeError) as exc:
        official_error = exc
        if venue != "ICML" or year != 2026:
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
        try:
            events, pinned_metrics = load_icml_pinned_snapshot(
                timeout=timeout,
                retries=retries,
                canonical_path=canonical_root / "icml" / "2026.json",
            )
        except (HttpFetchError, RuntimeError, ValueError, OSError, json.JSONDecodeError) as pinned_error:
            sidecar = write_collection_result(
                output.parent,
                outcome="incomplete_source",
                venue=venue,
                year=year,
                sources=[*sources, PINNED_ICML_URL],
                reason=f"official={official_error}; pinned={pinned_error}",
                metrics=page_metrics,
            )
            raise RuntimeError(
                f"Official and pinned ICML sources are unusable; sidecar={sidecar}"
            ) from pinned_error
        sources.append(PINNED_ICML_URL)
        page_metrics["official_incomplete"] = str(official_error)
        page_metrics["pinned_snapshot"] = pinned_metrics
        provider = "icml_virtual_pinned_snapshot"

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
        "papers": records,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sidecar = write_collection_result(
        output.parent,
        outcome="collected",
        venue=venue,
        year=year,
        sources=sources,
        reason="official_virtual_complete" if official_error is None else "approved_pinned_snapshot_after_official_pagination_failure",
        metrics={
            **page_metrics,
            "raw_events": len(events),
            "filtered_events": len(filtered),
            "paper_count": len(records),
            "source_provider": provider,
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
