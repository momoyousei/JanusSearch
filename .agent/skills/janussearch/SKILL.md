---
name: janussearch
description: Explicit-only router for JanusSearch. Use only when the user explicitly invokes $janussearch or asks to route a JanusSearch task; dispatch paper retrieval, corpus expansion, or operational maintenance to the matching focused workflow.
---

# JanusSearch Router

Use this Skill only as an explicit entrypoint. Do not turn it into a second implementation of the focused Skills.

## Route the request

Classify the primary requested outcome, then read the selected sibling Skill completely and follow it:

| User outcome | Workflow to load |
|---|---|
| Find papers, related work, records, statistics, exports, or explicitly requested PDFs | `../janus-query/SKILL.md` |
| Add venue-years, collect metadata, normalize, validate, stage, or publish corpus data | `../janus-corpus/SKILL.md` |
| Diagnose, rebuild, repair, evaluate, or inspect operational health | `../janus-ops/SKILL.md` |

If a request spans multiple outcomes, preserve this order unless the user says otherwise:

1. Use corpus workflow to create and validate canonical data.
2. Use operations workflow to rebuild catalog/projections and evaluate them.
3. Use query workflow to demonstrate retrieval behavior.

## Shared invariants

- Work from the repository root.
- Run project commands with `./.venv/bin/python -m ...`; never use the system Python.
- Before a command that needs environment credentials, silently load `.codex/.env` when present:

```bash
set -a
source .codex/.env
set +a
```

- Never print, echo, persist, or summarize credential values.
- Treat `data/raw` as the canonical corpus and `data/papers.db` as the query catalog.
- Report actual command results and artifact paths. Never infer PASS from an old report.
- Keep collection, online evaluation, PDF download, and expensive rebuilds explicit.

