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

Read `.janus-collection.json`. `no_update` is a successful terminal state: do not prepare, publish, or create an empty canonical file. `incomplete_source` is a failure even when a partial response exists.

4. Normalize into isolated staging. Add `--enrich --enable-arxiv-title` only when missing-field repair is in scope:

```bash
./.venv/bin/python -m tools.corpus prepare \
  --input-glob '<SNAPSHOT>/*.json' \
  --staging-root '<STAGING>'
```

5. Reconcile staged records with canonical IDs and the versioned approval policy:

```bash
./.venv/bin/python -m tools.corpus reconcile \
  --staging-root '<STAGING>' \
  --output-root '<RECONCILED>'
```

Reconciliation ignores run timestamps, matches stable source IDs before titles, preserves old `paper_id` across retitles, and blocks every deletion not listed in the policy. Keep the per-record report with the run artifacts.

6. Validate reconciled staging. Duplicate titles and author/abstract coverage remain hard gates. Official paper count, track count, and presentation-level alignment are warnings by default:

```bash
./.venv/bin/python -m tools.corpus validate \
  --input-glob '<RECONCILED>/*/*.json'
```

Use `--strict-official-alignment` only when the user or release policy explicitly requires official alignment as a hard gate.

7. Publish only through the validating publish command. It rechecks the reconciliation report, staging hashes, and unchanged canonical baseline:

```bash
./.venv/bin/python -m tools.corpus publish --staging-root '<RECONCILED>'
```

8. Build and validate the query catalog once after all accepted venue batches:

```bash
./.venv/bin/python -m tools.catalog build
./.venv/bin/python -m tools.catalog validate
```

Use `tools.corpus add` only when the user wants the complete collect-to-catalog transaction. Add `--build-projections` only when embeddings/topics/cache are also explicitly requested.

## Failure rules

- Stop the batch when staging validation fails; canonical JSON must remain unchanged.
- Stop on pagination shortfall, source fingerprint mismatch, duplicate mapping, or an unapproved deletion.
- A fixed third-party snapshot is allowed only when the version, SHA-256, filtering, and canonical-subset rules are explicitly approved and recorded; otherwise treat it as `incomplete_source`.
- Preserve the old SQLite database when its replacement build fails.
- If catalog validation fails after canonical publication, freeze the batch and report that canonical is published but the prior atomic SQLite catalog remains available; do not claim rollback of canonical.
- Do not fabricate missing fields or edit statistics by hand.
- Record the run manifest at `artifacts/runs/<run_id>/manifest.json` and report its absolute path.
- Treat exit code 0 as success or warning-only, 1 as operation/gate failure, and 2 as usage/configuration failure.

Read `references/collectors-and-gates.md` when choosing a collector, explaining alignment warnings, or handling a failed batch. Use the presentation override JSON files in `references/` only for explicit, evidence-backed oral/best-paper corrections.
