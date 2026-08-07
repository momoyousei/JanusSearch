# 核心架构与能力边界

## 系统目标

JanusSearch 是本地论文归档与检索系统，要求数据可追溯、状态可诊断、写入可恢复、查询可复现。M1～M4 是历史实施阶段，不再作为软件架构边界。

## 能力模型

```text
collectors -> corpus -> catalog -> projections -> evaluate
                          |             |
                          +-- search ---+
                                 |
                               doctor
```

| 能力 | 职责 | 主入口 |
|---|---|---|
| `corpus` | 采集计划、快照、规范化、补源、门禁、发布 | `tools.corpus` |
| `catalog` | SQLite 原子构建、校验、FTS 和统计 | `tools.catalog` |
| `projections` | Chroma 向量、主题和 Markdown 缓存 | `tools.projections` |
| `search` | FTS/hybrid 查询、详情、导出和显式 PDF 下载 | `tools.search` |
| `evaluate` | 默认离线回归、显式在线回归、新鲜度状态 | `tools.evaluate` |
| `doctor` | query/corpus/ops 三类只读诊断 | `tools.doctor` |

## 代码分层

| 层 | 路径 | 允许职责 |
|---|---|---|
| 领域层 | `janussearch/domain/` | 退出码、状态和稳定业务语义 |
| 应用层 | `janussearch/application/` | 能力工作流、门禁和发布编排 |
| 采集层 | `janussearch/collectors/` | 采集器注册表和通用采集实现 |
| 基础设施层 | `janussearch/infrastructure/` | 指纹、运行清单和持久化辅助 |
| CLI 适配层 | `tools/` | `argparse`、日志、输入输出和兼容入口 |

业务实现位于 `janussearch/`；生产包不得导入 `tools`。`tools.m1_pipeline`～`tools.m4_validate` 与旧 venue collector 模块仅为模块别名兼容入口，`tools.corpus/catalog/projections/evaluate/search/doctor` 负责 CLI 适配。新增工作不得继续扩展 M 编号或把业务逻辑放回 `tools`。

## 数据事实源

1. 采集快照/历史输入：`archives/root_json/` 与 `artifacts/runs/<run_id>/collected/`
2. 唯一规范化事实源：`data/raw/{venue}/{year}.json`
3. 可重建查询目录：`data/papers.db`
4. 可重建派生投影：`data/vectors/chroma/`、`artifacts/m3/`、`artifacts/indexes/`、`venues/`、`topics/`、`subtopics/`
5. 运行证据：`artifacts/runs/<run_id>/manifest.json`

下游只能读取 `data/raw`，不得直接把 `archives/root_json` 当作事实源。

## 数据契约

Canonical 顶层字段包括 `venue`、`year`、`collected_at`、`source`、`count`、`metrics`、`papers`。记录保留 `paper_id`、`title`、`authors`、`abstract`、`doi`、`url`、`source_ids`、`field_provenance`、`track`、`track_group`、`presentation_level`、`record_status` 和 `quality_flags`。

现有 canonical JSON、SQLite schema 与 Chroma collection 保持兼容；本次能力化重构不触发数据重写。

## 一致性与恢复模型

- Corpus：采集 sidecar、隔离 staging、显式 reconcile 和门禁全部通过后按 canonical 相对路径发布；任一步失败不得改变事实源。
- SQLite：构建临时数据库，成功后原子替换；失败保留旧库。
- Chroma：原位增量，按 `paper_id`、文本 hash、模型配置和 schema metadata 判断重算；可重跑恢复，不宣称原子替换。
- Cache/topic：原位可重建，使用进度文件、输入指纹和验证报告判断状态。
- Evaluation：报告记录输入指纹；数据库、向量、主题或查询 fixture 变化后，旧 PASS 必须判定为 stale。

## 运行契约

每次有状态的能力操作写入 `artifacts/runs/<run_id>/manifest.json`，至少包含 scope、Git revision、脱敏配置与指纹、步骤、状态、指标、问题和产物。

统一退出码：

- `0`：成功，允许 warning-only 状态；
- `1`：操作失败或硬门禁失败；
- `2`：参数或配置用法错误。
