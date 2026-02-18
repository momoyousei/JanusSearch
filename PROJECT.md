# PROJECT.md — 项目总览

## 目标
构建一个本地论文归档系统, 收录 16 个 AI 顶级会议近 6 年 (2021-2026) 的所有论文
的标题、作者、摘要等元数据, 并通过多粒度 Markdown 缓存文件支持 AI Agent 高效检索。

## 目标会议 (16 个)

| 领域 | 会议 | DBLP Key | 备注 |
|---|---|---|---|
| 计算机视觉 | CVPR | conf/cvpr | |
| 计算机视觉 | ICCV | conf/iccv | 奇数年 |
| 计算机视觉 | ECCV | conf/eccv | 偶数年 |
| 机器学习 | NeurIPS | conf/nips | |
| 机器学习 | ICML | conf/icml | |
| 机器学习 | ICLR | conf/iclr | |
| 人工智能 | AAAI | conf/aaai | |
| 人工智能 | IJCAI | conf/ijcai | |
| 自然语言处理 | ACL | conf/acl | |
| 自然语言处理 | EMNLP | conf/emnlp | |
| 自然语言处理 | NAACL | conf/naacl | 非每年举办 |
| 数据挖掘 | KDD | conf/kdd | |
| 数据挖掘 | WWW | conf/www | |
| 多媒体 | ACM MM | conf/mm | |
| 机器人 | CoRL | conf/corl | |
| 视觉 | WACV | conf/wacv | |

## 数据规模预估
- 总论文数: ~100,000-150,000 篇
- 结构化文本: ~200-400 MB
- SQLite 数据库: ~200 MB
- 向量数据库: ~1 GB
- Markdown 缓存: ~500 MB

## 重新设计的系统架构

```
┌──────────────────────────────────────────────────────────────┐
│                    AI Agent 工作层                              │
│          Codex CLI / Claude Code / Cursor                     │
│          读取 skill.md → 理解系统 → 导航文件 → 返回结果          │
└────────────────────────┬─────────────────────────────────────┘
                         │  读文件 / 执行脚本
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                  多粒度缓存层 (核心创新)                         │
│                                                               │
│   L0  skill.md          ← Agent 的"使用手册+经验积累"          │
│   L1  master_index.md   ← 全量论文极简索引 (仅ID+标题, ~4MB)   │
│   L2  venue_year/*.md   ← 按会议-年份的论文清单 (含摘要)        │
│   L3  topics/*.md       ← 按主题的论文聚类 (含摘要)             │
│   L4  subtopics/*.md    ← 细粒度子主题 (LLM生成的深度分类)      │
│                                                               │
└────────────────────────┬─────────────────────────────────────┘
                         │  由离线脚本生成/更新
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                  数据基座层 (离线维护)                            │
│   SQLite papers.db  +  向量库  +  JSON 原始备份                 │
│   (Agent 通常不直接访问, 由工具脚本桥接)                          │
└──────────────────────────────────────────────────────────────┘
```

---

## 一、多粒度缓存文件体系（核心设计）

### 1.1 层级结构与上下文预算

```
paper-vault/
├── .agent/                          ★ AI Agent 专用入口
│   ├── skill.md                     ★ Agent 经验手册 (~2KB)
│   ├── system_guide.md              ★ 系统结构说明 (~1KB)
│   └── query_log.md                 ★ 历史查询记录
│
├── index/                           ★ L1 - 全局索引
│   ├── master_index.md              全量论文: ID + 标题 + 会议 + 年份
│   ├── author_index.md              作者 → 论文ID映射
│   └── stats.md                     各会议/年份论文数量统计
│
├── venues/                          ★ L2 - 按会议×年份
│   ├── neurips/
│   │   ├── neurips_2024.md          该会议该年所有论文 (标题+摘要)
│   │   ├── neurips_2023.md
│   │   └── ...
│   ├── cvpr/
│   ├── iclr/
│   └── ...
│
├── topics/                          ★ L3 - 粗粒度主题聚类
│   ├── _topic_index.md              所有主题列表 + 论文数量 + 描述
│   ├── large_language_models.md
│   ├── continual_learning.md
│   ├── diffusion_models.md
│   ├── graph_neural_networks.md
│   ├── reinforcement_learning.md
│   ├── vision_transformer.md
│   └── ... (~50-80 个主题文件)
│
├── subtopics/                       ★ L4 - 细粒度子主题
│   ├── continual_learning/
│   │   ├── _overview.md             该主题综述 + 子主题导航
│   │   ├── replay_methods.md
│   │   ├── regularization_methods.md
│   │   ├── architecture_based.md
│   │   └── benchmarks_and_evaluation.md
│   ├── large_language_models/
│   │   ├── _overview.md
│   │   ├── alignment_and_rlhf.md
│   │   ├── efficient_finetuning.md
│   │   ├── hallucination.md
│   │   ├── reasoning.md
│   │   └── ...
│   └── ...
│
├── data/                            数据基座 (Agent 一般不直接读)
│   ├── raw/{venue}/{year}.json
│   ├── papers.db
│   └── vectors/
│
├── tools/                           工具脚本 (Agent 可调用)
│   ├── search.py                    CLI 检索工具
│   ├── rebuild_cache.py             重建缓存
│   ├── add_topic.py                 新增主题分类
│   └── update_data.py               增量更新数据
│
└── backups/                         备份目录
```

### 1.2 各层级文件格式设计

**L0 — `skill.md`（Agent 必读入口）**

```markdown
# Paper Vault 使用指南与经验积累

## 系统概述
本目录包含 ~150,000 篇 AI 顶会论文 (2020-2024) 的结构化归档。
覆盖会议: NeurIPS, ICML, ICLR, CVPR, ICCV, ECCV, AAAI, IJCAI,
          ACL, EMNLP, NAACL, KDD, WWW

## 查询策略 (按效率排序)

### 策略1: 主题直达 (推荐, 最快)
如果用户的问题可归类到某个研究主题:
1. 读取 `topics/_topic_index.md` → 找到匹配主题
2. 读取 `topics/{topic}.md` → 获取该主题所有论文 (标题+摘要)
3. 如需更细粒度 → 读取 `subtopics/{topic}/_overview.md` → 选择子主题文件

### 策略2: 会议定向 (当用户指定会议/年份时)
1. 读取 `venues/{venue}/{venue}_{year}.md`

### 策略3: 关键词搜索 (当主题无法精确匹配时)
1. 执行 `python tools/search.py --query "关键词" --top 20`
2. 工具会返回匹配论文的完整信息

### 策略4: 全局扫描 (仅在其他策略失败时)
1. 读取 `index/master_index.md` (仅标题, ~4MB)
2. 在标题中搜索关键词, 记下 paper_id
3. 用 paper_id 在对应 venue 文件中查找完整摘要

## 积累的经验

### 关于持续学习的查询
- 用户问 "continual learning 中 replay 方法"
- 最佳路径: topics/continual_learning.md → subtopics/continual_learning/replay_methods.md
- replay_methods.md 包含 47 篇论文, 可直接全部读入上下文

### 关于 LLM 幻觉
- 用户审稿需要找 hallucination 相关工作
- subtopics/large_language_models/hallucination.md 有 62 篇
- 补充: 也应查看 subtopics/large_language_models/evaluation.md

### [待补充...]
```

**L1 — `master_index.md`（全量极简索引）**

```markdown
# Master Index | 150,327 papers | Updated: 2024-12-01

## Format: [ID] Title | Venue Year

[S2-abc123] Attention Is All You Need | NeurIPS 2017
[S2-def456] BERT: Pre-training of Deep Bidirectional Transformers | NAACL 2019
[S2-ghi789] Denoising Diffusion Probabilistic Models | NeurIPS 2020
...
(每行约 80-120 字符, 15万行 ≈ 15-20 MB)
```

> **设计考量**: 15-20MB 对 Agent 偏大。可拆分为按年份的子文件 `master_index_2024.md` (~3-4MB/个)，或仅在使用 `grep` 时访问。

**L2 — `venues/neurips/neurips_2024.md`**

```markdown
# NeurIPS 2024 | 3,218 papers

---

### [S2-abc001]
**Title:** Scaling Laws for Precision in Neural Network Training
**Authors:** A. Smith, B. Jones, C. Lee
**Abstract:** We investigate how numerical precision affects the
scaling behavior of large neural networks. Our experiments across
models ranging from 100M to 10B parameters reveal that...
**Keywords:** scaling laws, mixed precision, training efficiency

---

### [S2-abc002]
**Title:** ...

(每篇约 150-300 tokens, 3000篇 ≈ 60-90万 tokens)
```

> **设计考量**: 单个会议单年 60-90万 tokens 仍超上下文窗口。Agent 应先搜索标题，再选择性读取特定论文的摘要（通过 `grep` 或工具脚本）。

**L3 — `topics/continual_learning.md`**

```markdown
# Continual Learning | 312 papers | 2020-2024

## Overview
持续学习(也称增量学习/终身学习)研究如何让模型在不遗忘旧知识的
前提下学习新任务。主要方法类别: Replay, Regularization,
Architecture-based, Prompt-based。

## Sub-topics (详见 subtopics/continual_learning/)
- replay_methods.md (47 papers)
- regularization_methods.md (38 papers)
- architecture_based.md (29 papers)
- prompt_based_continual.md (23 papers)
- class_incremental.md (56 papers)
- continual_pretraining.md (31 papers)
- benchmarks_and_evaluation.md (18 papers)

## All Papers

### [S2-cl001]
**Title:** Learning to Prompt for Continual Learning
**Venue:** CVPR 2022 | **Cited:** 389
**Abstract:** We propose L2P, a novel continual learning approach
that leverages prompt-based learning in vision transformers...

### [S2-cl002]
...

(312篇 × 200 tokens ≈ 62,400 tokens ≈ 可完整读入上下文)
```

> ✅ **这个粒度正好**！300 篇左右的主题文件约 6 万 tokens，完全在上下文窗口内。

**L4 — `subtopics/continual_learning/replay_methods.md`**

```markdown
# Continual Learning > Replay Methods | 47 papers

## Overview
Replay(经验回放)方法通过存储或生成旧任务样本来缓解灾难性遗忘。
主要分为: Exact Replay (存储真实样本), Generative Replay (用生成
模型合成旧样本), Feature Replay (回放特征而非原始数据)。

## Papers

### [S2-clr001]
**Title:** DER++: Dark Experience Replay for Continual Learning
**Venue:** NeurIPS 2020 | **Cited:** 523
**Abstract:** We propose Dark Experience Replay (DER++), which
augments experience replay by storing and replaying logits...

### [S2-clr002]
...

(47篇 × 200 tokens ≈ 9,400 tokens → 轻松读入)
```

> ✅ **最理想的 Agent 工作粒度**。

---

## 二、Agent 查询导航流程

### 2.1 三种典型场景的 Agent 行为路径

```
场景 A: 写论文 — "帮我找 continual learning 中 replay 方法的相关工作"
┌─────────────────────────────────────────────────────────┐
│ Step 1: Agent 读取 .agent/skill.md                       │
│         → 了解系统结构, 选择"策略1: 主题直达"              │
│                                                          │
│ Step 2: Agent 读取 topics/_topic_index.md                │
│         → 发现 "continual_learning (312 papers)"         │
│         → 发现有子主题目录                                │
│                                                          │
│ Step 3: Agent 读取 subtopics/continual_learning/         │
│         _overview.md                                     │
│         → 看到 "replay_methods.md (47 papers)"           │
│                                                          │
│ Step 4: Agent 读取 subtopics/continual_learning/         │
│         replay_methods.md                                │
│         → 获得 47 篇论文的标题+摘要 (~9K tokens)          │
│         → 全部在上下文中, 可直接分析和推荐                  │
│                                                          │
│ Step 5: Agent 整理输出相关论文列表, 并按子类别归纳          │
│                                                          │
│ Step 6: Agent 追加经验到 .agent/skill.md                  │
│         "replay 查询 → 直达 subtopics 文件即可"           │
└─────────────────────────────────────────────────────────┘
总共读取文件: 4 个 | 总 tokens: ~15K | 耗时: 数秒
```

```
场景 B: 审稿 — "这篇关于 vision-language model pruning 的论文,
                帮我找类似工作判断其新颖性"
┌─────────────────────────────────────────────────────────┐
│ Step 1: 读取 skill.md → 判断: 涉及两个主题交叉             │
│                                                          │
│ Step 2: 读取 topics/_topic_index.md                      │
│         → 找到 "model_compression.md" 和                 │
│           "vision_language_models.md"                    │
│                                                          │
│ Step 3: 两个主题文件都不太大 → 全部读入                    │
│         model_compression.md (~200 papers, ~40K tokens)  │
│         vision_language_models.md (~180 papers, ~36K)    │
│         合计 ~76K tokens, 在窗口内                        │
│                                                          │
│ Step 4: 或者用工具脚本做交叉查询                           │
│         python tools/search.py \                         │
│           --query "vision language model pruning" \      │
│           --top 30                                       │
│                                                          │
│ Step 5: 综合分析, 判断新颖性                               │
│                                                          │
│ Step 6: 更新 skill.md: "跨主题查询 → 先读两个主题文件      │
│         或用 search.py 做交叉检索"                        │
└─────────────────────────────────────────────────────────┘
```

```
场景 C: 构思 idea — "2024 年 NeurIPS 上有哪些关于
                      test-time adaptation 的新趋势?"
┌─────────────────────────────────────────────────────────┐
│ Step 1: 读取 skill.md → 涉及特定会议+特定主题              │
│                                                          │
│ Step 2: 读取 topics/test_time_adaptation.md              │
│         → 筛选 venue=NeurIPS, year=2024 的论文            │
│         或                                               │
│ Step 3: 读取 venues/neurips/neurips_2024.md 的标题部分    │
│         → grep "test-time" / "adaptation" / "TTA"        │
│         → 拿到匹配的 paper_id 列表                        │
│         → 读取对应摘要                                    │
│                                                          │
│ Step 4: 分析趋势, 输出洞察                                │
└─────────────────────────────────────────────────────────┘
```

### 2.2 Agent 可调用的工具脚本

```
tools/search.py — Agent 的核心工具
┌──────────────────────────────────────────────────────┐
│ 用法:                                                 │
│                                                       │
│ # 关键词检索 (FTS5, 毫秒级)                            │
│ python tools/search.py \                              │
│   --query "replay continual learning" \               │
│   --top 20 \                                          │
│   --format md                                         │
│                                                       │
│ # 语义检索 (向量, 需 embedding)                         │
│ python tools/search.py \                              │
│   --semantic "methods to prevent catastrophic         │
│               forgetting in neural networks" \        │
│   --top 20                                            │
│                                                       │
│ # 过滤条件                                             │
│ python tools/search.py \                              │
│   --query "diffusion" \                               │
│   --venue CVPR,ICCV \                                 │
│   --year 2023,2024 \                                  │
│   --top 30                                            │
│                                                       │
│ # 输出: 直接打印 Markdown 格式到 stdout                 │
│ # Agent 可直接捕获输出                                  │
└──────────────────────────────────────────────────────┘

tools/get_paper.py — 查看单篇详情
  python tools/get_paper.py --id S2-abc123

tools/list_topics.py — 列出所有主题和论文数
  python tools/list_topics.py

tools/rebuild_cache.py — 重建/更新缓存文件
  python tools/rebuild_cache.py --level topics
  python tools/rebuild_cache.py --level subtopics --topic continual_learning

tools/add_topic.py — 交互式添加新主题分类
  python tools/add_topic.py --name "test_time_training" --keywords "TTT,test-time"
```

---

## 三、缓存生成机制（离线构建）

### 3.1 主题聚类的生成策略

```
┌──────────────────────────────────────────────────────────┐
│               主题缓存生成流水线                             │
│                                                           │
│  方案 A: Embedding 聚类 (全自动)                           │
│  ┌─────────────────────────────────────────────┐         │
│  │ 1. 对所有论文的 title+abstract 编码 embedding  │         │
│  │ 2. 用 HDBSCAN / K-Means 聚类 (~50-100 簇)   │         │
│  │ 3. 用 LLM 为每个簇生成主题名称和描述          │         │
│  │ 4. 输出 topics/{topic_name}.md               │         │
│  └─────────────────────────────────────────────┘         │
│                                                           │
│  方案 B: 预定义主题 + 分类器 (半自动, 推荐)                 │
│  ┌─────────────────────────────────────────────┐         │
│  │ 1. 人工定义 50-80 个主题 + 关键词种子          │         │
│  │    例: "continual_learning":                  │         │
│  │         keywords: [continual, incremental,    │         │
│  │         lifelong, catastrophic forgetting]    │         │
│  │                                               │         │
│  │ 2. 初筛: 关键词匹配 title+abstract            │         │
│  │    → 命中论文归入候选集                        │         │
│  │                                               │         │
│  │ 3. 精筛: 用 LLM 批量验证                      │         │
│  │    "以下论文是否属于 continual learning?"      │         │
│  │    → 去除误匹配                               │         │
│  │                                               │         │
│  │ 4. 一篇论文可属于多个主题 (多标签)             │         │
│  └─────────────────────────────────────────────┘         │
│                                                           │
│  方案 C: LLM 逐篇分类 (最精确但最贵)                       │
│  ┌─────────────────────────────────────────────┐         │
│  │ 对每篇论文, 请 LLM 从预定义主题列表中选择      │         │
│  │ 15万篇 × ~200 input tokens ≈ 3000万 tokens   │         │
│  │ 用 GPT-4o-mini: ~$4.5 | 用 Claude Haiku: ~$3 │         │
│  │ ✔ 成本可控, 质量最高                          │         │
│  └─────────────────────────────────────────────┘         │
│                                                           │
│  ★ 推荐: 方案 B (初筛) + 方案 C (精筛, 仅对候选集)          │
└──────────────────────────────────────────────────────────┘
```

### 3.2 子主题的生成（L4 层级）

```
针对每个 L3 主题 (如 continual_learning, 312 篇):

Step 1: 将该主题所有论文的标题列表交给 LLM
        Prompt: "以下是 312 篇 continual learning 论文,
                 请将它们分为 5-10 个子类别,
                 给出类别名称、描述和所属论文ID"

Step 2: LLM 返回子主题划分方案

Step 3: 生成 subtopics/continual_learning/ 下的各子文件

Step 4: 生成 _overview.md 导航文件

成本: 312 篇的标题 ≈ 6000 tokens input → 几乎免费
      对所有 ~60 个主题执行 → 总计 < $1
```

### 3.3 缓存更新策略

```
触发条件              处理逻辑
─────────           ────────
新增论文入库          → 重新运行分类 → 追加到相关主题文件末尾
                     → 更新 master_index.md
                     → 更新 stats.md

用户手动请求          → python tools/rebuild_cache.py --level all
"新增一个主题"

Agent 发现缺失主题    → Agent 调用 tools/add_topic.py
                     → 自动创建新主题文件
                     → 记录到 skill.md
```

---

## 四、备份与经验积累系统

### 4.1 Git 驱动的备份方案

```
paper-vault/               ← 整个目录作为 Git 仓库
├── .gitignore
│     data/vectors/         # 向量库文件大, 排除
│     data/papers.db        # SQLite 可选排除 (有 JSON 备份)
│
├── data/raw/**/*.json      ✅ 纳入版本控制 (原始数据, 可重建一切)
├── index/**                ✅ 纳入版本控制
├── venues/**               ✅ 纳入版本控制
├── topics/**               ✅ 纳入版本控制
├── subtopics/**            ✅ 纳入版本控制
├── .agent/**               ✅ 纳入版本控制 (经验文件很重要)
└── tools/**                ✅ 纳入版本控制

备份策略:
  本地 Git commit → 推送至 GitHub Private Repo (或 NAS)
  周期: 每次数据更新后自动 commit + push

恢复能力:
  从 Git 克隆 → 运行 rebuild_cache.py → 重建 SQLite + 向量库
  原始 JSON 是 "single source of truth"
```

### 4.2 `skill.md` 经验积累机制

```markdown
# Paper Vault — Agent 经验积累

## 查询策略库

### 已验证的高效路径
| 查询类型 | 最佳路径 | 备注 |
|---|---|---|
| 单一主题查询 | topics/{topic}.md 直读 | 300篇以下可全量读入 |
| 子方向查询 | subtopics/{topic}/{sub}.md | 50篇以下, 最高效 |
| 跨主题交叉 | search.py --query + --venue | 比读两个大文件更快 |
| 特定会议浏览 | venues/ + grep 标题 | 不要全量读入 venue 文件 |
| 模糊/探索性 | search.py --semantic | 语义检索效果好 |

### 主题别名映射
| 用户可能说的 | 实际主题文件 |
|---|---|
| "增量学习" | continual_learning |
| "知识蒸馏" | knowledge_distillation |
| "大模型对齐" | large_language_models/ → alignment_and_rlhf |
| "图像生成" | diffusion_models + generative_adversarial_networks |
| "小样本学习" | few_shot_learning |
}
```

## 敏捷迭代策略
Phase 1 (MVP): 先用 3 个会议 (NeurIPS, ICML, ICLR) × 6 年 (2021-2026) 跑通全流程
Phase 2: 扩展到全部 16 会议 × 5 年
Phase 3: 优化和完善

验证标准: "Continual Learning > Replay Methods" 的端到端查询
