# AGENTS.md — Codex CLI 全局指令

## 项目名称
JanusSearch, Gate of AI Papers — AI 顶会论文归档与智能检索系统

## 技术约束
- 语言: Python 3.11+
- OS: macOS (使用 unix 命令, brew 安装依赖)
- 包管理: 使用 uv (如未安装则先 `brew install uv`)
- 数据库: SQLite3 (内置, 无需额外安装)
- 向量库: ChromaDB (pip install chromadb)
- 不使用 Docker
- 不使用任何 Web 框架 (无 Flask/FastAPI/Django)
- 所有脚本均为 CLI 工具, 用 argparse 处理参数

## 编码规范
- 所有 Python 文件顶部添加 `#!/usr/bin/env python3` 和 `# -*- coding: utf-8 -*-`
- 使用 type hints
- 每个模块必须有 docstring
- 日志使用 Python logging 模块, 不使用 print (调试除外)
- 配置统一读取 `config.toml`
- 敏感信息 (API key) 保存到本地就行，这不是开源的项目
- 所有文件路径使用 pathlib.Path, 不使用字符串拼接
- 错误处理: 网络请求必须有重试机制 (tenacity 库)

## 项目目录结构 (必须严格遵守)
paper-vault/
├── AGENTS.md
├── PROJECT.md
├── config.toml
├── requirements.txt
├── .env.example
├── .gitignore
├── collectors/
│   ├── __init__.py
│   ├── base.py
│   ├── semantic_scholar.py
│   ├── dblp.py
│   ├── openreview_collector.py
│   └── acl_anthology.py
├── etl/
│   ├── __init__.py
│   ├── cleaner.py
│   ├── db_loader.py
│   ├── embedder.py
│   └── cache_builder.py
├── search/
│   ├── __init__.py
│   ├── sql_search.py
│   ├── fts_search.py
│   ├── vector_search.py
│   └── hybrid_search.py
├── tools/
│   ├── search.py
│   ├── rebuild_cache.py
│   ├── stats.py
│   └── validate.py
├── data/
│   ├── raw/
│   │   └── {venue}/{year}.json
│   ├── papers.db
│   └── vectors/
├── index/
│   ├── master_index.md
│   └── stats.md
├── venues/
│   └── {venue}/{venue}_{year}.md
├── topics/
│   ├── _topic_index.md
│   └── {topic_name}.md
├── subtopics/
│   └── {topic_name}/
│       ├── _overview.md
│       └── {subtopic}.md
├── .agent/
│   ├── skill.md
│   └── query_log.md
├── tests/
│   ├── test_collectors.py
│   ├── test_etl.py
│   ├── test_search.py
│   └── test_validation.py
└── backups/


## 里程碑执行顺序
1. M1: 数据采集 → 见 `docs/M1_DATA_COLLECTION.md`
2. M2: 数据入库 → 见 `docs/M2_DATABASE.md`
3. M3: 缓存构建 → 见 `docs/M3_CACHE.md`
4. M4: Agent 验证 → 见 `docs/M4_AGENT_VALIDATION.md`

## 验证基准
所有里程碑均以 **"Continual Learning > Replay Methods"** 作为端到端验证用例。
已知应被检索到的代表性论文 (ground truth):
- "Dark Experience for General Continual Learning: a Strong, Simple Baseline" (DER++, NeurIPS 2020)
- "GDumb: A Simple Approach that Questions Our Progress in Continual Learning" (ECCV 2020)
- "Online Continual Learning with Maximal Interfered Retrieval" (MIR, NeurIPS 2019)
- "Experience Replay for Continual Learning" (NeurIPS 2019)
- "Rainbow Memory: Continual Learning with a Memory of Diverse Samples" (CVPR 2021)
- "Co2L: Contrastive Continual Learning" (ICCV 2021)
- "Memory Replay with Data Compression for Continual Learning" (ICLR 2022)
- "Repeated Augmented Rehearsal" (NeurIPS 2022)
- "ESMER: Energy-based Summarization for Memory Replay" (NeurIPS 2023)

以上论文在最终验证中, 至少 **7/9** 应出现在 `subtopics/continual_learning/replay_methods.md` 中。

## Codex CLI 工作方式
- 每个里程碑对应一个 docs/M{n}_*.md 文件
- 按顺序执行每个里程碑
- 每个里程碑内部按 Task 顺序执行
- 每个 Task 完成后运行该 Task 的验证命令
- 验证失败则修复后重新验证, 不跳过
