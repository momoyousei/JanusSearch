#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M2-B/M3 search CLI: SQL+FTS and hybrid retrieval over local SQLite database."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

LOGGER = logging.getLogger("search_cli")

DEFAULT_DB_PATH = Path("data/papers.db")
DEFAULT_VECTORS_ROOT = Path("data/vectors/chroma")
DEFAULT_COLLECTION_NAME = "papers_v1"
DEFAULT_EMBED_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_EMBED_MODEL = "Qwen/Qwen3-Embedding-8B"

DEFAULT_TOP_K = 20
DEFAULT_OFFSET = 0


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
    except ImportError as exc:
        raise RuntimeError("Missing dependency `chromadb`. Install with: uv add chromadb") from exc
    if not vectors_root.exists():
        raise FileNotFoundError(f"Vector root does not exist: {vectors_root}")
    client = chromadb.PersistentClient(path=str(vectors_root))
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
