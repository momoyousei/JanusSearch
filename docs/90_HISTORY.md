# 历史冻结与复盘（History）

## 时间线
- 2026-08-07：M1～M4 降级为兼容入口，系统改用能力化架构与四个职责单一 Skill
- 2026-02-19：M1 初步冻结
- 2026-02-22：ICML-2021 专项修复
- 2026-02-22：CVPR 2021-2025 采集与补齐复盘
- 2026-02-22：AAAI 2021-2025 官方采集复盘（AAAI-26 移除）
- 2026-02-23：ACL 2021-2025 采集与摘要补全复盘
- 2026-05-06：CVPR/AAAI/TPAMI 2026 更新检查与 CVPR virtual 采集补充
- 2026-05-07：M3 向量构建改为 paper_id 级增量 embed
- 2026-05-07：ICML 2026 官方 virtual JSON 采集入库

## 关键结论
0. 能力化架构重构（2026-08-07）
- 主架构调整为 corpus/catalog/projections/search/evaluate/doctor，M1～M4 仅保留历史与 CLI 兼容语义。
- 建立 `janussearch/` 正式包、run manifest、输入指纹与采集器注册表。
- 官方 paper/track/presentation 对齐改为默认 warning，显式 strict 时仍为硬门禁；字段质量与重复检查继续硬失败。
- 评估改为离线默认、在线显式，status 必须拒绝无指纹或输入已变化的历史 PASS。
- 原两个复合 Skill 替换为显式 router、query、corpus、ops 四个 Skill；生产采集逻辑移出 Skill 目录。

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

8. ICML 2026 更新检查与入库（2026-05-07）
- 官方 ICML virtual 页面已发布 `https://icml.cc/static/virtual/data/icml-2026-orals-posters.json`，响应头 `Last-Modified: Wed, 06 May 2026 17:36:55 GMT`。
- 官方 JSON 共 6567 条唯一标题：`conference=6352`，`position_paper_track=215`；`poster=5992`，`oral=575`；作者与摘要覆盖率均为 100%。
- `.agent/skills/paper-search/scripts/fetch_conference_papers.py ICML-26 --provider openreview` 当天返回 0 条，因此 ICML 2026 不能用 OpenReview API 0 结果判断“无更新”；本轮新增 `tools.icml_collect`，以官方 virtual JSON 为事实采集源。
- M1 子集 `gate_fail_files=0` 且官方 `paper_count`、`track_counts`、`presentation_level_counts` 全部 `aligned=true`；M2 全量重建后 `paper_count_actual=128403`、`source_file_count_actual=85`、`all_pass=true`。
- M3 增量补齐 ICML 2026 的 6567 个向量后，`vector_count=124683`、`assignment_count=124683`、`all_pass=true`；topic 命名沿用 `Pro/deepseek-ai/DeepSeek-V3.2`，避免从旧进度漂移到默认 Qwen 模型。
- M4 在当前 DB/Chroma/topic assignments 上重跑通过：`overall_pass=true`，固定集 4/4，抽样集 40/40。

## 可复用经验
- 先做小样本命中率探测，再决定全量重跑
- 回填策略优先“会议特化源”，其次通用 API
- 保留报告链路，确保每轮增量有可审计证据
- M3 增量向量化必须优先按 `paper_id` 和 metadata fingerprint 判断，source-file marker 只能作为加速信息，不能作为唯一事实源
- 对 ICML 2026 这类 virtual JSON 已发布但 OpenReview API 未返回 accepted notes 的情况，优先信任官方 virtual JSON，并保留 `Last-Modified`、track/sourceurl 计数和门禁报告。
