# 2026 全 Venue 增量采集问题记录

开始时间：2026-08-07（Asia/Shanghai）

## 记录边界

- 本文件只增量记录重试耗尽后的终态失败事件，不记录正常 `PASS`、`NO_UPDATE` 或成功发布。
- 分类包括：`CODE_DEFECT`、`SOURCE_UNAVAILABLE`、`HTTP_ERROR`、`RATE_LIMIT`、`AUTH_MISSING`、`EMPTY_SOURCE`、`DATA_GATE`、`ENVIRONMENT`。
- 每个失败项必须包含 venue/year、失败阶段、实际命令、退出码、预期与实际、证据、run manifest、canonical 是否变化和状态。
- 禁止记录 API key、token 或 `.codex/.env` 内容。
- 本轮只记录问题，不修改采集器或业务代码。

## 执行基线

- Canonical：17 个 venue、85 个 JSON、128403 篇论文。
- Catalog：`data/papers.db` SHA-256 `867a6624f08c30e5eb80c90011b1b7ec6fd8e8e8fa040f234150699ae1971216`。
- 单元测试：78/78 通过。
- Corpus doctor：2 项通过、0 错误、0 警告。

## 终态失败事件

### PF-001 — `HTTP_ERROR` — AAAI 2026

- 时间：2026-08-07 08:12（Asia/Shanghai）
- 阶段：`collect`
- 命令：`./.venv/bin/python -m tools.corpus collect --venue AAAI --years 2026 --output-root artifacts/runs/20260807-venue-sweep/aaai-2026/collected`
- 退出码：`1`
- 预期：AAAI OJS 有 2026 Technical Track 时完成采集；否则 OpenReview fallback 返回可解析 JSON。
- 实际：OJS 未解析到 2026 issue，OpenReview fallback 终止；API 独立探测返回 HTTP 403 `ChallengeRequiredError`，采集器最终报告 invalid JSON。
- 证据：`https://api2.openreview.net/notes?content.venue=AAAI+2026&limit=1&offset=0` 返回 `application/json`、HTTP 403、`Challenge verification required`。
- Run manifest：`artifacts/runs/20260807T001255Z-43c5e9b1/manifest.json`
- Canonical：未变化；`data/raw/aaai/2026.json` SHA-256 仍为 `89c273653aebafb5901c25c144d5bac8b95ebbdd2112369f5354c9f1b14afdc0`。
- 状态：`OPEN`；AAAI 2026 批次冻结，继续下一个 venue。

#### PF-001 解决记录（2026-08-07 12:30 Asia/Shanghai）

- 当前状态：`RESOLVED` / `UPDATED`。
- 根因修复：统一 HTTP 层识别缺失 `Content-Encoding` 的 gzip magic；AAAI 路由禁用 OpenReview 静默 fallback，空 issue 不再写空 JSON。
- 验证：OJS archive 实际读取到 43 个 Technical Track issue，4,149/4,149 篇详情成功，0 失败；对账为新增 0、删除 0、稳定 ID 继承 4,149。
- 实质变化：1 篇官方摘要扩展，1 篇标题拼写由 `Muti-hop` 更正为 `Multi-hop`；其余差异仅为运行时间字段。
- 门禁与发布：重复标题 0，resolved 作者/摘要覆盖率均为 100%，`all_pass=true`；发布 manifest `artifacts/runs/20260807T043053Z-b10be3c4/manifest.json`。
- Canonical：`data/raw/aaai/2026.json` 已通过 reconciliation 哈希复核后原子替换，论文数保持 4,149。

### PF-002 — `SOURCE_UNAVAILABLE` — ACMMM 2026

- 时间：2026-08-07 08:15（Asia/Shanghai）
- 阶段：`collect`
- 命令：`./.venv/bin/python -m tools.corpus collect --venue ACMMM --years 2026 --output-root artifacts/runs/20260807-venue-sweep/acmmm-2026/collected`
- 退出码：`1`
- 预期：DBLP 至少一个 ACMMM 2026 XML 候选可用，或采集器明确报告尚未发布。
- 实际：Trier 与 dblp.org 两个 `mm2026.xml` 候选在 6 次尝试后均不可用，最终错误为 HTTP 404。
- 证据：失败 URL 为 `https://dblp.uni-trier.de/db/conf/mm/mm2026.xml`、`https://dblp.org/db/conf/mm/mm2026.xml`。
- Run manifest：`artifacts/runs/20260807T001458Z-cd26a4bf/manifest.json`
- Canonical：未变化；`data/raw/acmmm/2026.json` 仍不存在。
- 状态：`OPEN`；判定为来源尚未发布，批次冻结并继续。

### PF-003 — `SOURCE_UNAVAILABLE` — AISTATS 2026

- 时间：2026-08-07 08:16（Asia/Shanghai）
- 阶段：`collect`
- 命令：`./.venv/bin/python -m tools.corpus collect --venue AISTATS --years 2026 --output-root artifacts/runs/20260807-venue-sweep/aistats-2026/collected`
- 退出码：`1`
- 预期：采集器从 PMLR series 索引解析出 AISTATS 2026 volume。
- 实际：PMLR volume 解析没有找到 2026，对应年份被列入 `missing_years=[2026]` 并终止。
- 证据：采集器终态错误 `Failed to resolve AISTATS PMLR volumes for years: [2026]`。
- Run manifest：`artifacts/runs/20260807T001605Z-f8a0dc51/manifest.json`
- Canonical：未变化；`data/raw/aistats/2026.json` 仍不存在。
- 状态：`OPEN`；判定为 PMLR 来源尚未发布或尚未被索引，批次冻结并继续。

### PF-004 — `DATA_GATE` — CVPR 2026

- 时间：2026-08-07 08:23（Asia/Shanghai）
- 阶段：`pre-publish delta guard`
- 命令：`./.venv/bin/python -m tools.corpus collect --venue CVPR --years 2026 --output-root artifacts/runs/20260807-venue-sweep/cvpr-2026/collected`
- 退出码：采集、prepare、validate 均为 `0`；发布因本轮额外删除保护未执行。
- 预期：刷新现有 canonical 时不得产生无法解释的论文删除。
- 实际：旧 virtual canonical 为 4070 篇，最新 CVF OpenAccess snapshot 为 4068 篇；正式出版源带来大量标题修订，规范化标题集合仍为旧侧独有 241、新侧独有 239，净少 2 篇，不能自动证明安全替换。
- 质量门禁：4068 篇全部 resolved，作者/摘要覆盖率 100%，重复标题 0，`all_pass=true`。
- Run manifests：collect `artifacts/runs/20260807T001642Z-58f607e4/manifest.json`；validate `artifacts/runs/20260807T002310Z-bf8cb068/manifest.json`。
- Canonical：未变化；`data/raw/cvpr/2026.json` SHA-256 仍为 `f5b5971bd93ef4f0b6fb816d9a00429d2bd1c7b471c1b9d1a2ad54ab218f3e4c`。
- 状态：`OPEN`；需另行建立 virtual→OpenAccess 标题/来源 ID 映射并解释净少 2 篇后再发布。

#### PF-004 解决记录（2026-08-07 12:50 Asia/Shanghai）

- 当前状态：`RESOLVED` / `UPDATED`。
- 根因修复：新增逐记录 reconciliation；发布前复核 source/canonical/staging SHA-256，未知删除硬失败。CVPR 路由锁定最终 CVF OpenAccess，不再自动回退 virtual。
- 采集：CVF OpenAccess 4,068 篇，4,068 个详情摘要全部成功、0 失败。
- 对账：旧 4,070 → 新 4,068；直接稳定匹配 3,825，改题映射 242（规范化标题 9、作者/标题模糊匹配 233），新增 1，批准删除 3，未知删除 0。
- 新增：`Align Once to Explain: Feature Alignment for Scalable B-cosification of Foundational Vision Transformers`。
- 门禁与发布：重复标题 0，作者/摘要覆盖率均为 100%，`all_pass=true`；发布 manifest `artifacts/runs/20260807T045011Z-9e1abc70/manifest.json`。
- Canonical：`data/raw/cvpr/2026.json` 已安全替换为 4,068 篇。

### PF-005 — `EMPTY_SOURCE` — ECCV 2026

- 时间：2026-08-07 08:24（Asia/Shanghai）
- 阶段：`collect`
- 命令：`./.venv/bin/python -m tools.corpus collect --venue ECCV --years 2026 --output-root artifacts/runs/20260807-venue-sweep/eccv-2026/collected`
- 退出码：`0`
- 预期：ECVA 已发布时获得非空论文集；未发布时应形成可区分的无来源终态。
- 实际：`https://www.ecva.net/papers.php` 被成功读取，但 ECCV 2026 解析结果为 0，采集器仍写出空 JSON 并以成功退出。
- 证据：日志显示 `Fetching abstracts for ECCV 2026 (0 papers)`、`Total collected papers: 0`。
- Run manifest：`artifacts/runs/20260807T002425Z-bd48e024/manifest.json`
- Canonical：未变化；`data/raw/eccv/2026.json` 仍不存在。
- 状态：`OPEN`；空 snapshot 不进入 prepare/publish，后续需评估空结果是否应返回非零或 warning 状态。

#### PF-005 解决记录（2026-08-07 12:43 Asia/Shanghai）

- 当前状态：`RESOLVED` / `NO_UPDATE`。
- 根因修复：ECCV 2026 不再读取只含旧年份的 ECVA 总页，改用官方 virtual events/abstracts；真实空源使用显式 `no_update` 结果。
- 结果：官方声明数 0、实际结果 0、分页 1；仅生成 `.janus-collection.json`，未生成 `ECCV-26.json`。
- Run manifest：`artifacts/runs/20260807T044301Z-b23fbf9f/manifest.json`。
- Canonical：`data/raw/eccv/2026.json` 仍不存在，未进入 prepare/reconcile/publish。

### PF-006 — `SOURCE_UNAVAILABLE` — ICDE 2026

- 时间：2026-08-07 08:32（Asia/Shanghai）
- 阶段：`collect`
- 命令：`./.venv/bin/python -m tools.corpus collect --venue ICDE --years 2026 --output-root artifacts/runs/20260807-venue-sweep/icde-2026/collected`
- 退出码：`1`
- 预期：DBLP 至少一个 ICDE 2026 XML 候选可用，或采集器明确报告尚未发布。
- 实际：Trier 与 dblp.org 两个 `icde2026.xml` 候选在 6 次尝试后均不可用，最终错误为 HTTP 404。
- 证据：失败 URL 为 `https://dblp.uni-trier.de/db/conf/icde/icde2026.xml`、`https://dblp.org/db/conf/icde/icde2026.xml`。
- Run manifest：`artifacts/runs/20260807T003202Z-56ad673b/manifest.json`
- Canonical：未变化；`data/raw/icde/2026.json` 仍不存在。
- 状态：`OPEN`；判定为来源尚未发布，批次冻结并继续。

### PF-007 — `EMPTY_SOURCE` — ICLR 2026

- 时间：2026-08-07 08:33（Asia/Shanghai）
- 阶段：`collect`
- 命令：`./.venv/bin/python -m tools.corpus collect --venue ICLR --years 2026 --output-root artifacts/runs/20260807-venue-sweep/iclr-2026/collected`
- 退出码：`1`
- 预期：OpenReview 的 ICLR 2026 venue/invitation 查询返回可发布论文；若无新增，也应能与现有 canonical 安全比较。
- 实际：采集器遍历 venue、submission invitation 与 `content.venue*` 查询后均为 0，终止为 `OpenReview provider selected, but no papers were retrieved`。
- 证据：日志对 `ICLR.cc/2026/Conference` 的所有候选查询均报告 `0 accepted=0`。
- Run manifest：`artifacts/runs/20260807T003302Z-fdfe4c0e/manifest.json`
- Canonical：未变化；`data/raw/iclr/2026.json` SHA-256 仍为 `71ea63d5a99310c4862632e9ad35505327a0f235590b5263997c31469b94a53b`。
- 状态：`OPEN`；空来源批次冻结并继续。

#### PF-007 解决记录（2026-08-07 12:41 Asia/Shanghai）

- 当前状态：`RESOLVED` / `UPDATED`。
- 根因修复：ICLR 改走官方 virtual events + abstracts；只保留 `ICLR.cc/2026/Conference`，按标题合并 poster/oral 展示副本，并保留真实 OpenReview submission ID。
- 来源审计：官方 5,691 个事件中 Conference 5,576 条，合并 223 个重复展示后为 5,353 篇；BlogPosts/TMLR/JMLR 未进入论文集。
- 对账：旧 5,358 → 新 5,353；5,353 个稳定 ID 全部继承，删除仅为已批准的 5 个撤回 OpenReview ID，未知删除 0，新增 0。
- 门禁与发布：重复标题 0，作者/摘要覆盖率均为 100%，poster 5,129、oral 224，`all_pass=true`；发布 manifest `artifacts/runs/20260807T044115Z-25c00d53/manifest.json`。
- Canonical：`data/raw/iclr/2026.json` 已通过 reconciliation 哈希复核后原子替换。

### PF-008 — `DATA_GATE` — ICML 2026

- 时间：2026-08-07 08:34（Asia/Shanghai）
- 阶段：`validate` / `pre-publish delta guard`
- 命令：`./.venv/bin/python -m tools.corpus validate --input-glob 'artifacts/runs/20260807-venue-sweep/icml-2026/staging/icml/*.json'`
- 退出码：`1`
- 预期：刷新 snapshot 通过作者与摘要覆盖门禁，且不得删除现有论文。
- 实际：当前 ICML virtual 文件仅含 196 篇（16 oral、180 poster），而 canonical 有 6567 篇；196 条记录全部为 placeholder，resolved 作者/摘要覆盖率均为 0%，质量门禁失败，且会净删除 6371 篇。
- 证据：官方 track/presentation 对齐为 196/196，但质量报告为 `gate_fail_files=1`、`all_pass=false`；问题为 `resolved_authors_coverage=0.00<90.00`、`resolved_abstract_coverage=0.00<85.00`。
- Run manifests：collect `artifacts/runs/20260807T003357Z-5bd50e7c/manifest.json`；validate `artifacts/runs/20260807T003420Z-cb566dbe/manifest.json`。
- Canonical：未变化；`data/raw/icml/2026.json` SHA-256 仍为 `862c2ddc7ea65eca4869ed18690f1538784854a61dbcc8c03d55d11ca399f84c`。
- 状态：`OPEN`；门禁与删除保护共同冻结该批次，未发布。

#### PF-008 解决记录（2026-08-07 12:54 Asia/Shanghai）

- 当前状态：`RESOLVED` / `UPDATED_PARTIAL`。
- 根因修复：官方 virtual 分页声明 6,796 条但第二页返回 HTTP 403 时，不再把首 200 条当完整源；仅对 ICML 2026 使用用户批准的固定快照。
- 来源身份：提交 `2cf625b555c51e61086a3b009c59d47e768466cf`；SHA-256 `73b6c52566255c85761977cc3f423739ef54deebc1befa7b8b79eb9f5cf3ac1a`；`source_provider=icml_virtual_pinned_snapshot`。
- 子集门禁：过滤后 6,559 个唯一 OpenReview ID，第三方独有 0；Conference 6,346、Position Track 213。
- 对账：旧 6,567 → 新 6,559；6,559 个稳定 ID 全部继承，批准删除 8、未知删除 0、新增 0。
- 质量与发布：重复标题 0，作者/摘要覆盖率均为 100%，`all_pass=true`；发布 manifest `artifacts/runs/20260807T045401Z-9d828031/manifest.json`。
- Canonical：`data/raw/icml/2026.json` 已安全替换；因官方分页仍不可完整访问，终态明确标记 `UPDATED_PARTIAL`。

### PF-009 — `SOURCE_UNAVAILABLE` — IJCAI 2026

- 时间：2026-08-07 08:34（Asia/Shanghai）
- 阶段：`collect`
- 命令：`./.venv/bin/python -m tools.corpus collect --venue IJCAI --years 2026 --output-root artifacts/runs/20260807-venue-sweep/ijcai-2026/collected`
- 退出码：`1`
- 预期：IJCAI 2026 proceedings 页面可用时采集论文；尚未发布时保持 canonical 不变。
- 实际：`https://www.ijcai.org/proceedings/2026/` 连续 4 次返回 HTTP 404，采集终止。
- 证据：终态错误为 `Failed to fetch https://www.ijcai.org/proceedings/2026/ after 4 attempts: HTTP Error 404: Not Found`。
- Run manifest：`artifacts/runs/20260807T003452Z-8fb3f4a0/manifest.json`
- Canonical：未变化；`data/raw/ijcai/2026.json` 仍不存在。
- 状态：`OPEN`；判定为官方 proceedings 尚未发布，批次冻结并继续。

### PF-010 — `EMPTY_SOURCE` — NeurIPS 2026

- 时间：2026-08-07 08:36（Asia/Shanghai）
- 阶段：`collect`
- 命令：`./.venv/bin/python -m tools.corpus collect --venue NEURIPS --years 2026 --output-root artifacts/runs/20260807-venue-sweep/neurips-2026/collected`
- 退出码：`1`
- 预期：OpenReview 的 NeurIPS 2026 venue/invitation 查询返回已公开的可采集论文；未公开时保持 canonical 不变。
- 实际：采集器遍历 venue、submission invitation 与 `content.venue*` 查询后均为 0，终止为 `OpenReview provider selected, but no papers were retrieved`。
- 证据：日志对 `NeurIPS.cc/2026/Conference` 的所有候选查询均报告 `0 accepted=0`。
- Run manifest：`artifacts/runs/20260807T003608Z-73ea4811/manifest.json`
- Canonical：未变化；`data/raw/neurips/2026.json` 仍不存在。
- 状态：`OPEN`；空来源批次冻结并继续。

#### PF-010 解决记录（2026-08-07 12:42 Asia/Shanghai）

- 当前状态：`RESOLVED` / `NO_UPDATE`。
- 根因修复：NeurIPS 2026 改用官方 virtual 端点；HTTP/鉴权失败与真实 `count=0` 分开处理，真实空源不再被当成 OpenReview 查询失败。
- 结果：官方声明数 0、实际结果 0、分页 1；仅生成版本化 `.janus-collection.json`，未生成论文 JSON。
- Run manifest：`artifacts/runs/20260807T044207Z-f4d1775c/manifest.json`。
- Canonical：`data/raw/neurips/2026.json` 仍不存在，未进入 prepare/reconcile/publish。

### PF-011 — `DATA_GATE` — VLDB 2026

- 时间：2026-08-07 08:40（Asia/Shanghai）
- 阶段：`validate`
- 命令：`./.venv/bin/python -m tools.corpus validate --input-glob 'artifacts/runs/20260807-venue-sweep/vldb-2026/staging/vldb/*.json'`
- 退出码：`1`
- 预期：PVLDB volume 19 snapshot 经规范化后满足默认作者与摘要门禁。
- 实际：DBLP volume 19 解析出 81 条、去重后 78 篇，但 OpenAlex 未补得任何摘要；78 条均为 placeholder，resolved 作者/摘要覆盖率均为 0%，质量门禁失败。
- 证据：质量报告为 `gate_fail_files=1`、`all_pass=false`，问题为 `resolved_authors_coverage=0.00<90.00`、`resolved_abstract_coverage=0.00<85.00`。
- Run manifests：collect `artifacts/runs/20260807T004008Z-98b66520/manifest.json`；validate `artifacts/runs/20260807T004030Z-3ab84ead/manifest.json`。
- Canonical：未变化；`data/raw/vldb/2026.json` 仍不存在。
- 状态：`OPEN`；门禁失败，批次冻结且未发布。

#### PF-011 解决记录（2026-08-07 12:56 Asia/Shanghai）

- 当前状态：`RESOLVED` / `UPDATED_PARTIAL`。
- 根因修复：VLDB 2026 论文集合改由 PVLDB Volume 19 `__NEXT_DATA__` 决定；DBLP 仅按标题补充 key/URL/DOI（若存在），不再承担摘要来源。
- 结果：官方 143 条 summary 中排除 8 个 Front Matter，得到 135 个唯一论文标题；135 篇作者、摘要均非空。
- DBLP：77 篇确定性匹配，仅补充标识；当前源未提供可解析 DOI，未伪造 DOI；58 篇保持 PVLDB-only。
- 门禁与发布：重复标题 0，作者/摘要覆盖率均为 100%，`all_pass=true`；发布 manifest `artifacts/runs/20260807T045617Z-a456d925/manifest.json`。
- Canonical：新建 `data/raw/vldb/2026.json`，135 篇；Volume 19 尚在更新，终态明确标记 `UPDATED_PARTIAL`。

### PF-012 — `CODE_DEFECT` — ICML 2026 固定快照解析

- 时间：2026-08-07 12:51（Asia/Shanghai）
- 阶段：`collect` / pinned snapshot parse
- 命令：`UV_CACHE_DIR=.uv-cache uv run python -m tools.corpus collect --venue ICML --years 2026 --output-root artifacts/runs/20260807-seven-venue-repair/06-icml-2026/collected`
- 退出码：`1`
- 预期：固定快照通过提交与 SHA-256 校验后读取 6,559 个 Conference/Position Track 记录。
- 实际：快照真实顶层为 `{summary, papers}` 对象，collector 错误地只接受顶层列表并报 `Pinned ICML snapshot is not a list`。
- 证据：固定提交本地只读副本显示 `dict_keys(['summary', 'papers'])`，其中 `papers` 为列表。
- Run manifest：`artifacts/runs/20260807T045107Z-300dffa7/manifest.json`。
- Canonical：未变化；失败发生在任何论文 JSON 写入前。
- 状态：`OPEN`；修复真实 payload 结构后重试到新的独立 snapshot。

#### PF-012 重试 2（2026-08-07 12:52 Asia/Shanghai）

- 退出码：`1`；Run manifest：`artifacts/runs/20260807T045159Z-1ba229fa/manifest.json`。
- 新发现：记录使用 `openreview_url`、`virtual_url`、顶层 `institutions`，而不是官方 events 的 `paper_url`、`virtualsite_url`、author 内 institution；显式 ID 提取因此为空。
- Canonical：仍未变化；失败继续发生在论文 JSON 写入前。
- 当前状态：`OPEN`；补齐固定快照的已验证字段适配后再次重试。

#### PF-012 重试 3（2026-08-07 12:53 Asia/Shanghai）

- 退出码：`1`；Run manifest：`artifacts/runs/20260807T045253Z-81a1435b/manifest.json`。
- 新发现：现有 ICML canonical 的 6,567 个 OpenReview ID 全部位于 `source_ids.openreview_id`，顶层 `openreview_id` 为空；子集门禁只检查顶层导致 6,559 个误报为第三方独有。
- Canonical：仍未变化；失败发生在输出前。
- 当前状态：`OPEN`；子集门禁同时读取 canonical 顶层和 `source_ids` 后重试。

#### PF-012 解决记录（2026-08-07 12:54 Asia/Shanghai）

- 当前状态：`RESOLVED`。
- 修复：解析 `{summary, papers}` 顶层；适配 `openreview_url`、`virtual_url`、顶层 institutions；canonical 子集校验兼容顶层和 `source_ids.openreview_id`。
- 回归：新增三组固定快照结构测试；第四个独立 snapshot 成功产出 6,559 篇，并通过 PF-008 的来源、子集、质量和发布门禁。
- 成功 collect manifest：`artifacts/runs/20260807T045328Z-80bee3a6/manifest.json`。

### PF-013 — `DATA_GATE` — VLDB 2026 DBLP 补充匹配

- 时间：2026-08-07 12:54（Asia/Shanghai）
- 阶段：`collect` / DBLP supplement gate
- 命令：`UV_CACHE_DIR=.uv-cache uv run python -m tools.corpus collect --venue VLDB --years 2026 --output-root artifacts/runs/20260807-seven-venue-repair/07-vldb-2026/collected`
- 退出码：`1`
- 预期：官方 PVLDB 135 篇与 DBLP 至少 77 条精确标题匹配；DBLP 只补标识。
- 实际：官方 135 篇解析成功，但 DBLP 精确标题匹配为 76，低于预检基线 77，collector 在写论文 JSON 前冻结。
- Run manifest：`artifacts/runs/20260807T045440Z-349d240a/manifest.json`。
- Canonical：`data/raw/vldb/2026.json` 仍不存在；snapshot 目录无论文文件。
- 状态：`OPEN`；审计 1 条标题差异后，只有证明为同一论文才允许加入确定性补充匹配。

#### PF-013 解决记录（2026-08-07 12:56 Asia/Shanghai）

- 当前状态：`RESOLVED`。
- 审计：DBLP `Balancing the Blend: An Experimental Analysis of Trade-offs in Hybrid Search` 与 PVLDB 同名条目仅相差末尾冒号，标题相似度 0.993，8 位作者逐一对应（DBLP disambiguation 后缀除外）。
- 修复：确定性 title key 统一忽略末尾出版标点 `.`、`:`、`;`；DBLP 匹配恢复为 77，新增回归测试。
- 成功 collect manifest：`artifacts/runs/20260807T045545Z-e15dda5c/manifest.json`；发布结果见 PF-011。

### PF-014 — `CODE_DEFECT` — doctor 只读诊断修改生产 Chroma 容器

- 时间：2026-08-07 16:23（Asia/Shanghai）
- 阶段：最终生产数据哈希回验
- 命令：`./.venv/bin/python -m tools.doctor --profile query`、`./.venv/bin/python -m tools.doctor --profile ops`
- 预期：doctor 为严格只读诊断，生产 `data/vectors/chroma` 聚合 SHA-256 保持基线 `eec71c30baa282fd3b2eee0a2e9379819271e79233dcf4e4639c51da50a2276d`。
- 实际：旧实现通过 `chromadb.PersistentClient` 打开生产集合，`chroma.sqlite3` 的 mtime 更新为本轮诊断时刻，目录聚合 SHA-256 变为 `55ef270f6081b06b3f7930002c83a875ef585142f05dd9c8202a490e22e4a524`；HNSW 索引文件 mtime 均未变化，集合仍为 124,683 条。
- 处置：doctor 改为直接以 SQLite `mode=ro&immutable=1` 读取集合、paper ID 和必要元数据，不再实例化 PersistentClient。定向测试 3/3 通过；修复后的生产 query doctor 前后 `chroma.sqlite3` SHA-256 均为 `f8a4a5d28154a0e0a75bb85bd330400be82583561a12afeee6fa1cf8a57324a8`，mtime 保持不变。
- 生产数据：未执行重建、覆盖或回滚。由于没有实施前 `chroma.sqlite3` 副本，无法证明或恢复原始容器字节；为避免破坏语义数据，不做猜测性回滚。
- 当前状态：`RESOLVED_WITH_RESIDUAL_RISK`；后续只读诊断已修复，既有一次性容器字节漂移保留为明确验收例外。
