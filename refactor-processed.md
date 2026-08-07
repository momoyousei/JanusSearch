# JanusSearch 未解决问题逐项修复台账

更新时间：2026-08-07

## 本轮边界

- 只修复代码、契约、架构和验证缺陷，不完善论文数据库。
- 不修改 `data/raw`、`data/papers.db`、生产 Chroma、topic、cache 或正式评估报告。
- `.codex/.env` 只规范变量名并保留现有密钥；禁止在日志、报告或台账中记录密钥。
- 保留 `problem-finding.md` 历史证据；只有新的终态失败才追加记录。
- 不提交、不推送、不创建 PR。

## 生产数据冻结基线

| 对象 | SHA-256 聚合值 |
|---|---|
| `data/raw/**/*.json` | `8324fa1f42e86e0e8f6ae1cb99fe257aac5827d3bfb4fa0d0e93f92c1dcabce9` |
| `data/papers.db` | `f15465eafa75d3e28437d6ff51b6e119ba635f6868adc90c05dfedc3ffcfc339` |
| `data/vectors/chroma` | `eec71c30baa282fd3b2eee0a2e9379819271e79233dcf4e4639c51da50a2276d` |
| `artifacts/m3` | `5cb266d48c3514f5ef74fe24b65f0ff3fec31d5c7b19270ec19d889a5f6e0168` |
| cache roots | `637feda011757b47a538f0ebc73a929e08ce30b57bbc7cacb3ee701d9e2e6eed` |

## 顺序 TODO

| ID | 严重级别 | 状态 | 问题 | 验收条件 |
|---|---:|---|---|---|
| R-000 | P1 | DONE | 台账与 LLM/Embed 配置不统一 | 两份台账按约定重置；六个 `JANUS_*` 配置生效且不泄露密钥 |
| R-001 | P1 | DONE | publish 未覆盖额外 staging 文件 | reconciliation v2 精确覆盖 staging 文件集合并拒绝非法路径 |
| R-002 | P1 | DONE | reconciliation 未复核完整契约和指纹 | publish 复核 policy、collection sidecar、源文件、canonical 和输出哈希 |
| R-003 | P1 | DONE | CVPR fuzzy 改题可被数量门禁自动放行 | 仅接受逐条版本化映射；拒绝未知、重复或歧义映射 |
| R-004 | P2 | DONE | 作者集合索引被拆成单个作者 | 完整作者签名作为一个键参与唯一匹配 |
| R-005 | P1 | DONE | ICML 固定快照 fallback 范围过宽 | 仅批准的第二页分页不完整可 fallback |
| R-006 | P2 | DONE | virtual collector 可能生成裸站点 URL | 空路径不生成 URL，真实 paper URL 不被覆盖 |
| R-007 | P1 | DONE | collection sidecar 非通用、非原子且作用域弱 | v2 sidecar 覆盖全部注册 collector，校验 venue/years/files/hashes |
| R-008 | P2 | DONE | 缺失 presentation 被归类为 poster | 会议缺失为 unknown，期刊为 not_applicable；不迁移生产数据 |
| R-009 | P1 | DONE | query doctor 对不完整向量库误报 PASS | 精确核对非 placeholder ID 与必要元数据，当前生产库应正确 FAIL |
| R-010 | P2 | DONE | PVLDB 生产逻辑位于 CLI 层 | 实现迁入 `janussearch.collectors`，旧命令只保留适配层 |
| R-011 | P1 | DONE | M1～M4 能力层仍反向依赖 `tools` | `janussearch/**` 不导入 `tools/**`，旧入口保持兼容 |
| R-012 | P2 | DONE | 文档 SOP 缺少 reconcile 和新门禁 | AGENTS 与核心文档流程和实际 CLI 一致 |
| R-013 | P1 | BLOCKED | 全局与真实服务验证 | 全量测试、隔离 Embed/LLM smoke、哈希与安全审计完成；生产哈希全部保持基线 |
| R-014 | P1 | DONE | doctor 只读诊断触发 Chroma SQLite 字节写入 | doctor 仅以 SQLite `mode=ro&immutable=1` 读取，重复诊断不改变文件哈希或 mtime |

## 非代码缺陷与明确延期

- ACMMM、AISTATS、ICDE、IJCAI 2026 尚未发布属于外部来源状态，不作为代码修复项。
- 生产向量、topic、cache 和评估报告保持 stale；本轮只要求 doctor 正确暴露，不执行数据完善。

## 进度证据

| 时间 | ID | 状态 | 证据 |
|---|---|---|---|
| 2026-08-07 | R-000 | IN_PROGRESS | 已冻结生产数据哈希；实施前 92/92 单元测试通过。 |
| 2026-08-07 | R-000 | DONE | `architecture-refactor-progress.md` 为 0 字节；新台账已建立；`.codex/.env` 使用六个规范变量；配置解析测试 4/4 通过。 |
| 2026-08-07 | R-001 | IN_PROGRESS | 开始升级 reconciliation 文件集合和路径校验。 |
| 2026-08-07 | R-001 | DONE | reconciliation report 升级为 v2；额外文件和路径穿越回归 2/2 通过。 |
| 2026-08-07 | R-002 | IN_PROGRESS | 开始记录并复核 policy、采集结果和源输入指纹。 |
| 2026-08-07 | R-002 | DONE | prepare metadata v2 记录源文件/sidecar 哈希；publish 复核 metadata、policy、source、canonical 和 output；源漂移回归通过。 |
| 2026-08-07 | R-003 | IN_PROGRESS | 开始移除 fuzzy 自动批准并升级显式映射 policy。 |
| 2026-08-07 | R-003 | DONE | policy v2 固化 242 条唯一 CVPR 映射和来源哈希；未批准改题阻断、显式映射继承 ID 回归通过。 |
| 2026-08-07 | R-004 | IN_PROGRESS | 开始修复 tuple 作者签名被展开的问题。 |
| 2026-08-07 | R-004 | DONE | tuple 作为原子键，set/list 作为多键；多作者和单作者签名回归通过。 |
| 2026-08-07 | R-005 | IN_PROGRESS | 开始区分 ICML 首屏失败、分页失败和解析失败。 |
| 2026-08-07 | R-005 | DONE | 新增批准分页异常；第二页 403 可 fallback，首屏 403 不调用固定快照；3/3 回归通过。 |
| 2026-08-07 | R-006 | IN_PROGRESS | 开始修复空 virtual 路径的 URL 合成。 |
| 2026-08-07 | R-006 | DONE | 空 virtual 路径不再生成站点根 URL，paper/OpenReview URL 优先；回归通过。 |
| 2026-08-07 | R-007 | DONE | sidecar v2 使用 venue/years/files/hashes，原子替换并校验范围；注册采集编排统一补齐契约，移除 legacy reason；相关 44 项测试通过。 |
| 2026-08-07 | R-008 | IN_PROGRESS | 开始扩展 presentation 语义且保持生产数据冻结。 |
| 2026-08-07 | R-008 | DONE | presentation 扩展为 unknown/not_applicable；ICLR、TPAMI、VLDB fixture 回归通过，生产 canonical 未迁移。 |
| 2026-08-07 | R-009 | IN_PROGRESS | 开始让 query doctor 核对候选 ID 集合和向量元数据。 |
| 2026-08-07 | R-009 | DONE | 等量错误 ID fixture 被拦截；生产 query doctor 正确 FAIL：缺失 6,597、多余 17、旧元数据 118,116。未重建向量。 |
| 2026-08-07 | R-010 | IN_PROGRESS | 开始迁移 PVLDB 实现并保留旧模块兼容。 |
| 2026-08-07 | R-010 | DONE | PVLDB 与 DBLP 依赖迁入正式 collector 包；旧模块仅兼容别名；parser/registry 回归通过。 |
| 2026-08-07 | R-011 | DONE | M1～M4、search 和 venue collectors 已迁入正式分层；生产包对 `tools` 导入为 0；五个旧帮助命令 exit 0；106/106 测试通过。 |
| 2026-08-07 | R-012 | IN_PROGRESS | 开始同步 AGENTS 与核心 SOP。 |
| 2026-08-07 | R-012 | DONE | AGENTS、pipeline、expansion、architecture 已同步 reconcile v2、sidecar v2、向量门禁和六项服务变量。 |
| 2026-08-07 | R-013 | IN_PROGRESS | 开始全量、隔离真实 API、哈希与安全验收。 |
| 2026-08-07 | R-014 | IN_PROGRESS | 哈希回验发现生产 `chroma.sqlite3` 在旧 doctor 诊断后 mtime 与 SHA-256 改变；HNSW 文件未变化。 |
| 2026-08-07 | R-014 | DONE | doctor 已移除 `PersistentClient` 只读路径，改用 SQLite `mode=ro&immutable=1`；3/3 定向测试通过，生产 query doctor 前后 SQLite SHA-256 均为 `f8a4a5d28154a0e0a75bb85bd330400be82583561a12afeee6fa1cf8a57324a8` 且 mtime 未变。既有字节漂移不可无基线副本安全回滚，记录于 PF-014。 |
| 2026-08-07 | R-013 | BLOCKED | 107/107 单元测试与 compileall 通过；corpus doctor PASS；query/ops doctor 按预期因既有向量缺口和旧元数据 FAIL；隔离真实服务 smoke 成功（Embed 3/3、4096 维；LLM topic JSON 含 name/description）；`git diff --check`、真实密钥字面量、Git 100MB 可达对象/候选检查均通过。raw、SQLite、artifacts/m3、cache 哈希保持基线，但 Chroma 聚合哈希为 `55ef270f6081b06b3f7930002c83a875ef585142f05dd9c8202a490e22e4a524`，不等于冻结基线，故不得标记 DONE。 |
