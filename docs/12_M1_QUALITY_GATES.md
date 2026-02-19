# M1 质量门禁与官方口径对齐

## 门禁目标
M1 的完成判定由自动验证决定，而非主观判断。
验证入口：`python3 -m tools.m1_pipeline validate`

## 基础门禁（默认阈值）
- `duplicate_title_count == 0`
- `resolved_authors_coverage >= 90.0`
- `resolved_abstract_coverage >= 85.0`

说明：
- 覆盖率按 `record_status != placeholder` 的记录计算（即 resolved + repaired）。

## 官方统计口径对齐门禁
对齐维度：
1. 年度论文总数（paper_count）
2. track 分布（track_counts）
3. presentation 分布（poster/oral/bestpaper）

`aligned` 三态语义：
- `true`: 已有官方基线，且完全对齐
- `false`: 已有官方基线，但不对齐（判定失败）
- `null`: 当前无该维度官方基线（不计失败，但属于能力缺口）

## 对齐基线来源优先级
1. `official_tracks`（若可用，优先）
2. `reconciliation`（外部标题/track 对齐信息）
3. 无可用基线时记为 `null`

## 当前阶段验收（M1 初步冻结）
- 要求：不得出现 `aligned=false` 的口径错位
- 允许：`aligned=null`（但需在冻结报告中列明覆盖缺口）
- 详细结果见：`14_M1_FREEZE_2026-02-19.md`

## 端到端语义基准（后续里程碑复用）
基准主题：**Continual Learning > Replay Methods**

代表性 ground truth（节选）：
- Dark Experience for General Continual Learning (NeurIPS 2020)
- GDumb (ECCV 2020)
- MIR (NeurIPS 2019)
- Rainbow Memory (CVPR 2021)
- ESMER (NeurIPS 2023)

目标：最终检索结果中命中至少 7/9（按最终主题缓存文件核验）。

## 关联文档
- M1 方法：`10_M1_METHOD.md`
- 数据契约：`11_M1_DATA_STANDARD.md`
- 运行实操：`13_M1_OPERATIONS_RUNBOOK.md`
