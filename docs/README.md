# Docs 索引

## 阅读顺序
1. `01_SCOPE_AND_MILESTONES.md`  
   项目范围、阶段边界、里程碑目标。
2. `10_M1_METHOD.md`  
   M1 的方法论与流水线设计。
3. `11_M1_DATA_STANDARD.md`  
   M1 数据契约与标准化字段定义。
4. `12_M1_QUALITY_GATES.md`  
   质量门禁与官方统计口径对齐规则。
5. `13_M1_OPERATIONS_RUNBOOK.md`  
   实操命令、无 key 场景策略、回填节奏。
6. `14_M1_FREEZE_2026-02-19.md`  
   本次 M1 初步冻结结果与遗留风险。
7. `15_M1_ICML21_PATCH_AND_LESSONS_2026-02-22.md`
   ICML-2021 修复复盘、方法论经验与可复用策略。
8. `16_M1_CVPR2021_2025_PATCH_AND_LESSONS_2026-02-22.md`
   CVPR 2021-2025 官方口径采集、摘要补齐与验收复盘。
9. `17_M1_AAAI2021_2026_COLLECTION_AND_LESSONS_2026-02-22.md`
   AAAI 2021-2025 官方口径采集、对齐统计与执行复盘（AAAI-26 已移除）。
10. `20_M2_DATABASE.md`
   M2-A：JSON 到 SQLite 的入库与校验规范。
11. `21_M2_SEARCH_CLI.md`
   M2-B：基于 SQL+FTS 的检索 CLI（search/get/stats）。
12. `22_M3_CACHE_AND_HYBRID.md`
   M3：向量构建、主题缓存（L1-L4）与混合检索（hybrid）。
13. `30_M4_AGENT_VALIDATION.md`
   M4：云端硬门禁 + 固定查询 + 抽样查询 + Replay 当前覆盖口径验收。
14. `40_EXPANSION_PLAYBOOK.md`
   扩充阶段：新会议/新年份的采集、规范化、入库、验收批处理手册。

## 文档关系
- `AGENTS.md`：全局约束与执行入口（链接到本目录）
- `PROJECT.md`：项目目标与阶段状态（链接到本目录）
- `index/m1_quality_report.json`：当前质量事实来源
- `index/m1_backfill_report.json`：最近回填结果事实来源
- `index/cvpr_collection_report.json`：CVPR 官方口径采集与补齐执行报告
- `index/aaai_collection_report.json`：AAAI 官方口径采集与补齐执行报告
- `index/m2_load_report.json`：M2 入库执行报告
- `index/m2_validate_report.json`：M2 一致性校验报告
- `index/m2_fts_report.json`：FTS 重建报告（手动 reindex 时生成）
- `index/m3_build_report.json`：M3 构建执行报告
- `index/m3_validate_report.json`：M3 校验报告
- `index/m3_topic_assignments.json`：M3 主题/子主题分配结果
- `index/m4_eval_report.json`：M4 机器可读验收报告
- `index/m4_eval_report.md`：M4 人工可读验收摘要
- `index/m4_sampled_queries.json`：M4 抽样查询快照

## 更新规则
- 规则变化优先更新 `10~13` 系列文档。
- 冻结或阶段性里程碑用新文件沉淀（如 `14_*`）。
- 不在 `AGENTS.md` / `PROJECT.md` 填写实现细节，统一链接到 `docs/`。
