# AAAI 2021-2025 官方采集与复盘（2026-02-22）

## 目标与范围
- 目标：按 M1 官方口径流程完成 AAAI 2021-2025 采集。
- 官方源：AAAI OJS（`https://ojs.aaai.org/index.php/AAAI/issue/archive`）。
- 口径：仅纳入 `AAAI-YY Technical Tracks *` issue（OJS）。

## 执行流程
1. 先统计官方口径总量（按年份聚合 Technical Tracks issue 的 article 条目）。
2. 再批量拉取论文详情页（title/authors/abstract/doi/pdf/track）。
3. 对齐校验（`official_unique == collected`）。
4. 对失败详情做增量补抓（仅 placeholder 子集，不重跑全量）。

## 产物
- 采集脚本：`tools/aaai_collect.py`
- 年份文件：
  - `archives/root_json/AAAI-21.json`
  - `archives/root_json/AAAI-22.json`
  - `archives/root_json/AAAI-23.json`
  - `archives/root_json/AAAI-24.json`
  - `archives/root_json/AAAI-25.json`
- 汇总报告：`index/aaai_collection_report.json`

## 结果总览
- 官方总量（2021-2025）：`9910`
- 已采集总量（2021-2025）：`9910`
- 对齐结果：`aligned = true`
- 说明：`AAAI-26` 暂无官方 published list，已从当前采集基线中移除。

## 年份统计
- 2021: `1654`
- 2022: `1319`
- 2023: `1578`
- 2024: `2331`
- 2025: `3028`

## 关键经验
1. AAAI OJS archive 存在分页，必须遍历 `archive -> archive/2 -> ...`，否则会漏掉早年数据。
2. 网络抖动（read timeout / handshake timeout / reset）在高并发下较常见，必须开启重试并允许增量补抓。
3. 严格“官方先计数、再抓取、再对齐”可避免后续补数时口径漂移。
4. 对于详情页拉取失败，不删记录，先保留 placeholder，再做针对性补齐更高效。

## 推荐执行命令
```bash
python3 -m tools.aaai_collect --years 2021-2025 --workers 24 --timeout 45 --retries 8 --min-interval 0.6 --log-level INFO
```

## 后续衔接
- M1 子集验证：
```bash
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/AAAI-*.json' inventory
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/AAAI-*.json' normalize
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/AAAI-*.json' validate
```
- M2 入库：
```bash
python3 -m tools.m2_db run
```
