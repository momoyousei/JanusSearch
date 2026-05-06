---
name: paper-search
description: 针对指定会议-年份目标，抓取并导出论文元数据到单个 JSON 文件。当用户需要抓取 AAAI/CVPR/NeurIPS 等会议某一年的全部论文，并要求包含题目、作者、机构、摘要、关键词、展示级别、track 标注以及对账补齐时使用。
---

# 论文采集与导出（Paper Search）

## 适用场景

当用户提出以下需求时使用本 skill：

- "抓取某个会议某一年的全部论文"
- "导出成 JSON，包含题目、作者、机构、摘要、关键词"
- "对账官网列表，补齐缺失条目"
- "给每篇论文标注 track（main/conference、datasets、position、journal 等）"
- "批量跑多个年份（例如 NeurIPS-21 到 NeurIPS-25）"

核心脚本：

- `.agent/skills/paper-search/scripts/fetch_conference_papers.py`

## 输入/输出约定

输入：

- 必填目标 token：`VENUE-YY` 或 `VENUE-YYYY`
- 示例：`AAAI-26`、`CVPR-2025`、`NeurIPS-25`

环境变量：

- 在 JanusSearch 项目中，默认先读取项目根目录 `.codex/.env`，再执行依赖 API key 的采集、回填、M3 或 M4 命令。
- 读取方式必须避免打印密钥明文；推荐命令前缀：`set -a; source .codex/.env; set +a; ...`
- `.codex/.env` 中的 `JANUS_EMBED_API_KEY`、`JANUS_LLM_API_KEY` 可供 M3/M4 使用；若命令需要 OpenAlex/S2 key，也优先从同一文件读取对应环境变量。

输出：

- 单个 JSON 文件（不要按 track 拆分）
- 推荐路径：`archives/root_json/{VENUE}-{YY}.json`
- 单篇论文字段包含：
  - `paper_title`
  - `authors`
  - `institutions`
  - `abstract`
  - `keywords`
  - `presentation_level`（`poster` / `oral` / `bestpaper`）
  - `track`（规范化 slug）
  - `track_display_name`（面向人类）
  - `track_group`（`main` 或 `other`）

顶层字段包含：

- `query`, `source`, `generated_at_utc`, `paper_count`
- `track_counts`, `track_group_counts`
- `papers`
- 可选 `reconciliation`（当使用 `--reconcile-url`）
- 可选 `official_tracks`（NeurIPS 官方 track 映射元数据）

## 端到端流程

1. 解析目标
- 将 `VENUE-YY` 解析为 `venue_code + year`（例如 `NeurIPS-25 -> NEURIPS + 2025`）。
- 校验年份范围与 token 格式。

2. 读取展示级别覆盖（可选）
- 若提供 `--overrides`，读取基于标题的 overrides 用于设置 `presentation_level`。

3. 预加载 NeurIPS 官方 track 索引（仅 NeurIPS）
- 抓取 `https://neurips.cc/static/virtual/data/neurips-{year}-orals-posters.json`。
- 根据官方 `sourceurl` 构建 title -> track 映射。
- track 示例：
  - `conference`
  - `datasets_and_benchmarks_track`
  - `position_paper_track`
  - 期刊 track（`journal_track_jmlr`、`journal_track_tmlr`、`journal_track_annals_of_statistics` 等）

4. 获取 provider 数据
- `provider=openalex`：仅 OpenAlex。
- `provider=openreview`：仅 OpenReview（按 accepted-paper 逻辑）。
- `provider=auto`：优先 OpenAlex；当数量异常偏低时 fallback/switch 到 OpenReview。

5. 规范化 provider 记录
- 规范化 authors/institutions，并在保序前提下去重。
- 从 OpenAlex 的 `abstract_inverted_index` 重建摘要。
- 从 `keywords` 提取关键词；缺失时回退到 ranked concepts。
- 通过统一 setter 规范化 track 字段：
  - `track`
  - `track_display_name`
  - `track_group`

6. 与外部清单对账（可选）
- 使用 `--reconcile-url` 解析外部论文列表（通常为 NeurIPS virtual 的 `papers.html`）。
- 噪声清理：
  - 移除导航与非论文锚点
  - 过滤 `session` / `town-hall` 等非论文分类
- 对比规范化后的标题：
  - `matched`
  - `missing_in_provider`
  - `extra_in_provider`
- 使用 `--reconcile-include-missing` 时，为缺失标题追加 placeholder 记录。

7. 应用 NeurIPS 官方 track 映射（若已加载）
- 按规范化标题匹配，并用官方 track 覆盖 provider/默认 track。
- 为命中的论文附加 `official_track_source_url`。
- 若官方目录含 `conference`，将残留旧 `main` 重映射为 `conference`。

8. 收尾并导出
- 按规范化标题排序。
- 生成 `track_counts` 与 `track_group_counts`。
- 写出单文件 JSON。

## Track 命名规则

NeurIPS：优先使用官方 track；仅在未命中时启用兜底启发式。

- `track`：机器可读标识（slug）
- `track_display_name`：面向人类展示
- `track_group`：粗粒度分类：
  - `main`：conference/main body（主会/主轨）
  - `other`：datasets、position、journal、challenge、workshop-like tracks（非主轨）

## 命令示例

```bash
python3 .agent/skills/paper-search/scripts/fetch_conference_papers.py CVPR-26 \
  --output archives/root_json/CVPR-26.json \
  --api-key "$OPENALEX_API_KEY"

python3 .agent/skills/paper-search/scripts/fetch_conference_papers.py NeurIPS-25 \
  --output archives/root_json/NeurIPS-25.json \
  --provider openreview \
  --reconcile-url https://neurips.cc/virtual/2025/papers.html \
  --reconcile-include-missing

python3 .agent/skills/paper-search/scripts/fetch_conference_papers.py NeurIPS-25 \
  --output archives/root_json/NeurIPS-25.json \
  --provider auto \
  --api-key "$OPENALEX_API_KEY" \
  --no-progress
```

批量重跑 NeurIPS 2021-2025：

```bash
for y in 21 22 23 24 25; do
  python3 .agent/skills/paper-search/scripts/fetch_conference_papers.py "NeurIPS-$y" \
    --output "archives/root_json/NeurIPS-$y.json" \
    --provider openreview \
    --reconcile-url "https://neurips.cc/virtual/20${y}/papers.html" \
    --reconcile-include-missing
done
```

当会议名称匹配存在歧义时，使用 `--source-id` 强制指定 OpenAlex source：

```bash
python3 .agent/skills/paper-search/scripts/fetch_conference_papers.py CVPR-26 \
  --source-id https://openalex.org/S4306400393 \
  --source-name "IEEE/CVF Conference on Computer Vision and Pattern Recognition" \
  --api-key "$OPENALEX_API_KEY" \
  --output archives/root_json/cvpr26.json
```

采集完成后，继续执行：
- `python3 -m tools.m1_pipeline --input-glob 'archives/root_json/{VENUE}-*.json' normalize`
- `python3 -m tools.m1_pipeline --input-glob 'archives/root_json/{VENUE}-*.json' validate`
- `python3 -m tools.m2_db run`

## 数据质量说明

- OpenReview 的接收数量可能低于官方接收公告。
- 当来源的收录口径不同，即使做过清理，对账仍可能出现 `missing`。
- NeurIPS 官方 track 索引可能包含 OpenReview 未返回的条目。
- 外部对账解析器是启发式实现；网站结构变化时可能需要新增分类清理规则。

## 运行规则

- 将 `paper_title`、`authors`、`abstract` 视为 OpenAlex 元数据的主字段来源。
- 机构（institutions）去重时保持首次出现顺序不变。
- 优先从 OpenAlex 的 `keywords` 获取关键词；必要时回退到 top concepts。
- 当支持的会议在 OpenAlex 返回数量异常偏低时，使用 OpenReview 兜底（`--provider auto`）。
- 对已知 OpenReview ID 的会议，可用 `--provider openreview` 强制走 accepted-paper 拉取。
- 使用 `--reconcile-url` 与外部清单对账。
- 使用 `--reconcile-include-missing` 将缺失标题追加为 placeholder 记录。
- `presentation_level` 默认 `poster`；可用 JSON overrides 覆盖为 `oral` 或 `bestpaper`。
- 未解析字段保持为空值，不要臆造内容。

## 资源

### scripts/

- `scripts/fetch_conference_papers.py`：抓取指定会议-年份论文并导出规范化 JSON。

### references/

- `references/presentation_overrides_template.json`：手工 oral/bestpaper 覆盖模板。
- `references/presentation_overrides.json`：可选的手工覆盖数据。
