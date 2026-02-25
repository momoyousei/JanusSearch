# Loss Landscape 检索报告

- 生成时间: 2026-02-25
- 数据库: `data/papers.db`
- 检索口径: `papers_fts MATCH '"loss landscape" OR "loss landscapes"'`
- 过滤: `record_status != 'placeholder'`

## 总览

- 总命中: **332**
- 会议命中: **324**
- 期刊命中: **8**（全部来自 `TPAMI`）
- 命中 venue 数: **15**

## venue 分布（按命中数降序）

| venue | count |
|---|---:|
| NEURIPS | 100 |
| ICLR | 84 |
| ICML | 51 |
| CVPR | 25 |
| AAAI | 17 |
| AISTATS | 12 |
| ICCV | 10 |
| TPAMI | 8 |
| ECCV | 7 |
| ACL | 5 |
| KDD | 5 |
| ACMMM | 3 |
| IJCAI | 3 |
| ICDE | 1 |
| SIGIR | 1 |

## 持续学习相关（在 loss landscape 命中集合中）

- CL 相关总数（宽松口径）: **13**
- 明确 CL 标题信号（严格口径）: **8**

### 严格口径（优先关注）

| year | venue | title |
|---:|---|---|
| 2025 | AAAI | Enhancing Robustness in Incremental Learning with Adversarial Training |
| 2025 | TPAMI | Revisiting Flatness-Aware Optimization in Continual Learning With Orthogonal Gradient Projection |
| 2024 | NEURIPS | Make Continual Learning Stronger via C-Flat |
| 2022 | CVPR | Towards Better Plasticity-Stability Trade-Off in Incremental Learning: A Simple Linear Connector |
| 2022 | ECCV | CoSCL: Cooperation of Small Continual Learners Is Stronger than a Big One |
| 2022 | ICLR | Representational Continuity for Unsupervised Continual Learning |
| 2022 | NEURIPS | A simple but strong baseline for online continual learning: Repeated Augmented Rehearsal |
| 2021 | NEURIPS | Flattening Sharpness for Dynamic Gradient Projection Memory Benefits Continual Learning |

### 宽松口径补充（建议二次人工确认）

| year | venue | title |
|---:|---|---|
| 2026 | ICLR | MergeTune: Continued Fine-Tuning of Vision-Language Models |
| 2025 | NEURIPS | Gradient Descent as Loss Landscape Navigation: a Normative Framework for Deriving Learning Rules |
| 2024 | ECCV | Flatness-aware Sequential Learning Generates Resilient Backdoors |
| 2024 | NEURIPS | Normalization and effective learning rates in reinforcement learning |
| 2022 | ICLR | How Well Does Self-Supervised Pre-Training Perform with Streaming Data? |

## 导出文件

- 全量命中（332）: `index/loss_landscape_all.tsv`
- CL 宽松子集（13）: `index/loss_landscape_continual.tsv`
- CL 严格子集（8）: `index/loss_landscape_continual_strict.tsv`
- BM25 Top 50: `index/loss_landscape_top50.tsv`
- venue 统计: `index/loss_landscape_by_venue.tsv`
