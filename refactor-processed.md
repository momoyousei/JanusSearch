# JanusSearch 审查问题修复进度

更新时间：2026-08-06

## 范围约束

- [x] 按用户要求排除“工作区明文保存真实 API 凭据”；不读取、不修改、不删除 `.codex/.env`。
- [x] 修改仅覆盖上一轮审查报告中的其余问题，并补充针对性测试与门禁。
- [x] 修改过程中保留无关用户改动；不提交、不推送、不创建 PR。

## TODO

- [x] M2：把全量重建改为临时数据库构建、校验后原子替换，失败时保留旧运行库。
- [x] M4：抽样查询必须验证返回论文属于目标 topic/subtopic，而不只是检查非空和字段结构。
- [x] CVPR/ICCV/ECCV：修复 OpenAccess 路径返回未定义 `provider` 的运行时错误。
- [x] Chroma：修复当前 `embedding_fulltext_search` FTS5 索引，并把 SQLite 完整性检查加入 M3 validate。
- [x] M1：只有目标缺失字段实际修复后才标记 `repaired`、计入 `updated_records`。
- [x] ACL：HTTP 4xx/5xx 必须触发 curl 失败与重试，禁止写出伪成功空结果。
- [x] AAAI：OpenReview 相对 PDF 路径必须使用 OpenReview 域名。
- [x] canonical provenance：将 `field_provenance` 纳入 M2 必填/验证，并迁移现有缺失记录。
- [x] 测试：为上述失败路径补充回归测试。
- [x] 验证：运行针对性测试、全量测试、数据契约检查、SQLite 完整性检查和最终 diff 审查。

## 进度记录

| 时间 | 状态 | 事项 | 证据/备注 |
|---|---|---|---|
| 2026-08-06 | 已完成 | 建立修复台账 | 初始 TODO 来自上一轮全库审查；Git 工作区基线干净。 |
| 2026-08-06 | 已完成 | 修复三个会议采集器缺陷 | OpenAccess 返回 `venue_provider`；ACL curl 增加 `--fail-with-body`；AAAI OpenReview PDF 改用 OpenReview 基址。`tests.test_collectors` 3 项通过。 |
| 2026-08-06 | 已完成 | 修复 M1 回填状态语义 | 元数据变化但摘要仍为空时保留原状态，并计入 `failed_records`；增加 provenance 原子迁移入口。`tests.test_m1_pipeline` 11 项通过。 |
| 2026-08-06 | 已完成 | M2 原子重建与 provenance 门禁 | 使用同目录临时数据库完整构建后原子替换；失败回归验证旧库及哨兵数据不变。输入记录和数据库 JSON 均验证 provenance 契约；M2 及关联模块 42 项测试通过。 |
| 2026-08-06 | 已完成 | M4 抽样相关性判定 | 从 `topic_assignments.json` 构建 topic/subtopic 论文集合；抽样结果至少命中一篇目标集合论文才通过。结构正确但无关、真实成员命中两条路径均有回归测试；M4 10 项通过。 |
| 2026-08-06 | 已完成 | Chroma 完整性门禁与 FTS5 修复 | M3 validate 新增只读 `PRAGMA quick_check` 门禁；健康索引单测通过。确认无相关写进程后重建 `embedding_fulltext_search`（124,683 行，18.597 秒）；修复后 `quick_check` 与 `integrity_check` 均为 `ok`。 |
| 2026-08-06 | 已完成 | canonical provenance 迁移 | 85 文件、128,403 条记录完成检查；81 文件共 113,245 条记录补齐 provenance。迁移前后论文 ID 数量与 SHA-256 一致，逐文件对比确认非 provenance 字段 0 处变化，provenance 契约及字段顺序错误均为 0。 |
| 2026-08-06 | 已完成 | 最终回归与运行面验证 | 全量单元测试 61/61 通过；M2 最终版本全量重建成功且 `all_pass=true`；M3 validate 全部 6 项通过（含 Chroma 完整性）；FTS 检索冒烟返回 220 条。`git diff --check` 与 16 个变更 Python 文件 AST 解析通过。 |
| 2026-08-06 | 已记录 | 范围外既有门禁状态 | M1 validate 对 85 文件完成扫描，但 NeurIPS 2021–2025 共 5 个文件的既有官方数量对齐失败，且沙箱阻止在线刷新；本次迁移已证明所有非 provenance 字段不变。M4 仅检查既有状态报告（2026-05-06 PASS），未使用凭据重跑在线套件。 |

## 剩余风险

- canonical provenance 产生 81 个数据文件、759,672 行新增差异；自动逐字段核对已确认非 provenance 值不变，但人工审阅成本仍较高。
- M1 当前仍有 NeurIPS 2021–2025 五个既有数量对齐失败；它不属于上一轮审查列出的修复项，本次未扩大范围去重采集数据。
- M4 新相关性语义已有离线正反回归测试，但未调用外部 embedding 服务重跑在线套件；当前 `status` 指向 2026-05-06 的旧 PASS 报告。
