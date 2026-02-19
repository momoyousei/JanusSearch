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

## 无 S2 API key 场景建议
问题：429 限流频繁，吞吐不稳定，长任务性价比低。

建议策略：
1. 用小批次增量回填，避免长时间空耗
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline --input-glob 'ICML-21.json' backfill --max-records-per-file 40 --min-interval 6.0 --retries 4 --timeout 30 --enable-arxiv-title
```

2. 每轮后立刻验证增益
```bash
PYTHONPYCACHEPREFIX=.pycache python3 -m tools.m1_pipeline --input-glob 'ICML-21.json' validate
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
