#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vector, topic, and cache projection application workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

from tools import m3_pipeline as legacy


def execute(args: Any) -> Tuple[Dict[str, Any], bool]:
    """Execute one projection operation from parsed compatible arguments."""
    db_path = Path(args.db_path)
    index_root = Path(args.index_root)
    vectors_root = Path(args.vectors_root)
    collection_name = legacy.ensure_str(args.collection_name) or legacy.DEFAULT_COLLECTION_NAME
    master_index_path = (
        Path(args.master_index_path)
        if legacy.ensure_str(args.master_index_path)
        else index_root / "indexes" / "master_index.md"
    )
    venues_root = Path(args.venues_root)
    topics_root = Path(args.topics_root)
    subtopics_root = Path(args.subtopics_root)
    max_papers = int(args.max_papers) if int(args.max_papers) > 0 else None

    common = {
        "db_path": db_path,
        "vectors_root": vectors_root,
        "collection_name": collection_name,
        "index_root": index_root,
        "master_index_path": master_index_path,
        "venues_root": venues_root,
        "topics_root": topics_root,
        "subtopics_root": subtopics_root,
    }
    if args.command == "build-vectors":
        payload = legacy.run_build_vectors(
            db_path=db_path,
            vectors_root=vectors_root,
            collection_name=collection_name,
            embed_base_url=args.embed_base_url,
            embed_model=args.embed_model,
            embed_batch_size=args.embed_batch_size,
            embed_timeout_seconds=args.embed_timeout_seconds,
            embed_cooldown_seconds=args.embed_cooldown_seconds,
            exclude_placeholder=bool(args.exclude_placeholder),
            force_rebuild_vectors=bool(args.force_rebuild_vectors),
            max_papers=max_papers,
            embed_api_key=args.embed_api_key,
        )
        return payload, True
    if args.command == "build-topics":
        payload = legacy.run_build_topics(
            vectors_root=vectors_root,
            collection_name=collection_name,
            index_root=index_root,
            llm_base_url=args.llm_base_url,
            llm_model=args.llm_model,
            llm_api_key=args.llm_api_key,
        )
        return payload, True
    if args.command == "build-cache":
        payload = legacy.run_build_cache(
            db_path=db_path,
            index_root=index_root,
            master_index_path=master_index_path,
            venues_root=venues_root,
            topics_root=topics_root,
            subtopics_root=subtopics_root,
        )
        return payload, True
    if args.command == "validate":
        payload = legacy.run_validate(
            **common,
            exclude_placeholder=bool(args.exclude_placeholder),
            max_papers=max_papers,
        )
        return payload, bool(payload.get("summary", {}).get("all_pass"))
    if args.command == "run":
        payload = legacy.run_pipeline(
            **common,
            embed_base_url=args.embed_base_url,
            embed_model=args.embed_model,
            embed_batch_size=args.embed_batch_size,
            embed_timeout_seconds=args.embed_timeout_seconds,
            embed_cooldown_seconds=args.embed_cooldown_seconds,
            llm_base_url=args.llm_base_url,
            llm_model=args.llm_model,
            exclude_placeholder=bool(args.exclude_placeholder),
            force_rebuild_vectors=bool(args.force_rebuild_vectors),
            max_papers=max_papers,
            embed_api_key=args.embed_api_key,
            llm_api_key=args.llm_api_key,
        )
        validate_step = payload.get("steps", {}).get("validate", {})
        return payload, bool(validate_step.get("summary", {}).get("all_pass"))
    raise ValueError(f"Unknown projections operation: {args.command}")

