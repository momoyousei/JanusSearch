# 扩充策略（会议/年份增量）

## 适用场景
在当前稳定基线之上，持续新增会议与年份覆盖。

## 扩充原则
1. 先单批次跑通，再扩大批次
2. 先官方口径对齐，再优化摘要覆盖
3. 每批次必须产出可审计报告
4. 事实源以 `data/raw` 为准

## 单批次标准流程
1. 采集到历史输入层
```bash
python3 -m tools.<venue>_collect --years <RANGE> --output-root archives/root_json
```

2. M1 子集处理
```bash
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/{VENUE}-*.json' inventory
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/{VENUE}-*.json' normalize
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/{VENUE}-*.json' backfill --max-records-per-file 0 --enable-arxiv-title
python3 -m tools.m1_pipeline --input-glob 'archives/root_json/{VENUE}-*.json' validate
```

3. M2 全量重建
```bash
python3 -m tools.m2_db run
```

4. M3/M4 回归
```bash
python3 -m tools.m3_pipeline validate --db-path data/papers.db --vectors-root data/vectors/chroma --collection-name papers_v1 --exclude-placeholder
python3 -m tools.m4_validate status
```

## 摘要补齐矩阵（必须）
1. 会议专用源（优先）
- CVPR/CVF 详情页
- ICML/PMLR
- ACL Anthology 详情页
- OpenReview note

2. DOI 查询
- OpenAlex DOI
- Semantic Scholar DOI

3. 标题查询（DOI 失败后必走）
- OpenAlex title
- Semantic Scholar title
- `m1_pipeline backfill --enable-arxiv-title`

4. 人工补录（最后手段）
- 仅用于小规模残缺
- 必须记录来源与时间
- 不得篡改官方总量口径

## 批次门禁建议
1. M1 子集 `gate_fail_files = 0`
2. M2 `all_pass = true`
3. 检索冒烟通过
```bash
python3 -m tools.search --db-path data/papers.db stats
python3 -m tools.search search --query "continual learning replay" --top-k 20
```

## 风险处理
1. 无 S2 key：优先会议专用源，降低通用 API 依赖
2. 官方页面 404：报告中区分“源失效”与“解析失败”
3. DOI 命中低：立即切标题检索通道
4. 年份门禁失败：冻结当前批次，修复后再并入下一批
