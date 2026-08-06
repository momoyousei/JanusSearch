# JanusSearch

JanusSearch 是面向 AI 论文归档、可审计检索与离线优先评估的本地 CLI 系统。系统按稳定能力划分，不再用 M1～M4 作为主架构。

AI、LLM、Agent 或 Bot 请先阅读 `AGENTS.md`。

## 环境

- Python 3.11+
- `uv`
- macOS / Unix CLI
- SQLite + ChromaDB

```bash
uv sync
UV_CACHE_DIR=.uv-cache uv run python -V
```

仓库内默认使用 `./.venv/bin/python -m ...`，不要使用系统 Python。

## 快速开始

### 查询

```bash
./.venv/bin/python -m tools.doctor --profile query
./.venv/bin/python -m tools.search search --query "continual learning replay"
./.venv/bin/python -m tools.search hybrid --query "continual learning replay" --top-k 20
```

### 扩充语料

```bash
./.venv/bin/python -m tools.corpus plan --venue ACL --years 2021-2025
./.venv/bin/python -m tools.corpus collect --venue ACL --years 2021-2025
```

完整 staged 流程见 `docs/30_EXPANSION_POLICY.md`。

### 构建查询目录与派生投影

```bash
./.venv/bin/python -m tools.catalog build
./.venv/bin/python -m tools.catalog validate
./.venv/bin/python -m tools.projections run
```

### 评估

```bash
./.venv/bin/python -m tools.evaluate run --suite offline
./.venv/bin/python -m tools.evaluate status
```

在线套件只在显式需要且 embedding 凭据可用时运行：

```bash
./.venv/bin/python -m tools.evaluate run --suite all
```

## 数据与产物

| 内容 | 路径 |
|---|---|
| 历史采集输入 | `archives/root_json/` |
| 唯一规范化事实源 | `data/raw/{venue}/{year}.json` |
| SQLite/FTS 目录 | `data/papers.db` |
| Chroma 向量 | `data/vectors/chroma/` |
| 运行清单 | `artifacts/runs/<run_id>/manifest.json` |
| 查询导出 | `artifacts/queries/` |
| 新评估报告 | `artifacts/evaluate/` |
| 历史兼容报告 | `artifacts/m1/`～`artifacts/m4/` |

## Skills

| Skill | 用途 |
|---|---|
| `janussearch` | 仅显式调用的总路由 |
| `janus-query` | 查询、导出、详情与显式 PDF 下载 |
| `janus-corpus` | 采集、staging、门禁和发布 |
| `janus-ops` | 诊断、重建、修复与评估 |

## 文档

- `docs/README.md`：阅读入口
- `docs/10_CORE_ARCHITECTURE.md`：能力架构与一致性模型
- `docs/20_PIPELINE_AND_GATES.md`：命令与门禁
- `docs/30_EXPANSION_POLICY.md`：批次扩充 SOP
- `docs/90_HISTORY.md`：历史决策
