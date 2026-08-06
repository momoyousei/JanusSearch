# JanusSearch 能力化架构重构进度

> 本文件是本次重构的执行账本。实施顺序、完成状态、验证证据和剩余风险均以此为准。

## 目标与边界

- 将 M1～M4 从系统架构抽象降级为兼容入口，以 `corpus`、`catalog`、`projections`、`evaluate`、`search`、`doctor` 能力替代。
- 建立 `janussearch/` 正式包，分离领域模型、应用编排、采集器和基础设施。
- 将 `.agent/skills/` 中两个职责混杂的 Skill 替换为四个职责单一的 Skill。
- 保持现有规范化 JSON、SQLite 和 Chroma 数据兼容，不重写现有数据。
- 保持 `tools.m1_pipeline`～`tools.m4_validate` 可用，并给出迁移提示。
- SQLite 使用原子发布；Chroma 与缓存保持可恢复的原位增量更新。
- 官方数量、track、presentation 对齐默认降级为警告；显式严格模式仍作为硬门禁。
- 查询默认离线优先；混合检索、在线评估和 PDF 下载必须显式触发或满足既定条件。
- 保留 Skill 内静默加载 `.codex/.env` 的现有约定，但不得打印任何凭据。
- 不新增生产依赖，不提交、不推送。

## 基线证据

| 项目 | 基线 |
|---|---|
| Git HEAD | `3451d28 feat：debug 阶段一` |
| 工作区 | 开始实施时干净 |
| 单元测试 | `./.venv/bin/python -m unittest discover -s tests -q`：61 项通过 |
| 规范化语料 | 17 个 venue、85 个文件、128403 篇论文 |
| 当前 M1 | 5 个 NeurIPS 官方对齐失败；其余质量门禁需保持 |
| 当前 M2/M3 | 现有报告通过 |
| 当前 M4 | 报告曾通过，但生成于 2026-05-06，必须按新鲜度重新判断 |

## 进度

| 阶段 | 状态 | 验收证据 |
|---|---|---|
| 0. 基线审计与决策冻结 | 已完成 | 代码、测试、配置、两份旧 Skill 和报告均已检查 |
| 1. 执行账本 | 已完成 | 本文件及 `refactor-processed.md` 已同步 |
| 2. 正式包与运行清单 | 已完成 | `janussearch/` 四层包、退出码、指纹和运行清单测试通过 |
| 3. 新能力 CLI 与兼容入口 | 已完成 | 五个新能力 CLI 及 M1～M4 兼容入口的帮助命令通过 |
| 4. 采集器注册表与门禁语义 | 已完成 | 17 个 venue 注册；默认警告/显式严格失败与发布安全测试通过 |
| 5. 四个新 Skills | 已完成 | 四次 `quick_validate.py` 与三组隔离前向复核均通过 |
| 6. 文档与 SOP | 已完成 | README、AGENTS 与核心 docs 已改为能力优先流程，并与 CLI 交叉检查 |
| 7. 全量回归与审计 | 已完成 | 78/78 测试、compileall、diff、大文件和敏感字面量审计完成 |

## TODO

- [x] 建立 `janussearch/domain`、`application`、`collectors`、`infrastructure`。
- [x] 实现统一退出码、运行状态、配置指纹和 `artifacts/runs/<run_id>/manifest.json`。
- [x] 实现 `tools.corpus`：`inspect/plan/collect/prepare/validate/publish/add`。
- [x] 实现 `tools.catalog`：`build/validate/reindex-fts/stats`。
- [x] 实现 `tools.projections`：`build-vectors/build-topics/build-cache/validate/run`。
- [x] 实现 `tools.evaluate`：离线默认、在线显式、指纹感知的 `status`。
- [x] 实现 `tools.doctor --profile query|corpus|ops`。
- [x] 旧 M1～M4 入口保留并输出迁移提示。
- [x] 采集器生产代码移出 Skill，旧脚本路径仅保留兼容 shim。
- [x] 建立 venue collector 注册表和能力检查。
- [x] M1 官方对齐默认警告，`--strict-official-alignment` 恢复硬失败。
- [x] 新建 `janussearch`、`janus-query`、`janus-corpus`、`janus-ops` 四个 Skill。
- [x] 删除两个旧 Skill 的可发现元数据，避免重复路由。
- [x] 更新 README、AGENTS 和核心 docs，移除 M1～M4 作为主架构的表述。
- [x] 增加领域、工作流、注册表、清单、指纹、CLI 兼容和故障安全测试。
- [x] 验证 staging 失败不改变 canonical，SQLite 构建失败保留旧库。
- [x] 验证 Chroma 损坏和评估报告过期可被 doctor/status 检出。
- [x] 运行四个 Skill 快速校验和隔离前向路由测试。
- [x] 运行全量回归并审计最终 diff、敏感信息和大文件。

## 最终验证

| 检查 | 结果 |
|---|---|
| 全量单元测试 | `./.venv/bin/python -m unittest discover -s tests -q`：78 项通过 |
| Python 编译检查 | `./.venv/bin/python -m compileall -q ...`：通过 |
| Skill 结构校验 | 四个 Skill 的 `quick_validate.py` 均通过 |
| Skill 前向复核 | query、corpus、ops/router 三组隔离复核均为 PASS |
| 查询运行面 doctor | 4 项通过、0 错误、0 警告；SQLite/FTS/128403 papers/124683 vectors 均健康 |
| 真实语料门禁 | 85 文件、128403 条记录通过默认语料门禁；5 个 NeurIPS 官方对齐差异保留为警告 |
| 真实 catalog | 128403 篇论文、85 个来源文件、FTS 128403 行，`all_pass=true` |
| 真实 projections | 六项门禁全部通过；向量与 topic assignment 均为 124683 |
| 离线评估 | 固定查询套件通过，紧随其后的 `status` 判定 `stale=false` |
| Git 100 MB 限制 | 所有可达历史 blob 与当前非忽略候选文件均无超过 100,000,000 字节者 |
| 本地大型运行数据 | 4 个超过 100 MB 的数据库/向量文件均被 `.gitignore` 排除，不会进入 Git |
| 敏感字面量 | 未发现真实 key 形态；仅命中 3 个测试用 `test-key` |
| 最终差异 | `git diff --check` 通过；通用采集器除新增规范编码头外的内容与迁移前 SHA-256 完全一致 |

## 剩余风险

- 未调用网络采集器，也未使用真实 embedding/LLM 凭据执行在线评估；这些仍需在有网络和凭据的环境中显式运行。
- Chroma 与缓存继续采用可恢复的原位增量更新，不具备 SQLite catalog 的整库原子替换语义；doctor/validate 会暴露损坏或不一致。
- M1～M4 兼容入口当前仍调用既有实现，它们已退出主架构但尚未删除；后续版本可在迁移期结束后移除。

## 变更日志

- 2026-08-07：记录实施基线、冻结架构决策并开始阶段 1。
- 2026-08-07：完成能力包、五个能力 CLI、四个职责单一 Skill、兼容层、文档与故障安全测试。
- 2026-08-07：根据隔离前向复核补齐复杂查询成对约束、hybrid smoke 与发布后 catalog 失败语义。
- 2026-08-07：78 项全量测试与最终审计通过，本次授权范围内 TODO 全部收口。
