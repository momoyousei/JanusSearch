# M4：Agent 端到端验收（云端硬门禁）

## 目标
在 M3 基线（SQLite + 向量 + 缓存）上提供可复现、可量化、可一键执行的端到端验收。

M4 由以下部分组成：
- 云端 embedding 健康检查（硬门禁）
- 固定查询集（版本化）
- topic 抽样查询集（可复现）
- Replay 基准（当前覆盖口径）

## CLI 入口
```bash
# 全量执行（失败返回非 0）
python3 -m tools.m4_validate run \
  --db-path data/papers.db \
  --vectors-root data/vectors/chroma \
  --collection-name papers_v1 \
  --topics-file index/m3_topic_assignments.json \
  --fixed-query-file docs/fixtures/m4_fixed_queries.yaml \
  --embed-base-url https://api.siliconflow.cn/v1/embeddings \
  --embed-model Qwen/Qwen3-Embedding-8B \
  --embed-api-key "$JANUS_EMBED_API_KEY"

# 查看最近状态摘要
python3 -m tools.m4_validate status
```

## 参数（run）
- `--sample-topics`：默认 `20`
- `--sample-per-topic`：默认 `2`
- `--sample-seed`：默认 `42`
- `--top-k`：固定/抽样套件默认 `50`
- `--replay-top-k`：默认 `100`
- `--output-json`：默认 `index/m4_eval_report.json`
- `--output-md`：默认 `index/m4_eval_report.md`
- `--sampled-dump`：默认 `index/m4_sampled_queries.json`

## 门禁规则
1. 在线门禁必须通过（key 必须可用，embedding 健康检查必须成功）。
2. 固定查询套件必须 100% 通过。
3. 抽样查询套件通过率必须 >= 90%。
4. Replay 仅按当前覆盖口径评估：eligible 项必须 100% 命中。

总门禁：
`overall_pass = online_gate_pass AND fixed_suite_pass AND sampled_suite_pass AND replay_suite_pass`

## 固定查询集
- 文件：`docs/fixtures/m4_fixed_queries.yaml`
- 每条 case 字段：
  - `case_id`
  - `mode`：`search | hybrid`
  - `query`
  - `filters`
  - `top_k`
  - `expect_min_results`
  - `expect_any_title_fragments`
  - `expect_all_title_fragments`（可选）

## 产物
- `index/m4_eval_report.json`：机器可读总报告
- `index/m4_eval_report.md`：人工可读摘要
- `index/m4_sampled_queries.json`：抽样查询快照（可复现）

## 回归测试
```bash
python3 -m unittest \
  tests/test_m2_db.py \
  tests/test_search_cli.py \
  tests/test_m3_pipeline.py \
  tests/test_hybrid_search.py \
  tests/test_m4_validate.py
```
