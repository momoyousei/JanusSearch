# 能力流程与质量门禁

所有项目命令从仓库根目录用 `./.venv/bin/python -m ...` 执行。

## 1. Corpus：采集到事实源

```bash
./.venv/bin/python -m tools.corpus plan --venue ACL --years 2021-2025
./.venv/bin/python -m tools.corpus collect --venue ACL --years 2021-2025
./.venv/bin/python -m tools.corpus prepare \
  --input-glob 'artifacts/runs/<run_id>/collected/*.json' \
  --staging-root 'artifacts/runs/<run_id>/staging'
./.venv/bin/python -m tools.corpus validate \
  --input-glob 'artifacts/runs/<run_id>/staging/*/*.json'
./.venv/bin/python -m tools.corpus publish \
  --staging-root 'artifacts/runs/<run_id>/staging'
```

需要补齐缺失字段时，在 `prepare` 中显式加入 `--enrich --enable-arxiv-title`。采集、补源和发布均不得臆造字段或手工篡改统计。

### 硬门禁

- `duplicate_title_count == 0`
- `resolved_authors_coverage >= 90.0`
- `resolved_abstract_coverage >= 85.0`
- JSON/schema/staging 操作有效

### 默认警告

`paper_count`、`track_counts`、`presentation_level_counts` 与官方口径不一致时记录 warning，默认不导致退出码 1。只有显式 `--strict-official-alignment` 才把这些项目升级为硬失败。

### 补源顺序

1. 会议专用源：CVF/PMLR/ACL Anthology/OpenReview/官方 virtual JSON；
2. OpenAlex DOI 与 Semantic Scholar DOI；
3. OpenAlex/Semantic Scholar/arXiv 标题检索，并执行标题相似度约束；
4. 小规模人工补录，必须记录来源和时间。

禁止 DOI-only。`papers.cool` 只作为 ACL/AAAI 的显式最后兜底，且必须保留 `field_provenance`。

## 2. Catalog：SQLite 与 FTS

```bash
./.venv/bin/python -m tools.catalog build
./.venv/bin/python -m tools.catalog validate
./.venv/bin/python -m tools.catalog reindex-fts
./.venv/bin/python -m tools.catalog stats
```

`build` 只读取 `data/raw`，在临时路径构建并校验后原子发布。失败不得覆盖当前 `data/papers.db`。

## 3. Search：离线优先查询

```bash
./.venv/bin/python -m tools.doctor --profile query
./.venv/bin/python -m tools.search search --query "continual learning replay" --top-k 20
./.venv/bin/python -m tools.search hybrid --query "continual learning replay" --top-k 20
./.venv/bin/python -m tools.search get --paper-id <PAPER_ID>
./.venv/bin/python -m tools.search stats
```

默认先用 FTS。Hybrid 只在用户明确要求语义检索，或 FTS 低召回且向量健康时使用。Hybrid 失败必须先报告错误，再明确降级为 FTS。PDF 下载只在用户显式要求时执行。

复杂多概念交集可在 `keywords.json` 中提供 `candidate_queries` 与 `required_labels`：export 按相同过滤条件执行窄查询并集，再对 title+abstract+keywords 做每个必选标签的确定性 alias 过滤。简单查询不需要这两个字段。

## 4. Projections：向量、主题与缓存

```bash
./.venv/bin/python -m tools.projections build-vectors
./.venv/bin/python -m tools.projections build-topics
./.venv/bin/python -m tools.projections build-cache
./.venv/bin/python -m tools.projections validate
./.venv/bin/python -m tools.projections run
```

向量保持 `paper_id` 级增量：缺失、文本 hash 变化、模型配置变化或无法验证的 legacy metadata 才重算。全量非抽样构建可以删除当前 DB 候选集中不存在的 stale vectors。只有明确要求时使用 `--force-rebuild-vectors`。

需要 embedding/LLM 的命令从环境变量读取凭据，不得写入参数记录或报告明文。

## 5. Evaluate：离线默认、在线显式

```bash
./.venv/bin/python -m tools.evaluate run --suite offline
./.venv/bin/python -m tools.evaluate status
```

离线套件把固定查询全部通过 FTS 执行，不依赖外部服务。只有显式要求时运行：

```bash
./.venv/bin/python -m tools.evaluate run --suite online
./.venv/bin/python -m tools.evaluate run --suite all
```

`status` 比较数据库、Chroma、主题分配和固定查询文件的当前指纹；缺失指纹或指纹变化一律返回 stale，不允许复用历史 PASS。

## 6. Doctor：只读诊断

```bash
./.venv/bin/python -m tools.doctor --profile query
./.venv/bin/python -m tools.doctor --profile corpus
./.venv/bin/python -m tools.doctor --profile ops
```

Doctor 不执行修复。先定位最早失败的依赖，再由用户明确要求最小修复。

## 兼容入口

以下命令继续可用并输出迁移提示：

| 历史入口 | 新入口 |
|---|---|
| `tools.m1_pipeline` | `tools.corpus` |
| `tools.m2_db` | `tools.catalog` |
| `tools.m3_pipeline` | `tools.projections` |
| `tools.m4_validate` | `tools.evaluate` |

旧报告路径继续由旧入口维护；新运行清单统一位于 `artifacts/runs/`。
