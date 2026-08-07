#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect PVLDB 2026 from the official Volume 19 Next.js payload."""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from janussearch.collectors.outcomes import write_collection_result
from janussearch.infrastructure.http import fetch_text
from tools.dblp_expand_collect import (
    VENUE_CONFIG,
    build_dblp_url_candidates,
    collect_one_year as collect_legacy_year,
    dedupe_records,
    fetch_text_from_candidates,
    parse_dblp_xml_records,
    parse_years,
)

LOGGER = logging.getLogger("pvldb_collect")
PVLDB_VOLUME_URL = "https://www.vldb.org/pvldb/volumes/19"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


def normalize_text(value: Any) -> str:
    """Normalize whitespace for matching and output."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def title_key(value: Any) -> str:
    """Build an exact normalized title key."""
    text = normalize_text(value).casefold()
    return text.rstrip(" .:;")


def parse_next_data(html: str) -> dict[str, Any]:
    """Extract and validate the official PVLDB page payload."""
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise RuntimeError("PVLDB page has no __NEXT_DATA__ payload")
    payload = json.loads(match.group(1))
    page_props = payload.get("props", {}).get("pageProps", {})
    if not isinstance(page_props.get("volumeSummaries"), list):
        raise RuntimeError("PVLDB __NEXT_DATA__ has no volumeSummaries list")
    if not isinstance(page_props.get("groupedIssues"), dict):
        raise RuntimeError("PVLDB __NEXT_DATA__ has no groupedIssues object")
    return page_props


def split_authors(value: Any) -> list[str]:
    """Parse comma/and separated author display text."""
    text = normalize_text(value)
    if not text:
        return []
    return [part.strip() for part in re.split(r",\s*|\s+and\s+", text) if part.strip()]


def official_records(
    page_props: dict[str, Any], *, minimum_count: int = 135
) -> list[dict[str, Any]]:
    """Join summaries with issue/page metadata and exclude Front Matter."""
    issue_by_title: dict[str, dict[str, Any]] = {}
    for issue_items in page_props["groupedIssues"].values():
        if not isinstance(issue_items, list):
            continue
        for item in issue_items:
            if isinstance(item, dict):
                issue_by_title[title_key(item.get("title"))] = item
    records: list[dict[str, Any]] = []
    for summary in page_props["volumeSummaries"]:
        if not isinstance(summary, dict):
            continue
        title = normalize_text(summary.get("Paper Title"))
        abstract = normalize_text(summary.get("Abstract"))
        if title_key(title) == "front matter":
            continue
        issue_item = issue_by_title.get(title_key(title))
        if issue_item is None:
            raise RuntimeError(f"PVLDB issue metadata missing for title: {title}")
        authors = split_authors(summary.get("Author Names") or issue_item.get("authors"))
        if not authors or not abstract:
            raise RuntimeError(f"PVLDB official record missing authors/abstract: {title}")
        records.append(
            {
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "paper_id_source": normalize_text(summary.get("Paper ID")),
                "issue": int(issue_item.get("issue")),
                "start_page": issue_item.get("start_page"),
                "end_page": issue_item.get("end_page"),
                "pdf": normalize_text(issue_item.get("pdf")),
            }
        )
    if len(records) < minimum_count or len({title_key(item["title"]) for item in records}) != len(records):
        raise RuntimeError(f"PVLDB paper count/uniqueness gate failed: {len(records)}")
    return records


def collect_year(
    year: int,
    output_root: Path,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    min_interval: float = 0.5,
) -> dict[str, Any]:
    """Collect PVLDB Volume 19 and supplement identifiers from DBLP."""
    if year != 2026:
        raise ValueError("Dedicated PVLDB collector currently supports year 2026 only")
    output_root.mkdir(parents=True, exist_ok=True)
    page_props = parse_next_data(
        fetch_text(PVLDB_VOLUME_URL, timeout=timeout, retries=retries, min_interval=min_interval)
    )
    official = official_records(page_props)
    xml_path = "/db/journals/pvldb/pvldb19.xml"
    xml_text, xml_url = fetch_text_from_candidates(
        urls=build_dblp_url_candidates(xml_path),
        timeout=timeout,
        retries=retries,
        min_interval=min_interval,
    )
    dblp = dedupe_records(
        parse_dblp_xml_records(
            xml_text=xml_text,
            xml_url=xml_url,
            venue="VLDB",
            year=year,
            mode="pvldb",
        )
    )
    dblp_by_title = {title_key(item.get("title")): item for item in dblp}
    collected_at = datetime.now(timezone.utc).isoformat()
    papers: list[dict[str, Any]] = []
    matched = 0
    for item in official:
        supplement = dblp_by_title.get(title_key(item["title"]))
        if supplement:
            matched += 1
        source_ids = {
            "pvldb_paper_id": item["paper_id_source"],
            "pvldb_issue": str(item["issue"]),
        }
        doi = None
        url = item["pdf"] or None
        if supplement:
            doi = supplement.get("doi")
            if supplement.get("dblp_key"):
                source_ids["dblp_key"] = supplement["dblp_key"]
            if supplement.get("rec_url"):
                source_ids["dblp_url"] = supplement["rec_url"]
        papers.append(
            {
                "paper_title": item["title"],
                "title": item["title"],
                "authors": item["authors"],
                "institutions": [],
                "abstract": item["abstract"],
                "keywords": [],
                "presentation_level": "poster",
                "track": "main",
                "track_display_name": "Main",
                "track_group": "main",
                "doi": doi,
                "url": url,
                "external_url": url,
                "venue": "VLDB",
                "year": year,
                "source_provider": "pvldb_official",
                "source_ids": source_ids,
                "record_status": "resolved",
                "quality_flags": ["missing_institutions", "missing_keywords"],
                "collected_at": collected_at,
            }
        )
    if matched < 77:
        raise RuntimeError(f"PVLDB DBLP supplement regressed: matched={matched} expected_at_least=77")
    output = output_root / "VLDB-26.json"
    payload = {
        "query": {"venue_code": "VLDB", "year": year, "provider": "pvldb_official"},
        "source": {"provider": "pvldb_official", "urls": [PVLDB_VOLUME_URL, xml_url]},
        "generated_at_utc": collected_at,
        "reconciliation": {
            "external_title_count": len(papers),
            "front_matter_excluded": len(page_props["volumeSummaries"]) - len(papers),
            "dblp_matches": matched,
            "official_only": len(papers) - matched,
        },
        "papers": papers,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sidecar = write_collection_result(
        output_root,
        outcome="collected",
        venue="VLDB",
        year=year,
        sources=[PVLDB_VOLUME_URL, xml_url],
        reason="pvldb_volume_19_live_partial_year",
        metrics={"paper_count": len(papers), "dblp_matches": matched, "partial": True},
    )
    return {"output": str(output), "sidecar": str(sidecar), "count": len(papers), "dblp_matches": matched}


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", required=True)
    parser.add_argument("--output-root", default="archives/root_json")
    parser.add_argument("--index-root", default="artifacts")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--min-interval", type=float, default=0.5)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    years = parse_years(args.years)
    output_root = Path(args.output_root)
    results: list[dict[str, Any]] = []
    for year in years:
        if year == 2026:
            try:
                results.append(
                    collect_year(
                        year,
                        output_root,
                        timeout=args.timeout,
                        retries=args.retries,
                        min_interval=args.min_interval,
                    )
                )
            except Exception as exc:
                write_collection_result(
                    output_root,
                    outcome="incomplete_source",
                    venue="VLDB",
                    year=year,
                    sources=[PVLDB_VOLUME_URL],
                    reason=str(exc),
                )
                raise
        else:
            results.append(
                collect_legacy_year(
                    venue="VLDB",
                    config=VENUE_CONFIG["VLDB"],
                    year=year,
                    output_root=output_root,
                    timeout=args.timeout,
                    retries=args.retries,
                    min_interval=args.min_interval,
                    openalex_chunk_size=20,
                    openalex_workers=8,
                    crossref_workers=6,
                )
            )
    print(json.dumps({"items": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
