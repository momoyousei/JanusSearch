# 会议与年份扩充策略

## 原则

1. 一个 venue/year scope 对应一个可审计批次。
2. 采集结果先进入 run-scoped snapshot，再进入 staging；不得直接覆盖 `data/raw`。
3. 事实门禁与官方口径观察分离：字段质量是硬门禁，官方对齐默认是警告。
4. 批次失败时冻结 snapshot、staging、报告与 manifest，修复后从最早失败步骤恢复。
5. 只有 canonical 发布成功后才重建 catalog；只有明确要求时才继续 projections/online evaluation。

## 标准流程

```bash
./.venv/bin/python -m tools.corpus plan --venue <VENUE> --years <RANGE>
./.venv/bin/python -m tools.corpus collect --venue <VENUE> --years <RANGE>
./.venv/bin/python -m tools.corpus prepare \
  --input-glob '<SNAPSHOT>/*.json' --staging-root '<STAGING>' \
  --enrich --enable-arxiv-title
./.venv/bin/python -m tools.corpus validate --input-glob '<STAGING>/*/*.json'
./.venv/bin/python -m tools.corpus publish --staging-root '<STAGING>'
./.venv/bin/python -m tools.catalog build
./.venv/bin/python -m tools.catalog validate
./.venv/bin/python -m tools.evaluate run --suite offline
./.venv/bin/python -m tools.search search \
  --query "continual learning replay" --top-k 20
```

`tools.corpus add --venue <VENUE> --years <RANGE>` 可执行 collect→prepare→validate→publish→catalog。该命令涉及网络和数据发布，只在用户明确要求完整接入时使用；`--build-projections` 也是显式选项。

## 批次通过标准

| 层 | 标准 |
|---|---|
| Corpus | hard `gate_fail_files = 0`；alignment warnings 已解释 |
| Catalog | `all_pass = true` |
| Projections（若涉及） | `summary.all_pass = true` |
| Evaluation | 当前输入指纹下 `overall_pass = true` |
| Search | 基准查询能返回可核验结果 |

## 冻结与恢复

冻结条件：任一硬门禁失败、采集器异常退出、canonical 发布失败、catalog 校验失败或要求范围内的派生验证失败。

冻结动作：

1. 保留 `artifacts/runs/<run_id>/manifest.json`；
2. 保留 snapshot、staging 和报告；
3. 不手工修正统计字段；
4. 记录最早失败步骤、输入 scope 与错误；
5. 停止后续依赖更新。

恢复时从最早失败能力重跑。SQLite 失败继续使用旧数据库；Chroma 不默认删除，优先利用 ID/指纹增量恢复。

## 常见风险

| 风险 | 处理 |
|---|---|
| API key 缺失 | 使用可离线或会议专用源；明确跳过在线步骤 |
| 官方页面 404/503 | 区分远端不可用与解析器错误，保留失败证据 |
| DOI 命中低 | 进入标题检索链，不得以 DOI-only 结束 |
| 官方数量不一致 | 默认 warning 并解释口径；发布政策要求时再启用 strict |
| 评估报告旧 | 用 `tools.evaluate status` 检测 stale，重新运行离线套件 |
