---
name: janussearch-agent
description: JanusSearch CLI 的自然语言入口：默认 search-first，将 query/主题检索请求按“关键词优先级”输出固定模板，并在命中总量超过 20 条时自动导出全量 TSV（包含 DB 全量字段 + matched_topic + janus_topic/janus_subtopic）。当用户明确要求下载论文 PDF 时，优先使用 results.tsv 或显式 paper_id 调用 `python3 -m tools.search download-pdfs`，从 OpenReview、arXiv 或会议/期刊官网解析并批量落盘。仅在用户明确要求时升级执行 M1/M2/M3/M4 等写入/重建操作。
---

# JanusSearch Agent Skill（自然语言路由）

## 定位（必读）

将自然语言请求路由为 JanusSearch 的 CLI 命令，并保证输出可审计、可复现。

- 首要目标：论文发现与检索获取。
- 默认策略：先检索（search-first），再升级（按需）。
- 本 skill 不取代专用于采集的 skill：
  - `.agent/skills/paper-search/SKILL.md`

## 何时使用（触发）

当用户希望执行以下类型任务时使用本 skill：

- 按主题、会议、年份、track、presentation level 查论文
- 通过 `paper_id` 拉取单篇完整记录
- 查看 DB/检索覆盖统计
- 采集新的会议-年份数据并并入当前基线
- 重建 FTS 索引或校验 DB 一致性
- 重建向量/缓存层
- 运行 M4 端到端验收并读取报告
- 排查检索失败问题

## 执行策略（锁定）

默认策略为 `search-first, upgrade-later`。

1. 优先执行读/检索操作：
   - `python3 -m tools.search search`
   - `python3 -m tools.search hybrid`
   - `python3 -m tools.search get`
   - `python3 -m tools.search stats`
2. 仅在用户意图明确时执行写入/重建/长耗时操作：
   - M1 run、M2 run、M3 run、M4 run
3. 对高成本命令保持可追溯性（必须给证据）：
   - 执行的命令
   - 关键指标
   - 报告路径

## 检索类请求（Query Mode，锁定）

当用户提出“query/查找某某主题相关论文/相关工作”时，严格按本节执行与输出。

### 1) 关键词优先级抽取（必须输出）

- 从用户 query 中提炼 3–6 条“关键词组（label）”，按优先级排序。
- 每条关键词组必须包含：
  - `label`：可用 `A / B / 中文` 的形式（面向人类展示）。
  - `aliases`：用于匹配的同义词/缩写/中英文变体（面向机器匹配）。
- 优先级建议（从高到低）：
  1) 用户原始用词（原文短语）
  2) 常见同义词（英文/中文）
  3) 缩写（例如 CIL）
  4) 相关近义术语（但不要发散成无关概念）

生成 `keywords.json`（供导出与审计）：

```json
{
  "query": "原始用户 query",
  "keywords": [
    {
      "label": "Continual Learning / Class-Incremental Learning（持续学习/类增量）",
      "aliases": ["continual learning", "class-incremental learning", "CIL", "持续学习", "类增量学习"]
    }
  ]
}
```

写入路径（必须使用本地文件，便于复现）：
- `artifacts/queries/<query_slug>/run_<timestamp>/keywords.json`

`query_slug` 生成规则（锁定）：
- 小写化；将非字母数字字符替换为 `_`；合并连续 `_`；去掉首尾 `_`；空则用 `query`。

### 2) 检索命令选择（search-first + 条件升级）

- 默认先跑 FTS：

```bash
python3 -m tools.search search --query "<QUERY>" --format json --top-k 20
```

- 满足任一条件时升级到 hybrid（语义相关）：
  - 用户明确要求“语义相关/类似/相关工作/related work”
  - `search` 总命中很少（例如 `< 5`）且向量库就绪（`data/vectors/chroma` 存在）

Hybrid 示例：

```bash
python3 -m tools.search hybrid --query "<QUERY>" --format json --top-k 20
```

### 3) 关键词匹配与主题分组（锁定）

- 将每篇论文的 `title + abstract + keywords` 拼成文本做匹配。
- 匹配方式：大小写不敏感 substring 匹配。
- 按 `keywords.json` 中的 `keywords` 数组顺序（优先级）依次匹配；命中第一个即确定该论文的 `matched_topic=label`，并记录 `matched_keyword=alias`。
- 若全部未命中：`matched_topic="Other"`。

### 4) 本地导出（当 total > 20，锁定）

- 若 `total <= 20`：不导出。
- 若 `20 < total <= 2000`：自动导出全量（`--max-export 0`）。
- 若 `total > 2000`：先在对话中征求确认：
  - 导出全量：`--max-export 0`
  - 仅导出 Top2000：`--max-export 2000`

导出路径（锁定）：
- `artifacts/queries/<query_slug>/run_<timestamp>/results.tsv`

导出命令（锁定，统一用 export 子命令）：

```bash
python3 -m tools.search export \
  --query "<QUERY>" \
  --mode <search|hybrid> \
  --out-tsv "artifacts/queries/<query_slug>/run_<timestamp>/results.tsv" \
  --keywords-json "artifacts/queries/<query_slug>/run_<timestamp>/keywords.json" \
  --topics-json "artifacts/m3/topic_assignments.json" \
  --max-export 0
```

导出 TSV 必须包含：
- DB 全量字段（`papers` 表 + 关系表聚合列）
- `matched_topic` / `matched_keyword`
- `janus_topic` / `janus_subtopic`（来自 `topics-json`；缺失则留空）

### 5) 对话输出模板（锁定）

对话内严格使用以下模板（保持结构与标题不变）：

**匹配的关键词**
- <关键词组 label 1>
- <关键词组 label 2>
- <关键词组 label 3>

**JanusSearch 中的相关工作（按主题）**

**1) <matched_topic_1>**

paper_id · title · VENUE YEAR
paper_id · title · VENUE YEAR

**2) <matched_topic_2>**

paper_id · title · VENUE YEAR

规则（锁定）：
- 对话内总展示不超过 20 条。
- 按关键词优先级（keywords.json 顺序）依次输出主题分组；`Other` 放最后。
- 单个主题最多展示 8 条（防止刷屏）。

当 `total > 20` 时追加导出块（必须）：

**导出（当 total > 20）**
- total=<N>，exported=<M>
- TSV: <绝对路径>
- keywords.json: <绝对路径>
- 说明：TSV 含 `matched_topic` + `janus_topic/janus_subtopic` + DB 全量字段

## PDF 下载请求（PDF Download Mode，锁定）

当用户明确说“下载 PDF / 下载论文 / 把这些论文下下来 / download pdf”时，严格按本节执行。

### 1) 输入优先级（锁定）

按以下顺序选择下载目标：

1. 用户显式给出的 `results.tsv` 路径
2. 用户显式给出的一个或多个 `paper_id`
3. 当前线程最近一次 JanusSearch 输出中的论文列表

上下文模式只接受可识别的行：

- `paper_id · title · VENUE YEAR`

如果上下文里只有标题、没有 `paper_id`，且无法唯一定位，必须要求用户澄清；不要猜测。

### 2) 下载命令（锁定）

TSV 全量下载：

```bash
python3 -m tools.search download-pdfs --input-tsv "<ABS_RESULTS_TSV>"
```

TSV 子集下载：

```bash
python3 -m tools.search download-pdfs \
  --input-tsv "<ABS_RESULTS_TSV>" \
  --paper-id "S2-..." \
  --paper-id "S2-..."
```

上下文 / `paper_id` 模式：

```bash
python3 -m tools.search download-pdfs \
  --paper-id "S2-..." \
  --paper-id "S2-..." \
  --output-dir "artifacts/queries/<query_slug>/run_<timestamp>/pdfs"
```

规则（锁定）：

- 若给了 `results.tsv` 且未指定 `--output-dir`，默认保存到 `results.tsv` 同级的 `pdfs/`
- 若文件已存在，默认跳过并记录；只有用户明确要求覆盖时才加 `--overwrite`
- `download-pdfs` 会自动生成 `pdf_download_report.json`
- `download-pdfs` 还会自动生成 `failed.tsv`，列出所有失败项（至少含 `paper_id/title/resolved_pdf_url/error`）

### 3) PDF 解析链（锁定）

`download-pdfs` 的解析优先级固定为：

1. `source_ids_json` / DB `source_ids` 中的直接 PDF URL（如 `cvf_pdf_url`、`openreview_pdf_url`、`aaai_pdf_url`、`ijcai_pdf_url`）
2. `openreview_id -> https://openreview.net/pdf?id=<id>`
3. `arxiv_id -> https://arxiv.org/pdf/<id>.pdf`
4. 官方页面回退解析（从 `url` 或会议/期刊官网 HTML 中提取 `citation_pdf_url` 或 `.pdf` 链接）

只下载公开可访问的 PDF；不要尝试 DOI 付费页绕过。

### 4) 对话输出模板（锁定）

完成后使用以下结果块：

**PDF 下载结果**
- Downloaded: <N>
- Skipped Existing: <N>
- Failed: <N>

**Artifacts/Report Paths**
- PDFs: <绝对目录路径>
- Report JSON: <绝对路径>
- failed.tsv: <绝对路径>

若有失败项，再补充最多 5 条：

- `paper_id · title · error`

## 意图路由（按任务意图）

- 论文列表检索 -> `python3 -m tools.search search`
- 语义/相似检索 -> `python3 -m tools.search hybrid`
- PDF 批量下载 -> `python3 -m tools.search download-pdfs`
- 单篇详情获取 -> `python3 -m tools.search get`
- 检索/DB 统计 -> `python3 -m tools.search stats`
- FTS 修复/重建 -> `python3 -m tools.m2_db reindex-fts`
- DB 重建/校验 -> `python3 -m tools.m2_db run` / `validate`
- 向量/缓存重建 -> `python3 -m tools.m3_pipeline run`（或分步子命令）
- 端到端验收 -> `python3 -m tools.m4_validate run` / `status`
- 数据质量处理 -> `python3 -m tools.m1_pipeline` 子命令
- 扩充采集 -> `python3 .agent/skills/paper-search/scripts/fetch_conference_papers.py` + `m1_pipeline` + `m2_db run`

## 参数抽取规则

从自然语言中抽取（若用户提供）：

- `query`
- `venue`（规范化为大写；多个时用逗号分隔）
- `year_from`, `year_to`
- `track`
- `presentation_level`（`poster|oral|bestpaper`）
- `top_k`

默认值：

- 未提供参数时使用工具默认值
- 默认优先 `search`
- 当用户意图是“语义相关/相似/找类似”时切换到 `hybrid`

## 环境与前置条件

执行命令前先检查：

1. 读操作所需 DB 是否存在：
   - `data/papers.db`
2. Hybrid 所需向量库是否存在：
   - `data/vectors/chroma`
   - collection `papers_v1`
3. M4 运行所需环境变量是否具备：
   - `JANUS_EMBED_API_KEY` or `JANUS_LLM_API_KEY`
4. 若依赖/索引缺失，先给修复命令：
   - `python3 -m tools.m2_db reindex-fts`

## 失败处理与回退

1. `search` 无结果：
   - 建议放宽过滤条件
   - 建议切换到 `hybrid`
2. `hybrid` 失败：
   - 回退到 `search`
   - 明确报告向量链路问题
3. M4 online gate 失败：
   - 明确标记硬失败
   - 不得声称通过
   - 给出可执行的修复建议（key/env/网络连通性）

## 输出格式约定（锁定）

除 Query Mode 外，对每个请求返回固定结构：

1. `Intent`
2. `Command Executed`
3. `Key Results`
4. `Artifacts/Report Paths`
5. `Next Options`（编号 `1/2/3`）

规则：

- 只要产出文件，就必须给出证据路径。
- 报告路径使用绝对路径，例如：
  - `/Users/yangli/Workspace/JanusSearch/artifacts/m4/eval_report.json`

## 路由示例（>=8）

1. 精确检索请求
- 用户："查 ICLR 2024 continual learning replay 前20篇"
- 路由：`tools.search search`
- 命令：
  - `python3 -m tools.search search --query "continual learning replay" --venue ICLR --year-from 2024 --year-to 2024 --top-k 20`

2. 语义检索请求
- 用户："找和 replay methods 语义最相关的论文"
- 路由：`tools.search hybrid`
- 命令：
  - `python3 -m tools.search hybrid --query "replay methods" --top-k 20`

3. 单篇详情获取
- 用户："查看 paper_id=S2-6625578ea850761e 的完整信息"
- 路由：`tools.search get`
- 命令：
  - `python3 -m tools.search get --paper-id S2-6625578ea850761e`

4. DB/检索统计
- 用户："数据库检索面统计"
- 路由：`tools.search stats`
- 命令：
  - `python3 -m tools.search stats`

5. 重建 FTS
- 用户："重建 FTS 索引"
- 路由：`tools.m2_db reindex-fts`
- 命令：
  - `python3 -m tools.m2_db reindex-fts`

6. 全量重建 M3
- 用户："重新构建向量和缓存"
- 路由：`tools.m3_pipeline run`
- 命令：
  - `python3 -m tools.m3_pipeline run --db-path data/papers.db --embed-base-url https://api.siliconflow.cn/v1/embeddings --embed-model Qwen/Qwen3-Embedding-8B --exclude-placeholder`

7. 正式执行 M4 验收
- 用户："执行 M4 正式验收并出报告"
- 路由：`tools.m4_validate run`
- 命令：
  - `python3 -m tools.m4_validate run --db-path data/papers.db --vectors-root data/vectors/chroma --collection-name papers_v1 --topics-file artifacts/m3/topic_assignments.json --fixed-query-file docs/fixtures/m4_fixed_queries.yaml --embed-base-url https://api.siliconflow.cn/v1/embeddings --embed-model Qwen/Qwen3-Embedding-8B --embed-api-key "$JANUS_EMBED_API_KEY"`

8. 查看 M4 状态
- 用户："看最新 M4 状态"
- 路由：`tools.m4_validate status`
- 命令：
  - `python3 -m tools.m4_validate status`

9. Hybrid 失败回退场景
- 用户："用语义检索找 continual replay"
- 主路径：`tools.search hybrid`
- 失败回退：
  - `python3 -m tools.search search --query "continual replay"`
- 输出必须包含：回退原因 + 向量链路报错片段。

10. 扩充批次接入
- 用户："新增 AAAI 2024-2025 并接入检索"
- 路由：
  - `python3 .agent/skills/paper-search/scripts/fetch_conference_papers.py AAAI-24 --output archives/root_json/AAAI-24.json`
  - `python3 .agent/skills/paper-search/scripts/fetch_conference_papers.py AAAI-25 --output archives/root_json/AAAI-25.json`
  - `python3 -m tools.m1_pipeline --input-glob 'archives/root_json/AAAI-2*.json' normalize`
  - `python3 -m tools.m1_pipeline --input-glob 'archives/root_json/AAAI-2*.json' validate`
  - `python3 -m tools.m2_db run`

## 本 skill 的测试场景

1. "查 ICLR 2024 continual learning replay 前20篇" -> 路由到 `search`，并带 venue/year/top_k 过滤。
2. "找和 replay methods 语义最相关的论文" -> 路由到 `hybrid`。
3. "查看 paper_id=... 的完整信息" -> 路由到 `get`。
4. "数据库检索面统计" -> 路由到 `stats`。
5. "重建 FTS 索引" -> 路由到 `m2_db reindex-fts`。
6. "执行 M4 正式验收并出报告" -> 路由到 `m4_validate run`，并返回 3 个报告路径。
7. 模拟 `hybrid` 失败 -> 回退到 `search` 并给出修复建议。

## 文档入口链接

- `docs/README.md`
- `docs/10_CORE_ARCHITECTURE.md`
- `docs/20_PIPELINE_AND_GATES.md`
- `docs/30_EXPANSION_POLICY.md`
- `docs/90_HISTORY.md`
