# AGENTS.md — JanusSearch AI 执行入口

## 项目

JanusSearch, Gate of AI Papers — AI 论文归档、检索与评估系统。

## 每次任务首读

1. `docs/README.md`
2. `docs/20_PIPELINE_AND_GATES.md`
3. `docs/30_EXPANSION_POLICY.md`
4. 涉及架构时补读 `docs/10_CORE_ARCHITECTURE.md`
5. 涉及历史决策时补读 `docs/90_HISTORY.md`

## 授权边界

- 解释、审查、诊断、分析或规划：只读检查并汇报，除非用户同时明确要求修改。
- 修改、实现、构建或修复：完成范围内本地改动并运行非破坏性验证。
- 外部写入、破坏性操作、新增生产依赖、高成本长任务或扩大范围前先征得同意。
- 除非用户明确要求，否则不提交、不推送、不创建或合并 PR。
- 保留与当前任务无关的用户修改；不得重置、覆盖或格式化任务范围外文件。

## 能力路由

| 意图 | 主入口 |
|---|---|
| 论文检索、详情、导出、显式 PDF 下载 | `tools.search` |
| 会议/年份采集、规范化、门禁、发布 | `tools.corpus` |
| SQLite 构建、校验、FTS、统计 | `tools.catalog` |
| 向量、主题、缓存构建与校验 | `tools.projections` |
| 离线/在线回归与状态 | `tools.evaluate` |
| 查询、语料或运维只读诊断 | `tools.doctor` |

项目 Skills 位于 `.agent/skills/`：

- `janussearch`：仅显式调用的总路由；
- `janus-query`：查询工作流；
- `janus-corpus`：语料扩充工作流；
- `janus-ops`：诊断、修复和评估工作流。

## 执行环境

- Python 3.11+
- macOS + Unix CLI
- 包管理：`uv`
- SQLite3 + ChromaDB
- 不使用 Docker 或 Web 框架；全部为 `argparse` CLI

禁止直接用系统 `python3` 执行项目命令。使用：

```bash
./.venv/bin/python -m <module> ...
UV_CACHE_DIR=.uv-cache uv run <command>
```

## 代码规范

- Python 文件头：`#!/usr/bin/env python3` 与 `# -*- coding: utf-8 -*-`
- 使用模块 docstring、type hints、`logging` 和 `pathlib.Path`
- 网络请求必须带重试
- API key 只从环境变量读取，禁止硬编码或打印
- 真实暴露错误，不用宽泛捕获、静默默认值或未说明 fallback 掩盖失败
- 未实际运行的命令、测试或实验不得声称通过

## 架构边界

生产包为 `janussearch/`：

- `domain/`：稳定业务语义；
- `application/`：能力工作流；
- `collectors/`：采集实现与注册表；
- `infrastructure/`：运行清单、指纹与持久化辅助；
- `tools/`：薄 CLI 适配与历史兼容入口。

M1～M4 只表示历史实现阶段。`tools.m1_pipeline`～`tools.m4_validate` 保持兼容，但新增功能不得继续扩展 M 编号。

## 数据与恢复

1. 采集快照：`archives/root_json/` 或 run-scoped `collected/`
2. 唯一事实源：`data/raw/{venue}/{year}.json`
3. 查询目录：`data/papers.db`
4. 派生投影：`data/vectors/chroma`、topic/cache artifacts

约束：

- 下游只依赖 `data/raw`；
- corpus 必须 staging 验证后发布；
- SQLite 临时构建成功后原子替换，失败保留旧库；
- Chroma/cache 原位增量且可重跑，不宣称原子发布；
- 每个有状态的新能力操作生成 `artifacts/runs/<run_id>/manifest.json`；
- 退出码：0 成功/警告，1 操作或门禁失败，2 用法/配置错误。

## 门禁

Corpus 硬门禁：

- `duplicate_title_count == 0`
- `resolved_authors_coverage >= 90.0`
- `resolved_abstract_coverage >= 85.0`
- JSON/schema/staging 有效

`paper_count`、`track_counts`、`presentation_level_counts` 官方对齐默认是 warning；只有 `--strict-official-alignment` 才作为硬门禁。

Catalog：`all_pass = true`。Projections：`summary.all_pass = true`。Evaluation：必须在当前输入指纹下 `overall_pass = true`，不得复用 stale PASS。

## SOP 1：查询

```bash
./.venv/bin/python -m tools.doctor --profile query
./.venv/bin/python -m tools.search search --query "<QUERY>" --top-k 20
```

FTS 优先。Hybrid 仅在明确语义意图，或 FTS 低召回且向量健康时使用。Hybrid 失败先报告错误再降级。PDF 下载必须显式请求。

## SOP 2：会议批次扩充

```bash
./.venv/bin/python -m tools.corpus plan --venue <VENUE> --years <RANGE>
./.venv/bin/python -m tools.corpus collect --venue <VENUE> --years <RANGE>
./.venv/bin/python -m tools.corpus prepare \
  --input-glob '<SNAPSHOT>/*.json' --staging-root '<STAGING>'
./.venv/bin/python -m tools.corpus validate --input-glob '<STAGING>/*/*.json'
./.venv/bin/python -m tools.corpus publish --staging-root '<STAGING>'
./.venv/bin/python -m tools.catalog build
./.venv/bin/python -m tools.catalog validate
./.venv/bin/python -m tools.evaluate run --suite offline
```

任一硬门禁失败即冻结 snapshot、staging、报告与 manifest，停止后续层。

## SOP 3：运维与回归

```bash
./.venv/bin/python -m tools.doctor --profile ops
./.venv/bin/python -m tools.catalog validate
./.venv/bin/python -m tools.projections validate
./.venv/bin/python -m tools.evaluate run --suite offline
./.venv/bin/python -m tools.evaluate status
./.venv/bin/python -m tools.search search \
  --query "continual learning replay" --top-k 20
```

先诊断最早失败依赖，只执行用户授权的最小修复，再验证受影响的所有下游层。

## 验证与汇报

- 优先针对性验证；风险或仓库规则要求时再扩大范围。
- 完成前检查最终 diff、意外生成文件、敏感信息和 GitHub 100MB 限制。
- 最终回复说明完成结果、修改文件、实际命令及结果、剩余风险或未完成验证。
- 默认中文，简单直白；多项对比、进度和方案选择优先 Markdown 表格。
- 回答最后一个字加“喵”。
