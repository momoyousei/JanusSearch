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

## 文档关系
- `AGENTS.md`：全局约束与执行入口（链接到本目录）
- `PROJECT.md`：项目目标与阶段状态（链接到本目录）
- `index/m1_quality_report.json`：当前质量事实来源
- `index/m1_backfill_report.json`：最近回填结果事实来源

## 更新规则
- 规则变化优先更新 `10~13` 系列文档。
- 冻结或阶段性里程碑用新文件沉淀（如 `14_*`）。
- 不在 `AGENTS.md` / `PROJECT.md` 填写实现细节，统一链接到 `docs/`。
