# M3：缓存层与混合检索

## 目标
在 M2（SQLite + FTS）基线上交付：
- 向量构建（Chroma）
- 主题/子主题分配（Topic/Subtopic）
- L1-L4 Markdown 缓存
- 混合检索 `hybrid`（FTS + Vector 融合）

## 入口命令
```bash
# 全流程（vectors -> topics -> cache -> validate）
python3 -m tools.m3_pipeline run \
  --db-path data/papers.db \
  --embed-base-url https://api.siliconflow.cn/v1/embeddings \
  --embed-model Qwen/Qwen3-Embedding-8B \
  --embed-batch-size 32 \
  --exclude-placeholder

# 分步执行
python3 -m tools.m3_pipeline build-vectors \
  --db-path data/papers.db \
  --embed-base-url https://api.siliconflow.cn/v1/embeddings \
  --embed-model Qwen/Qwen3-Embedding-8B

python3 -m tools.m3_pipeline build-topics \
  --llm-base-url https://api.siliconflow.cn/v1 \
  --llm-model Qwen/Qwen3-8B

python3 -m tools.m3_pipeline build-cache
python3 -m tools.m3_pipeline validate
```

## 环境变量
- `JANUS_LLM_API_KEY`：LLM 命名必须（`build-topics` 硬失败策略）
- `JANUS_LLM_BASE_URL`：默认 `https://api.siliconflow.cn/v1`
- `JANUS_LLM_MODEL`：默认 `Qwen/Qwen3-8B`
- `JANUS_EMBED_BASE_URL`：默认 `https://api.siliconflow.cn/v1`
- `JANUS_EMBED_API_KEY`：可选，embedding 端点需要时使用（默认回退 `JANUS_LLM_API_KEY`）

## 关键产物
- `data/vectors/chroma/`：向量库目录
- `index/m3_topic_assignments.json`：paper->topic/subtopic 分配
- `index/master_index.md`：L1 主索引
- `venues/{venue}/{venue}_{year}.md`：L2 会议年页
- `topics/_topic_index.md`, `topics/{topic}.md`：L3 主题页
- `subtopics/{topic}/_overview.md`, `subtopics/{topic}/{subtopic}.md`：L4 子主题页
- `index/m3_build_report.json`：构建报告
- `index/m3_validate_report.json`：校验报告

## `tools.search hybrid`
```bash
python3 -m tools.search hybrid \
  --query "continual learning replay" \
  --embed-base-url https://api.siliconflow.cn/v1/embeddings \
  --embed-model Qwen/Qwen3-Embedding-8B \
  --embed-api-key "$JANUS_EMBED_API_KEY" \
  --alpha 0.6 \
  --vector-top-k 100 \
  --bm25-top-k 100 \
  --top-k 20
```

参数说明：
- `--alpha`：融合权重，`final = alpha * vector_norm + (1-alpha) * bm25_norm`
- `--vector-top-k`：向量召回深度
- `--bm25-top-k`：FTS 召回深度
- `--embed-api-key`：可选，默认取 `JANUS_EMBED_API_KEY`，再回退 `JANUS_LLM_API_KEY`
- 复用 `search` 过滤参数：`--venue --year-from --year-to --track --presentation-level`
- 默认排除 placeholder，显式 `--include-placeholder` 才纳入

## 低负载模式（可选）
- `--embed-batch-size`：减小批次可降低瞬时负载（如 `16` / `32`）
- `--embed-cooldown-seconds`：每批后休眠，降低持续满载
- `--max-papers`：分段构建，避免单次长时间运行  
  示例：`python3 -m tools.m3_pipeline run ... --max-papers 3000`

## 验收最小集
```bash
python3 -m unittest \
  tests/test_m2_db.py \
  tests/test_search_cli.py \
  tests/test_m3_pipeline.py \
  tests/test_hybrid_search.py
```
