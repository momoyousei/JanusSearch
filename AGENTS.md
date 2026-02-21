# AGENTS.md — 全局执行约束

## 项目名称
JanusSearch, Gate of AI Papers — AI 顶会论文归档与智能检索系统

## 文档分层原则
- `AGENTS.md` / `PROJECT.md` 仅保留目标、约束、里程碑与入口。
- 具体流程、数据契约、质量门禁、运行细节一律写在 `docs/`。
- 进入任务前先阅读：`docs/README.md`。

## 当前阶段
- 长期目标：以现有 16 个会议年份文件为基线，走通完整项目（M1→M4）。
- 当前冻结点：M1 初步冻结（见 `docs/14_M1_FREEZE_2026-02-19.md`）。
- 当前重点：在 M2 基线上推进 M3（向量、缓存、混合检索）闭环。

## 技术约束
- Python 3.11+
- macOS + Unix CLI
- 包管理: `uv`
- 数据库: SQLite3
- 向量库: ChromaDB
- 不使用 Docker
- 不使用 Web 框架（Flask/FastAPI/Django）
- 全部为 CLI 工具（`argparse`）

## 编码与安全规范
- Python 文件头：
  - `#!/usr/bin/env python3`
  - `# -*- coding: utf-8 -*-`
- 使用 type hints 与模块 docstring
- 日志统一使用 `logging`
- 路径统一 `pathlib.Path`
- 网络请求必须有重试机制
- 敏感信息（API key）仅通过环境变量读取，禁止硬编码

## 里程碑顺序
1. M1 数据采集与规范化（冻结基线见 `docs/14_M1_FREEZE_2026-02-19.md`）
2. M2 数据入库（SQLite/索引）
3. M3 缓存与检索增强（主题/子主题与向量检索）
4. M4 Agent 端到端验证

## M1/M2 文档入口
- M1 方法论：`docs/10_M1_METHOD.md`
- M1 数据规范：`docs/11_M1_DATA_STANDARD.md`
- M1 质量门禁与官方口径对齐：`docs/12_M1_QUALITY_GATES.md`
- M1 运行手册：`docs/13_M1_OPERATIONS_RUNBOOK.md`
- M2 入库与校验：`docs/20_M2_DATABASE.md`
- M2 检索 CLI（SQL+FTS）：`docs/21_M2_SEARCH_CLI.md`
- M3 缓存与混合检索：`docs/22_M3_CACHE_AND_HYBRID.md`

## M3 执行入口
- 全流程：`python3 -m tools.m3_pipeline run --db-path data/papers.db --embed-base-url http://127.0.0.1:1234/v1 --embed-model text-embedding-qwen3-embedding-8b --exclude-placeholder`
- 混合检索：`python3 -m tools.search hybrid --query "continual learning replay" --embed-base-url http://127.0.0.1:1234/v1 --embed-model text-embedding-qwen3-embedding-8b --alpha 0.6 --top-k 20`

## 验证基准
- 端到端基准主题：**Continual Learning > Replay Methods**
- Ground truth 论文集与验收口径：`docs/12_M1_QUALITY_GATES.md`
