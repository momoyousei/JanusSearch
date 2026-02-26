---
name: janussearch-agent
description: Natural-language entry skill for JanusSearch CLI. Route user intent to search-first workflows and upgrade to M1/M2/M3/M4 operations only on explicit request.
---

# JanusSearch Agent Skill

## Main Position

This is the primary skill for natural-language to CLI routing in JanusSearch.

- Primary objective: paper discovery and retrieval.
- Default policy: search first, then escalate.
- This skill does not replace the collection-specialized skill at:
  - `.agent/skills/paper-search/SKILL.md`

## When To Use

Use this skill when the user asks to:

- find papers by topic, venue, year, track, or presentation level
- retrieve one full paper record by `paper_id`
- inspect DB/search coverage stats
- collect new venue-year papers and merge them into current baseline
- rebuild FTS index or validate DB consistency
- rebuild vectors/cache layers
- run M4 end-to-end validation and read reports
- troubleshoot retrieval failures

## Execution Policy (Locked)

Default policy is `search-first, upgrade-later`.

1. Start with read/search operations:
   - `python3 -m tools.search search`
   - `python3 -m tools.search hybrid`
   - `python3 -m tools.search get`
   - `python3 -m tools.search stats`
2. Run write/rebuild/long-running operations only when user intent is explicit:
   - M1 run, M2 run, M3 run, M4 run
3. For high-cost commands, preserve clear traceability:
   - command used
   - key metrics
   - report paths

## Intent Routing (Task-Intent Based)

- Paper list search -> `python3 -m tools.search search`
- Semantic/similarity retrieval -> `python3 -m tools.search hybrid`
- Single paper detail -> `python3 -m tools.search get`
- Search/DB statistics -> `python3 -m tools.search stats`
- FTS repair -> `python3 -m tools.m2_db reindex-fts`
- DB rebuild/validate -> `python3 -m tools.m2_db run` / `validate`
- Vector/cache rebuild -> `python3 -m tools.m3_pipeline run` (or step-by-step commands)
- End-to-end validation -> `python3 -m tools.m4_validate run` / `status`
- Data quality processing -> `python3 -m tools.m1_pipeline` subcommands
- Expansion collection -> `python3 .agent/skills/paper-search/scripts/fetch_conference_papers.py` + `m1_pipeline` + `m2_db run`

## Parameter Extraction Rules

Extract from natural language when present:

- `query`
- `venue` (normalize to uppercase, comma-separated when multiple)
- `year_from`, `year_to`
- `track`
- `presentation_level` (`poster|oral|bestpaper`)
- `top_k`

Defaults:

- use tool defaults when parameter not provided
- prefer `search` by default
- switch to `hybrid` when user intent is semantic/related/similar retrieval

## Environment And Preconditions

Before executing commands, verify:

1. DB exists for read operations:
   - `data/papers.db`
2. Hybrid prerequisites:
   - `data/vectors/chroma`
   - collection `papers_v1`
3. M4 run prerequisites:
   - `JANUS_EMBED_API_KEY` or `JANUS_LLM_API_KEY`
4. If dependency/index is missing, provide repair command first:
   - `python3 -m tools.m2_db reindex-fts`

## Failure Handling And Fallback

1. `search` returns no results:
   - suggest relaxed filters
   - suggest switching to `hybrid`
2. `hybrid` fails:
   - fallback to `search`
   - report vector-chain issue explicitly
3. M4 online gate fails:
   - mark hard failure clearly
   - do not claim success
   - provide actionable remediation (key/env/connectivity)

## Output Contract (Locked)

For each request, return this fixed structure:

1. `Intent`
2. `Command Executed`
3. `Key Results`
4. `Artifacts/Report Paths`
5. `Next Options` (numbered `1/2/3`)

Rules:

- Always include evidence paths when files are produced.
- Use absolute paths for report references, for example:
  - `/Users/yangli/Workspace/JanusSearch/index/m4_eval_report.json`

## Example Routes (>=8)

1. Exact retrieval query
- User: "查 ICLR 2024 continual learning replay 前20篇"
- Route: `tools.search search`
- Command:
  - `python3 -m tools.search search --query "continual learning replay" --venue ICLR --year-from 2024 --year-to 2024 --top-k 20`

2. Semantic retrieval query
- User: "找和 replay methods 语义最相关的论文"
- Route: `tools.search hybrid`
- Command:
  - `python3 -m tools.search hybrid --query "replay methods" --top-k 20`

3. Single paper detail
- User: "查看 paper_id=S2-6625578ea850761e 的完整信息"
- Route: `tools.search get`
- Command:
  - `python3 -m tools.search get --paper-id S2-6625578ea850761e`

4. DB/search stats
- User: "数据库检索面统计"
- Route: `tools.search stats`
- Command:
  - `python3 -m tools.search stats`

5. Reindex FTS
- User: "重建 FTS 索引"
- Route: `tools.m2_db reindex-fts`
- Command:
  - `python3 -m tools.m2_db reindex-fts`

6. Full M3 rebuild
- User: "重新构建向量和缓存"
- Route: `tools.m3_pipeline run`
- Command:
  - `python3 -m tools.m3_pipeline run --db-path data/papers.db --embed-base-url https://api.siliconflow.cn/v1/embeddings --embed-model Qwen/Qwen3-Embedding-8B --exclude-placeholder`

7. Formal M4 validation
- User: "执行 M4 正式验收并出报告"
- Route: `tools.m4_validate run`
- Command:
  - `python3 -m tools.m4_validate run --db-path data/papers.db --vectors-root data/vectors/chroma --collection-name papers_v1 --topics-file index/m3_topic_assignments.json --fixed-query-file docs/fixtures/m4_fixed_queries.yaml --embed-base-url https://api.siliconflow.cn/v1/embeddings --embed-model Qwen/Qwen3-Embedding-8B --embed-api-key "$JANUS_EMBED_API_KEY"`

8. M4 status summary
- User: "看最新 M4 状态"
- Route: `tools.m4_validate status`
- Command:
  - `python3 -m tools.m4_validate status`

9. Hybrid failure fallback scenario
- User: "用语义检索找 continual replay"
- Primary: `tools.search hybrid`
- On failure fallback:
  - `python3 -m tools.search search --query "continual replay"`
- Response must include fallback reason and vector-chain error snippet.

10. Expansion batch onboarding
- User: "新增 AAAI 2024-2025 并接入检索"
- Route:
  - `python3 .agent/skills/paper-search/scripts/fetch_conference_papers.py AAAI-24 --output archives/root_json/AAAI-24.json`
  - `python3 .agent/skills/paper-search/scripts/fetch_conference_papers.py AAAI-25 --output archives/root_json/AAAI-25.json`
  - `python3 -m tools.m1_pipeline --input-glob 'archives/root_json/AAAI-2*.json' normalize`
  - `python3 -m tools.m1_pipeline --input-glob 'archives/root_json/AAAI-2*.json' validate`
  - `python3 -m tools.m2_db run`

## Test Scenarios For This Skill

1. "查 ICLR 2024 continual learning replay 前20篇" -> route to `search` with venue/year/top_k filters.
2. "找和 replay methods 语义最相关的论文" -> route to `hybrid`.
3. "查看 paper_id=... 的完整信息" -> route to `get`.
4. "数据库检索面统计" -> route to `stats`.
5. "重建 FTS 索引" -> route to `m2_db reindex-fts`.
6. "执行 M4 正式验收并出报告" -> route to `m4_validate run` and return 3 report paths.
7. Simulate `hybrid` failure -> fallback to `search` plus remediation hint.

## Documentation Entry Links

- `docs/README.md`
- `docs/10_CORE_ARCHITECTURE.md`
- `docs/20_PIPELINE_AND_GATES.md`
- `docs/30_EXPANSION_POLICY.md`
- `docs/90_HISTORY.md`
