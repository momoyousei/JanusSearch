# Query output and export contract

## Keyword file

Write `keywords.json` as:

```json
{
  "query": "original query",
  "candidate_queries": ["narrow FTS query A", "narrow FTS query B"],
  "required_labels": ["Mandatory concept A", "Mandatory concept B"],
  "keywords": [
    {
      "label": "Continual Learning / 持续学习",
      "aliases": ["continual learning", "class-incremental learning", "CIL", "持续学习"]
    }
  ]
}
```

Omit `candidate_queries` and `required_labels` for a simple query. For a complex intersection:

- `candidate_queries` are executed in order with identical venue/year/track filters;
- candidates are unioned by `paper_id` in first-seen order;
- every `required_labels` entry must exactly match a keyword-group label;
- export keeps only records with at least one case-insensitive alias hit from every required label across title, abstract, and paper keywords.

This makes a narrow-query union reproducible without labeling unrelated union members as intersection results.

Generate `query_slug` by lowercasing, replacing non-alphanumeric runs with `_`, trimming `_`, and using `query` when empty.

## Conversation result

Use these headings:

1. `匹配的关键词`
2. `JanusSearch 中的相关工作（按主题）`
3. `导出（当 total > 20）` only when an export exists

Render each paper as `paper_id · title · VENUE YEAR`. Show no more than 20 rows total and 8 per keyword group. Order groups by keyword priority and place `Other` last.

For exports report `total`, `exported`, the absolute TSV path, and the absolute `keywords.json` path. State whether ranking used FTS or hybrid.

## PDF result

Report downloaded, skipped-existing, and failed counts plus absolute paths for the PDF directory, report JSON, and `failed.tsv`. Show at most five failures as `paper_id · title · error`.
