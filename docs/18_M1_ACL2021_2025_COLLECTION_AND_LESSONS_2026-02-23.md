# M1 增量复盘：ACL 2021-2025 采集与摘要补全（2026-02-23）

## 背景
- 目标：按 ACL Anthology 官方口径完成 ACL 2021-2025 采集，并尽可能补齐摘要。
- 范围：`ACL + Findings of ACL`（同年多 track/多卷并存，遵循 ARR 时代发布形态）。
- 核心经验：摘要补全不能只依赖 DOI，必须纳入标题检索链路。

## 执行口径
1. 官方口径来源
- `https://aclanthology.org/events/acl-{year}/`
- 统计规则：仅纳入 `year.(acl|findings-acl).*`，排除 `.0` 卷条目。

2. 采集与补齐顺序（已落地到采集脚本）
- 事件页内嵌 abstract
- 论文详情页 abstract fallback
- OpenAlex DOI fallback
- 标题检索 fallback（OpenAlex title + Semantic Scholar title，阈值匹配）

## 产物
- 采集脚本：`tools/acl_collect.py`
- 年份文件：
  - `archives/root_json/ACL-21.json`
  - `archives/root_json/ACL-22.json`
  - `archives/root_json/ACL-23.json`
  - `archives/root_json/ACL-24.json`
  - `archives/root_json/ACL-25.json`
- 执行报告：`index/acl_collection_report.json`

## 结果摘要
- 官方总量（2021-2025）：`9865`
- 已采集总量（2021-2025）：`9865`
- M1 子集门禁：`gate_fail_files = 0`
- 当前残余缺摘要：`ACL-24` 1 条

## 关键经验（供后续找论文/补摘要参考）
1. 禁止 DOI-only
- DOI 对部分会议（尤其 Findings/跨来源发布）覆盖不完整，直接造成“可检索但不可补齐”。
- 必须在 DOI 后追加标题检索；否则会把“可恢复缺失”误判为“不可恢复缺失”。

2. 标题检索要做“相似度约束”
- 标题查询会命中同名/近名论文，必须做归一化后相似度阈值过滤（当前实践阈值 `0.90`）。
- 仅在命中摘要且过阈值时写回，避免误填。

3. 多源并行优于单源重试
- 同一批次中，OpenAlex title 与 S2 title 命中分布不同。
- 单源反复重试收益递减，双源标题检索通常更高效。

4. 报告必须可审计
- 每轮补齐后检查：
  - `index/acl_collection_report.json`
  - `index/m1_backfill_report.json`
  - `index/m1_quality_report.json`
- 残留缺口要保留明确清单，不得静默丢弃记录或修改官方总量。

## 推荐命令（ACL 批次）
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.acl_collect \
  --years 2021-2025 \
  --output-root archives/root_json \
  --index-root index \
  --workers 16 \
  --timeout 120 \
  --retries 3 \
  --min-interval 0.5 \
  --title-threshold 0.90
```

```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline --input-glob 'archives/root_json/ACL-2*.json' normalize
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline --input-glob 'archives/root_json/ACL-2*.json' backfill --max-records-per-file 0 --enable-arxiv-title
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline --input-glob 'archives/root_json/ACL-2*.json' validate
```

## 后续建议
1. 将“标题检索 fallback”沉淀为统一模块，供非 ACL 会议复用。
2. 为标题检索链路增加误匹配样例测试（低相似度拒绝写回）。
3. 对残余缺摘要条目建立人工补录台账，并在下一轮增量优先处理。
