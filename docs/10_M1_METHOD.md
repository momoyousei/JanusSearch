# M1 方法论与流水线

## 方法论原则
- 先标准化，再回填，再验证，不跳步骤
- 以“可复现 CLI + 报告文件”作为事实来源
- 优先保证统计对齐与数据一致性，再追求覆盖率提升

## M1 流水线（实操版）
入口脚本：`tools/m1_pipeline.py`

1. `inventory`（盘点）
- 读取输入文件（默认 `archives/root_json/*-*.json`）
- 预估规范化后质量指标，不改动数据
- 输出：`index/m1_inventory.json`

2. `normalize`（规范化）
- 字段清洗：标题/作者/DOI/track/presentation 等归一
- 去重：基于标准化标题聚合，保留质量更高记录
- 状态标注：`resolved` / `repaired` / `placeholder`
- 写回：
  - 根文件（可选写回）
  - 规范化副本：`data/raw/{venue}/{year}.json`
- 输出：`index/m1_normalize_report.json`

3. `backfill`（摘要补全）
- 优先级：
  - ICML 优先 PMLR（`proceedings.mlr.press`）按标题回填 abstract
  - S2 DOI 查询
  - S2 标题查询
  - arXiv ID 补全
  - （可选）arXiv 标题补全
- 对缺摘要记录做增量修复
- 输出：`index/m1_backfill_report.json`

4. `validate`（质量门禁）
- 覆盖率门禁（作者/摘要）
- 去重门禁
- 官方口径对齐门禁（paper/track/presentation）
- 输出：
  - `index/m1_quality_report.json`
  - `index/stats.md`

## CVPR 官方源策略（2026-02 增补）
- 采集源：CVF OpenAccess（`https://openaccess.thecvf.com/CVPR{year}?day=all`）。
- 执行顺序必须为：
  1. 先统计官方口径总量（list 页面 `ptitle` 条目数）
  2. 再批量抓取论文
  3. 最后做一致性校验（official count == collected count）
- 对 CVPR 的摘要补全优先级：
  1. 详情页 `paper.html` 的 `div#abstract`
  2. `m1_pipeline backfill`（S2 / arXiv）
  3. Wayback 历史快照（仅在官方链接 404 且可追溯时启用）
- 当官方链接长期 404 且外部源不可用时，允许保留极小规模残缺并在报告中显式登记，不改动官方总量口径。

## AAAI 新年份回补策略（2026-02 增补）
- 主口径仍为 AAAI OJS Technical Tracks（`AAAI-YY Technical Tracks *`）。
- 当 OJS 尚未发布对应年份 issue 时，允许启用 OpenReview fallback：
  1. 先确认 OJS 缺失（无对应 Technical Tracks issue）。
  2. 再按 OpenReview `content.venue` 精确匹配回补（如 `AAAI 2026`, `AAAI 2026 Oral`）。
  3. 最后将“OpenReview 公告/官方通报”中的投稿规模作为参考口径写入报告，不与已发布 OJS 正式总量混算。
- fallback 结果必须在报告中显式标注来源为 `openreview`，避免与 OJS 正式口径混淆。

## 官方口径对齐策略
- NeurIPS：优先使用 `official_tracks + source_url` 拉取官方 catalog，并按标题映射回写 track/presentation。
- 其他会议：若无官方 catalog，使用 `reconciliation` 作为可用基线。
- 无基线时标记为 `aligned = null`，不误判为失败。

## 关键实操结论
- 在无 S2 key 场景，429 会显著限制 backfill 吞吐与成功率。
- 小批次增量回填可控，但单位时间收益可能很低。
- 对齐与规范化可先稳定冻结，再在后续里程碑分离优化覆盖率债务。

## 关联文档
- 数据规范：`11_M1_DATA_STANDARD.md`
- 质量门禁：`12_M1_QUALITY_GATES.md`
- 实操命令：`13_M1_OPERATIONS_RUNBOOK.md`
- 冻结结果：`14_M1_FREEZE_2026-02-19.md`
- 增量复盘：`16_M1_CVPR2021_2025_PATCH_AND_LESSONS_2026-02-22.md`
