# 全流程执行与门禁（M1-M4）

## 总原则
- 先标准化，再回填，再验证，不跳步骤
- 先保证官方口径对齐，再追求摘要覆盖率
- 每轮操作必须产出报告，不做“无证据更新”
- 新增数据以下游事实源 `data/raw` 为准

## M1：采集后处理（inventory -> normalize -> backfill -> validate）
入口：`python3 -m tools.m1_pipeline`

### 常用命令
```bash
python3 -m tools.m1_pipeline inventory
python3 -m tools.m1_pipeline normalize
python3 -m tools.m1_pipeline backfill --min-interval 3.0 --retries 3 --timeout 30
python3 -m tools.m1_pipeline validate
```

子集执行示例：
```bash
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/CVPR-2*.json' normalize
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/CVPR-2*.json' backfill --max-records-per-file 0 --enable-arxiv-title
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/CVPR-2*.json' validate
```

### M1 质量门禁（默认）
- `duplicate_title_count == 0`
- `resolved_authors_coverage >= 90.0`
- `resolved_abstract_coverage >= 85.0`

### 官方口径对齐门禁
- 维度：`paper_count`, `track_counts`, `presentation_level_counts`
- 三态：
  - `true`：有基线且对齐
  - `false`：有基线但不对齐（失败）
  - `null`：暂无基线（不计失败，但属于能力缺口）
- 基线优先级：`official_tracks` > `reconciliation`

### 回填强规则
- 禁止 DOI-only
- DOI 未命中必须进入标题检索链路（OpenAlex/S2/arXiv title）
- 标题命中必须做相似度阈值约束后写回
- `papers.cool` 仅可作为 ACL/AAAI 的可选最后兜底补源，默认关闭；不得替代官方事实源
- `papers.cool` 写回仅允许补缺失字段，且必须记录 `field_provenance` 与 `source_ids`

### 会议特化补源优先
- ICML：PMLR（已落地 2021 -> v139）
- CVPR：CVF 详情页摘要优先，404 时可有限启用 Wayback
- ACL：Anthology event + 详情页摘要，DOI 后必须追加标题检索
- AAAI：OJS Technical Tracks 为主，未发布年份可用 OpenReview fallback 并显式标注来源

## M2：SQLite 入库与校验
入口：`python3 -m tools.m2_db`

```bash
python3 -m tools.m2_db load
python3 -m tools.m2_db validate
python3 -m tools.m2_db run
python3 -m tools.m2_db reindex-fts
python3 -m tools.m2_db stats
```

规则：
- 仅从 `data/raw` 入库，不读 `archives/root_json`
- `load` 后自动重建 FTS（`papers_fts`）
- 保留 `record_status` 全量入库（包含 placeholder）

## M2 检索 CLI（SQL + FTS）
入口：`python3 -m tools.search`

```bash
python3 -m tools.search search --query "continual learning replay"
python3 -m tools.search hybrid --query "continual learning replay" --top-k 20
python3 -m tools.search get --paper-id <PAPER_ID>
python3 -m tools.search stats
```

默认行为：
- `search` 走 `title + abstract` FTS BM25
- 默认排除 `record_status=placeholder`

## M3：向量、主题缓存、混合检索
入口：`python3 -m tools.m3_pipeline`

```bash
python3 -m tools.m3_pipeline run \
  --db-path data/papers.db \
  --embed-base-url https://api.siliconflow.cn/v1/embeddings \
  --embed-model Qwen/Qwen3-Embedding-8B \
  --exclude-placeholder
```

分步：`build-vectors`, `build-topics`, `build-cache`, `validate`

关键环境变量：
- `JANUS_LLM_API_KEY`（必需，用于 topic/subtopic 命名）
- `JANUS_LLM_BASE_URL`, `JANUS_LLM_MODEL`
- `JANUS_EMBED_BASE_URL`, `JANUS_EMBED_API_KEY`

关键产物：
- `artifacts/m3/topic_assignments.json`
- `artifacts/m3/build_report.json`
- `artifacts/m3/validate_report.json`
- `artifacts/indexes/master_index.md`
- `data/vectors/chroma/`

## M4：端到端验收（云端硬门禁）
入口：`python3 -m tools.m4_validate`

```bash
python3 -m tools.m4_validate run \
  --db-path data/papers.db \
  --vectors-root data/vectors/chroma \
  --collection-name papers_v1 \
  --topics-file artifacts/m3/topic_assignments.json \
  --fixed-query-file docs/fixtures/m4_fixed_queries.yaml \
  --embed-base-url https://api.siliconflow.cn/v1/embeddings \
  --embed-model Qwen/Qwen3-Embedding-8B \
  --embed-api-key "$JANUS_EMBED_API_KEY"

python3 -m tools.m4_validate status
```

M4 总门禁：
- `online_gate_pass`
- `fixed_suite_pass`（固定查询 100%）
- `sampled_suite_pass`（抽样 >= 90%）
- `overall_pass = online_gate_pass AND fixed_suite_pass AND sampled_suite_pass`

## 批次报告清单
- `artifacts/m1/quality_report.json`
- `artifacts/m1/backfill_report.json`
- `artifacts/m2/load_report.json`
- `artifacts/m2/validate_report.json`
- `artifacts/m3/build_report.json`
- `artifacts/m3/validate_report.json`
- `artifacts/m4/eval_report.json`
- `artifacts/m4/eval_report.md`

## 验证基准
- 端到端主题基准：`Continual Learning > Replay Methods`
