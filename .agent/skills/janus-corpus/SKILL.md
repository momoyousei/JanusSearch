---
name: janus-corpus
description: Expand and curate the JanusSearch canonical paper corpus. Use when collecting one or more conference years, importing metadata, normalizing or enriching records, checking coverage and official alignment, staging changes, publishing validated JSON, or adding a batch to the catalog.
---

# Janus Corpus

Use the staged corpus lifecycle. Never write collector output directly into `data/raw`.

## Required workflow

1. Resolve collector support and planned commands without data collection or network access. The command still writes its audit manifest:

```bash
./.venv/bin/python -m tools.corpus plan --venue <VENUE> --years <YYYY-YYYY>
```

2. Silently source `.codex/.env` when present. Never print credential values.

```bash
if [ -f .codex/.env ]; then
  set -a
  source .codex/.env
  set +a
fi
```
3. Collect into the run-scoped immutable snapshot:

```bash
./.venv/bin/python -m tools.corpus collect --venue <VENUE> --years <YYYY-YYYY>
```

4. Normalize into isolated staging. Add `--enrich --enable-arxiv-title` only when missing-field repair is in scope:

```bash
./.venv/bin/python -m tools.corpus prepare \
  --input-glob '<SNAPSHOT>/*.json' \
  --staging-root '<STAGING>'
```

5. Validate staging. Duplicate titles and author/abstract coverage remain hard gates. Official paper count, track count, and presentation-level alignment are warnings by default:

```bash
./.venv/bin/python -m tools.corpus validate \
  --input-glob '<STAGING>/*/*.json'
```

Use `--strict-official-alignment` only when the user or release policy explicitly requires official alignment as a hard gate.

6. Publish only through the validating publish command:

```bash
./.venv/bin/python -m tools.corpus publish --staging-root '<STAGING>'
```

7. Build and validate the query catalog:

```bash
./.venv/bin/python -m tools.catalog build
./.venv/bin/python -m tools.catalog validate
```

Use `tools.corpus add` only when the user wants the complete collect-to-catalog transaction. Add `--build-projections` only when embeddings/topics/cache are also explicitly requested.

## Failure rules

- Stop the batch when staging validation fails; canonical JSON must remain unchanged.
- Preserve the old SQLite database when its replacement build fails.
- If catalog validation fails after canonical publication, freeze the batch and report that canonical is published but the prior atomic SQLite catalog remains available; do not claim rollback of canonical.
- Do not fabricate missing fields or edit statistics by hand.
- Record the run manifest at `artifacts/runs/<run_id>/manifest.json` and report its absolute path.
- Treat exit code 0 as success or warning-only, 1 as operation/gate failure, and 2 as usage/configuration failure.

Read `references/collectors-and-gates.md` when choosing a collector, explaining alignment warnings, or handling a failed batch. Use the presentation override JSON files in `references/` only for explicit, evidence-backed oral/best-paper corrections.
