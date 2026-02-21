# JanusSearch

JanusSearch is a local AI paper vault and retrieval system.
It supports structured data ingestion, SQLite + FTS retrieval, vector retrieval, topic cache generation, and end-to-end validation.

## Project Goals

- Build a reproducible local paper search workflow for top AI venues.
- Keep `data/raw` as canonical facts.
- Provide CLI-only operations for data processing and retrieval.
- Support natural-language driven operation through skill routing.

## Current Milestone Status

- M1: data normalization baseline frozen.
- M2: SQLite ingestion + FTS retrieval available.
- M3: vector store + topic/subtopic cache + hybrid retrieval available.
- M4: cloud-gated end-to-end validation pipeline available.

## Core Architecture

- Canonical data: `data/raw/{venue}/{year}.json`
- Database: `data/papers.db` (SQLite)
- FTS index: `papers_fts` (title + abstract)
- Vector store: `data/vectors/chroma`
- Cache outputs:
  - `index/master_index.md`
  - `venues/`
  - `topics/`
  - `subtopics/`

## Prerequisites

- Python 3.11+
- `uv`
- macOS / Unix shell

## Setup

```bash
uv sync
uv run python -V
```

## Common Commands

### Search

```bash
python3 -m tools.search search --query "continual learning replay"
python3 -m tools.search hybrid --query "continual learning replay" --top-k 20
python3 -m tools.search get --paper-id <PAPER_ID>
python3 -m tools.search stats
```

### M2 (DB + FTS)

```bash
python3 -m tools.m2_db run
python3 -m tools.m2_db reindex-fts
python3 -m tools.m2_db validate
```

### M3 (Vectors + Cache)

```bash
python3 -m tools.m3_pipeline run \
  --db-path data/papers.db \
  --embed-base-url https://api.siliconflow.cn/v1/embeddings \
  --embed-model Qwen/Qwen3-Embedding-8B \
  --exclude-placeholder
```

### M4 (Formal Validation)

```bash
export JANUS_EMBED_API_KEY="<YOUR_KEY>"

python3 -m tools.m4_validate run \
  --db-path data/papers.db \
  --vectors-root data/vectors/chroma \
  --collection-name papers_v1 \
  --topics-file index/m3_topic_assignments.json \
  --fixed-query-file docs/fixtures/m4_fixed_queries.yaml \
  --embed-base-url https://api.siliconflow.cn/v1/embeddings \
  --embed-model Qwen/Qwen3-Embedding-8B

python3 -m tools.m4_validate status
```

## Reports and Outputs

- `index/m1_quality_report.json`
- `index/m2_load_report.json`
- `index/m2_validate_report.json`
- `index/m3_build_report.json`
- `index/m3_validate_report.json`
- `index/m4_eval_report.json`
- `index/m4_eval_report.md`

## Documentation Entry

- `docs/README.md`
- `docs/21_M2_SEARCH_CLI.md`
- `docs/22_M3_CACHE_AND_HYBRID.md`
- `docs/30_M4_AGENT_VALIDATION.md`

## Notes

- Root-level `ICLR-*.json`, `ICML-*.json`, `NeurIPS-*.json` are historical files kept for traceability.
- Canonical operational source is `data/raw`.
