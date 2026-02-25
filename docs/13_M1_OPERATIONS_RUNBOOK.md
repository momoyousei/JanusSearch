# M1 实操手册（Runbook）

## 目标
用固定命令完成 M1 全流程，并可复现实操结果与质量结论。

## 典型执行顺序
1. 盘点
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline inventory
```

2. 规范化（写回）
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline normalize
```

3. 回填（默认）
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline backfill --min-interval 3.0 --retries 3 --timeout 30
```

4. 验证
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline validate
```

## CVPR 2021-2025 增量采集（官方口径优先）
1. 先统计官方口径总量
```bash
for y in 2021 2022 2023 2024 2025; do
  url="https://openaccess.thecvf.com/CVPR${y}?day=all"
  c=$(curl -sL "$url" | rg -o '<dt class="ptitle">' -N | wc -l | tr -d ' ')
  echo "$y $c $url"
done
```

2. 再批量采集并写入 `archives/root_json/CVPR-2*.json`
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.cvpr_collect \
  --years 2021-2025 \
  --output-root archives/root_json \
  --index-root index \
  --workers 12 \
  --timeout 30 \
  --retries 2 \
  --min-interval 0.2
```

3. 仅对 CVPR 子集执行 M1 规范化与验证
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline --input-glob 'archives/root_json/CVPR-2*.json' normalize
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline --input-glob 'archives/root_json/CVPR-2*.json' validate
```

4. 对少量残缺项做定向回填
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline --input-glob 'archives/root_json/CVPR-2[24].json' backfill \
  --max-records-per-file 10 \
  --min-interval 2.0 \
  --retries 3 \
  --timeout 30 \
  --enable-arxiv-title
```

5. 结果追踪
- 采集报告：`index/cvpr_collection_report.json`
- 门禁报告：`index/m1_quality_report.json`
- 回填报告：`index/m1_backfill_report.json`

## ACL 2021-2025 增量采集（ARR 时代口径）
1. 先按官方页面聚合并采集（含 ACL + Findings）
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

2. 仅对 ACL 子集执行 M1 规范化与验证
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline --input-glob 'archives/root_json/ACL-2*.json' normalize
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline --input-glob 'archives/root_json/ACL-2*.json' validate
```

3. 定向回填时必须启用“标题链路”
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline --input-glob 'archives/root_json/ACL-2*.json' backfill \
  --max-records-per-file 0 \
  --min-interval 3.0 \
  --retries 3 \
  --timeout 30 \
  --enable-arxiv-title
```

4. 结果追踪
- 采集报告：`index/acl_collection_report.json`
- 门禁报告：`index/m1_quality_report.json`
- 回填报告：`index/m1_backfill_report.json`

## DOI 失败后的标准补齐动作（通用）
1. 不停在 DOI 层
- OpenAlex/S2 DOI 未命中后，必须继续标题检索（OpenAlex title + S2 title + arXiv title）。

2. 标题检索必须做质量约束
- 标题归一化后做相似度阈值过滤；不过阈值不写回。

3. 每轮回填都要复核报告增益
- 对比 `index/m1_backfill_report.json` 的命中项与失败项，避免低收益重复重跑。

## 无 S2 API key 场景建议
问题：429 限流频繁，吞吐不稳定，长任务性价比低。

说明：
- 对 ICML 年份，回填会优先尝试 PMLR 官方页面抽取 abstract，可显著降低对 S2 的依赖。

建议策略：
1. 用小批次增量回填，避免长时间空耗
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline --input-glob 'archives/root_json/ICML-21.json' backfill --max-records-per-file 40 --min-interval 6.0 --retries 4 --timeout 30 --enable-arxiv-title
```

2. 每轮后立刻验证增益
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline --input-glob 'archives/root_json/ICML-21.json' validate
```

3. 若连续两轮增益很小（例如 < 0.5%），优先冻结并转后续里程碑处理。

## 回滚与追踪
- 根文件写回前会备份到：`backups/raw/{timestamp}/`
- 质量报告：`index/m1_quality_report.json`
- 回填报告：`index/m1_backfill_report.json`
- 汇总页：`index/stats.md`

## 运行注意事项
- 避免并发回填同一文件，先完成再验证。
- 每次策略变更（interval/retries/batch）都要记录在冻结报告或变更日志中。
- 不要手改统计字段；统一通过 `normalize`/`validate` 重新计算。

## 关联文档
- 方法论：`10_M1_METHOD.md`
- 门禁规则：`12_M1_QUALITY_GATES.md`
- 当前冻结：`14_M1_FREEZE_2026-02-19.md`
- 修复复盘：`15_M1_ICML21_PATCH_AND_LESSONS_2026-02-22.md`
- 增量复盘：`16_M1_CVPR2021_2025_PATCH_AND_LESSONS_2026-02-22.md`
- 增量复盘：`18_M1_ACL2021_2025_COLLECTION_AND_LESSONS_2026-02-23.md`
