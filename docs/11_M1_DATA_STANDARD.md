# M1 数据规范（Data Contract）

## 顶层文件结构（历史输入文件）
每个 `archives/root_json/*-*.json` 在 M1 规范化后应包含：
- `query`: 查询上下文（target, venue_code, year, provider 等）
- `source`: 数据源描述
- `generated_at_utc`
- `paper_count`
- `track_counts`
- `track_group_counts`
- `presentation_level_counts`
- `papers`: 论文记录数组
- `reconciliation`（可选）
- `official_tracks`（可选）
- `m1`: 本轮规范化元信息与指标快照

## canonical 文件结构（data/raw）
规范化产物：`data/raw/{venue}/{year}.json`

字段：
- `venue`, `year`, `collected_at`, `source`, `count`, `metrics`
- `papers[]` 每条记录：
  - `paper_id`
  - `title`
  - `authors`
  - `venue`, `year`
  - `abstract`
  - `doi`
  - `url`
  - `citation_count`
  - `source_provider`
  - `source_ids`
  - `keywords`
  - `track`
  - `track_display_name`
  - `track_group`
  - `presentation_level`
  - `institutions`
  - `record_status`
  - `quality_flags`
  - `collected_at`

## 字段归一规则
1. `title` / `paper_title`
- 统一保留一致文本；去重和匹配使用标准化标题键（小写+去标点+去空格）

2. `doi`
- 统一为裸 DOI（去掉 `https://doi.org/` / `doi:` 前缀）

3. `track`
- 归一到 slug（示例：`conference`, `datasets_and_benchmarks_track`）

4. `track_group`
- 仅 `main` 或 `other`

5. `presentation_level`
- 枚举：`poster` / `oral` / `bestpaper`
- 缺省值：`poster`

6. `record_status`
- `resolved`: 常规完整记录
- `repaired`: 通过回填修复后记录
- `placeholder`: 外部对齐补位但信息不完整记录

## 去重规则
- 主键：标准化标题
- 标题为空时降级使用 DOI / openreview_id / openalex_id
- 多条冲突时按记录得分保留高质量条目（标题/作者/摘要/标识符优先）

## 占位记录判定
满足以下典型条件之一时标记为 `placeholder`：
- `external_only = true`
- 作者缺失 + 摘要缺失 + 无 DOI/OpenReview/OpenAlex 主锚点，且仅外链存在

## CVPR 补充约定（2026-02）
- 当来源为 CVF OpenAccess 时，`query.provider` 与 `source.provider` 统一使用 `cvf_openaccess`。
- `source_ids` 建议包含：
  - `cvf_paper_html`
  - `cvf_pdf_url`
  - `cvf_arxiv_url`（若存在）
  - `arxiv_id`（若存在）
  - `cvf_bibtex_key`（若存在）
  - `cvf_pages`（若存在）
- 若通过 Wayback 恢复摘要，补充 `source_ids.wayback_pdf_url`，并将 `record_status` 置为 `repaired`。
- 若官方链接长期 404 且无法恢复，保留 `missing_abstract` 质量标记并在阶段报告登记，不得修改 `paper_count`。

## AAAI OpenReview 回补约定（2026-02）
- 当 `AAAI-YY` 在 OJS 尚无 Technical Tracks 时，可使用 OpenReview 回补，`query.provider` 与 `source.provider` 统一为 `openreview`。
- `source_ids` 建议包含：
  - `openreview_note_id`
  - `openreview_forum_id`
  - `openreview_venue`
  - `openreview_venueid`
  - `openreview_invitation`（若有）
  - `openreview_pdf_url`（若有）
- 回补数据需在 `query.work_filter_strategy` 中显式记录匹配策略（如 `content.venue in [...]`）。
- 回补结果属于“早期可见样本”，不得与 OJS 正式最终口径直接混算。

## 关联文档
- 方法论：`10_M1_METHOD.md`
- 门禁规则：`12_M1_QUALITY_GATES.md`
