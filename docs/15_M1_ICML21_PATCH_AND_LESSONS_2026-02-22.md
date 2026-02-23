# M1 ICML-2021 修复复盘与经验沉淀（2026-02-22）

## 背景
- 问题文件：`ICML-21.json`
- 初始门禁失败项：`resolved_abstract_coverage = 79.44% < 85%`
- 影响：M1 全量验证 `gate_fail_files = 1`

## 诊断过程（实操结论）
1. 先做样本探测，而不是直接大规模重跑
- 对缺摘要样本验证 `Semantic Scholar`（无 key）和 `arXiv title fallback` 命中率。
- 结果：`arXiv title fallback` 在该批次近乎无效；S2 在无 key 场景受 429 显著影响。

2. 数据结构驱动定位补源方向
- 缺摘要记录多数具备 `openalex_id`，但 DOI/arXiv 锚点不足。
- 判断应引入“会议专用源”而不是继续依赖通用回填。

3. 会场特化源验证
- 对 ICML-2021 使用 `PMLR v139` 标题映射验证：
  - 缺摘要记录 96 条
  - 标题可匹配 `PMLR abs` 链接 94 条
  - 94 条均可提取 abstract

## 实现策略（增量且可控）
- 在 `tools/m1_pipeline.py` 增加 ICML 专用 fallback：
  1. `ICML year -> PMLR volume` 映射（当前含 2021 -> v139）
  2. 加载 PMLR volume 索引（标题 -> abs URL）
  3. 在 backfill 中优先尝试 PMLR（仅 ICML 生效）
  4. 命中后写入 `abstract`，并在 `source_ids` 记录 `pmlr_abs_url`
- 保持原有链路顺序与接口不破坏：
  - `PMLR -> S2 DOI -> S2 title -> arXiv id -> arXiv title`

## 修复结果
- 定向回填（ICML-21）：
  - `candidates = 96`
  - `updated_records = 94`
  - `pmlr_hits = 94`
  - `failed_records = 2`
- 指标变化：
  - `resolved_abstract_coverage: 79.44% -> 99.57%`
- 全量 M1 验证：
  - `gate_pass_files = 16`
  - `gate_fail_files = 0`
  - `alignment_fail_files = 0`

## 方法论经验（可复用）
1. 先“采样探测命中率”，再决定是否改代码
- 先用 20~30 条样本验证候选补源，避免盲目全量重跑。

2. 回填要“会议特化优先”
- 顶会来源结构差异大，通用 API 难覆盖边缘记录。
- ICML 与 PMLR 的结构化耦合是高性价比补源点。

3. 无 key 场景要默认“节流 + 专用源”策略
- 无 key 的 S2 不适合承担主回填通道。

4. 修复动作必须保留可审计证据
- 统一以报告与统计文件作为事实：
  - `index/m1_backfill_report.json`
  - `index/m1_quality_report.json`
  - `index/stats.md`

5. 目录职责清晰能减少误操作
- 历史输入：`archives/root_json/`
- 事实源：`data/raw/`
- 检索运行面：`data/papers.db` + `data/vectors/chroma`

## 后续建议
1. 扩展 ICML 年份映射（如 2022+ 对应 PMLR volume），沉淀为配置而非硬编码。
2. 为 PMLR fallback 增加独立测试（mock HTML）覆盖解析稳定性。
3. 对其他会议建立“专用补源矩阵”（ACL Anthology / OpenReview 等）并按同样方法先做样本命中率评估。
