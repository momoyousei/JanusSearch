# M2-A：JSON 入库 SQLite

## 目标
将 M1 规范化产物 `data/raw/*/*.json` 全量写入本地 SQLite，
并提供机器可读的一致性校验报告。

## 入口命令
```bash
# 全量重建入库
python3 -m tools.m2_db load

# 入库后校验
python3 -m tools.m2_db validate

# 一键执行（load + validate）
python3 -m tools.m2_db run

# 手动重建 FTS
python3 -m tools.m2_db reindex-fts

# 查看数据库摘要
python3 -m tools.m2_db stats
```

可选参数：
- `--input-root`（默认 `data/raw`）
- `--db-path`（默认 `data/papers.db`）
- `--index-root`（默认 `index`）
- `--log-level`

## 输入与事实来源
- 本阶段仅使用 `data/raw`，不直接读取根目录 16 个历史文件。
- 根目录文件保留用于历史追溯，不参与 M2 入库。

## 表结构
- 主表：`papers`
- 关系表：
  - `paper_authors`
  - `paper_keywords`
  - `paper_institutions`
  - `paper_quality_flags`
  - `paper_source_ids`
- 元数据表：
  - `source_files`
  - `ingestion_runs`

## 装载模式
- `rebuild`：每次删除并重建 `data/papers.db`，确保可复现。
- 保留 `record_status` 全量入库（包括 `placeholder`）。
- `load` 完成后自动重建 FTS5 索引 `papers_fts`（`title + abstract`）。

## 产物报告
- `index/m2_load_report.json`
  - 运行状态、总耗时、总记录数、每文件行数与关系行数。
- `index/m2_validate_report.json`
  - JSON 与 DB 的计数对比、分布对比、失败项清单。
- `index/m2_fts_report.json`
  - 手动执行 `reindex-fts` 时输出 FTS 重建结果。

## 校验项
1. 总论文数一致
2. source_file 数量一致
3. venue-year 计数一致
4. record_status 分布一致
5. 关系表行数一致（authors/keywords/institutions/quality_flags/source_ids）
6. `paper_id` 无重复
7. 主字段无空值（title/venue/year/track/presentation/status）
8. `source_files` declared/loaded 清单一致
9. `papers_fts` 存在且行数与 `papers` 一致

## 测试
```bash
python3 -m unittest tests/test_m2_db.py
```

## 下一步（M2-B）
- 在此 DB 基线上增加 `tools/search.py` 检索 CLI（见 `21_M2_SEARCH_CLI.md`）。
