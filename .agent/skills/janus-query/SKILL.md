---
name: janus-query
description: Search and retrieve papers from the local JanusSearch catalog. Use for topic or related-work discovery, venue/year/track filtering, paper details, catalog statistics, auditable TSV exports, keyword grouping, and explicitly requested public PDF downloads.
---

# Janus Query

Use a local, auditable, FTS-first retrieval workflow. Do not mutate the corpus, catalog, vectors, or caches.

## Workflow

1. Run the read-only preflight:

```bash
./.venv/bin/python -m tools.doctor --profile query
```

2. Extract the query and any venue, year, track, presentation-level, or top-k filters. Apply the same filters to every search, fallback, and export command.
3. Create 3–6 prioritized keyword groups. Preserve the user's words first, then common English/Chinese aliases and abbreviations.
4. Run FTS first. Include every extracted filter, for example:

```bash
./.venv/bin/python -m tools.search search \
  --query "<QUERY>" --venue ICLR,ICML,NEURIPS \
  --year-from 2021 --year-to 2025 --format json --top-k 20
```

5. For a multi-concept intersection, try the strict FTS query once. If it is too restrictive, define narrow `candidate_queries`, union their FTS candidates by `paper_id`, then require deterministic alias hits for every `required_labels` dimension. Put these fields in `keywords.json`; do not describe an unfiltered union as an intersection.
6. Use hybrid only when the user explicitly requests semantic similarity, or FTS returns fewer than 5 results and doctor confirms vectors are healthy.
7. If hybrid fails, report the vector error before falling back to FTS. Never call the fallback a hybrid result.
8. Show no more than 20 results, grouped by the first matching keyword label. Put `Other` last and cap each group at 8 rows.
9. When total results exceed 20, export an auditable TSV. Ask before an unlimited export only when total exceeds 2000.

Before hybrid commands, silently source `.codex/.env` when it exists. Never print key values.

## Audit artifacts

Use `artifacts/queries/<query_slug>/run_<timestamp>/` and write:

- `keywords.json`: original query plus ordered `label` and `aliases` arrays.
- `results.tsv`: full DB fields plus `matched_topic`, `matched_keyword`, `janus_topic`, and `janus_subtopic`.

Export with:

```bash
./.venv/bin/python -m tools.search export \
  --query "<QUERY>" --mode search \
  --venue "<VENUES>" --year-from <YEAR> --year-to <YEAR> \
  --out-tsv "<RUN_DIR>/results.tsv" \
  --keywords-json "<RUN_DIR>/keywords.json" \
  --topics-json artifacts/m3/topic_assignments.json \
  --max-export 0
```

Use `--mode hybrid` only if hybrid was the successful ranking mode. `candidate_queries` is FTS-only. Read `references/output-contract.md` before presenting or exporting a multi-paper result.

## Other query tasks

```bash
./.venv/bin/python -m tools.search stats
./.venv/bin/python -m tools.search get --paper-id "<PAPER_ID>"
```

Download PDFs only when the user explicitly asks. Prefer an explicit `results.tsv`, then explicit paper IDs, then unambiguous IDs from the current task:

```bash
./.venv/bin/python -m tools.search download-pdfs --input-tsv "<ABS_RESULTS_TSV>"
```

Do not bypass paywalls. Report the generated `pdf_download_report.json` and `failed.tsv`.
