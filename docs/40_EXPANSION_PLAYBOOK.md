# 扩充阶段手册（会议/论文增量）

## 阶段目标
在现有可复现基线（M1→M4 已打通）上，持续扩展会议与年份覆盖，同时保持质量门禁、入库一致性与检索可用性。

当前稳定基线：
- 历史输入：`archives/root_json/`（当前 21 个会议年份文件）
- 事实源：`data/raw/{venue}/{year}.json`
- 检索面：`data/papers.db` + `data/vectors/chroma`

## 扩充原则
1. 先单批次跑通，再扩大批次。
2. 先保证官方口径对齐，再追求摘要覆盖率。
3. 每批次必须产出报告，不做“无证据更新”。
4. 新增数据以 `data/raw` 为唯一事实源，不直接依赖历史输入。

## 单批次标准流程
以“一个会议的若干年份”为一个批次执行。

1. 采集到历史输入层（按会议脚本）
- 输出到：`archives/root_json/{VENUE}-{YY}.json`

2. M1 子集处理（只跑本批次）
```bash
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/{VENUE}-*.json' inventory
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/{VENUE}-*.json' normalize
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/{VENUE}-*.json' backfill --max-records-per-file 0
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/{VENUE}-*.json' validate
```

3. M2 全量重建与校验
```bash
python3 -m tools.m2_db run
```

4. M3/M4 回归（建议按批次或日终执行）
```bash
python3 -m tools.m3_pipeline validate --db-path data/papers.db --vectors-root data/vectors/chroma --collection-name papers_v1 --exclude-placeholder
python3 -m tools.m4_validate status
```

## 批次门禁（建议）
1. M1 子集 `gate_fail_files = 0`。
2. M2 `all_pass = true`。
3. 检索冒烟通过：
```bash
python3 -m tools.search --db-path data/papers.db stats
python3 -m tools.search search --query "continual learning replay" --top-k 20
```

## 报告清单（每批次至少检查）
- `index/m1_quality_report.json`
- `index/m1_backfill_report.json`
- `index/m2_load_report.json`
- `index/m2_validate_report.json`
- `index/m3_validate_report.json`
- `index/m4_eval_report.json`（若本批次执行了 M4 run）

## 风险与处理
1. 无 S2 key 场景：优先会议专用源（如 ICML/PMLR、CVPR/CVF）并降低对 S2 的依赖。
2. 官方页面 404：记录到报告并区分“源失效”与“解析失败”。
3. 某年份门禁不通过：冻结该批次，先修复后再合入下一批次。

## 与主文档关系
- 全局入口：`AGENTS.md`, `PROJECT.md`
- 详细流程：`10_M1_METHOD.md`, `20_M2_DATABASE.md`, `22_M3_CACHE_AND_HYBRID.md`, `30_M4_AGENT_VALIDATION.md`
