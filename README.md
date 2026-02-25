# JanusSearch

JanusSearch 是本地 AI 顶会论文归档与智能检索系统，覆盖从采集、规范化、入库到向量检索与端到端验收的完整 CLI 流程。

如果你是AI、LLM、Agent、Bot，请阅读AGENTS.md

## 当前状态
- M1→M4 主链路已打通并完成基础端到端验证
- 当前工作重心：按批次扩充会议与年份覆盖

## 快速开始

### 环境要求
- Python 3.11+
- `uv`
- macOS / Unix CLI

### 安装
```bash
uv sync
uv run python -V
```

### 常用命令

1. 检索
```bash
python3 -m tools.search search --query "continual learning replay"
python3 -m tools.search hybrid --query "continual learning replay" --top-k 20
python3 -m tools.search get --paper-id <PAPER_ID>
python3 -m tools.search stats
```

2. 入库（M2）
```bash
python3 -m tools.m2_db run
```

3. 向量与缓存（M3）
```bash
python3 -m tools.m3_pipeline run \
  --db-path data/papers.db \
  --embed-base-url https://api.siliconflow.cn/v1/embeddings \
  --embed-model Qwen/Qwen3-Embedding-8B \
  --exclude-placeholder
```

4. 端到端验收（M4）
```bash
export JANUS_EMBED_API_KEY="<YOUR_KEY>"

python3 -m tools.m4_validate run \
  --db-path data/papers.db \
  --vectors-root data/vectors/chroma \
  --collection-name papers_v1 \
  --topics-file index/m3_topic_assignments.json \
  --fixed-query-file docs/fixtures/m4_fixed_queries.yaml \
  --embed-base-url https://api.siliconflow.cn/v1/embeddings \
  --embed-model Qwen/Qwen3-Embedding-8B

python3 -m tools.m4_validate status
```

## 数据与产物
- 历史输入：`archives/root_json/`
- 规范化事实源：`data/raw/{venue}/{year}.json`
- 数据库：`data/papers.db`
- 向量库：`data/vectors/chroma`
- 报告：`index/*.json`, `index/*.md`

## 文档入口
- 架构与范围：`docs/10_CORE_ARCHITECTURE.md`
- 全流程与门禁：`docs/20_PIPELINE_AND_GATES.md`
- 扩充策略：`docs/30_EXPANSION_POLICY.md`
- 历史复盘：`docs/90_HISTORY.md`
- 操作流程模板：`AGENTS.md`（SOP 章节）

## AI 协作入口
- AI 执行约束见：`AGENTS.md`
