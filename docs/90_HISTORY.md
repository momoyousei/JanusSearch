# 历史冻结与复盘（History）

## 时间线
- 2026-02-19：M1 初步冻结
- 2026-02-22：ICML-2021 专项修复
- 2026-02-22：CVPR 2021-2025 采集与补齐复盘
- 2026-02-22：AAAI 2021-2025 官方采集复盘（AAAI-26 移除）
- 2026-02-23：ACL 2021-2025 采集与摘要补全复盘
- 2026-05-06：CVPR/AAAI/TPAMI 2026 更新检查与 CVPR virtual 采集补充
- 2026-05-07：M3 向量构建改为 paper_id 级增量 embed

## 关键结论
1. M1 初冻（2026-02-19）
- 主流程可复现，可进入 M2
- 当时仅 `ICML-21` 摘要覆盖率未过 85%
- 无已知官方口径错配（无 `aligned=false`）

2. ICML-2021 修复（2026-02-22）
- 通过 PMLR 专用补源（v139）定向修复
- `resolved_abstract_coverage` 由 `79.44% -> 99.57%`
- 全量 M1 达成 `gate_fail_files = 0`

3. CVPR 2021-2025 增量
- 官方总量先验：11674
- 采集与官方口径对齐
- 摘要缺失缩减至极小规模残留

4. AAAI 2021-2025 增量
- 官方总量与采集总量一致（9910）
- OJS Technical Tracks 口径稳定可复现

5. ACL 2021-2025 增量
- 官方总量与采集总量一致（9865）
- 经验：禁止 DOI-only，必须执行标题检索链路

6. CVPR/AAAI/TPAMI 2026 更新检查（2026-05-06）
- AAAI-2026：AAAI OJS 已发布 AAAI-26 Technical Tracks，本地已有 4149 篇，摘要与作者覆盖率均为 100%；本轮全量重跑在 OJS `issue/view/699` 遇到远端 503 中断，但 archive issue 数仍为 43，和 2026-04-05 报告一致
- TPAMI-2026：DBLP PAMI volume 48 已更新至第 5 期附近，`tools.tpami_collect` 重跑后从 308 篇增至 372 篇，摘要与作者覆盖率均为 100%
- CVPR-2026：CVF OpenAccess 首页尚未发布 CVPR 2026 主会入口，但 CVPR virtual 官方 JSON 已可用；`tools.cvpr_collect` 增加 `--source virtual|auto`，对 `cvpr-2026-orals-posters.json` 按标题去重，原始 4211 条展示记录归并为 4070 篇唯一论文，摘要与作者覆盖率均为 100%

7. M3 向量增量优化（2026-05-07）
- 问题：旧版 `build-vectors` 的增量粒度是 source-file marker；一个文件 marker 失效时会重 embed 整个文件，即使大部分 `paper_id` 已在 Chroma 中。
- 决策：改为先按 `paper_id` 查询 Chroma，本地判断缺失、文本 hash/模型配置变更、legacy metadata 与 marker 配置关系，只对必要论文请求 embedding。
- 附加保护：marker 命中前先确认目标 IDs 均存在；非 `--max-papers` 全量构建会删除 DB 当前候选集之外的 stale vectors。
- 验证：`tests.test_m3_pipeline` 通过；当前真实库复跑 `build-vectors` 得到 `embedded_count=0`、`source_files_skipped_by_marker=84`、`collection_count=118116`。

## 可复用经验
- 先做小样本命中率探测，再决定全量重跑
- 回填策略优先“会议特化源”，其次通用 API
- 保留报告链路，确保每轮增量有可审计证据
- M3 增量向量化必须优先按 `paper_id` 和 metadata fingerprint 判断，source-file marker 只能作为加速信息，不能作为唯一事实源
