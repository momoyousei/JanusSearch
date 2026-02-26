# 核心架构与范围（Core Architecture）

## 目标
JanusSearch 是本地 AI 顶会论文归档与检索系统，要求：
- 数据可追溯：保留历史输入、规范化事实源、运行报告
- 质量可量化：覆盖率、去重、官方口径对齐可自动验证
- 流程可复现：固定 CLI、固定输出路径、固定门禁

## 范围
- 目标会议（16）：CVPR, ICCV, ECCV, NeurIPS, ICML, ICLR, AAAI, IJCAI, ACL, EMNLP, NAACL, KDD, WWW, ACM MM, CoRL, WACV
- 当前阶段：M1→M4 主链路已贯通，进入会议/年份扩充阶段
- 当前冻结基线：早期 21 个会议年份文件（ICLR/ICML/NeurIPS/CVPR）已完成端到端验证

## 分层与事实源
1. 历史输入层
- 路径：`archives/root_json/{VENUE}-{YY}.json`
- 作用：采集快照与回放，不是下游事实源

2. 规范化事实层（唯一事实源）
- 路径：`data/raw/{venue}/{year}.json`
- 作用：M2/M3/M4 的统一输入

3. 检索运行层
- SQLite：`data/papers.db`
- 向量：`data/vectors/chroma`
- 缓存：`artifacts/indexes/master_index.md`, `venues/`, `topics/`, `subtopics/`

## 里程碑
1. M1 数据采集与规范化
- 目标：规范化、去重、回填、质量门禁

2. M2 数据入库与 SQL/FTS 检索
- 目标：将 `data/raw` 入库 SQLite，并提供可复现查询面

3. M3 缓存与混合检索
- 目标：向量构建、主题/子主题分配、L1-L4 缓存、hybrid 检索

4. M4 Agent 端到端验收
- 目标：在线门禁 + 固定查询 + 抽样查询的可量化回归

## 数据契约（摘要）
- 根输入（`archives/root_json/*-*.json`）关键字段：
  - `query`, `source`, `generated_at_utc`, `paper_count`, `papers`, `reconciliation`(可选), `official_tracks`(可选), `m1`
- canonical（`data/raw/{venue}/{year}.json`）关键字段：
  - 顶层：`venue`, `year`, `collected_at`, `source`, `count`, `metrics`, `papers`
  - 记录：`paper_id`, `title`, `authors`, `abstract`, `doi`, `url`, `source_ids`, `track`, `track_group`, `presentation_level`, `record_status`, `quality_flags`
- 状态语义：`resolved`, `repaired`, `placeholder`

## 关键约束
- Python 3.11+
- macOS + Unix CLI
- 包管理：`uv`
- 数据库：SQLite3
- 向量库：ChromaDB
- 禁止 Docker 与 Web 框架（Flask/FastAPI/Django）
- 全流程 CLI（`argparse`）

## 关联入口
- 执行与门禁：`docs/20_PIPELINE_AND_GATES.md`
- 扩充策略：`docs/30_EXPANSION_POLICY.md`
- 历史决策与复盘：`docs/90_HISTORY.md`
