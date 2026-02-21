# PROJECT.md — 项目总览

## 项目目标
构建本地 AI 顶会论文归档与智能检索系统，覆盖 16 个目标会议，支持：
- 结构化采集与标准化
- 官方统计口径对齐（论文数 / track / presentation）
- SQLite + 多层缓存 + 混合检索
- Agent 端到端可复现调用

## 当前状态（M1 初步冻结）
- 冻结日期：2026-02-19
- 基线数据：16 个会议年份文件（以现有文件集为准）
- 冻结结论与指标：见 `docs/14_M1_FREEZE_2026-02-19.md`
- 详细质量报告：`index/m1_quality_report.json`

## 阶段划分
1. M1：数据采集、规范化、回填、质量门禁  
   详情：`docs/10_M1_METHOD.md`
2. M2：数据库入库与查询面（SQL/FTS）  
   规划：`docs/01_SCOPE_AND_MILESTONES.md`，M2-A 入库说明：`docs/20_M2_DATABASE.md`，M2-B 检索说明：`docs/21_M2_SEARCH_CLI.md`
3. M3：缓存层与向量/混合检索  
   规划：`docs/01_SCOPE_AND_MILESTONES.md`，执行手册：`docs/22_M3_CACHE_AND_HYBRID.md`
4. M4：Agent 端到端验证与回归  
   规划：`docs/01_SCOPE_AND_MILESTONES.md`

## 文档导航
- 文档总入口：`docs/README.md`
- 范围与里程碑：`docs/01_SCOPE_AND_MILESTONES.md`
- M1 数据规范：`docs/11_M1_DATA_STANDARD.md`
- M1 质量门禁：`docs/12_M1_QUALITY_GATES.md`
- M1 运行手册：`docs/13_M1_OPERATIONS_RUNBOOK.md`
- M2 入库手册：`docs/20_M2_DATABASE.md`
- M2 检索手册：`docs/21_M2_SEARCH_CLI.md`
- M3 缓存与混合检索手册：`docs/22_M3_CACHE_AND_HYBRID.md`

## 设计原则
- 先建立稳定数据基线，再扩展功能
- 以可验证指标驱动迭代（质量门禁 > 主观完成度）
- 使用“主文档简洁 + docs 细节下沉”降低维护成本
