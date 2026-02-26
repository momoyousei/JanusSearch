#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M3 pipeline: vector build, topic cache generation, and hybrid retrieval artifacts."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Sequence, Tuple

try:
    from tenacity import retry, stop_after_attempt, wait_exponential
except ImportError:
    def retry(*_args: Any, **_kwargs: Any) -> Any:
        """Fallback retry decorator when tenacity is unavailable."""

        def decorator(func: Any) -> Any:
            return func

        return decorator

    def stop_after_attempt(attempts: int) -> int:
        """Fallback stop policy placeholder."""
        return attempts

    def wait_exponential(**_kwargs: Any) -> int:
        """Fallback wait policy placeholder."""
        return 0

LOGGER = logging.getLogger("m3_pipeline")

DEFAULT_DB_PATH = Path("data/papers.db")
DEFAULT_INDEX_ROOT = Path("artifacts")
DEFAULT_VECTORS_ROOT = Path("data/vectors/chroma")
DEFAULT_COLLECTION_NAME = "papers_v1"
DEFAULT_TOPICS_FILE = "topic_assignments.json"
DEFAULT_TOPICS_PROGRESS_FILE = "topic_assignments.progress.json"
DEFAULT_BUILD_REPORT = "build_report.json"
DEFAULT_VALIDATE_REPORT = "validate_report.json"
DEFAULT_MASTER_INDEX = Path("artifacts/indexes/master_index.md")
DEFAULT_VENUES_ROOT = Path("venues")
DEFAULT_TOPICS_ROOT = Path("topics")
DEFAULT_SUBTOPICS_ROOT = Path("subtopics")

DEFAULT_EMBED_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_EMBED_MODEL = "Qwen/Qwen3-Embedding-8B"
DEFAULT_EMBED_BATCH_SIZE = 128

DEFAULT_LLM_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_LLM_MODEL = "Qwen/Qwen3-8B"
DEFAULT_RANDOM_SEED = 42
DEFAULT_PRIMARY_TOPIC_COUNT = 40


@dataclass
class VectorPaper:
    """Paper row used for vectorization."""

    paper_id: str
    title: str
    abstract: str
    venue: str
    year: int
    track: str
    presentation_level: str
    record_status: str
    source_file: str


@dataclass
class VectorItem:
    """Vector item loaded from vector store."""

    paper_id: str
    embedding: List[float]
    document: str
    metadata: Dict[str, Any]


def utc_now_iso() -> str:
    """Return UTC timestamp in ISO-8601."""
    return datetime.now(timezone.utc).isoformat()


def ensure_str(value: Any) -> str:
    """Return stripped string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def slugify(value: Any) -> str:
    """Convert text into a lowercase slug."""
    text = ensure_str(value).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "unknown"


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


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write a UTF-8 JSON file with indentation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


def load_json(path: Path) -> Dict[str, Any]:
    """Load JSON file into dict."""
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid JSON object payload: {path}")
    return payload


def chunked(values: Sequence[Any], chunk_size: int) -> Iterator[Sequence[Any]]:
    """Yield fixed-size chunks from a sequence."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    for index in range(0, len(values), chunk_size):
        yield values[index : index + chunk_size]


def connect_db(db_path: Path) -> sqlite3.Connection:
    """Open SQLite connection as Row mapping."""
    if not db_path.exists():
        raise FileNotFoundError(f"Database does not exist: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def load_vector_papers(
    conn: sqlite3.Connection,
    exclude_placeholder: bool,
    max_papers: int | None = None,
) -> List[VectorPaper]:
    """Load papers from SQLite for vector building."""
    where_sql = "WHERE record_status != 'placeholder'" if exclude_placeholder else ""
    limit_sql = ""
    params: List[Any] = []
    if max_papers is not None and max_papers > 0:
        limit_sql = "LIMIT ?"
        params.append(int(max_papers))

    rows = conn.execute(
        f"""
        SELECT
            paper_id,
            title,
            abstract,
            venue,
            year,
            track,
            presentation_level,
            record_status,
            source_file
        FROM papers
        {where_sql}
        ORDER BY year, venue, paper_id
        {limit_sql}
        """  # noqa: S608
        ,
        tuple(params),
    ).fetchall()

    papers: List[VectorPaper] = []
    for row in rows:
        papers.append(
            VectorPaper(
                paper_id=ensure_str(row["paper_id"]),
                title=ensure_str(row["title"]),
                abstract=ensure_str(row["abstract"]),
                venue=ensure_str(row["venue"]),
                year=int(row["year"]),
                track=ensure_str(row["track"]),
                presentation_level=ensure_str(row["presentation_level"]),
                record_status=ensure_str(row["record_status"]),
                source_file=ensure_str(row["source_file"]),
            )
        )
    return papers


def load_source_file_manifest(
    conn: sqlite3.Connection,
    source_files: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    """Load source-file metadata from SQLite manifest table."""
    result: Dict[str, Dict[str, Any]] = {}
    unique_source_files = sorted({ensure_str(item) for item in source_files if ensure_str(item)})
    if not unique_source_files:
        return result

    try:
        for chunk in chunked(unique_source_files, 500):
            placeholders = ",".join(["?"] * len(chunk))
            rows = conn.execute(
                f"""
                SELECT file_path, loaded_count, loaded_at_utc
                FROM source_files
                WHERE file_path IN ({placeholders})
                """  # noqa: S608
                ,
                tuple(chunk),
            ).fetchall()
            for row in rows:
                file_path = ensure_str(row["file_path"])
                if not file_path:
                    continue
                result[file_path] = {
                    "loaded_count": int(row["loaded_count"]),
                    "loaded_at_utc": ensure_str(row["loaded_at_utc"]),
                }
    except sqlite3.Error:
        LOGGER.warning("source_files table unavailable; vector marker will use derived file stats.")
    return result


def vector_marker_path(vectors_root: Path, collection_name: str) -> Path:
    """Return vector marker file path for a collection."""
    return vectors_root / f"{slugify(collection_name)}_vectorized_sources.json"


def load_vector_marker(path: Path) -> Dict[str, Any]:
    """Load vectorization marker from disk if exists."""
    if not path.exists():
        return {}
    try:
        payload = load_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        LOGGER.warning("Failed to read vector marker: %s", path)
        return {}
    source_files = payload.get("source_files")
    if not isinstance(source_files, dict):
        return {}
    return payload


def marker_hit(
    *,
    marker_entry: Dict[str, Any],
    loaded_count: int,
    loaded_at_utc: str,
    embed_model: str,
    embed_base_url: str,
    exclude_placeholder: bool,
) -> bool:
    """Check whether one source file can be skipped by marker."""
    return (
        ensure_str(marker_entry.get("status")) == "done"
        and int(marker_entry.get("loaded_count") or -1) == int(loaded_count)
        and ensure_str(marker_entry.get("loaded_at_utc")) == ensure_str(loaded_at_utc)
        and ensure_str(marker_entry.get("embed_model")) == ensure_str(embed_model)
        and ensure_str(marker_entry.get("embed_base_url")) == ensure_str(embed_base_url)
        and bool(marker_entry.get("exclude_placeholder")) == bool(exclude_placeholder)
    )


def upsert_vector_batch(
    collection: Any,
    *,
    ids: List[str],
    documents: List[str],
    embeddings: List[List[float]],
    metadatas: List[Dict[str, Any]],
) -> None:
    """Upsert one vector batch, falling back to add when unavailable."""
    if hasattr(collection, "upsert"):
        collection.upsert(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        return
    collection.add(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )


def make_embedding_client(base_url: str, api_key: str | None = None) -> Any:
    """Create OpenAI-compatible embedding client."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency `openai`. Install with: uv add openai"
        ) from exc
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency `httpx`. Install with: uv add httpx"
        ) from exc

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


def make_llm_client(base_url: str, api_key: str | None = None) -> Any:
    """Create OpenAI-compatible LLM client for topic naming."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency `openai`. Install with: uv add openai"
        ) from exc
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency `httpx`. Install with: uv add httpx"
        ) from exc

    normalized_base = normalize_openai_base_url(base_url)
    key = ensure_str(api_key) or ensure_str(os.getenv("JANUS_LLM_API_KEY"))
    if not key:
        raise ValueError(
            "JANUS_LLM_API_KEY is required for build-topics (LLM naming hard-fail policy)."
        )
    return OpenAI(
        base_url=normalized_base,
        api_key=key,
        http_client=httpx.Client(trust_env=False),
    )


def make_chroma_client(vectors_root: Path) -> Any:
    """Create Chroma persistent client."""
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency `chromadb`. Install with: uv add chromadb"
        ) from exc

    vectors_root.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(vectors_root))


def reset_chroma_collection(vectors_root: Path, collection_name: str) -> Any:
    """Delete and recreate Chroma collection."""
    client = make_chroma_client(vectors_root)
    try:
        client.delete_collection(collection_name)
    except Exception:  # noqa: BLE001
        pass
    return client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )


def load_chroma_collection(vectors_root: Path, collection_name: str) -> Any:
    """Load existing Chroma collection."""
    client = make_chroma_client(vectors_root)
    return client.get_or_create_collection(name=collection_name)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=16), reraise=True)
def embed_batch(client: Any, model: str, texts: Sequence[str]) -> List[List[float]]:
    """Embedding API call with retry."""
    response = client.embeddings.create(model=model, input=list(texts))
    vectors = [list(item.embedding) for item in response.data]
    if len(vectors) != len(texts):
        raise RuntimeError(
            f"Embedding response length mismatch: expected={len(texts)} actual={len(vectors)}"
        )
    return vectors


def _extract_json_object(text: str) -> Dict[str, Any]:
    """Extract first JSON object from LLM output."""
    raw = ensure_str(text)
    if not raw:
        raise ValueError("LLM output is empty")

    stripped = raw
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
        stripped = stripped.strip()

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if not match:
        raise ValueError("Unable to parse JSON object from LLM output")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM output JSON is not an object")
    return parsed


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=16), reraise=True)
def generate_topic_label(
    *,
    client: Any,
    model: str,
    level: str,
    sample_titles: Sequence[str],
    parent_topic: str | None = None,
) -> Dict[str, str]:
    """Generate topic/subtopic name and description via LLM."""
    trimmed_titles = [ensure_str(title) for title in sample_titles if ensure_str(title)]
    if not trimmed_titles:
        trimmed_titles = ["Untitled paper cluster"]

    parent_text = ensure_str(parent_topic)
    system_prompt = (
        "You are an ML taxonomy assistant. Return strict JSON only, no markdown. "
        'Schema: {"name":"...", "description":"..."}'
    )
    user_prompt = (
        f"Task: Name a {level} for AI research papers.\n"
        f"Parent topic: {parent_text or 'N/A'}\n"
        "Use concise naming, avoid generic words like 'misc'.\n"
        "Paper title samples:\n- "
        + "\n- ".join(trimmed_titles[:18])
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=128,
        timeout=90.0,
        response_format={"type": "json_object"},
    )
    content = ensure_str(response.choices[0].message.content if response.choices else "")
    payload = _extract_json_object(content)
    name = ensure_str(
        payload.get("name")
        or payload.get("topic")
        or payload.get("subtopic")
        or payload.get("label")
        or payload.get("title")
    )
    description = ensure_str(
        payload.get("description")
        or payload.get("summary")
        or payload.get("rationale")
        or payload.get("details")
    )
    if not name or not description:
        raise ValueError(
            f"LLM returned invalid {level} label payload: missing name/description fields"
        )
    return {"name": name, "description": description}


def run_build_vectors(
    *,
    db_path: Path,
    vectors_root: Path,
    collection_name: str,
    embed_base_url: str,
    embed_model: str,
    embed_batch_size: int,
    embed_cooldown_seconds: float,
    exclude_placeholder: bool,
    max_papers: int | None = None,
    embed_api_key: str | None = None,
    force_rebuild_vectors: bool = False,
) -> Dict[str, Any]:
    """Build Chroma vectors from SQLite papers."""
    if embed_batch_size <= 0:
        raise ValueError("--embed-batch-size must be > 0")
    if embed_cooldown_seconds < 0:
        raise ValueError("--embed-cooldown-seconds must be >= 0")

    conn = connect_db(db_path)
    try:
        papers = load_vector_papers(
            conn,
            exclude_placeholder=exclude_placeholder,
            max_papers=max_papers,
        )
        source_manifest = load_source_file_manifest(
            conn,
            source_files=[paper.source_file for paper in papers],
        )
    finally:
        conn.close()

    base_url = normalize_openai_base_url(embed_base_url)
    marker_path = vector_marker_path(vectors_root=vectors_root, collection_name=collection_name)
    marker_payload = load_vector_marker(marker_path)
    marker_source_files = marker_payload.get("source_files")
    if not isinstance(marker_source_files, dict):
        marker_source_files = {}

    if force_rebuild_vectors:
        collection = reset_chroma_collection(vectors_root=vectors_root, collection_name=collection_name)
        marker_source_files = {}
    else:
        collection = load_chroma_collection(vectors_root=vectors_root, collection_name=collection_name)
        if int(collection.count()) == 0 and marker_source_files:
            LOGGER.warning(
                "Collection is empty but vector marker exists. Marker will be ignored and all source files re-embedded."
            )
            marker_source_files = {}

    papers_by_source: Dict[str, List[VectorPaper]] = {}
    for paper in papers:
        source_file = paper.source_file or "__unknown__"
        papers_by_source.setdefault(source_file, []).append(paper)

    embed_client: Any | None = None
    embedded_count = 0
    skipped_empty = 0
    files_total = len(papers_by_source)
    files_embedded = 0
    files_skipped_by_marker = 0
    files_with_no_text = 0

    for source_file in sorted(papers_by_source):
        file_papers = papers_by_source[source_file]
        manifest = source_manifest.get(source_file, {})
        loaded_count = int(manifest.get("loaded_count") or len(file_papers))
        loaded_at_utc = ensure_str(manifest.get("loaded_at_utc"))

        marker_entry_raw = marker_source_files.get(source_file, {})
        marker_entry = marker_entry_raw if isinstance(marker_entry_raw, dict) else {}
        if (
            not force_rebuild_vectors
            and marker_hit(
                marker_entry=marker_entry,
                loaded_count=loaded_count,
                loaded_at_utc=loaded_at_utc,
                embed_model=embed_model,
                embed_base_url=base_url,
                exclude_placeholder=exclude_placeholder,
            )
        ):
            files_skipped_by_marker += 1
            continue

        rows_for_embedding: List[Tuple[VectorPaper, str]] = []
        skipped_empty_in_file = 0
        for paper in file_papers:
            text = f"{paper.title}\n\n{paper.abstract}".strip()
            if not text:
                skipped_empty_in_file += 1
                continue
            rows_for_embedding.append((paper, text))

        skipped_empty += skipped_empty_in_file
        if not rows_for_embedding:
            files_with_no_text += 1
            marker_source_files[source_file] = {
                "status": "done",
                "loaded_count": loaded_count,
                "loaded_at_utc": loaded_at_utc,
                "embedded_count": 0,
                "skipped_empty_text_count": skipped_empty_in_file,
                "embed_model": embed_model,
                "embed_base_url": base_url,
                "exclude_placeholder": bool(exclude_placeholder),
                "updated_at_utc": utc_now_iso(),
            }
            write_json(
                marker_path,
                {
                    "generated_at_utc": utc_now_iso(),
                    "db_path": str(db_path),
                    "collection_name": collection_name,
                    "source_files": marker_source_files,
                },
            )
            continue

        if embed_client is None:
            embed_client = make_embedding_client(base_url=embed_base_url, api_key=embed_api_key)

        embedded_in_file = 0
        for batch in chunked(rows_for_embedding, embed_batch_size):
            batch = list(batch)
            batch_papers = [item[0] for item in batch]
            batch_texts = [item[1] for item in batch]
            batch_vectors = embed_batch(embed_client, embed_model, batch_texts)

            upsert_vector_batch(
                collection,
                ids=[paper.paper_id for paper in batch_papers],
                documents=batch_texts,
                embeddings=batch_vectors,
                metadatas=[
                    {
                        "paper_id": paper.paper_id,
                        "title": paper.title,
                        "venue": paper.venue,
                        "year": int(paper.year),
                        "track": paper.track,
                        "presentation_level": paper.presentation_level,
                        "record_status": paper.record_status,
                    }
                    for paper in batch_papers
                ],
            )
            embedded_count += len(batch)
            embedded_in_file += len(batch)
            LOGGER.info(
                "Embedded %s papers from source file %s (%s/%s source files processed)",
                embedded_in_file,
                source_file,
                files_embedded + 1,
                files_total,
            )
            if embed_cooldown_seconds > 0:
                time.sleep(embed_cooldown_seconds)

        marker_source_files[source_file] = {
            "status": "done",
            "loaded_count": loaded_count,
            "loaded_at_utc": loaded_at_utc,
            "embedded_count": embedded_in_file,
            "skipped_empty_text_count": skipped_empty_in_file,
            "embed_model": embed_model,
            "embed_base_url": base_url,
            "exclude_placeholder": bool(exclude_placeholder),
            "updated_at_utc": utc_now_iso(),
        }
        write_json(
            marker_path,
            {
                "generated_at_utc": utc_now_iso(),
                "db_path": str(db_path),
                "collection_name": collection_name,
                "source_files": marker_source_files,
            },
        )
        files_embedded += 1

    payload = {
        "generated_at_utc": utc_now_iso(),
        "db_path": str(db_path),
        "vectors_root": str(vectors_root),
        "collection_name": collection_name,
        "embed_base_url": base_url,
        "embed_model": embed_model,
        "embed_cooldown_seconds": embed_cooldown_seconds,
        "exclude_placeholder": exclude_placeholder,
        "max_papers": max_papers,
        "force_rebuild_vectors": force_rebuild_vectors,
        "vector_marker_path": str(marker_path),
        "summary": {
            "db_candidate_count": len(papers),
            "embedded_count": embedded_count,
            "skipped_empty_text_count": skipped_empty,
            "collection_count": int(collection.count()),
            "source_file_count": files_total,
            "source_files_embedded": files_embedded,
            "source_files_skipped_by_marker": files_skipped_by_marker,
            "source_files_no_text": files_with_no_text,
        },
    }
    return payload


def _resolve_subcluster_count(size: int) -> int:
    """Resolve sub-cluster count in [3, 5] for medium/large clusters."""
    if size <= 1:
        return 1
    if size <= 3:
        return size
    if size >= 1200:
        return 5
    if size >= 300:
        return 4
    return 3


def _cluster_labels(
    embeddings: Sequence[Sequence[float]],
    *,
    cluster_count: int,
    random_seed: int,
) -> List[int]:
    """Cluster vectors with sklearn KMeans, with deterministic fallback."""
    if cluster_count <= 1 or len(embeddings) <= 1:
        return [0] * len(embeddings)
    if cluster_count >= len(embeddings):
        return list(range(len(embeddings)))

    try:
        import numpy as np
        from sklearn.cluster import KMeans

        array = np.asarray(embeddings, dtype=float)
        model = KMeans(
            n_clusters=cluster_count,
            random_state=random_seed,
            n_init=10,
        )
        return [int(value) for value in model.fit_predict(array)]
    except ImportError:
        LOGGER.warning(
            "numpy/scikit-learn not installed, using deterministic fallback clustering."
        )
        # Fallback: deterministic bucket split by index. This keeps pipeline testable
        # when sklearn stack is unavailable.
        return [index % cluster_count for index, _vector in enumerate(embeddings)]


def _load_vector_items(collection: Any) -> List[VectorItem]:
    """Load all vector items from Chroma collection."""
    count = int(collection.count())
    if count <= 0:
        return []

    step = 2000
    items: List[VectorItem] = []
    for offset in range(0, count, step):
        payload = collection.get(
            include=["embeddings", "documents", "metadatas"],
            limit=step,
            offset=offset,
        )
        ids = payload.get("ids", [])
        embeddings = payload.get("embeddings", [])
        documents = payload.get("documents", [])
        metadatas = payload.get("metadatas", [])
        if ids is None:
            ids = []
        if embeddings is None:
            embeddings = []
        if documents is None:
            documents = []
        if metadatas is None:
            metadatas = []
        if not (len(ids) == len(embeddings) == len(documents) == len(metadatas)):
            raise RuntimeError(
                "Vector collection payload size mismatch: ids/embeddings/documents/metadatas"
            )

        for index, paper_id in enumerate(ids):
            metadata = metadatas[index]
            if not isinstance(metadata, dict):
                metadata = {}
            items.append(
                VectorItem(
                    paper_id=ensure_str(paper_id),
                    embedding=[float(value) for value in embeddings[index]],
                    document=ensure_str(documents[index]),
                    metadata=metadata,
                )
            )
    return items


def _titles_from_items(items: Sequence[VectorItem], limit: int = 18) -> List[str]:
    """Extract representative titles from vector items."""
    titles: List[str] = []
    seen: set[str] = set()
    for item in items:
        raw = item.metadata.get("title") if isinstance(item.metadata, dict) else ""
        title = ensure_str(raw)
        if not title:
            title = ensure_str(item.document).split("\n", maxsplit=1)[0]
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        titles.append(title)
        if len(titles) >= limit:
            break
    if not titles:
        titles.append("Untitled cluster")
    return titles


def _build_topic_progress_seed(
    *,
    vectors_root: Path,
    collection_name: str,
    llm_base_url: str,
    llm_model: str,
    random_seed: int,
) -> Dict[str, Any]:
    """Build a new empty topic naming progress payload."""
    return {
        "version": 1,
        "generated_at_utc": utc_now_iso(),
        "updated_at_utc": utc_now_iso(),
        "status": "in_progress",
        "vectors_root": str(vectors_root),
        "collection_name": collection_name,
        "llm": {
            "base_url": normalize_openai_base_url(llm_base_url),
            "model": llm_model,
        },
        "random_seed": random_seed,
        "summary": {
            "resolved_topic_labels": 0,
            "resolved_subtopic_labels": 0,
        },
        "topics": {},
    }


def _progress_label_tuple(payload: Any) -> Tuple[str, str, str] | None:
    """Extract (name, description, slug) from checkpoint label payload."""
    if not isinstance(payload, dict):
        return None
    name = ensure_str(payload.get("name"))
    description = ensure_str(payload.get("description"))
    slug = ensure_str(payload.get("slug") or slugify(name))
    if not name or not description:
        return None
    return (name, description, slug)


def _refresh_topic_progress_summary(progress_payload: Dict[str, Any]) -> None:
    """Refresh progress label counters."""
    topics = progress_payload.get("topics")
    if not isinstance(topics, dict):
        topics = {}
        progress_payload["topics"] = topics

    resolved_topics = 0
    resolved_subtopics = 0
    for topic_payload in topics.values():
        if not isinstance(topic_payload, dict):
            continue
        if _progress_label_tuple(topic_payload.get("topic_label")) is not None:
            resolved_topics += 1
        subtopics = topic_payload.get("subtopics")
        if not isinstance(subtopics, dict):
            continue
        for sub_payload in subtopics.values():
            if _progress_label_tuple(sub_payload) is not None:
                resolved_subtopics += 1

    progress_payload["summary"] = {
        "resolved_topic_labels": resolved_topics,
        "resolved_subtopic_labels": resolved_subtopics,
    }


def _write_topic_progress(progress_path: Path, progress_payload: Dict[str, Any]) -> None:
    """Write progress file with refreshed summary and timestamp."""
    _refresh_topic_progress_summary(progress_payload)
    progress_payload["updated_at_utc"] = utc_now_iso()
    write_json(progress_path, progress_payload)


def _load_or_init_topic_progress(
    *,
    progress_path: Path,
    vectors_root: Path,
    collection_name: str,
    llm_base_url: str,
    llm_model: str,
    random_seed: int,
) -> Dict[str, Any]:
    """Load resume checkpoint for topic naming, or initialize a new one."""
    seed = _build_topic_progress_seed(
        vectors_root=vectors_root,
        collection_name=collection_name,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        random_seed=random_seed,
    )
    if not progress_path.exists():
        return seed

    try:
        payload = load_json(progress_path)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Failed to parse topic progress file %s (%s). Start fresh.", progress_path, exc)
        return seed

    expected = {
        "vectors_root": str(vectors_root),
        "collection_name": collection_name,
        "llm_base_url": normalize_openai_base_url(llm_base_url),
        "llm_model": llm_model,
        "random_seed": int(random_seed),
    }
    actual = {
        "vectors_root": ensure_str(payload.get("vectors_root")),
        "collection_name": ensure_str(payload.get("collection_name")),
        "llm_base_url": ensure_str((payload.get("llm") or {}).get("base_url")),
        "llm_model": ensure_str((payload.get("llm") or {}).get("model")),
        "random_seed": int(payload.get("random_seed") or -1),
    }
    if actual != expected:
        LOGGER.warning(
            "Topic progress metadata mismatch. Ignore previous checkpoint: expected=%s actual=%s",
            expected,
            actual,
        )
        return seed

    topics = payload.get("topics")
    if not isinstance(topics, dict):
        payload["topics"] = {}
    if not isinstance(payload.get("llm"), dict):
        payload["llm"] = seed["llm"]
    payload["status"] = "in_progress"
    _refresh_topic_progress_summary(payload)
    return payload


def run_build_topics(
    *,
    vectors_root: Path,
    collection_name: str,
    index_root: Path,
    llm_base_url: str,
    llm_model: str,
    llm_api_key: str | None = None,
    random_seed: int = DEFAULT_RANDOM_SEED,
) -> Dict[str, Any]:
    """Build primary/subtopic assignments from vectors using KMeans + LLM naming."""
    collection = load_chroma_collection(vectors_root=vectors_root, collection_name=collection_name)
    items = _load_vector_items(collection)
    if not items:
        raise RuntimeError("No vectors found. Run `build-vectors` first.")

    embeddings = [item.embedding for item in items]
    total = len(embeddings)
    primary_k = max(1, min(DEFAULT_PRIMARY_TOPIC_COUNT, total))

    primary_labels = _cluster_labels(
        embeddings,
        cluster_count=primary_k,
        random_seed=random_seed,
    )

    by_primary: Dict[int, List[int]] = {}
    for index, label in enumerate(primary_labels):
        by_primary.setdefault(int(label), []).append(index)

    skip_topic_llm = ensure_str(os.getenv("JANUS_M3_SKIP_TOPIC_LLM")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    skip_subtopic_llm = ensure_str(os.getenv("JANUS_M3_SKIP_SUBTOPIC_LLM")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if skip_topic_llm or skip_subtopic_llm:
        raise ValueError(
            "Local naming fallback is disabled. Unset JANUS_M3_SKIP_TOPIC_LLM/JANUS_M3_SKIP_SUBTOPIC_LLM."
        )

    llm_client = make_llm_client(base_url=llm_base_url, api_key=llm_api_key)
    m3_root = index_root / "m3"
    m3_root.mkdir(parents=True, exist_ok=True)
    progress_path = m3_root / DEFAULT_TOPICS_PROGRESS_FILE
    progress_payload = _load_or_init_topic_progress(
        progress_path=progress_path,
        vectors_root=vectors_root,
        collection_name=collection_name,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        random_seed=random_seed,
    )
    progress_topics = progress_payload.get("topics")
    if not isinstance(progress_topics, dict):
        progress_topics = {}
        progress_payload["topics"] = progress_topics

    topic_payloads: List[Dict[str, Any]] = []
    assignments: List[Dict[str, Any]] = []

    for topic_order, primary_label in enumerate(sorted(by_primary), start=1):
        topic_id = f"t{topic_order:02d}"
        member_indexes = by_primary[primary_label]
        topic_items = [items[idx] for idx in member_indexes]
        topic_titles = _titles_from_items(topic_items)

        topic_progress = progress_topics.get(topic_id)
        if not isinstance(topic_progress, dict):
            topic_progress = {}
        topic_progress["topic_id"] = topic_id
        topic_progress["topic_size"] = len(member_indexes)
        subtopic_progress = topic_progress.get("subtopics")
        if not isinstance(subtopic_progress, dict):
            subtopic_progress = {}
            topic_progress["subtopics"] = subtopic_progress
        topic_label_cached = _progress_label_tuple(topic_progress.get("topic_label"))
        if topic_label_cached is None:
            topic_label = generate_topic_label(
                client=llm_client,
                model=llm_model,
                level="topic",
                sample_titles=topic_titles,
            )
            topic_name = ensure_str(topic_label["name"])
            topic_desc = ensure_str(topic_label["description"])
            topic_slug = slugify(topic_name)
            topic_progress["topic_label"] = {
                "name": topic_name,
                "description": topic_desc,
                "slug": topic_slug,
                "generated_at_utc": utc_now_iso(),
            }
            progress_topics[topic_id] = topic_progress
            _write_topic_progress(progress_path, progress_payload)
            LOGGER.info("Topic label checkpointed: %s", topic_id)
        else:
            topic_name, topic_desc, topic_slug = topic_label_cached
            progress_topics[topic_id] = topic_progress

        subset_embeddings = [embeddings[idx] for idx in member_indexes]
        sub_k = min(_resolve_subcluster_count(len(member_indexes)), len(member_indexes))
        local_sub_labels = _cluster_labels(
            subset_embeddings,
            cluster_count=sub_k,
            random_seed=random_seed,
        )

        by_sub: Dict[int, List[int]] = {}
        for local_index, sub_label in enumerate(local_sub_labels):
            by_sub.setdefault(int(sub_label), []).append(member_indexes[local_index])

        sub_payloads: List[Dict[str, Any]] = []
        sub_label_to_payload: Dict[int, Dict[str, Any]] = {}
        subtopic_sizes = topic_progress.get("subtopic_sizes")
        if not isinstance(subtopic_sizes, dict):
            subtopic_sizes = {}
            topic_progress["subtopic_sizes"] = subtopic_sizes
        for sub_order, sub_label in enumerate(sorted(by_sub), start=1):
            sub_indexes = by_sub[sub_label]
            sub_items = [items[idx] for idx in sub_indexes]
            sub_titles = _titles_from_items(sub_items)
            sub_id = f"{topic_id}_s{sub_order:02d}"
            subtopic_sizes[sub_id] = len(sub_indexes)
            sub_cached = _progress_label_tuple(subtopic_progress.get(sub_id))
            if sub_cached is None:
                sub_topic = generate_topic_label(
                    client=llm_client,
                    model=llm_model,
                    level="subtopic",
                    sample_titles=sub_titles,
                    parent_topic=topic_name,
                )
                sub_name = ensure_str(sub_topic["name"])
                sub_desc = ensure_str(sub_topic["description"])
                sub_slug = slugify(sub_name)
                subtopic_progress[sub_id] = {
                    "name": sub_name,
                    "description": sub_desc,
                    "slug": sub_slug,
                    "generated_at_utc": utc_now_iso(),
                }
                progress_topics[topic_id] = topic_progress
                _write_topic_progress(progress_path, progress_payload)
                LOGGER.info("Subtopic label checkpointed: %s", sub_id)
            else:
                sub_name, sub_desc, sub_slug = sub_cached
            sub_payload = {
                "subtopic_id": sub_id,
                "subtopic_slug": sub_slug,
                "subtopic_name": sub_name,
                "description": sub_desc,
                "paper_count": len(sub_indexes),
            }
            sub_payloads.append(sub_payload)
            sub_label_to_payload[sub_label] = sub_payload

        topic_payloads.append(
            {
                "topic_id": topic_id,
                "topic_slug": topic_slug,
                "topic_name": topic_name,
                "description": topic_desc,
                "paper_count": len(member_indexes),
                "subtopics": sub_payloads,
            }
        )

        for local_index, global_index in enumerate(member_indexes):
            item = items[global_index]
            sub_label = local_sub_labels[local_index]
            sub_payload = sub_label_to_payload[sub_label]
            metadata = item.metadata if isinstance(item.metadata, dict) else {}
            assignments.append(
                {
                    "paper_id": item.paper_id,
                    "topic_id": topic_id,
                    "topic_slug": topic_slug,
                    "topic_name": topic_name,
                    "subtopic_id": sub_payload["subtopic_id"],
                    "subtopic_slug": sub_payload["subtopic_slug"],
                    "subtopic_name": sub_payload["subtopic_name"],
                    "venue": ensure_str(metadata.get("venue")),
                    "year": int(metadata.get("year")) if metadata.get("year") is not None else None,
                    "track": ensure_str(metadata.get("track")),
                    "presentation_level": ensure_str(metadata.get("presentation_level")),
                    "record_status": ensure_str(metadata.get("record_status")),
                }
            )

    topic_file = m3_root / DEFAULT_TOPICS_FILE
    payload = {
        "generated_at_utc": utc_now_iso(),
        "vectors_root": str(vectors_root),
        "collection_name": collection_name,
        "progress_file": str(progress_path),
        "llm": {
            "base_url": normalize_openai_base_url(llm_base_url),
            "model": llm_model,
        },
        "summary": {
            "paper_count": len(assignments),
            "topic_count": len(topic_payloads),
            "subtopic_count": sum(len(topic["subtopics"]) for topic in topic_payloads),
        },
        "topics": topic_payloads,
        "assignments": assignments,
    }
    write_json(topic_file, payload)
    progress_payload["status"] = "completed"
    progress_payload["completed_at_utc"] = utc_now_iso()
    progress_payload["output"] = {
        "topics_file": str(topic_file),
        "paper_count": len(assignments),
        "topic_count": len(topic_payloads),
        "subtopic_count": sum(len(topic["subtopics"]) for topic in topic_payloads),
    }
    _write_topic_progress(progress_path, progress_payload)
    LOGGER.info("M3 topic assignments written: %s", topic_file)
    return payload


def _fetch_papers_map(conn: sqlite3.Connection, paper_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Load paper rows for selected paper IDs."""
    if not paper_ids:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for chunk in chunked(list(paper_ids), 500):
        placeholders = ",".join(["?"] * len(chunk))
        rows = conn.execute(
            f"""
            SELECT paper_id, title, venue, year, track, presentation_level, citation_count, record_status
            FROM papers
            WHERE paper_id IN ({placeholders})
            """  # noqa: S608
            ,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            result[ensure_str(row["paper_id"])] = {
                "paper_id": ensure_str(row["paper_id"]),
                "title": ensure_str(row["title"]),
                "venue": ensure_str(row["venue"]),
                "year": int(row["year"]),
                "track": ensure_str(row["track"]),
                "presentation_level": ensure_str(row["presentation_level"]),
                "citation_count": row["citation_count"],
                "record_status": ensure_str(row["record_status"]),
            }
    return result


def _fetch_authors_map(conn: sqlite3.Connection, paper_ids: Sequence[str]) -> Dict[str, List[str]]:
    """Load authors grouped by paper ID."""
    if not paper_ids:
        return {}
    result: Dict[str, List[str]] = {}
    for chunk in chunked(list(paper_ids), 500):
        placeholders = ",".join(["?"] * len(chunk))
        rows = conn.execute(
            f"""
            SELECT paper_id, author_name
            FROM paper_authors
            WHERE paper_id IN ({placeholders})
            ORDER BY paper_id, author_index
            """  # noqa: S608
            ,
            tuple(chunk),
        ).fetchall()
        for row in rows:
            paper_id = ensure_str(row["paper_id"])
            result.setdefault(paper_id, []).append(ensure_str(row["author_name"]))
    return result


def _group_assignments(assignments: Sequence[Dict[str, Any]], key: str) -> Dict[str, List[Dict[str, Any]]]:
    """Group assignment rows by key string."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in assignments:
        value = ensure_str(item.get(key))
        grouped.setdefault(value, []).append(item)
    return grouped


def run_build_cache(
    *,
    db_path: Path,
    index_root: Path,
    master_index_path: Path,
    venues_root: Path,
    topics_root: Path,
    subtopics_root: Path,
) -> Dict[str, Any]:
    """Build L1-L4 markdown cache artifacts."""
    assignment_path = (index_root / "m3") / DEFAULT_TOPICS_FILE
    if not assignment_path.exists():
        raise FileNotFoundError(
            f"Missing topic assignments: {assignment_path}. Run `build-topics` first."
        )
    with assignment_path.open("r", encoding="utf-8") as handle:
        assignment_payload = json.load(handle)

    assignments = assignment_payload.get("assignments", [])
    topics = assignment_payload.get("topics", [])
    if not isinstance(assignments, list) or not isinstance(topics, list):
        raise ValueError("Invalid topic assignment file schema.")

    paper_ids = [ensure_str(item.get("paper_id")) for item in assignments if ensure_str(item.get("paper_id"))]
    conn = connect_db(db_path)
    try:
        papers_map = _fetch_papers_map(conn, paper_ids)
        authors_map = _fetch_authors_map(conn, paper_ids)
    finally:
        conn.close()

    # L1 master index
    venue_year_groups: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    for assignment in assignments:
        venue = ensure_str(assignment.get("venue"))
        year = assignment.get("year")
        if not venue or year is None:
            continue
        key = (venue, int(year))
        venue_year_groups.setdefault(key, []).append(assignment)

    master_lines = [
        "# JanusSearch Master Index",
        "",
        f"- Generated at: {utc_now_iso()}",
        f"- Papers with topic assignment: {len(assignments)}",
        f"- Topics: {len(topics)}",
        "",
        "## Venues",
    ]
    for (venue, year), group in sorted(venue_year_groups.items()):
        venue_slug = slugify(venue)
        venue_file = venues_root / venue_slug / f"{venue_slug}_{year}.md"
        rel = os.path.relpath(venue_file, start=master_index_path.parent)
        master_lines.append(f"- [{venue} {year}]({rel}) ({len(group)} papers)")

    master_lines.append("")
    master_lines.append("## Topics")
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        topic_name = ensure_str(topic.get("topic_name"))
        topic_slug = slugify(topic.get("topic_slug") or topic_name)
        topic_file = topics_root / f"{topic_slug}.md"
        rel = os.path.relpath(topic_file, start=master_index_path.parent)
        master_lines.append(
            f"- [{topic_name}]({rel}) ({int(topic.get('paper_count', 0))} papers)"
        )
    write_text(master_index_path, "\n".join(master_lines))

    # L2 venue/year pages
    venue_file_count = 0
    for (venue, year), group in sorted(venue_year_groups.items()):
        venue_slug = slugify(venue)
        out_path = venues_root / venue_slug / f"{venue_slug}_{year}.md"
        lines = [
            f"# {venue} {year}",
            "",
            f"- Papers: {len(group)}",
            "",
            "## Papers",
        ]
        sorted_group = sorted(
            group,
            key=lambda item: (
                ensure_str(item.get("topic_name")),
                ensure_str(item.get("subtopic_name")),
                ensure_str(item.get("paper_id")),
            ),
        )
        for assignment in sorted_group:
            paper_id = ensure_str(assignment.get("paper_id"))
            paper = papers_map.get(paper_id, {})
            title = ensure_str(paper.get("title")) or "(missing title)"
            topic_name = ensure_str(assignment.get("topic_name"))
            subtopic_name = ensure_str(assignment.get("subtopic_name"))
            lines.append(f"- `{paper_id}` {title} [{topic_name} / {subtopic_name}]")
        write_text(out_path, "\n".join(lines))
        venue_file_count += 1

    # L3 topic index + topic pages
    topic_index_lines = [
        "# Topic Index",
        "",
        f"- Generated at: {utc_now_iso()}",
        f"- Topic count: {len(topics)}",
        "",
    ]
    topic_file_count = 0
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        topic_id = ensure_str(topic.get("topic_id"))
        topic_name = ensure_str(topic.get("topic_name"))
        topic_slug = slugify(topic.get("topic_slug") or topic_name)
        topic_desc = ensure_str(topic.get("description"))
        topic_assignments = [
            item for item in assignments if ensure_str(item.get("topic_id")) == topic_id
        ]
        topic_out = topics_root / f"{topic_slug}.md"
        topic_index_lines.append(
            f"- [{topic_name}]({topic_slug}.md) ({len(topic_assignments)} papers)"
        )

        lines = [
            f"# {topic_name}",
            "",
            topic_desc,
            "",
            f"- Papers: {len(topic_assignments)}",
            f"- Subtopics: {len(topic.get('subtopics', []))}",
            "",
            "## Subtopics",
        ]
        for subtopic in topic.get("subtopics", []) or []:
            if not isinstance(subtopic, dict):
                continue
            sub_name = ensure_str(subtopic.get("subtopic_name"))
            sub_slug = slugify(subtopic.get("subtopic_slug") or sub_name)
            lines.append(
                f"- {sub_name} (`{sub_slug}`) ({int(subtopic.get('paper_count', 0))} papers)"
            )

        lines.append("")
        lines.append("## Representative Papers")
        for assignment in topic_assignments[:80]:
            paper_id = ensure_str(assignment.get("paper_id"))
            paper = papers_map.get(paper_id, {})
            title = ensure_str(paper.get("title")) or "(missing title)"
            authors = authors_map.get(paper_id, [])
            lead_authors = ", ".join(authors[:3]) if authors else "N/A"
            lines.append(f"- `{paper_id}` {title} — {lead_authors}")

        write_text(topic_out, "\n".join(lines))
        topic_file_count += 1
    write_text(topics_root / "_topic_index.md", "\n".join(topic_index_lines))

    # L4 subtopic files
    subtopic_file_count = 0
    overview_file_count = 0
    assignments_by_topic = _group_assignments(assignments, "topic_id")
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        topic_id = ensure_str(topic.get("topic_id"))
        topic_name = ensure_str(topic.get("topic_name"))
        topic_slug = slugify(topic.get("topic_slug") or topic_name)
        topic_dir = subtopics_root / topic_slug
        topic_subs = topic.get("subtopics", []) or []

        overview_lines = [
            f"# {topic_name} — Subtopic Overview",
            "",
            f"- Subtopics: {len(topic_subs)}",
            f"- Papers: {len(assignments_by_topic.get(topic_id, []))}",
            "",
            "## Subtopics",
        ]
        for subtopic in topic_subs:
            if not isinstance(subtopic, dict):
                continue
            sub_name = ensure_str(subtopic.get("subtopic_name"))
            sub_slug = slugify(subtopic.get("subtopic_slug") or sub_name)
            overview_lines.append(
                f"- [{sub_name}]({sub_slug}.md) ({int(subtopic.get('paper_count', 0))} papers)"
            )
        write_text(topic_dir / "_overview.md", "\n".join(overview_lines))
        overview_file_count += 1

        topic_assignments = assignments_by_topic.get(topic_id, [])
        for subtopic in topic_subs:
            if not isinstance(subtopic, dict):
                continue
            sub_id = ensure_str(subtopic.get("subtopic_id"))
            sub_name = ensure_str(subtopic.get("subtopic_name"))
            sub_desc = ensure_str(subtopic.get("description"))
            sub_slug = slugify(subtopic.get("subtopic_slug") or sub_name)
            out_path = topic_dir / f"{sub_slug}.md"
            sub_assignments = [
                item for item in topic_assignments if ensure_str(item.get("subtopic_id")) == sub_id
            ]

            lines = [
                f"# {sub_name}",
                "",
                sub_desc,
                "",
                f"- Papers: {len(sub_assignments)}",
                "",
                "## Papers",
            ]
            sorted_sub = sorted(
                sub_assignments,
                key=lambda item: (
                    -int(item.get("year") or 0),
                    ensure_str(item.get("venue")),
                    ensure_str(item.get("paper_id")),
                ),
            )
            for assignment in sorted_sub:
                paper_id = ensure_str(assignment.get("paper_id"))
                paper = papers_map.get(paper_id, {})
                title = ensure_str(paper.get("title")) or "(missing title)"
                venue = ensure_str(assignment.get("venue")) or ensure_str(paper.get("venue"))
                year = assignment.get("year") or paper.get("year")
                lines.append(f"- `{paper_id}` {title} ({venue} {year})")
            write_text(out_path, "\n".join(lines))
            subtopic_file_count += 1

    payload = {
        "generated_at_utc": utc_now_iso(),
        "summary": {
            "assigned_paper_count": len(assignments),
            "venue_page_count": venue_file_count,
            "topic_page_count": topic_file_count,
            "subtopic_overview_count": overview_file_count,
            "subtopic_page_count": subtopic_file_count,
            "master_index_path": str(master_index_path),
        }
    }
    return payload


def _validate_cache_files(
    *,
    master_index_path: Path,
    topics_root: Path,
    venues_root: Path,
    subtopics_root: Path,
    assignments: Sequence[Dict[str, Any]],
    topics: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Check required cache files exist."""
    missing: List[str] = []
    existing = 0

    required_static = [master_index_path, topics_root / "_topic_index.md"]
    for path in required_static:
        if path.exists():
            existing += 1
        else:
            missing.append(str(path))

    venue_year_pairs = {
        (slugify(item.get("venue")), int(item.get("year")))
        for item in assignments
        if ensure_str(item.get("venue")) and item.get("year") is not None
    }
    for venue_slug, year in sorted(venue_year_pairs):
        path = venues_root / venue_slug / f"{venue_slug}_{year}.md"
        if path.exists():
            existing += 1
        else:
            missing.append(str(path))

    for topic in topics:
        if not isinstance(topic, dict):
            continue
        topic_name = ensure_str(topic.get("topic_name"))
        topic_slug = slugify(topic.get("topic_slug") or topic_name)
        topic_file = topics_root / f"{topic_slug}.md"
        if topic_file.exists():
            existing += 1
        else:
            missing.append(str(topic_file))

        overview = subtopics_root / topic_slug / "_overview.md"
        if overview.exists():
            existing += 1
        else:
            missing.append(str(overview))

        for sub in topic.get("subtopics", []) or []:
            if not isinstance(sub, dict):
                continue
            sub_name = ensure_str(sub.get("subtopic_name"))
            sub_slug = slugify(sub.get("subtopic_slug") or sub_name)
            path = subtopics_root / topic_slug / f"{sub_slug}.md"
            if path.exists():
                existing += 1
            else:
                missing.append(str(path))

    return {
        "expected_file_count": existing + len(missing),
        "existing_file_count": existing,
        "missing_file_count": len(missing),
        "missing_files": missing,
    }


def run_validate(
    *,
    db_path: Path,
    vectors_root: Path,
    collection_name: str,
    index_root: Path,
    master_index_path: Path,
    venues_root: Path,
    topics_root: Path,
    subtopics_root: Path,
    exclude_placeholder: bool,
    max_papers: int | None = None,
) -> Dict[str, Any]:
    """Validate M3 outputs: vectors, assignments, cache."""
    m3_root = index_root / "m3"
    assignment_path = m3_root / DEFAULT_TOPICS_FILE
    validate_report_path = m3_root / DEFAULT_VALIDATE_REPORT
    if not assignment_path.exists():
        raise FileNotFoundError(f"Missing topic assignment file: {assignment_path}")

    with assignment_path.open("r", encoding="utf-8") as handle:
        assignment_payload = json.load(handle)
    assignments = assignment_payload.get("assignments", [])
    topics = assignment_payload.get("topics", [])
    if not isinstance(assignments, list) or not isinstance(topics, list):
        raise ValueError("Invalid topic assignment file schema.")

    conn = connect_db(db_path)
    try:
        if exclude_placeholder:
            expected_vector_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM papers WHERE record_status != 'placeholder'"
                ).fetchone()[0]
            )
        else:
            expected_vector_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        if max_papers is not None and max_papers > 0:
            expected_vector_count = min(expected_vector_count, int(max_papers))
        all_paper_ids = {
            ensure_str(row["paper_id"])
            for row in conn.execute("SELECT paper_id FROM papers").fetchall()
        }
    finally:
        conn.close()

    collection = load_chroma_collection(vectors_root=vectors_root, collection_name=collection_name)
    vector_count = int(collection.count())

    assigned_ids = [ensure_str(item.get("paper_id")) for item in assignments if ensure_str(item.get("paper_id"))]
    unique_assigned_ids = set(assigned_ids)
    missing_in_db = sorted(pid for pid in unique_assigned_ids if pid not in all_paper_ids)

    cache_status = _validate_cache_files(
        master_index_path=master_index_path,
        topics_root=topics_root,
        venues_root=venues_root,
        subtopics_root=subtopics_root,
        assignments=assignments,
        topics=topics,
    )

    checks: List[Dict[str, Any]] = []
    issues: List[str] = []

    def add_check(name: str, expected: Any, actual: Any) -> None:
        passed = expected == actual
        checks.append({"name": name, "pass": passed, "expected": expected, "actual": actual})
        if not passed:
            issues.append(f"{name} mismatch")

    add_check("vector_count", expected_vector_count, vector_count)
    add_check("assignment_count", vector_count, len(assignments))
    add_check("unique_assignment_count", len(assignments), len(unique_assigned_ids))
    add_check("assignment_missing_in_db", 0, len(missing_in_db))
    add_check("cache_missing_files", 0, cache_status["missing_file_count"])

    report = {
        "summary": {
            "generated_at_utc": utc_now_iso(),
            "all_pass": len(issues) == 0,
            "issue_count": len(issues),
            "db_path": str(db_path),
            "vectors_root": str(vectors_root),
            "collection_name": collection_name,
            "exclude_placeholder": exclude_placeholder,
            "max_papers": max_papers,
        },
        "checks": checks,
        "issues": issues,
        "details": {
            "expected_vector_count": expected_vector_count,
            "vector_count": vector_count,
            "assignment_count": len(assignments),
            "unique_assignment_count": len(unique_assigned_ids),
            "missing_paper_ids_in_db": missing_in_db,
            "cache": cache_status,
        },
    }
    write_json(validate_report_path, report)
    LOGGER.info("M3 validate report written: %s", validate_report_path)
    return report


def run_pipeline(
    *,
    db_path: Path,
    vectors_root: Path,
    collection_name: str,
    index_root: Path,
    master_index_path: Path,
    venues_root: Path,
    topics_root: Path,
    subtopics_root: Path,
    embed_base_url: str,
    embed_model: str,
    embed_batch_size: int,
    embed_cooldown_seconds: float,
    llm_base_url: str,
    llm_model: str,
    exclude_placeholder: bool,
    force_rebuild_vectors: bool = False,
    max_papers: int | None = None,
    embed_api_key: str | None = None,
    llm_api_key: str | None = None,
) -> Dict[str, Any]:
    """Run full M3 sequence and persist build report."""
    build_report_path = (index_root / "m3") / DEFAULT_BUILD_REPORT
    steps: Dict[str, Any] = {}
    status = "success"
    error_message = None
    try:
        steps["build_vectors"] = run_build_vectors(
            db_path=db_path,
            vectors_root=vectors_root,
            collection_name=collection_name,
            embed_base_url=embed_base_url,
            embed_model=embed_model,
            embed_batch_size=embed_batch_size,
            embed_cooldown_seconds=embed_cooldown_seconds,
            exclude_placeholder=exclude_placeholder,
            force_rebuild_vectors=force_rebuild_vectors,
            max_papers=max_papers,
            embed_api_key=embed_api_key,
        )
        build_topics_payload = run_build_topics(
            vectors_root=vectors_root,
            collection_name=collection_name,
            index_root=index_root,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            llm_api_key=llm_api_key,
        )
        steps["build_topics"] = {
            "generated_at_utc": build_topics_payload.get("generated_at_utc"),
            "progress_file": build_topics_payload.get("progress_file"),
            "llm": build_topics_payload.get("llm"),
            "summary": build_topics_payload.get("summary"),
        }
        steps["build_cache"] = run_build_cache(
            db_path=db_path,
            index_root=index_root,
            master_index_path=master_index_path,
            venues_root=venues_root,
            topics_root=topics_root,
            subtopics_root=subtopics_root,
        )
        steps["validate"] = run_validate(
            db_path=db_path,
            vectors_root=vectors_root,
            collection_name=collection_name,
            index_root=index_root,
            master_index_path=master_index_path,
            venues_root=venues_root,
            topics_root=topics_root,
            subtopics_root=subtopics_root,
            exclude_placeholder=exclude_placeholder,
            max_papers=max_papers,
        )
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        error_message = f"{exc.__class__.__name__}: {exc}"
        LOGGER.error("M3 run failed: %s", error_message)
        LOGGER.debug("Traceback:\n%s", traceback.format_exc())

    build_report = {
        "summary": {
            "generated_at_utc": utc_now_iso(),
            "status": status,
            "error_message": error_message,
            "db_path": str(db_path),
            "vectors_root": str(vectors_root),
            "collection_name": collection_name,
            "exclude_placeholder": exclude_placeholder,
            "force_rebuild_vectors": force_rebuild_vectors,
            "max_papers": max_papers,
        },
        "steps": steps,
    }
    write_json(build_report_path, build_report)
    LOGGER.info("M3 build report written: %s", build_report_path)

    if status != "success":
        raise RuntimeError(error_message or "M3 run failed")
    return build_report


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(description="M3 pipeline for vectors, topics, and cache")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite db path (default: {DEFAULT_DB_PATH})",
    )
    common.add_argument(
        "--index-root",
        default=str(DEFAULT_INDEX_ROOT),
        help=f"Artifacts output root (default: {DEFAULT_INDEX_ROOT})",
    )
    common.add_argument(
        "--vectors-root",
        default=str(DEFAULT_VECTORS_ROOT),
        help=f"Chroma root directory (default: {DEFAULT_VECTORS_ROOT})",
    )
    common.add_argument(
        "--collection-name",
        default=DEFAULT_COLLECTION_NAME,
        help=f"Chroma collection name (default: {DEFAULT_COLLECTION_NAME})",
    )
    common.add_argument(
        "--master-index-path",
        default=None,
        help="Master index markdown path (default: <index-root>/indexes/master_index.md)",
    )
    common.add_argument(
        "--venues-root",
        default=str(DEFAULT_VENUES_ROOT),
        help=f"Venues cache root (default: {DEFAULT_VENUES_ROOT})",
    )
    common.add_argument(
        "--topics-root",
        default=str(DEFAULT_TOPICS_ROOT),
        help=f"Topics cache root (default: {DEFAULT_TOPICS_ROOT})",
    )
    common.add_argument(
        "--subtopics-root",
        default=str(DEFAULT_SUBTOPICS_ROOT),
        help=f"Subtopics cache root (default: {DEFAULT_SUBTOPICS_ROOT})",
    )
    common.add_argument(
        "--embed-base-url",
        default=ensure_str(os.getenv("JANUS_EMBED_BASE_URL")) or DEFAULT_EMBED_BASE_URL,
        help=(
            "Embedding endpoint base URL (OpenAI-compatible). "
            f"(default env JANUS_EMBED_BASE_URL or {DEFAULT_EMBED_BASE_URL})"
        ),
    )
    common.add_argument(
        "--embed-model",
        default=DEFAULT_EMBED_MODEL,
        help=f"Embedding model name (default: {DEFAULT_EMBED_MODEL})",
    )
    common.add_argument(
        "--embed-batch-size",
        type=int,
        default=DEFAULT_EMBED_BATCH_SIZE,
        help=f"Embedding batch size (default: {DEFAULT_EMBED_BATCH_SIZE})",
    )
    common.add_argument(
        "--embed-cooldown-seconds",
        type=float,
        default=0.0,
        help="Cooldown sleep seconds after each embedding batch (default: 0)",
    )
    common.add_argument(
        "--embed-api-key",
        default=None,
        help="Embedding API key (optional, env fallback JANUS_EMBED_API_KEY / JANUS_LLM_API_KEY)",
    )
    common.add_argument(
        "--force-rebuild-vectors",
        action="store_true",
        help="Force vector rebuild and ignore per-source-file vectorization marker.",
    )
    common.add_argument(
        "--max-papers",
        type=int,
        default=0,
        help="Max papers to vectorize/validate in this run; 0 means all papers.",
    )
    common.add_argument(
        "--llm-base-url",
        default=ensure_str(os.getenv("JANUS_LLM_BASE_URL")) or DEFAULT_LLM_BASE_URL,
        help=f"LLM endpoint base URL (default env JANUS_LLM_BASE_URL or {DEFAULT_LLM_BASE_URL})",
    )
    common.add_argument(
        "--llm-model",
        default=ensure_str(os.getenv("JANUS_LLM_MODEL")) or DEFAULT_LLM_MODEL,
        help=f"LLM model (default env JANUS_LLM_MODEL or {DEFAULT_LLM_MODEL})",
    )
    common.add_argument(
        "--llm-api-key",
        default=None,
        help="LLM API key (optional, env fallback JANUS_LLM_API_KEY)",
    )
    common.add_argument(
        "--exclude-placeholder",
        dest="exclude_placeholder",
        action="store_true",
        help="Exclude placeholder records from vectors/hybrid (default).",
    )
    common.add_argument(
        "--include-placeholder",
        dest="exclude_placeholder",
        action="store_false",
        help="Include placeholder records in vectors/hybrid.",
    )
    common.set_defaults(exclude_placeholder=True)
    common.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Log level",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "build-vectors",
        parents=[common],
        help="Build Chroma vectors from SQLite papers",
    )
    subparsers.add_parser(
        "build-topics",
        parents=[common],
        help="Build topic/subtopic assignments from vectors",
    )
    subparsers.add_parser(
        "build-cache",
        parents=[common],
        help="Build L1-L4 markdown cache files",
    )
    subparsers.add_parser(
        "validate",
        parents=[common],
        help="Validate M3 artifacts",
    )
    subparsers.add_parser(
        "run",
        parents=[common],
        help="Run full sequence: vectors -> topics -> cache -> validate",
    )
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
    index_root = Path(args.index_root)
    vectors_root = Path(args.vectors_root)
    collection_name = ensure_str(args.collection_name) or DEFAULT_COLLECTION_NAME
    master_index_path = (
        Path(args.master_index_path)
        if ensure_str(args.master_index_path)
        else index_root / "indexes" / "master_index.md"
    )
    venues_root = Path(args.venues_root)
    topics_root = Path(args.topics_root)
    subtopics_root = Path(args.subtopics_root)

    try:
        max_papers = int(args.max_papers) if int(args.max_papers) > 0 else None
        if args.command == "build-vectors":
            payload = run_build_vectors(
                db_path=db_path,
                vectors_root=vectors_root,
                collection_name=collection_name,
                embed_base_url=args.embed_base_url,
                embed_model=args.embed_model,
                embed_batch_size=args.embed_batch_size,
                embed_cooldown_seconds=args.embed_cooldown_seconds,
                exclude_placeholder=bool(args.exclude_placeholder),
                force_rebuild_vectors=bool(args.force_rebuild_vectors),
                max_papers=max_papers,
                embed_api_key=args.embed_api_key,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "build-topics":
            payload = run_build_topics(
                vectors_root=vectors_root,
                collection_name=collection_name,
                index_root=index_root,
                llm_base_url=args.llm_base_url,
                llm_model=args.llm_model,
                llm_api_key=args.llm_api_key,
            )
            print(
                json.dumps(
                    {
                        "generated_at_utc": payload.get("generated_at_utc"),
                        "vectors_root": payload.get("vectors_root"),
                        "collection_name": payload.get("collection_name"),
                        "progress_file": payload.get("progress_file"),
                        "llm": payload.get("llm"),
                        "summary": payload.get("summary"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.command == "build-cache":
            payload = run_build_cache(
                db_path=db_path,
                index_root=index_root,
                master_index_path=master_index_path,
                venues_root=venues_root,
                topics_root=topics_root,
                subtopics_root=subtopics_root,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        if args.command == "validate":
            payload = run_validate(
                db_path=db_path,
                vectors_root=vectors_root,
                collection_name=collection_name,
                index_root=index_root,
                master_index_path=master_index_path,
                venues_root=venues_root,
                topics_root=topics_root,
                subtopics_root=subtopics_root,
                exclude_placeholder=bool(args.exclude_placeholder),
                max_papers=max_papers,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if bool(payload["summary"]["all_pass"]) else 1

        if args.command == "run":
            payload = run_pipeline(
                db_path=db_path,
                vectors_root=vectors_root,
                collection_name=collection_name,
                index_root=index_root,
                master_index_path=master_index_path,
                venues_root=venues_root,
                topics_root=topics_root,
                subtopics_root=subtopics_root,
                embed_base_url=args.embed_base_url,
                embed_model=args.embed_model,
                embed_batch_size=args.embed_batch_size,
                embed_cooldown_seconds=args.embed_cooldown_seconds,
                llm_base_url=args.llm_base_url,
                llm_model=args.llm_model,
                exclude_placeholder=bool(args.exclude_placeholder),
                force_rebuild_vectors=bool(args.force_rebuild_vectors),
                max_papers=max_papers,
                embed_api_key=args.embed_api_key,
                llm_api_key=args.llm_api_key,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            validate_step = payload.get("steps", {}).get("validate", {})
            all_pass = bool(validate_step.get("summary", {}).get("all_pass"))
            return 0 if all_pass else 1

        parser.error(f"Unknown command: {args.command}")
        return 2
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
