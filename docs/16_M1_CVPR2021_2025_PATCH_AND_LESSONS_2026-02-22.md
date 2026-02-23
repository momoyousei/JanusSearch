# M1 增量复盘：CVPR 2021-2025 采集与补齐（2026-02-22）

## 背景
- 增量前状态：`archives/root_json` 与 `data/raw` 中无 CVPR 年份文件。
- 目标：以 CVF OpenAccess 官方口径为准，完成 CVPR 2021-2025 全量采集、规范化、门禁验证，并接续 M2 入库。

## 执行事实
1. 官方口径先验统计（`CVPR{year}?day=all`）
   - 2021: 1660
   - 2022: 2074
   - 2023: 2353
   - 2024: 2716
   - 2025: 2871
   - 合计: 11674
2. 批量采集
   - 入口：`python3 -m tools.cvpr_collect --years 2021-2025 ...`
   - 产物：`archives/root_json/CVPR-21.json` ~ `CVPR-25.json`
   - 报告：`index/cvpr_collection_report.json`
3. M1 子流程（仅 CVPR 子集）
   - `inventory -> normalize -> validate`
   - 初次失败原因：`resolved_abstract_coverage = 0.00%`
4. 摘要补齐策略
   - 首选抓取 CVF 详情页 `div#abstract`
   - 对少量 404 条目执行 `m1_pipeline backfill`（S2/arXiv）
   - 对仍缺失且可追溯项，使用 Wayback 快照补齐
5. 结果
   - 缺失摘要从 6 条降到 1 条
   - `validate` 门禁通过（CVPR 子集 `gate_fail_files=0`）

## 最终留存缺口
- `CVPR-22` 仍有 1 条摘要缺失：
  - `A Graph Matching Perspective With Transformers on Video Instance Segmentation`
- 原因：官方主页面链接与当前 PDF 链接均为 404；回填源在无 S2 key 限流下未命中可用摘要。

## 方法论经验
1. 先官方口径、后采集是硬规则
   - 若不先锁定官方总量，后续难区分“真实缺失”与“采集遗漏”。
2. CVPR 不应只抓 list 页
   - list 页可拿 title/authors/links，但摘要必须走详情页。
3. 官方 404 属于源端不稳定，不是解析错误
   - 需在报告中显式区分“解析失败”和“源链接失效”。
4. 残缺可控时优先闭环主线
   - 当残缺规模极小且门禁达标，应继续推进 M2/M3，不阻塞主线里程碑。

## 对后续阶段的影响
- M1：CVPR 2021-2025 已纳入 canonical（`data/raw/cvpr/*.json`）。
- M2：已基于 `data/raw` 完成入库与一致性校验（见 `index/m2_load_report.json`、`index/m2_validate_report.json`）。

## 建议的后续动作
1. 为 S2 配置 API key，针对残留条目做一次低频定向补齐。
2. 在 `tools/cvpr_collect.py` 增加“404 源链接清单”输出，便于后续人工处理。
3. 每次增量采集后固定执行：
   - 官方口径复核
   - 子集 `normalize + validate`
   - 必要时定向 `backfill`
