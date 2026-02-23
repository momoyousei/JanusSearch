# PROJECT.md — 项目总览

## 项目目标
构建本地 AI 顶会论文归档与智能检索系统，覆盖 16 个目标会议，支持：
- 结构化采集与标准化
- 官方统计口径对齐（论文数 / track / presentation）
- SQLite + 多层缓存 + 混合检索
- Agent 端到端可复现调用

## 当前状态（M1~M4 基线已打通）
- M1：规范化与质量门禁流程可复现（含 ICML-2021 / CVPR 增量修复经验）。
- M2：SQLite + FTS 入库与一致性校验可复现。
- M3：向量、主题缓存、混合检索可复现。
- M4：云端硬门禁验收可复现。
- 基线数据：21 个会议年份文件（ICLR/ICML/NeurIPS/CVPR，2021-2026/2025 组合）。
- 当前阶段：会议与论文扩充（从已打通基线扩到更广会议/年份覆盖）。

## 阶段划分
1. M1：数据采集、规范化、回填、质量门禁  
   详情：`docs/10_M1_METHOD.md`
2. M2：数据库入库与查询面（SQL/FTS）  
   规划：`docs/01_SCOPE_AND_MILESTONES.md`，M2-A 入库说明：`docs/20_M2_DATABASE.md`，M2-B 检索说明：`docs/21_M2_SEARCH_CLI.md`
3. M3：缓存层与向量/混合检索  
   规划：`docs/01_SCOPE_AND_MILESTONES.md`，执行手册：`docs/22_M3_CACHE_AND_HYBRID.md`
4. M4：Agent 端到端验证与回归  
   规划：`docs/01_SCOPE_AND_MILESTONES.md`，执行手册：`docs/30_M4_AGENT_VALIDATION.md`

## 文档导航
- 文档总入口：`docs/README.md`
- 范围与里程碑：`docs/01_SCOPE_AND_MILESTONES.md`
- M1 数据规范：`docs/11_M1_DATA_STANDARD.md`
- M1 质量门禁：`docs/12_M1_QUALITY_GATES.md`
- M1 运行手册：`docs/13_M1_OPERATIONS_RUNBOOK.md`
- M2 入库手册：`docs/20_M2_DATABASE.md`
- M2 检索手册：`docs/21_M2_SEARCH_CLI.md`
- M3 缓存与混合检索手册：`docs/22_M3_CACHE_AND_HYBRID.md`
- M4 Agent 验收手册：`docs/30_M4_AGENT_VALIDATION.md`
- 扩充阶段手册：`docs/40_EXPANSION_PLAYBOOK.md`

## 设计原则
- 先建立稳定数据基线，再扩展功能
- 以可验证指标驱动迭代（质量门禁 > 主观完成度）
- 使用“主文档简洁 + docs 细节下沉”降低维护成本
