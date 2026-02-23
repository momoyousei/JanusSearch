# Scope 与里程碑

## 总体目标
基于本地文件和可复现 CLI 流程，构建从采集到检索的完整 AI 论文数据系统：
- 数据可追溯（原始文件、备份、报告）
- 质量可量化（覆盖率、去重、官方口径对齐）
- 运行可复现（固定命令、固定输出路径）

## 范围定义
- 目标会议（16）：CVPR, ICCV, ECCV, NeurIPS, ICML, ICLR, AAAI, IJCAI, ACL, EMNLP, NAACL, KDD, WWW, ACM MM, CoRL, WACV
- 当前基线：先在已有 21 个会议年份文件（ICLR/ICML/NeurIPS/CVPR）上跑通完整链路
- 数据源策略：
  - 首选：OpenReview / OpenAlex / DBLP（按会议可用性）
  - 补全：Semantic Scholar + arXiv
  - 对齐：官方统计入口（可用时）

## 里程碑
1. M1 数据采集与规范化  
   目标：稳定产出标准化文件，满足质量门禁  
   细节：`10_M1_METHOD.md`, `11_M1_DATA_STANDARD.md`, `12_M1_QUALITY_GATES.md`
2. M2 数据入库  
   目标：将规范化数据写入 SQLite，并提供 SQL/FTS 检索入口
3. M3 缓存与检索增强  
   目标：主题缓存、子主题缓存、向量检索与混合检索
4. M4 Agent 验证  
   目标：以固定查询集做端到端回归验证

## 当前所处阶段
- 状态：M1→M4 主链路已打通并完成基础用例验证
- 基线：21 个会议年份文件（ICLR/ICML/NeurIPS/CVPR）
- 当前重点：进入会议/论文扩充阶段，按批次扩展到更多会议与年份
- 执行入口：`40_EXPANSION_PLAYBOOK.md`
