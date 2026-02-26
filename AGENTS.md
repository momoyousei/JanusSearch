# AGENTS.md — AI 执行入口

## 项目名称
JanusSearch, Gate of AI Papers — AI 顶会论文归档与智能检索系统

## AI 首读顺序（每次任务开始）
1. `docs/README.md`
2. `docs/20_PIPELINE_AND_GATES.md`
3. `docs/30_EXPANSION_POLICY.md`
4. 涉及历史决策时补读 `docs/90_HISTORY.md`

## 文档分层（强约束）
- `README.md`：面向人类用户（项目介绍、快速上手）
- `AGENTS.md`：面向 AI（执行约束、路由、验收口径、SOP）
- `docs/`：少量核心规范文档（架构、流程门禁、扩充策略、历史复盘）

## 采集专用 Skill
- `.agent/skills/paper-search/SKILL.md`：仅用于会议年份采集与导出 JSON 的专用流程。
- 非采集类任务默认按本文件路由执行。

## 当前阶段
- M1→M4 主链路已贯通
- 当前重点：会议/年份扩充（批次化执行）

## 技术约束
- Python 3.11+
- macOS + Unix CLI
- 包管理：`uv`
- 数据库：SQLite3
- 向量库：ChromaDB
- 不使用 Docker
- 不使用 Web 框架（Flask/FastAPI/Django）
- 全部为 CLI（`argparse`）

## 代码与安全规范
- Python 文件头：
  - `#!/usr/bin/env python3`
  - `# -*- coding: utf-8 -*-`
- 使用 type hints 与模块 docstring
- 日志统一 `logging`
- 路径统一 `pathlib.Path`
- 网络请求必须带重试
- API key 仅从环境变量读取，禁止硬编码

## 数据分层与事实源
1. 历史输入：`archives/root_json/`
2. 唯一事实源：`data/raw/{venue}/{year}.json`
3. 检索运行面：`data/papers.db` + `data/vectors/chroma`

执行判断：
- M2/M3/M4 仅依赖 `data/raw`，不直接读取历史输入层

## 里程碑顺序
1. M1 数据采集与规范化
2. M2 数据入库（SQLite/FTS）
3. M3 缓存与混合检索
4. M4 Agent 端到端验证

## 关键执行入口
- M1：`python3 -m tools.m1_pipeline <inventory|normalize|backfill|validate|run>`
- M2：`python3 -m tools.m2_db run`
- M3：`python3 -m tools.m3_pipeline run --db-path data/papers.db --embed-base-url https://api.siliconflow.cn/v1/embeddings --embed-model Qwen/Qwen3-Embedding-8B --exclude-placeholder`
- M4：`python3 -m tools.m4_validate run --db-path data/papers.db --vectors-root data/vectors/chroma --collection-name papers_v1 --topics-file artifacts/m3/topic_assignments.json --fixed-query-file docs/fixtures/m4_fixed_queries.yaml --embed-base-url https://api.siliconflow.cn/v1/embeddings --embed-model Qwen/Qwen3-Embedding-8B --embed-api-key "$JANUS_EMBED_API_KEY"`
- M4 状态：`python3 -m tools.m4_validate status`

## 验收口径
- M1：`gate_fail_files = 0` 且无 `aligned=false`
- M2：`all_pass = true`
- M4：`overall_pass = true`
- 端到端基准主题：Continual Learning > Replay Methods

## 任务执行要求（AI）
- 优先复用本文件中的执行流程模板（SOP）
- 每个批次必须产出并检查报告文件
- 禁止无证据更新与手工篡改统计字段
- 当门禁失败：冻结当前批次，先修复再推进

## 执行流程模板（SOP）

### SOP 1：会议批次扩充（推荐默认流程）
触发条件：
- 新增一个会议的若干年份（例如 `ACL 2021-2025`）

输入：
- 会议采集脚本：`tools/<venue>_collect.py`
- 年份范围：`<YYYY-YYYY>`

流程：
1. 采集到历史输入层
```bash
python3 -m tools.<venue>_collect --years <YYYY-YYYY> --output-root archives/root_json
```

2. M1 子集处理
```bash
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/<VENUE>-*.json' inventory
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/<VENUE>-*.json' normalize
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/<VENUE>-*.json' backfill --max-records-per-file 0 --enable-arxiv-title
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/<VENUE>-*.json' validate
```

3. M2 全量重建
```bash
python3 -m tools.m2_db run
```

4. M3/M4 回归
```bash
python3 -m tools.m3_pipeline validate --db-path data/papers.db --vectors-root data/vectors/chroma --collection-name papers_v1 --exclude-placeholder
python3 -m tools.m4_validate status
```

通过标准：
- M1 子集 `gate_fail_files = 0`
- M2 `all_pass = true`
- 检索冒烟可用

### SOP 2：摘要缺失修复（missing_abstract）
强规则：
- 禁止 DOI-only
- DOI 失败后必须进入标题检索链路
- 标题命中必须过相似度阈值

执行顺序：
1. 会议专用源（CVF/PMLR/Anthology/OpenReview）
2. OpenAlex DOI + S2 DOI
3. OpenAlex title + S2 title + arXiv title
4. 人工补录（仅小规模残缺）

每轮后必须检查：
- `artifacts/m1/backfill_report.json`
- `artifacts/m1/quality_report.json`

### SOP 3：版本回归（发布前）
流程：
1. `python3 -m tools.m2_db run`
2. `python3 -m tools.m3_pipeline validate --db-path data/papers.db --vectors-root data/vectors/chroma --collection-name papers_v1 --exclude-placeholder`
3. `python3 -m tools.search search --query "continual learning replay" --top-k 20`
4. `python3 -m tools.search hybrid --query "continual learning replay" --top-k 20`
5. `python3 -m tools.m4_validate status`

关键报告：
- `artifacts/m2/validate_report.json`
- `artifacts/m3/validate_report.json`
- `artifacts/m4/eval_report.json`

### SOP 4：失败批次冻结与恢复
冻结条件：
- 任一门禁失败（M1/M2/M4）

冻结动作：
1. 记录失败会议与年份
2. 固定失败报告路径
3. 暂停该批次继续合并

恢复条件：
- 失败项修复并重新通过对应门禁
