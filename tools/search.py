#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M2-B/M3 search CLI: SQL+FTS and hybrid retrieval over local SQLite database."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import re
import socket
import sqlite3
import time
from html import unescape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("search_cli")

DEFAULT_DB_PATH = Path("data/papers.db")
DEFAULT_VECTORS_ROOT = Path("data/vectors/chroma")
DEFAULT_COLLECTION_NAME = "papers_v1"
DEFAULT_EMBED_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_EMBED_MODEL = "Qwen/Qwen3-Embedding-8B"

DEFAULT_TOP_K = 20
DEFAULT_OFFSET = 0
DEFAULT_EXPORT_MAX = 2000
DEFAULT_TOPICS_JSON = Path("artifacts/m3/topic_assignments.json")
DEFAULT_PDF_OUTPUT_DIR = Path("artifacts/pdfs")
DEFAULT_PDF_REPORT_NAME = "pdf_download_report.json"
DEFAULT_FAILED_TSV_NAME = "failed.tsv"
DEFAULT_HTTP_TIMEOUT = 30.0
DEFAULT_HTTP_RETRIES = 3
MAX_TITLE_SLUG_LENGTH = 120
PDF_DIRECT_SOURCE_KEYS = (
    "cvf_pdf_url",
    "openreview_pdf_url",
    "aaai_pdf_url",
    "ijcai_pdf_url",
    "aistats_pdf_url",
    "acl_pdf_url",
    "pmlr_pdf_url",
    "tpami_pdf_url",
    "kdd_pdf_url",
    "arxiv_pdf_url",
    "pdf_url",
)
FAILED_DOWNLOAD_COLUMNS = [
    "paper_id",
    "title",
    "resolver",
    "resolved_pdf_url",
    "file_path",
    "error",
]

EXPORT_COLUMNS = [
    # Query/export metadata
    "rank",
    "matched_topic",
    "matched_keyword",
    "janus_topic",
    "janus_subtopic",
    # Base paper fields (papers table)
    "paper_id",
    "title",
    "venue",
    "year",
    "abstract",
    "doi",
    "url",
    "citation_count",
    "source_provider",
    "track",
    "track_display_name",
    "track_group",
    "presentation_level",
    "record_status",
    "collected_at",
    "source_file",
    "ingested_at_utc",
    # Related relations (joined as single cell strings / JSON)
    "authors",
    "keywords",
    "institutions",
    "quality_flags",
    "source_ids_json",
]


def ensure_str(value: Any) -> str:
    """Convert value to stripped text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def connect_db(db_path: Path) -> sqlite3.Connection:
    """Open SQLite connection and enforce row mapping."""
    if not db_path.exists():
        raise FileNotFoundError(
            f"Database does not exist: {db_path}. Run `python3 -m tools.m2_db run` first."
        )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def fts_table_exists(conn: sqlite3.Connection) -> bool:
    """Check whether papers_fts virtual table exists."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='papers_fts'"
    ).fetchone()
    return row is not None


def ensure_fts_ready(conn: sqlite3.Connection) -> None:
    """Ensure FTS table exists before search."""
    if not fts_table_exists(conn):
        raise RuntimeError(
            "FTS table `papers_fts` is missing. Run `python3 -m tools.m2_db reindex-fts`."
        )


def parse_csv_values(raw: str | None, uppercase: bool = False) -> List[str]:
    """Parse comma-separated values into clean list."""
    if not raw:
        return []
    out: List[str] = []
    for value in raw.split(","):
        text = ensure_str(value)
        if not text:
            continue
        out.append(text.upper() if uppercase else text)
    return out


def normalize_fts_query(query: str) -> str:
    """Convert raw text into safe FTS MATCH query."""
    cleaned = ensure_str(query)
    if not cleaned:
        raise ValueError("`--query` must not be empty.")

    # Keep lexical tokens to avoid FTS special-operator parse failures.
    tokens = re.findall(r"\w+", cleaned, flags=re.UNICODE)
    if not tokens:
        raise ValueError("Query contains no searchable tokens.")
    return " ".join(tokens)


def normalize_openai_base_url(raw_url: str) -> str:
    """Normalize OpenAI-compatible base URL.

    Accepts full embedding endpoint URLs like .../v1/embeddings and normalizes to .../v1.
    """
    base = ensure_str(raw_url).rstrip("/")
    if not base:
        raise ValueError("Embedding base URL must not be empty.")
    lowered = base.lower()
    if lowered.endswith("/v1/embeddings"):
        return base[: -len("/embeddings")]
    if lowered.endswith("/embeddings"):
        return base[: -len("/embeddings")]
    return base


def _build_filters(
    *,
    venues: Sequence[str],
    year_from: int | None,
    year_to: int | None,
    track: str | None,
    presentation_level: str | None,
    include_placeholder: bool,
) -> Tuple[str, List[Any]]:
    """Build SQL WHERE clauses and bound parameters."""
    clauses: List[str] = []
    params: List[Any] = []

    if venues:
        placeholders = ",".join(["?"] * len(venues))
        clauses.append(f"p.venue IN ({placeholders})")
        params.extend(venues)

    if year_from is not None:
        clauses.append("p.year >= ?")
        params.append(year_from)

    if year_to is not None:
        clauses.append("p.year <= ?")
        params.append(year_to)

    if track:
        clauses.append("p.track = ?")
        params.append(track)

    if presentation_level:
        clauses.append("p.presentation_level = ?")
        params.append(presentation_level)

    if not include_placeholder:
        clauses.append("p.record_status != 'placeholder'")

    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


def _fetch_ordered_values(
    conn: sqlite3.Connection,
    *,
    table: str,
    value_column: str,
    order_column: str,
    paper_ids: Sequence[str],
) -> Dict[str, List[str]]:
    """Fetch ordered list values grouped by paper_id."""
    if not paper_ids:
        return {}
    placeholders = ",".join(["?"] * len(paper_ids))
    rows = conn.execute(
        f"""
        SELECT paper_id, {value_column} AS value
        FROM {table}
        WHERE paper_id IN ({placeholders})
        ORDER BY paper_id, {order_column}
        """,  # noqa: S608
        tuple(paper_ids),
    ).fetchall()
    grouped: Dict[str, List[str]] = {}
    for row in rows:
        key = ensure_str(row["paper_id"])
        grouped.setdefault(key, []).append(ensure_str(row["value"]))
    return grouped


def _fetch_source_ids(conn: sqlite3.Connection, paper_ids: Sequence[str]) -> Dict[str, Dict[str, str]]:
    """Fetch source key-values grouped by paper_id."""
    if not paper_ids:
        return {}
    placeholders = ",".join(["?"] * len(paper_ids))
    rows = conn.execute(
        f"""
        SELECT paper_id, source_key, source_value
        FROM paper_source_ids
        WHERE paper_id IN ({placeholders})
        ORDER BY paper_id, source_key
        """,  # noqa: S608
        tuple(paper_ids),
    ).fetchall()
    grouped: Dict[str, Dict[str, str]] = {}
    for row in rows:
        pid = ensure_str(row["paper_id"])
        grouped.setdefault(pid, {})[ensure_str(row["source_key"])] = ensure_str(
            row["source_value"]
        )
    return grouped


def _fetch_paper_rows(conn: sqlite3.Connection, paper_ids: Sequence[str]) -> Dict[str, sqlite3.Row]:
    """Fetch base paper fields for given paper IDs."""
    if not paper_ids:
        return {}
    placeholders = ",".join(["?"] * len(paper_ids))
    rows = conn.execute(
        f"""
        SELECT
            p.paper_id,
            p.title,
            p.venue,
            p.year,
            p.track,
            p.presentation_level,
            p.record_status,
            p.citation_count
        FROM papers p
        WHERE p.paper_id IN ({placeholders})
        """,  # noqa: S608
        tuple(paper_ids),
    ).fetchall()
    return {ensure_str(row["paper_id"]): row for row in rows}


def _fetch_full_paper_rows(conn: sqlite3.Connection, paper_ids: Sequence[str]) -> Dict[str, sqlite3.Row]:
    """Fetch full paper rows from papers table for given paper IDs."""
    if not paper_ids:
        return {}
    placeholders = ",".join(["?"] * len(paper_ids))
    rows = conn.execute(
        f"""
        SELECT *
        FROM papers
        WHERE paper_id IN ({placeholders})
        """,  # noqa: S608
        tuple(paper_ids),
    ).fetchall()
    return {ensure_str(row["paper_id"]): row for row in rows}


def _sanitize_tsv_value(value: Any) -> str:
    """Sanitize a value for TSV output as single-line text."""
    text = ensure_str(value)
    if not text:
        return ""
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _load_keyword_definition(
    keywords_json: Path,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Load keyword groups plus optional grouped-query audit settings."""
    if not keywords_json.exists():
        raise FileNotFoundError(f"keywords_json does not exist: {keywords_json}")
    payload = json.loads(keywords_json.read_text(encoding="utf-8"))
    groups = payload.get("keywords", [])
    if not isinstance(groups, list):
        raise ValueError("Invalid keywords_json schema: `keywords` must be a list.")
    normalized: List[Dict[str, Any]] = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        label = ensure_str(item.get("label"))
        aliases_raw = item.get("aliases", [])
        if not label or not isinstance(aliases_raw, list):
            continue
        aliases = [ensure_str(alias) for alias in aliases_raw if ensure_str(alias)]
        if not aliases:
            continue
        normalized.append({"label": label, "aliases": aliases})
    if not normalized:
        raise ValueError("No valid keyword groups found in keywords_json.")

    candidate_queries_raw = payload.get("candidate_queries", [])
    if candidate_queries_raw is None:
        candidate_queries_raw = []
    if not isinstance(candidate_queries_raw, list):
        raise ValueError("Invalid keywords_json schema: `candidate_queries` must be a list.")
    candidate_queries = list(
        dict.fromkeys(ensure_str(value) for value in candidate_queries_raw if ensure_str(value))
    )

    required_labels_raw = payload.get("required_labels", [])
    if required_labels_raw is None:
        required_labels_raw = []
    if not isinstance(required_labels_raw, list):
        raise ValueError("Invalid keywords_json schema: `required_labels` must be a list.")
    required_labels = list(
        dict.fromkeys(ensure_str(value) for value in required_labels_raw if ensure_str(value))
    )
    if bool(candidate_queries) != bool(required_labels):
        raise ValueError(
            "Invalid keywords_json schema: `candidate_queries` and `required_labels` "
            "must either both be non-empty or both be omitted."
        )
    known_labels = {ensure_str(group.get("label")) for group in normalized}
    unknown_labels = [label for label in required_labels if label not in known_labels]
    if unknown_labels:
        raise ValueError(f"required_labels not found in keywords groups: {unknown_labels}")
    return normalized, candidate_queries, required_labels


def _load_keyword_groups(keywords_json: Path) -> List[Dict[str, Any]]:
    """Load keyword groups while preserving the historical helper contract."""
    groups, _candidate_queries, _required_labels = _load_keyword_definition(keywords_json)
    return groups


def _match_topic_label(
    *,
    title: str,
    abstract: str,
    keywords: Sequence[str],
    keyword_groups: Sequence[Dict[str, Any]],
) -> Tuple[str, str]:
    """Match a paper to the first keyword group by substring search."""
    text = " ".join(
        [
            ensure_str(title),
            ensure_str(abstract),
            " ".join(ensure_str(item) for item in keywords if ensure_str(item)),
        ]
    ).casefold()
    if not text.strip():
        return "Other", ""
    for group in keyword_groups:
        label = ensure_str(group.get("label"))
        aliases = group.get("aliases", [])
        if not label or not isinstance(aliases, list):
            continue
        for alias in aliases:
            alias_text = ensure_str(alias)
            if not alias_text:
                continue
            if alias_text.casefold() in text:
                return label, alias_text
    return "Other", ""


def _matches_required_labels(
    *,
    title: str,
    abstract: str,
    keywords: Sequence[str],
    keyword_groups: Sequence[Dict[str, Any]],
    required_labels: Sequence[str],
) -> bool:
    """Require at least one deterministic alias hit from every requested label."""
    if not required_labels:
        return True
    text = " ".join(
        [
            ensure_str(title),
            ensure_str(abstract),
            " ".join(ensure_str(item) for item in keywords if ensure_str(item)),
        ]
    ).casefold()
    by_label = {
        ensure_str(group.get("label")): [
            ensure_str(alias).casefold()
            for alias in group.get("aliases", [])
            if ensure_str(alias)
        ]
        for group in keyword_groups
    }
    return all(
        any(alias in text for alias in by_label.get(label, []))
        for label in required_labels
    )


def _load_janus_topic_map(
    *,
    topics_json: Path,
    paper_ids: Sequence[str],
) -> Dict[str, Tuple[str, str]]:
    """Load paper_id -> (topic_name, subtopic_name) map from M3 topic assignments JSON."""
    if not topics_json.exists():
        return {}
    payload = json.loads(topics_json.read_text(encoding="utf-8"))
    assignments = payload.get("assignments", [])
    if not isinstance(assignments, list):
        raise ValueError("Invalid topics_json schema: `assignments` must be a list.")

    wanted = {ensure_str(pid) for pid in paper_ids if ensure_str(pid)}
    if not wanted:
        return {}
    mapping: Dict[str, Tuple[str, str]] = {}
    for item in assignments:
        if not isinstance(item, dict):
            continue
        pid = ensure_str(item.get("paper_id"))
        if not pid or pid not in wanted or pid in mapping:
            continue
        mapping[pid] = (
            ensure_str(item.get("topic_name")),
            ensure_str(item.get("subtopic_name")),
        )
        if len(mapping) >= len(wanted):
            break
    return mapping


def _dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    """Deduplicate non-empty strings while preserving original order."""
    seen: set[str] = set()
    normalized: List[str] = []
    for value in values:
        text = ensure_str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _normalize_source_ids(raw: Any) -> Dict[str, str]:
    """Normalize source_ids payload into a compact string map."""
    if not isinstance(raw, dict):
        return {}
    normalized: Dict[str, str] = {}
    for key, value in raw.items():
        key_text = ensure_str(key)
        value_text = ensure_str(value)
        if key_text and value_text:
            normalized[key_text] = value_text
    return normalized


def _parse_source_ids_json(raw: Any) -> Dict[str, str]:
    """Parse source_ids_json column from TSV export."""
    text = ensure_str(raw)
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        LOGGER.warning("Ignoring malformed source_ids_json: %s", text[:120])
        return {}
    return _normalize_source_ids(payload)


def _slugify_title(title: str) -> str:
    """Normalize title text for filesystem-safe PDF names."""
    slug = re.sub(r"[^0-9a-z]+", "_", ensure_str(title).casefold())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        return "paper"
    return slug[:MAX_TITLE_SLUG_LENGTH].rstrip("_") or "paper"


def _build_pdf_filename(*, paper_id: str, title: str) -> str:
    """Build deterministic PDF filename for one paper."""
    return f"{paper_id}__{_slugify_title(title)}.pdf"


def _build_http_request(url: str, *, accept: str) -> Request:
    """Build HTTP GET request with stable headers."""
    return Request(
        url,
        headers={
            "User-Agent": "JanusSearch/1.0 (paper-pdf-downloader)",
            "Accept": accept,
        },
        method="GET",
    )


def _sleep_before_retry(attempt: int) -> None:
    """Sleep with bounded exponential backoff."""
    wait_seconds = min(10.0, 1.2 * (2 ** max(attempt - 1, 0)))
    time.sleep(wait_seconds)


def _is_http_url(url: str) -> bool:
    """Check whether a URL is an absolute http(s) URL."""
    parsed = urlparse(ensure_str(url))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _resolve_direct_pdf_url(source_ids: Dict[str, str]) -> Tuple[str, str]:
    """Resolve direct PDF URL from source_ids map if present."""
    normalized = _normalize_source_ids(source_ids)
    for key in PDF_DIRECT_SOURCE_KEYS:
        value = ensure_str(normalized.get(key))
        if value and _is_http_url(value):
            return value, f"source_ids:{key}"
    for key, value in normalized.items():
        key_text = ensure_str(key).casefold()
        value_text = ensure_str(value)
        if key_text.endswith("_pdf_url") or key_text == "pdf_url":
            if value_text and _is_http_url(value_text):
                return value_text, f"source_ids:{key}"
    return "", ""


def _extract_openreview_id(url: str, source_ids: Dict[str, str]) -> str:
    """Extract OpenReview forum ID from source_ids or page URL."""
    normalized = _normalize_source_ids(source_ids)
    direct = ensure_str(normalized.get("openreview_id"))
    if direct:
        return direct
    parsed = urlparse(ensure_str(url))
    if "openreview.net" not in parsed.netloc.casefold():
        return ""
    query_id = ensure_str(parse_qs(parsed.query).get("id", [""])[0])
    if query_id:
        return query_id
    match = re.search(r"/(?:forum|pdf|attachment)\?id=([^&#]+)", ensure_str(url), flags=re.IGNORECASE)
    if match:
        return ensure_str(match.group(1))
    return ""


def _extract_arxiv_id(url: str, source_ids: Dict[str, str]) -> str:
    """Extract arXiv identifier from source_ids or page URL."""
    normalized = _normalize_source_ids(source_ids)
    direct = ensure_str(normalized.get("arxiv_id"))
    if direct:
        return direct
    parsed = urlparse(ensure_str(url))
    if "arxiv.org" not in parsed.netloc.casefold():
        return ""
    match = re.search(r"/(?:abs|pdf)/([^/?#]+)", parsed.path, flags=re.IGNORECASE)
    if not match:
        return ""
    identifier = ensure_str(match.group(1))
    if identifier.casefold().endswith(".pdf"):
        identifier = identifier[:-4]
    return identifier


def _resolve_pdf_url_from_page_url(url: str) -> Tuple[str, str]:
    """Resolve PDF URL from known official page URL patterns without extra requests."""
    candidate = ensure_str(url)
    if not candidate or not _is_http_url(candidate):
        return "", ""
    parsed = urlparse(candidate)
    host = parsed.netloc.casefold()
    path = parsed.path

    if path.casefold().endswith(".pdf"):
        return candidate, "url:direct_pdf"

    if "openreview.net" in host:
        openreview_id = _extract_openreview_id(candidate, {})
        if openreview_id:
            return f"https://openreview.net/pdf?id={quote(openreview_id)}", "url:openreview"

    if "arxiv.org" in host:
        arxiv_id = _extract_arxiv_id(candidate, {})
        if arxiv_id:
            return f"https://arxiv.org/pdf/{quote(arxiv_id)}.pdf", "url:arxiv"

    if "openaccess.thecvf.com" in host and re.search(r"/html/.*\.html$", path, flags=re.IGNORECASE):
        pdf_path = re.sub(r"/html/", "/papers/", path, count=1, flags=re.IGNORECASE)
        pdf_path = re.sub(r"\.html$", ".pdf", pdf_path, flags=re.IGNORECASE)
        return parsed._replace(path=pdf_path, query="", fragment="").geturl(), "url:cvf_html"

    if "proceedings.mlr.press" in host and path.casefold().endswith(".html"):
        pdf_path = re.sub(r"\.html$", ".pdf", path, flags=re.IGNORECASE)
        return parsed._replace(path=pdf_path, query="", fragment="").geturl(), "url:pmlr_html"

    return "", ""


def _extract_pdf_url_from_html(html_text: str, *, base_url: str) -> Tuple[str, str]:
    """Extract PDF URL from HTML metadata or links."""
    meta_patterns = [
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
        r'<meta[^>]+property=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']citation_pdf_url["\']',
    ]
    for pattern in meta_patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            candidate = urljoin(base_url, unescape(ensure_str(match.group(1))))
            if _is_http_url(candidate):
                return candidate, "html:citation_pdf_url"

    for match in re.finditer(r'href=["\']([^"\']+?\.pdf(?:\?[^"\']*)?)["\']', html_text, flags=re.IGNORECASE):
        candidate = urljoin(base_url, unescape(ensure_str(match.group(1))))
        if _is_http_url(candidate):
            return candidate, "html:pdf_link"

    return "", ""


def _resolve_pdf_url_from_official_page(
    *,
    url: str,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
    retries: int = DEFAULT_HTTP_RETRIES,
) -> Tuple[str, str]:
    """Resolve PDF URL by fetching official page HTML or inspecting redirected content."""
    candidate = ensure_str(url)
    if not candidate or not _is_http_url(candidate):
        return "", ""

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = _build_http_request(
                candidate,
                accept="text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
            )
            with urlopen(request, timeout=timeout) as response:
                final_url = response.geturl()
                content_type = ensure_str(response.headers.get("Content-Type")).casefold()
                head = response.read(8192)
                if "application/pdf" in content_type or head.startswith(b"%PDF-"):
                    return final_url, "page:direct_pdf"
                body = head + response.read()
            html_text = body.decode("utf-8", errors="ignore")
            return _extract_pdf_url_from_html(html_text, base_url=final_url or candidate)
        except (HTTPError, URLError, TimeoutError, socket.timeout, ValueError) as err:
            last_error = err
            LOGGER.warning(
                "PDF page resolution failed (%s/%s) %s: %s",
                attempt,
                retries,
                candidate,
                err,
            )
            if attempt == retries:
                break
            _sleep_before_retry(attempt)

    if last_error:
        LOGGER.debug("Unable to resolve PDF from official page %s: %s", candidate, last_error)
    return "", ""


def _resolve_pdf_url(
    *,
    paper_id: str,
    title: str,
    url: str,
    source_ids: Dict[str, str],
    allow_page_fetch: bool = True,
) -> Tuple[str, str]:
    """Resolve one paper to a downloadable PDF URL."""
    direct_url, direct_resolver = _resolve_direct_pdf_url(source_ids)
    if direct_url:
        return direct_url, direct_resolver

    openreview_id = _extract_openreview_id(url, source_ids)
    if openreview_id:
        return f"https://openreview.net/pdf?id={quote(openreview_id)}", "derived:openreview_id"

    arxiv_id = _extract_arxiv_id(url, source_ids)
    if arxiv_id:
        return f"https://arxiv.org/pdf/{quote(arxiv_id)}.pdf", "derived:arxiv_id"

    page_url, page_resolver = _resolve_pdf_url_from_page_url(url)
    if page_url:
        return page_url, page_resolver

    if allow_page_fetch:
        fetched_url, fetched_resolver = _resolve_pdf_url_from_official_page(url=url)
        if fetched_url:
            return fetched_url, fetched_resolver

    LOGGER.debug("No PDF URL resolved for %s (%s)", paper_id, title)
    return "", ""


def _download_pdf_file(
    *,
    pdf_url: str,
    target_path: Path,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
    retries: int = DEFAULT_HTTP_RETRIES,
) -> None:
    """Download one PDF file with retry, streaming, and content validation."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_suffix(target_path.suffix + ".part")
    if temp_path.exists():
        temp_path.unlink()

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = _build_http_request(
                pdf_url,
                accept="application/pdf,application/octet-stream,*/*;q=0.8",
            )
            with urlopen(request, timeout=timeout) as response:
                content_type = ensure_str(response.headers.get("Content-Type")).casefold()
                first_chunk = response.read(8192)
                if "application/pdf" not in content_type and not first_chunk.startswith(b"%PDF-"):
                    raise RuntimeError(
                        f"URL did not return PDF content: {pdf_url} (content-type={content_type or 'unknown'})"
                    )

                with temp_path.open("wb") as handle:
                    if first_chunk:
                        handle.write(first_chunk)
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        handle.write(chunk)

            if temp_path.stat().st_size <= 0:
                raise RuntimeError(f"Downloaded empty PDF file: {pdf_url}")

            temp_path.replace(target_path)
            return
        except (HTTPError, URLError, TimeoutError, socket.timeout, ValueError, RuntimeError) as err:
            last_error = err
            if temp_path.exists():
                temp_path.unlink()
            LOGGER.warning(
                "PDF download failed (%s/%s) %s: %s",
                attempt,
                retries,
                pdf_url,
                err,
            )
            if attempt == retries:
                break
            _sleep_before_retry(attempt)

    raise RuntimeError(f"Failed to download PDF after {retries} attempts: {pdf_url} ({last_error})")


def _load_download_records_from_tsv(
    *,
    input_tsv: Path,
    selected_ids: Sequence[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Load download records from TSV export, optionally filtering by paper_id."""
    if not input_tsv.exists():
        raise FileNotFoundError(f"Input TSV does not exist: {input_tsv}")

    requested_ids = _dedupe_preserve_order(selected_ids)
    requested_set = set(requested_ids)
    found_ids: set[str] = set()
    records: List[Dict[str, Any]] = []

    with input_tsv.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not reader.fieldnames or "paper_id" not in reader.fieldnames:
            raise ValueError("Input TSV must contain a `paper_id` column.")
        for raw_row in reader:
            row = {ensure_str(key): ensure_str(value) for key, value in raw_row.items() if key}
            paper_id = ensure_str(row.get("paper_id"))
            if not paper_id:
                continue
            if requested_set and paper_id not in requested_set:
                continue
            found_ids.add(paper_id)
            records.append(
                {
                    "paper_id": paper_id,
                    "title": ensure_str(row.get("title")),
                    "url": ensure_str(row.get("url")),
                    "source_provider": ensure_str(row.get("source_provider")),
                    "source_ids": _parse_source_ids_json(row.get("source_ids_json")),
                }
            )

    missing_ids = [paper_id for paper_id in requested_ids if paper_id not in found_ids]
    return records, missing_ids


def _load_download_records_from_db(
    *,
    db_path: Path,
    paper_ids: Sequence[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Load download records from DB for explicit paper IDs."""
    requested_ids = _dedupe_preserve_order(paper_ids)
    if not requested_ids:
        return [], []

    conn = connect_db(db_path)
    try:
        papers_map: Dict[str, sqlite3.Row] = {}
        source_ids_map: Dict[str, Dict[str, str]] = {}
        for chunk_start in range(0, len(requested_ids), 500):
            chunk_ids = requested_ids[chunk_start : chunk_start + 500]
            papers_map.update(_fetch_full_paper_rows(conn, chunk_ids))
            source_ids_map.update(_fetch_source_ids(conn, chunk_ids))
    finally:
        conn.close()

    records: List[Dict[str, Any]] = []
    missing_ids: List[str] = []
    for paper_id in requested_ids:
        row = papers_map.get(paper_id)
        if row is None:
            missing_ids.append(paper_id)
            continue
        records.append(
            {
                "paper_id": paper_id,
                "title": ensure_str(row["title"]),
                "url": ensure_str(row["url"]),
                "source_provider": ensure_str(row["source_provider"]),
                "source_ids": _normalize_source_ids(source_ids_map.get(paper_id, {})),
            }
        )
    return records, missing_ids


def _write_failed_tsv(*, failed_tsv: Path, items: Sequence[Dict[str, Any]]) -> None:
    """Write failed download rows to TSV for manual follow-up."""
    failed_tsv.parent.mkdir(parents=True, exist_ok=True)
    with failed_tsv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=FAILED_DOWNLOAD_COLUMNS,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        for item in items:
            if ensure_str(item.get("status")) != "failed":
                continue
            writer.writerow(
                {
                    key: _sanitize_tsv_value(item.get(key, ""))
                    for key in FAILED_DOWNLOAD_COLUMNS
                }
            )


def run_download_pdfs(
    *,
    db_path: Path,
    input_tsv: Path | None,
    paper_ids: Sequence[str],
    output_dir: Path | None,
    report_json: Path | None,
    overwrite: bool,
) -> Dict[str, Any]:
    """Download public paper PDFs from TSV export or explicit paper IDs."""
    selected_ids = _dedupe_preserve_order(paper_ids)
    if input_tsv is None and not selected_ids:
        raise ValueError("Provide `--input-tsv` or at least one `--paper-id`.")

    if input_tsv is not None:
        records, missing_ids = _load_download_records_from_tsv(
            input_tsv=input_tsv,
            selected_ids=selected_ids,
        )
        input_mode = "tsv_filtered" if selected_ids else "tsv"
        resolved_output_dir = output_dir or input_tsv.resolve().parent / "pdfs"
        requested_count = len(selected_ids) if selected_ids else len(records)
    else:
        records, missing_ids = _load_download_records_from_db(db_path=db_path, paper_ids=selected_ids)
        input_mode = "paper_id"
        resolved_output_dir = output_dir or DEFAULT_PDF_OUTPUT_DIR
        requested_count = len(selected_ids)

    resolved_output_dir = resolved_output_dir.resolve()
    resolved_output_dir.mkdir(parents=True, exist_ok=True)
    resolved_report_json = (report_json or (resolved_output_dir / DEFAULT_PDF_REPORT_NAME)).resolve()
    resolved_report_json.parent.mkdir(parents=True, exist_ok=True)
    resolved_failed_tsv = (resolved_output_dir / DEFAULT_FAILED_TSV_NAME).resolve()

    items: List[Dict[str, Any]] = []
    downloaded_count = 0
    skipped_existing_count = 0
    failed_count = 0

    for paper_id in missing_ids:
        items.append(
            {
                "paper_id": paper_id,
                "title": "",
                "resolver": "",
                "resolved_pdf_url": "",
                "status": "failed",
                "file_path": "",
                "error": (
                    "paper_id not found in input TSV"
                    if input_tsv is not None
                    else "paper_id not found in database"
                ),
            }
        )
        failed_count += 1

    for record in records:
        paper_id = ensure_str(record.get("paper_id"))
        title = ensure_str(record.get("title")) or paper_id
        file_path = resolved_output_dir / _build_pdf_filename(paper_id=paper_id, title=title)
        source_ids = _normalize_source_ids(record.get("source_ids"))
        url = ensure_str(record.get("url"))

        if file_path.exists() and not overwrite:
            resolved_pdf_url, resolver = _resolve_pdf_url(
                paper_id=paper_id,
                title=title,
                url=url,
                source_ids=source_ids,
                allow_page_fetch=False,
            )
            items.append(
                {
                    "paper_id": paper_id,
                    "title": title,
                    "resolver": resolver,
                    "resolved_pdf_url": resolved_pdf_url,
                    "status": "skipped_existing",
                    "file_path": str(file_path),
                    "error": "",
                }
            )
            skipped_existing_count += 1
            continue

        resolved_pdf_url, resolver = _resolve_pdf_url(
            paper_id=paper_id,
            title=title,
            url=url,
            source_ids=source_ids,
            allow_page_fetch=True,
        )
        if not resolved_pdf_url:
            items.append(
                {
                    "paper_id": paper_id,
                    "title": title,
                    "resolver": "",
                    "resolved_pdf_url": "",
                    "status": "failed",
                    "file_path": str(file_path),
                    "error": "Unable to resolve a public PDF URL from source_ids or official page",
                }
            )
            failed_count += 1
            continue

        try:
            _download_pdf_file(pdf_url=resolved_pdf_url, target_path=file_path)
            items.append(
                {
                    "paper_id": paper_id,
                    "title": title,
                    "resolver": resolver,
                    "resolved_pdf_url": resolved_pdf_url,
                    "status": "downloaded",
                    "file_path": str(file_path),
                    "error": "",
                }
            )
            downloaded_count += 1
        except RuntimeError as err:
            items.append(
                {
                    "paper_id": paper_id,
                    "title": title,
                    "resolver": resolver,
                    "resolved_pdf_url": resolved_pdf_url,
                    "status": "failed",
                    "file_path": str(file_path),
                    "error": ensure_str(err),
                }
            )
            failed_count += 1

    payload = {
        "input_mode": input_mode,
        "requested_count": requested_count,
        "downloaded_count": downloaded_count,
        "skipped_existing_count": skipped_existing_count,
        "failed_count": failed_count,
        "output_dir": str(resolved_output_dir),
        "report_json": str(resolved_report_json),
        "failed_tsv": str(resolved_failed_tsv),
        "items": items,
    }
    _write_failed_tsv(failed_tsv=resolved_failed_tsv, items=items)
    resolved_report_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def run_export(
    *,
    db_path: Path,
    query: str,
    mode: str,
    out_tsv: Path,
    keywords_json: Path,
    topics_json: Path,
    max_export: int,
    venues: Sequence[str],
    year_from: int | None,
    year_to: int | None,
    track: str | None,
    presentation_level: str | None,
    include_placeholder: bool,
    order: str,
    embed_base_url: str,
    embed_model: str,
    embed_api_key: str | None,
    alpha: float,
    vector_top_k: int,
    bm25_top_k: int,
    vectors_root: Path,
    collection_name: str,
) -> Dict[str, Any]:
    """Export full paper records for a query into a TSV file."""
    normalized_mode = ensure_str(mode).lower()
    if normalized_mode not in {"search", "hybrid"}:
        raise ValueError("`--mode` must be `search` or `hybrid`.")

    export_limit = int(max_export)
    if export_limit < 0:
        raise ValueError("`--max-export` must be >= 0.")

    keyword_groups, candidate_queries, required_labels = _load_keyword_definition(keywords_json)
    if normalized_mode == "hybrid" and candidate_queries:
        raise ValueError("keywords_json candidate_queries are supported only with --mode search")

    paper_ids: List[str] = []
    total = 0
    candidate_total = 0

    if normalized_mode == "search":
        page_size = 200
        seen_paper_ids: set[str] = set()
        retrieval_queries = candidate_queries or [query]
        for retrieval_query in retrieval_queries:
            offset = 0
            while True:
                page = run_search(
                    db_path=db_path,
                    query=retrieval_query,
                    venues=venues,
                    year_from=year_from,
                    year_to=year_to,
                    track=track,
                    presentation_level=presentation_level,
                    include_placeholder=include_placeholder,
                    order=order,
                    top_k=page_size,
                    offset=offset,
                )
                if not candidate_queries:
                    total = int(page.get("total", 0))
                if not page.get("results"):
                    break
                for item in page["results"]:
                    pid = ensure_str(item.get("paper_id"))
                    if not pid or pid in seen_paper_ids:
                        continue
                    seen_paper_ids.add(pid)
                    paper_ids.append(pid)
                    if not candidate_queries and export_limit and len(paper_ids) >= export_limit:
                        break
                if not candidate_queries and export_limit and len(paper_ids) >= export_limit:
                    break
                offset += page_size
                if offset >= int(page.get("total", 0)):
                    break
            if not candidate_queries and export_limit and len(paper_ids) >= export_limit:
                break
        candidate_total = len(paper_ids)

    else:
        # Hybrid candidate set size is bounded by recall depths; export all by default.
        hybrid_top_k = export_limit if export_limit else max(1, vector_top_k + bm25_top_k)
        result = run_hybrid(
            db_path=db_path,
            query=query,
            embed_base_url=embed_base_url,
            embed_model=embed_model,
            embed_api_key=embed_api_key,
            alpha=alpha,
            vector_top_k=vector_top_k,
            bm25_top_k=bm25_top_k,
            vectors_root=vectors_root,
            collection_name=collection_name,
            venues=venues,
            year_from=year_from,
            year_to=year_to,
            track=track,
            presentation_level=presentation_level,
            include_placeholder=include_placeholder,
            top_k=hybrid_top_k,
            offset=0,
        )
        total = int(result.get("total", 0))
        for item in result.get("results", []) or []:
            pid = ensure_str(item.get("paper_id"))
            if pid:
                paper_ids.append(pid)

    paper_ids = [pid for pid in paper_ids if pid]

    conn = connect_db(db_path)
    try:
        papers_map: Dict[str, sqlite3.Row] = {}
        for chunk_start in range(0, len(paper_ids), 500):
            chunk_ids = paper_ids[chunk_start : chunk_start + 500]
            papers_map.update(_fetch_full_paper_rows(conn, chunk_ids))

        authors_map = _fetch_ordered_values(
            conn,
            table="paper_authors",
            value_column="author_name",
            order_column="author_index",
            paper_ids=paper_ids,
        )
        keywords_map = _fetch_ordered_values(
            conn,
            table="paper_keywords",
            value_column="keyword",
            order_column="keyword_index",
            paper_ids=paper_ids,
        )
        institutions_map = _fetch_ordered_values(
            conn,
            table="paper_institutions",
            value_column="institution",
            order_column="institution_index",
            paper_ids=paper_ids,
        )
        quality_flags_map = _fetch_ordered_values(
            conn,
            table="paper_quality_flags",
            value_column="quality_flag",
            order_column="flag_index",
            paper_ids=paper_ids,
        )
        source_ids_map = _fetch_source_ids(conn, paper_ids)
    finally:
        conn.close()

    if candidate_queries:
        paper_ids = [
            paper_id
            for paper_id in paper_ids
            if paper_id in papers_map
            and _matches_required_labels(
                title=ensure_str(papers_map[paper_id]["title"]),
                abstract=ensure_str(papers_map[paper_id]["abstract"]),
                keywords=keywords_map.get(paper_id, []),
                keyword_groups=keyword_groups,
                required_labels=required_labels,
            )
        ]
        total = len(paper_ids)
        if export_limit:
            paper_ids = paper_ids[:export_limit]
    else:
        candidate_total = total

    janus_topic_map = _load_janus_topic_map(topics_json=topics_json, paper_ids=paper_ids)

    out_tsv.parent.mkdir(parents=True, exist_ok=True)
    exported = 0
    with out_tsv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=EXPORT_COLUMNS,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()

        for rank, paper_id in enumerate(paper_ids, start=1):
            row = papers_map.get(paper_id)
            if row is None:
                continue

            authors = authors_map.get(paper_id, [])
            paper_keywords = keywords_map.get(paper_id, [])
            institutions = institutions_map.get(paper_id, [])
            quality_flags = quality_flags_map.get(paper_id, [])
            source_ids = source_ids_map.get(paper_id, {})

            matched_topic, matched_keyword = _match_topic_label(
                title=ensure_str(row["title"]),
                abstract=ensure_str(row["abstract"]),
                keywords=paper_keywords,
                keyword_groups=keyword_groups,
            )
            janus_topic, janus_subtopic = janus_topic_map.get(paper_id, ("", ""))

            record = {
                "rank": str(rank),
                "matched_topic": matched_topic,
                "matched_keyword": matched_keyword,
                "janus_topic": janus_topic,
                "janus_subtopic": janus_subtopic,
                "paper_id": ensure_str(row["paper_id"]),
                "title": ensure_str(row["title"]),
                "venue": ensure_str(row["venue"]),
                "year": str(int(row["year"])),
                "abstract": ensure_str(row["abstract"]),
                "doi": ensure_str(row["doi"]),
                "url": ensure_str(row["url"]),
                "citation_count": (
                    str(row["citation_count"]) if row["citation_count"] is not None else ""
                ),
                "source_provider": ensure_str(row["source_provider"]),
                "track": ensure_str(row["track"]),
                "track_display_name": ensure_str(row["track_display_name"]),
                "track_group": ensure_str(row["track_group"]),
                "presentation_level": ensure_str(row["presentation_level"]),
                "record_status": ensure_str(row["record_status"]),
                "collected_at": ensure_str(row["collected_at"]),
                "source_file": ensure_str(row["source_file"]),
                "ingested_at_utc": ensure_str(row["ingested_at_utc"]),
                "authors": "; ".join(authors),
                "keywords": "; ".join(paper_keywords),
                "institutions": "; ".join(institutions),
                "quality_flags": "; ".join(quality_flags),
                "source_ids_json": json.dumps(
                    source_ids, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            }

            writer.writerow({key: _sanitize_tsv_value(record.get(key, "")) for key in EXPORT_COLUMNS})
            exported += 1

    resolved_topics_json = str(topics_json) if topics_json.exists() else ""
    truncated = exported < total if total else False
    return {
        "query": query,
        "mode": normalized_mode,
        "total": total,
        "exported": exported,
        "truncated": truncated,
        "max_export": export_limit,
        "candidate_queries": candidate_queries,
        "required_labels": required_labels,
        "candidate_total": candidate_total,
        "out_tsv": str(out_tsv.resolve()),
        "keywords_json": str(keywords_json.resolve()),
        "topics_json": str(Path(resolved_topics_json).resolve()) if resolved_topics_json else "",
    }


def _build_search_payload(conn: sqlite3.Connection, rows: Sequence[sqlite3.Row], *, offset: int) -> List[Dict[str, Any]]:
    """Build result payload with related authors/keywords."""
    paper_ids = [ensure_str(row["paper_id"]) for row in rows]
    authors_map = _fetch_ordered_values(
        conn,
        table="paper_authors",
        value_column="author_name",
        order_column="author_index",
        paper_ids=paper_ids,
    )
    keywords_map = _fetch_ordered_values(
        conn,
        table="paper_keywords",
        value_column="keyword",
        order_column="keyword_index",
        paper_ids=paper_ids,
    )

    results = []
    for index, row in enumerate(rows, start=offset + 1):
        paper_id = ensure_str(row["paper_id"])
        bm25_score = row["bm25_score"] if "bm25_score" in row.keys() else None
        results.append(
            {
                "rank": index,
                "paper_id": paper_id,
                "title": ensure_str(row["title"]),
                "venue": ensure_str(row["venue"]),
                "year": int(row["year"]),
                "track": ensure_str(row["track"]),
                "presentation_level": ensure_str(row["presentation_level"]),
                "record_status": ensure_str(row["record_status"]),
                "citation_count": row["citation_count"],
                "bm25_score": float(bm25_score) if bm25_score is not None else None,
                "authors": authors_map.get(paper_id, []),
                "keywords": keywords_map.get(paper_id, []),
            }
        )
    return results


def run_search(
    *,
    db_path: Path,
    query: str,
    venues: Sequence[str],
    year_from: int | None,
    year_to: int | None,
    track: str | None,
    presentation_level: str | None,
    include_placeholder: bool,
    order: str,
    top_k: int,
    offset: int,
) -> Dict[str, Any]:
    """Execute FTS-backed search with filters and pagination."""
    if top_k <= 0:
        raise ValueError("`--top-k` must be positive.")
    if offset < 0:
        raise ValueError("`--offset` must be >= 0.")

    match_query = normalize_fts_query(query)
    conn = connect_db(db_path)
    try:
        ensure_fts_ready(conn)
        base_from = "FROM papers_fts JOIN papers p ON p.paper_id = papers_fts.paper_id"
        extra_where, extra_params = _build_filters(
            venues=venues,
            year_from=year_from,
            year_to=year_to,
            track=track,
            presentation_level=presentation_level,
            include_placeholder=include_placeholder,
        )
        where_sql = f"WHERE papers_fts MATCH ?{extra_where}"
        params: List[Any] = [match_query, *extra_params]

        total = int(
            conn.execute(f"SELECT COUNT(*) {base_from} {where_sql}", tuple(params)).fetchone()[0]
        )

        if order == "bm25":
            order_sql = "ORDER BY bm25_score ASC, p.year DESC, COALESCE(p.citation_count, -1) DESC"
        elif order == "year":
            order_sql = "ORDER BY p.year DESC, COALESCE(p.citation_count, -1) DESC, p.paper_id ASC"
        elif order == "citation":
            order_sql = (
                "ORDER BY COALESCE(p.citation_count, -1) DESC, p.year DESC, p.paper_id ASC"
            )
        else:
            raise ValueError(f"Unsupported order: {order}")

        rows = conn.execute(
            f"""
            SELECT
                p.paper_id,
                p.title,
                p.venue,
                p.year,
                p.track,
                p.presentation_level,
                p.record_status,
                p.citation_count,
                bm25(papers_fts) AS bm25_score
            {base_from}
            {where_sql}
            {order_sql}
            LIMIT ? OFFSET ?
            """,
            tuple([*params, top_k, offset]),
        ).fetchall()

        results = _build_search_payload(conn, rows, offset=offset)
        return {
            "query": query,
            "match_query": match_query,
            "order": order,
            "top_k": top_k,
            "offset": offset,
            "total": total,
            "results": results,
        }
    finally:
        conn.close()


def _make_embedding_client(base_url: str, api_key: str | None = None) -> Any:
    """Create OpenAI-compatible client for embeddings."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Missing dependency `openai`. Install with: uv add openai") from exc
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("Missing dependency `httpx`. Install with: uv add httpx") from exc
    normalized_base = normalize_openai_base_url(base_url)
    key = (
        ensure_str(api_key)
        or ensure_str(os.getenv("JANUS_EMBED_API_KEY"))
        or ensure_str(os.getenv("JANUS_LLM_API_KEY"))
        or "not-required"
    )
    return OpenAI(
        base_url=normalized_base,
        api_key=key,
        http_client=httpx.Client(trust_env=False),
    )


def _embed_query(base_url: str, model: str, query: str, api_key: str | None = None) -> List[float]:
    """Embed query text via OpenAI-compatible endpoint."""
    client = _make_embedding_client(base_url=base_url, api_key=api_key)
    response = client.embeddings.create(model=model, input=[query])
    if not response.data:
        raise RuntimeError("Embedding response is empty.")
    return [float(value) for value in response.data[0].embedding]


def _load_vector_collection(vectors_root: Path, collection_name: str) -> Any:
    """Load Chroma collection used by hybrid search."""
    try:
        import chromadb
        from chromadb.config import Settings
    except ImportError as exc:
        raise RuntimeError("Missing dependency `chromadb`. Install with: uv add chromadb") from exc
    if not vectors_root.exists():
        raise FileNotFoundError(f"Vector root does not exist: {vectors_root}")
    client = chromadb.PersistentClient(
        path=str(vectors_root),
        settings=Settings(anonymized_telemetry=False),
    )
    return client.get_or_create_collection(name=collection_name)


def _normalize_scores(scores: Dict[str, float], higher_is_better: bool) -> Dict[str, float]:
    """Normalize score map to [0, 1]."""
    if not scores:
        return {}
    values = [float(value) for value in scores.values()]
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        return {key: 1.0 for key in scores}
    normalized: Dict[str, float] = {}
    for key, value in scores.items():
        if higher_is_better:
            normalized[key] = (float(value) - low) / (high - low)
        else:
            normalized[key] = (high - float(value)) / (high - low)
    return normalized


def _filter_candidate_ids(
    conn: sqlite3.Connection,
    *,
    candidate_ids: Sequence[str],
    venues: Sequence[str],
    year_from: int | None,
    year_to: int | None,
    track: str | None,
    presentation_level: str | None,
    include_placeholder: bool,
) -> List[str]:
    """Apply SQL filters to candidate IDs."""
    if not candidate_ids:
        return []
    placeholders = ",".join(["?"] * len(candidate_ids))
    extra_where, extra_params = _build_filters(
        venues=venues,
        year_from=year_from,
        year_to=year_to,
        track=track,
        presentation_level=presentation_level,
        include_placeholder=include_placeholder,
    )
    rows = conn.execute(
        f"""
        SELECT p.paper_id
        FROM papers p
        WHERE p.paper_id IN ({placeholders}) {extra_where}
        """,  # noqa: S608
        tuple([*candidate_ids, *extra_params]),
    ).fetchall()
    return [ensure_str(row["paper_id"]) for row in rows]


def run_hybrid(
    *,
    db_path: Path,
    query: str,
    embed_base_url: str,
    embed_model: str,
    embed_api_key: str | None = None,
    alpha: float,
    vector_top_k: int,
    bm25_top_k: int,
    vectors_root: Path,
    collection_name: str,
    venues: Sequence[str],
    year_from: int | None,
    year_to: int | None,
    track: str | None,
    presentation_level: str | None,
    include_placeholder: bool,
    top_k: int,
    offset: int,
) -> Dict[str, Any]:
    """Execute hybrid retrieval: vector + FTS with normalized score fusion."""
    if not (0.0 <= alpha <= 1.0):
        raise ValueError("`--alpha` must be between 0 and 1.")
    if vector_top_k <= 0 or bm25_top_k <= 0:
        raise ValueError("`--vector-top-k` and `--bm25-top-k` must be positive.")
    if top_k <= 0:
        raise ValueError("`--top-k` must be positive.")
    if offset < 0:
        raise ValueError("`--offset` must be >= 0.")

    match_query = normalize_fts_query(query)
    conn = connect_db(db_path)
    try:
        ensure_fts_ready(conn)

        # 1) BM25 recall
        extra_where, extra_params = _build_filters(
            venues=venues,
            year_from=year_from,
            year_to=year_to,
            track=track,
            presentation_level=presentation_level,
            include_placeholder=include_placeholder,
        )
        bm25_rows = conn.execute(
            f"""
            SELECT
                p.paper_id,
                bm25(papers_fts) AS bm25_score
            FROM papers_fts
            JOIN papers p ON p.paper_id = papers_fts.paper_id
            WHERE papers_fts MATCH ? {extra_where}
            ORDER BY bm25_score ASC
            LIMIT ?
            """,
            tuple([match_query, *extra_params, bm25_top_k]),
        ).fetchall()
        bm25_scores: Dict[str, float] = {
            ensure_str(row["paper_id"]): float(row["bm25_score"])
            for row in bm25_rows
            if row["bm25_score"] is not None
        }

        # 2) Vector recall
        query_vector = _embed_query(embed_base_url, embed_model, query, embed_api_key)
        collection = _load_vector_collection(vectors_root=vectors_root, collection_name=collection_name)
        vector_payload = collection.query(
            query_embeddings=[query_vector],
            n_results=vector_top_k,
            include=["distances", "metadatas"],
        )
        raw_ids = (vector_payload.get("ids") or [[]])[0]
        raw_distances = (vector_payload.get("distances") or [[]])[0]

        vector_scores: Dict[str, float] = {}
        for idx, raw_id in enumerate(raw_ids):
            paper_id = ensure_str(raw_id)
            if not paper_id:
                continue
            distance = float(raw_distances[idx]) if idx < len(raw_distances) else 1.0
            similarity = 1.0 / (1.0 + max(distance, 0.0))
            vector_scores[paper_id] = similarity

        # 3) Union + filter + normalization
        candidate_ids = sorted(set(bm25_scores) | set(vector_scores))
        filtered_ids = _filter_candidate_ids(
            conn,
            candidate_ids=candidate_ids,
            venues=venues,
            year_from=year_from,
            year_to=year_to,
            track=track,
            presentation_level=presentation_level,
            include_placeholder=include_placeholder,
        )
        if not filtered_ids:
            return {
                "query": query,
                "match_query": match_query,
                "alpha": alpha,
                "vector_top_k": vector_top_k,
                "bm25_top_k": bm25_top_k,
                "top_k": top_k,
                "offset": offset,
                "total": 0,
                "results": [],
            }

        bm25_filtered = {pid: bm25_scores[pid] for pid in filtered_ids if pid in bm25_scores}
        vector_filtered = {pid: vector_scores[pid] for pid in filtered_ids if pid in vector_scores}
        bm25_norm = _normalize_scores(bm25_filtered, higher_is_better=False)
        vector_norm = _normalize_scores(vector_filtered, higher_is_better=True)

        paper_rows = _fetch_paper_rows(conn, filtered_ids)
        scored: List[Tuple[str, float]] = []
        for paper_id in filtered_ids:
            final_score = alpha * vector_norm.get(paper_id, 0.0) + (1.0 - alpha) * bm25_norm.get(
                paper_id, 0.0
            )
            scored.append((paper_id, float(final_score)))

        scored.sort(
            key=lambda item: (
                -item[1],
                -int(paper_rows[item[0]]["year"]) if item[0] in paper_rows else 0,
                -int(paper_rows[item[0]]["citation_count"] or 0) if item[0] in paper_rows else 0,
                item[0],
            )
        )

        total = len(scored)
        page = scored[offset : offset + top_k]
        page_ids = [paper_id for paper_id, _score in page]
        authors_map = _fetch_ordered_values(
            conn,
            table="paper_authors",
            value_column="author_name",
            order_column="author_index",
            paper_ids=page_ids,
        )
        keywords_map = _fetch_ordered_values(
            conn,
            table="paper_keywords",
            value_column="keyword",
            order_column="keyword_index",
            paper_ids=page_ids,
        )

        results: List[Dict[str, Any]] = []
        for rank_offset, (paper_id, final_score) in enumerate(page, start=offset + 1):
            row = paper_rows.get(paper_id)
            if row is None:
                continue
            results.append(
                {
                    "rank": rank_offset,
                    "paper_id": paper_id,
                    "title": ensure_str(row["title"]),
                    "venue": ensure_str(row["venue"]),
                    "year": int(row["year"]),
                    "track": ensure_str(row["track"]),
                    "presentation_level": ensure_str(row["presentation_level"]),
                    "record_status": ensure_str(row["record_status"]),
                    "citation_count": row["citation_count"],
                    "vector_score": vector_filtered.get(paper_id),
                    "bm25_score": bm25_filtered.get(paper_id),
                    "vector_norm": vector_norm.get(paper_id, 0.0),
                    "bm25_norm": bm25_norm.get(paper_id, 0.0),
                    "final_score": final_score,
                    "authors": authors_map.get(paper_id, []),
                    "keywords": keywords_map.get(paper_id, []),
                }
            )

        return {
            "query": query,
            "match_query": match_query,
            "alpha": alpha,
            "vector_top_k": vector_top_k,
            "bm25_top_k": bm25_top_k,
            "top_k": top_k,
            "offset": offset,
            "total": total,
            "results": results,
        }
    finally:
        conn.close()


def run_get(*, db_path: Path, paper_id: str) -> Dict[str, Any]:
    """Fetch one paper with full related fields."""
    pid = ensure_str(paper_id)
    if not pid:
        raise ValueError("`--paper-id` must not be empty.")

    conn = connect_db(db_path)
    try:
        row = conn.execute("SELECT * FROM papers WHERE paper_id = ?", (pid,)).fetchone()
        if row is None:
            raise ValueError(f"Paper not found: {pid}")

        authors = _fetch_ordered_values(
            conn,
            table="paper_authors",
            value_column="author_name",
            order_column="author_index",
            paper_ids=[pid],
        ).get(pid, [])
        keywords = _fetch_ordered_values(
            conn,
            table="paper_keywords",
            value_column="keyword",
            order_column="keyword_index",
            paper_ids=[pid],
        ).get(pid, [])
        institutions = _fetch_ordered_values(
            conn,
            table="paper_institutions",
            value_column="institution",
            order_column="institution_index",
            paper_ids=[pid],
        ).get(pid, [])
        quality_flags = _fetch_ordered_values(
            conn,
            table="paper_quality_flags",
            value_column="quality_flag",
            order_column="flag_index",
            paper_ids=[pid],
        ).get(pid, [])
        source_ids = _fetch_source_ids(conn, [pid]).get(pid, {})

        return {
            "paper_id": ensure_str(row["paper_id"]),
            "title": ensure_str(row["title"]),
            "venue": ensure_str(row["venue"]),
            "year": int(row["year"]),
            "abstract": ensure_str(row["abstract"]),
            "doi": row["doi"],
            "url": row["url"],
            "citation_count": row["citation_count"],
            "source_provider": ensure_str(row["source_provider"]),
            "track": ensure_str(row["track"]),
            "track_display_name": ensure_str(row["track_display_name"]),
            "track_group": ensure_str(row["track_group"]),
            "presentation_level": ensure_str(row["presentation_level"]),
            "record_status": ensure_str(row["record_status"]),
            "collected_at": ensure_str(row["collected_at"]),
            "source_file": ensure_str(row["source_file"]),
            "ingested_at_utc": ensure_str(row["ingested_at_utc"]),
            "authors": authors,
            "keywords": keywords,
            "institutions": institutions,
            "quality_flags": quality_flags,
            "source_ids": source_ids,
        }
    finally:
        conn.close()


def run_stats(*, db_path: Path) -> Dict[str, Any]:
    """Return search-facing DB stats."""
    conn = connect_db(db_path)
    try:
        paper_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        source_file_count = int(conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0])
        status_rows = conn.execute(
            "SELECT record_status, COUNT(*) AS c FROM papers GROUP BY record_status ORDER BY record_status"
        ).fetchall()
        venue_year_rows = conn.execute(
            "SELECT venue, year, COUNT(*) AS c FROM papers GROUP BY venue, year ORDER BY venue, year"
        ).fetchall()
        has_fts = fts_table_exists(conn)
        fts_row_count = (
            int(conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0]) if has_fts else 0
        )
    finally:
        conn.close()

    status_counts = {ensure_str(row["record_status"]): int(row["c"]) for row in status_rows}
    venue_year_counts = {
        f"{ensure_str(row['venue'])}-{int(row['year'])}": int(row["c"]) for row in venue_year_rows
    }
    return {
        "db_path": str(db_path),
        "paper_count": paper_count,
        "source_file_count": source_file_count,
        "fts_table_exists": has_fts,
        "fts_row_count": fts_row_count,
        "fts_aligned": has_fts and fts_row_count == paper_count,
        "status_counts": status_counts,
        "venue_year_counts": venue_year_counts,
    }


def _truncate(text: str, width: int) -> str:
    """Truncate string with ellipsis to fixed width."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def render_search_table(results: Sequence[Dict[str, Any]]) -> str:
    """Render search results in plain text table."""
    headers = ["Rank", "PaperID", "Venue", "Year", "Cites", "Status", "Score", "Title"]
    widths = [4, 18, 8, 4, 5, 11, 8, 72]
    lines = [
        " | ".join(h.ljust(w) for h, w in zip(headers, widths)),
        "-+-".join("-" * w for w in widths),
    ]
    for item in results:
        score = item.get("bm25_score")
        score_text = f"{score:.3f}" if isinstance(score, float) else ""
        row = [
            str(item.get("rank", "")).rjust(widths[0]),
            _truncate(ensure_str(item.get("paper_id")), widths[1]).ljust(widths[1]),
            _truncate(ensure_str(item.get("venue")), widths[2]).ljust(widths[2]),
            str(item.get("year", "")).rjust(widths[3]),
            str(item.get("citation_count") if item.get("citation_count") is not None else "").rjust(
                widths[4]
            ),
            _truncate(ensure_str(item.get("record_status")), widths[5]).ljust(widths[5]),
            score_text.rjust(widths[6]),
            _truncate(ensure_str(item.get("title")), widths[7]).ljust(widths[7]),
        ]
        lines.append(" | ".join(row))
    return "\n".join(lines)


def render_search_markdown(results: Sequence[Dict[str, Any]]) -> str:
    """Render search results in markdown table."""
    lines = [
        "| Rank | Paper ID | Venue | Year | Cites | Status | Score | Title |",
        "|---:|---|---|---:|---:|---|---:|---|",
    ]
    for item in results:
        score = item.get("bm25_score")
        score_text = f"{score:.3f}" if isinstance(score, float) else ""
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item.get("rank", "")),
                    ensure_str(item.get("paper_id")),
                    ensure_str(item.get("venue")),
                    str(item.get("year", "")),
                    str(item.get("citation_count") if item.get("citation_count") is not None else ""),
                    ensure_str(item.get("record_status")),
                    score_text,
                    ensure_str(item.get("title")).replace("|", "\\|"),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def render_hybrid_table(results: Sequence[Dict[str, Any]]) -> str:
    """Render hybrid results in plain text table."""
    headers = ["Rank", "PaperID", "Venue", "Year", "Final", "Vec", "BM25", "Title"]
    widths = [4, 18, 8, 4, 7, 7, 7, 64]
    lines = [
        " | ".join(h.ljust(w) for h, w in zip(headers, widths)),
        "-+-".join("-" * w for w in widths),
    ]
    for item in results:
        row = [
            str(item.get("rank", "")).rjust(widths[0]),
            _truncate(ensure_str(item.get("paper_id")), widths[1]).ljust(widths[1]),
            _truncate(ensure_str(item.get("venue")), widths[2]).ljust(widths[2]),
            str(item.get("year", "")).rjust(widths[3]),
            f"{float(item.get('final_score', 0.0)):.3f}".rjust(widths[4]),
            (
                f"{float(item.get('vector_norm', 0.0)):.3f}"
                if item.get("vector_norm") is not None
                else ""
            ).rjust(widths[5]),
            (
                f"{float(item.get('bm25_norm', 0.0)):.3f}"
                if item.get("bm25_norm") is not None
                else ""
            ).rjust(widths[6]),
            _truncate(ensure_str(item.get("title")), widths[7]).ljust(widths[7]),
        ]
        lines.append(" | ".join(row))
    return "\n".join(lines)


def render_hybrid_markdown(results: Sequence[Dict[str, Any]]) -> str:
    """Render hybrid results in markdown table."""
    lines = [
        "| Rank | Paper ID | Venue | Year | Final | Vec | BM25 | Title |",
        "|---:|---|---|---:|---:|---:|---:|---|",
    ]
    for item in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item.get("rank", "")),
                    ensure_str(item.get("paper_id")),
                    ensure_str(item.get("venue")),
                    str(item.get("year", "")),
                    f"{float(item.get('final_score', 0.0)):.3f}",
                    f"{float(item.get('vector_norm', 0.0)):.3f}",
                    f"{float(item.get('bm25_norm', 0.0)):.3f}",
                    ensure_str(item.get("title")).replace("|", "\\|"),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _add_common_filter_args(parser: argparse.ArgumentParser) -> None:
    """Add common search filters to parser."""
    parser.add_argument("--query", required=True, help="Search query text")
    parser.add_argument("--venue", help="Comma-separated venue filter, e.g. ICLR,ICML,NEURIPS")
    parser.add_argument("--year-from", type=int, help="Lower bound of publication year")
    parser.add_argument("--year-to", type=int, help="Upper bound of publication year")
    parser.add_argument("--track", help="Track slug filter, e.g. conference")
    parser.add_argument(
        "--presentation-level",
        choices=("poster", "oral", "bestpaper"),
        help="Presentation level filter",
    )
    parser.add_argument(
        "--include-placeholder",
        action="store_true",
        help="Include placeholder records (default excludes them)",
    )
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help="Page size")
    parser.add_argument("--offset", type=int, default=DEFAULT_OFFSET, help="Result offset")
    parser.add_argument(
        "--format",
        default="table",
        choices=("table", "json", "md"),
        help="Output format",
    )


def _add_export_filter_args(parser: argparse.ArgumentParser) -> None:
    """Add export filters to parser."""
    parser.add_argument("--query", required=True, help="Search query text")
    parser.add_argument("--venue", help="Comma-separated venue filter, e.g. ICLR,ICML,NEURIPS")
    parser.add_argument("--year-from", type=int, help="Lower bound of publication year")
    parser.add_argument("--year-to", type=int, help="Upper bound of publication year")
    parser.add_argument("--track", help="Track slug filter, e.g. conference")
    parser.add_argument(
        "--presentation-level",
        choices=("poster", "oral", "bestpaper"),
        help="Presentation level filter",
    )
    parser.add_argument(
        "--include-placeholder",
        action="store_true",
        help="Include placeholder records (default excludes them)",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""
    parser = argparse.ArgumentParser(description="Search papers in local SQLite DB")
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite db path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Log level",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="FTS search over title+abstract")
    _add_common_filter_args(search)
    search.add_argument(
        "--order",
        default="bm25",
        choices=("bm25", "year", "citation"),
        help="Result ordering strategy",
    )

    hybrid = subparsers.add_parser("hybrid", help="Hybrid search (FTS + vector)")
    _add_common_filter_args(hybrid)
    hybrid.add_argument(
        "--embed-base-url",
        default=ensure_str(os.getenv("JANUS_EMBED_BASE_URL")) or DEFAULT_EMBED_BASE_URL,
        help=(
            "Embedding endpoint base URL. "
            f"(default env JANUS_EMBED_BASE_URL or {DEFAULT_EMBED_BASE_URL})"
        ),
    )
    hybrid.add_argument(
        "--embed-model",
        default=DEFAULT_EMBED_MODEL,
        help=f"Embedding model name (default: {DEFAULT_EMBED_MODEL})",
    )
    hybrid.add_argument(
        "--embed-api-key",
        default=ensure_str(os.getenv("JANUS_EMBED_API_KEY"))
        or ensure_str(os.getenv("JANUS_LLM_API_KEY"))
        or None,
        help="Embedding API key (default env JANUS_EMBED_API_KEY or JANUS_LLM_API_KEY)",
    )
    hybrid.add_argument(
        "--alpha",
        type=float,
        default=0.6,
        help="Hybrid fusion weight: alpha*vector + (1-alpha)*bm25 (default: 0.6)",
    )
    hybrid.add_argument("--vector-top-k", type=int, default=100, help="Vector recall depth")
    hybrid.add_argument("--bm25-top-k", type=int, default=100, help="BM25 recall depth")
    hybrid.add_argument(
        "--vectors-root",
        default=str(DEFAULT_VECTORS_ROOT),
        help=f"Chroma root path (default: {DEFAULT_VECTORS_ROOT})",
    )
    hybrid.add_argument(
        "--collection-name",
        default=DEFAULT_COLLECTION_NAME,
        help=f"Chroma collection name (default: {DEFAULT_COLLECTION_NAME})",
    )

    export = subparsers.add_parser("export", help="Export full records for a query to TSV")
    _add_export_filter_args(export)
    export.add_argument(
        "--mode",
        default="search",
        choices=("search", "hybrid"),
        help="Retrieval mode used to rank exported results (default: search)",
    )
    export.add_argument("--out-tsv", required=True, help="Output TSV path")
    export.add_argument("--keywords-json", required=True, help="Keywords definition JSON path")
    export.add_argument(
        "--topics-json",
        default=str(DEFAULT_TOPICS_JSON),
        help=f"M3 topic assignments JSON path (default: {DEFAULT_TOPICS_JSON})",
    )
    export.add_argument(
        "--max-export",
        type=int,
        default=DEFAULT_EXPORT_MAX,
        help=f"Max rows to export. 0 means no limit (default: {DEFAULT_EXPORT_MAX})",
    )
    export.add_argument(
        "--order",
        default="bm25",
        choices=("bm25", "year", "citation"),
        help="Search ordering strategy (only for mode=search)",
    )
    export.add_argument(
        "--embed-base-url",
        default=ensure_str(os.getenv("JANUS_EMBED_BASE_URL")) or DEFAULT_EMBED_BASE_URL,
        help=(
            "Embedding endpoint base URL (only for mode=hybrid). "
            f"(default env JANUS_EMBED_BASE_URL or {DEFAULT_EMBED_BASE_URL})"
        ),
    )
    export.add_argument(
        "--embed-model",
        default=DEFAULT_EMBED_MODEL,
        help=f"Embedding model name (only for mode=hybrid) (default: {DEFAULT_EMBED_MODEL})",
    )
    export.add_argument(
        "--embed-api-key",
        default=ensure_str(os.getenv("JANUS_EMBED_API_KEY"))
        or ensure_str(os.getenv("JANUS_LLM_API_KEY"))
        or None,
        help="Embedding API key (only for mode=hybrid) (default env JANUS_EMBED_API_KEY or JANUS_LLM_API_KEY)",
    )
    export.add_argument(
        "--alpha",
        type=float,
        default=0.6,
        help="Hybrid fusion weight (only for mode=hybrid) (default: 0.6)",
    )
    export.add_argument(
        "--vector-top-k",
        type=int,
        default=100,
        help="Vector recall depth (only for mode=hybrid)",
    )
    export.add_argument(
        "--bm25-top-k",
        type=int,
        default=100,
        help="BM25 recall depth (only for mode=hybrid)",
    )
    export.add_argument(
        "--vectors-root",
        default=str(DEFAULT_VECTORS_ROOT),
        help=f"Chroma root path (only for mode=hybrid) (default: {DEFAULT_VECTORS_ROOT})",
    )
    export.add_argument(
        "--collection-name",
        default=DEFAULT_COLLECTION_NAME,
        help=f"Chroma collection name (only for mode=hybrid) (default: {DEFAULT_COLLECTION_NAME})",
    )

    download_pdfs = subparsers.add_parser("download-pdfs", help="Download public paper PDFs")
    download_pdfs.add_argument("--input-tsv", help="Input TSV path from `tools.search export`")
    download_pdfs.add_argument(
        "--paper-id",
        action="append",
        default=[],
        help="Paper ID to download. Repeat flag for multiple papers.",
    )
    download_pdfs.add_argument(
        "--output-dir",
        help=f"Output directory for PDFs (default: <tsv_dir>/pdfs or {DEFAULT_PDF_OUTPUT_DIR})",
    )
    download_pdfs.add_argument(
        "--report-json",
        help=f"Output JSON report path (default: <output_dir>/{DEFAULT_PDF_REPORT_NAME})",
    )
    download_pdfs.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing PDF files instead of skipping them",
    )

    get = subparsers.add_parser("get", help="Fetch full record by paper_id")
    get.add_argument("--paper-id", required=True, help="Paper ID")

    subparsers.add_parser("stats", help="Show DB and FTS stats")
    return parser


def main() -> int:
    """CLI entry point."""
    parser = build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    db_path = Path(args.db_path)

    try:
        if args.command == "search":
            result = run_search(
                db_path=db_path,
                query=args.query,
                venues=parse_csv_values(args.venue, uppercase=True),
                year_from=args.year_from,
                year_to=args.year_to,
                track=ensure_str(args.track) or None,
                presentation_level=ensure_str(args.presentation_level) or None,
                include_placeholder=args.include_placeholder,
                order=args.order,
                top_k=args.top_k,
                offset=args.offset,
            )
            if args.format == "table":
                print(
                    f"Query: {result['query']} | total={result['total']} | "
                    f"offset={result['offset']} | top_k={result['top_k']} | order={result['order']}"
                )
                if result["results"]:
                    print(render_search_table(result["results"]))
                else:
                    print("No results.")
            elif args.format == "md":
                if result["results"]:
                    print(render_search_markdown(result["results"]))
                else:
                    print("No results.")
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "hybrid":
            result = run_hybrid(
                db_path=db_path,
                query=args.query,
                embed_base_url=args.embed_base_url,
                embed_model=args.embed_model,
                embed_api_key=args.embed_api_key,
                alpha=args.alpha,
                vector_top_k=args.vector_top_k,
                bm25_top_k=args.bm25_top_k,
                vectors_root=Path(args.vectors_root),
                collection_name=ensure_str(args.collection_name) or DEFAULT_COLLECTION_NAME,
                venues=parse_csv_values(args.venue, uppercase=True),
                year_from=args.year_from,
                year_to=args.year_to,
                track=ensure_str(args.track) or None,
                presentation_level=ensure_str(args.presentation_level) or None,
                include_placeholder=args.include_placeholder,
                top_k=args.top_k,
                offset=args.offset,
            )
            if args.format == "table":
                print(
                    f"Query: {result['query']} | total={result['total']} | "
                    f"offset={result['offset']} | top_k={result['top_k']} | alpha={result['alpha']}"
                )
                if result["results"]:
                    print(render_hybrid_table(result["results"]))
                else:
                    print("No results.")
            elif args.format == "md":
                if result["results"]:
                    print(render_hybrid_markdown(result["results"]))
                else:
                    print("No results.")
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "get":
            payload = run_get(db_path=db_path, paper_id=args.paper_id)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "export":
            payload = run_export(
                db_path=db_path,
                query=args.query,
                mode=args.mode,
                out_tsv=Path(args.out_tsv),
                keywords_json=Path(args.keywords_json),
                topics_json=Path(args.topics_json),
                max_export=args.max_export,
                venues=parse_csv_values(args.venue, uppercase=True),
                year_from=args.year_from,
                year_to=args.year_to,
                track=ensure_str(args.track) or None,
                presentation_level=ensure_str(args.presentation_level) or None,
                include_placeholder=args.include_placeholder,
                order=args.order,
                embed_base_url=args.embed_base_url,
                embed_model=args.embed_model,
                embed_api_key=args.embed_api_key,
                alpha=args.alpha,
                vector_top_k=args.vector_top_k,
                bm25_top_k=args.bm25_top_k,
                vectors_root=Path(args.vectors_root),
                collection_name=ensure_str(args.collection_name) or DEFAULT_COLLECTION_NAME,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "download-pdfs":
            payload = run_download_pdfs(
                db_path=db_path,
                input_tsv=Path(args.input_tsv) if ensure_str(args.input_tsv) else None,
                paper_ids=args.paper_id or [],
                output_dir=Path(args.output_dir) if ensure_str(args.output_dir) else None,
                report_json=Path(args.report_json) if ensure_str(args.report_json) else None,
                overwrite=args.overwrite,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "stats":
            payload = run_stats(db_path=db_path)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        parser.error(f"Unknown command: {args.command}")
        return 2
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
