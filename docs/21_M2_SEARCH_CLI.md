# M2-B：SQL + FTS 检索 CLI

## 目标
在 `data/papers.db` 上提供可复现、可测试的检索接口：
- 全文检索（标题 + 摘要）
- 单篇详情查询
- 检索面统计

## 入口命令
```bash
# 全文检索（默认 BM25、默认排除 placeholder）
python3 -m tools.search search --query "continual learning replay"

# 带过滤与分页
python3 -m tools.search search \
  --query "replay" \
  --venue ICLR,ICML,NEURIPS \
  --year-from 2021 \
  --year-to 2025 \
  --track conference \
  --presentation-level poster \
  --order bm25 \
  --top-k 20 \
  --offset 0

# 单篇详情
python3 -m tools.search get --paper-id S2-xxxxxxxxxxxxxxxx

# 统计
python3 -m tools.search stats
```

## `search` 参数
- `--query` 必填
- `--venue` 逗号分隔
- `--year-from`, `--year-to`
- `--track`
- `--presentation-level`：`poster|oral|bestpaper`
- `--include-placeholder`（默认 false）
- `--order`：`bm25|year|citation`（默认 `bm25`）
- `--top-k`（默认 20）
- `--offset`（默认 0）
- `--format`：`table|json|md`（默认 `table`）

## 默认行为
1. 检索字段：`title + abstract`
2. 排序：`bm25`
3. 默认排除 `record_status=placeholder`
4. 输出：table

## FTS 依赖
- 依赖表：`papers_fts`
- 若缺失，`search` 会报错并提示：
```bash
python3 -m tools.m2_db reindex-fts
```

## 与 M2-A 的关系
- M2-A `load` 后会自动重建 FTS
- 手动重建可用：
```bash
python3 -m tools.m2_db reindex-fts
```

## 测试
```bash
python3 -m unittest tests/test_m2_db.py tests/test_search_cli.py
```
