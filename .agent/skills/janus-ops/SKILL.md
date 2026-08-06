---
name: janus-ops
description: Diagnose and maintain JanusSearch runtime artifacts. Use for SQLite/FTS repair, vector/topic/cache rebuilds, Chroma health issues, stale evaluation reports, release validation, regression checks, or operational troubleshooting that is not corpus collection or ordinary paper search.
---

# Janus Operations

Diagnose first, repair only when explicitly requested, then validate every downstream dependency affected by the repair.

## Diagnose

Choose the smallest read-only profile:

```bash
./.venv/bin/python -m tools.doctor --profile query
./.venv/bin/python -m tools.doctor --profile corpus
./.venv/bin/python -m tools.doctor --profile ops
```

Report the failing check and evidence. Do not mutate state for a diagnosis-only request.

## Repair routing

| Problem | Minimal explicit repair | Required validation |
|---|---|---|
| Missing/broken FTS only | `tools.catalog reindex-fts` | `tools.catalog validate` and FTS smoke query |
| Catalog inconsistent with canonical JSON | `tools.catalog build` | `tools.catalog validate` |
| Vectors missing/stale | `tools.projections build-vectors` | `tools.projections validate` |
| Topics missing/stale | `tools.projections build-topics` | `tools.projections validate` |
| Markdown caches missing/stale | `tools.projections build-cache` | `tools.projections validate` |
| Complete derived-state rebuild | `tools.projections run` | built-in projection validation |

SQLite builds publish atomically and must preserve the old database on failure. Chroma and cache operations update in place; rely on stable paper IDs, fingerprints, progress artifacts, and rerunnable commands instead of claiming transactional replacement.

Silently source `.codex/.env` before embedding or LLM operations when present. Never print credential values.

## Evaluate

Run deterministic offline evaluation by default:

```bash
./.venv/bin/python -m tools.evaluate run --suite offline
./.venv/bin/python -m tools.evaluate status
```

Run `--suite online` or `--suite all` only when explicitly requested and a valid embedding key is available. `status` must reject a prior PASS if database, vectors, topics, or fixed-query fixtures changed.

After repair, run the smallest relevant smoke query:

```bash
./.venv/bin/python -m tools.search search \
  --query "continual learning replay" --top-k 20
```

If the reported fault involved hybrid retrieval, also rerun a hybrid smoke query after the FTS/catalog check. Report a hybrid error explicitly; do not silently replace it with the successful FTS result.

Read `references/recovery-matrix.md` for failure ordering, report paths, and release validation. Always report commands actually run, exit codes, key metrics, manifests, and remaining risks.
