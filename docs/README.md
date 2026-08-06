# JanusSearch 文档入口

## AI 首读顺序

1. `docs/README.md`
2. `docs/20_PIPELINE_AND_GATES.md`
3. `docs/30_EXPANSION_POLICY.md`
4. 涉及架构时读 `docs/10_CORE_ARCHITECTURE.md`
5. 涉及历史决策时读 `docs/90_HISTORY.md`

## 文档职责

| 文档 | 职责 |
|---|---|
| `README.md` | 面向人类的安装、快速使用和常用入口 |
| `AGENTS.md` | 面向 AI 的执行边界、路由和验收 SOP |
| `docs/10_CORE_ARCHITECTURE.md` | 能力边界、代码分层、数据事实源和一致性模型 |
| `docs/20_PIPELINE_AND_GATES.md` | corpus/catalog/projections/evaluate/search/doctor 命令与门禁 |
| `docs/30_EXPANSION_POLICY.md` | 会议/年份批次扩充、冻结与恢复策略 |
| `docs/90_HISTORY.md` | 已冻结的历史事实和旧 M1～M4 决策背景 |

固定离线查询集位于 `docs/fixtures/m4_fixed_queries.yaml`；文件名为兼容历史保留。
