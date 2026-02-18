---
name: paper-search
description: Search and export paper metadata for a venue-year target into one JSON file. Use when a user asks for all papers from conferences like AAAI/CVPR/NeurIPS, and needs title, authors, institutions, abstract, keywords, presentation level, and track annotations with reconciliation.
---

# Paper Search

## When to use

Use this skill when the user asks for:

- "抓取某个会议某一年的全部论文"
- "导出成 JSON，包含题目、作者、机构、摘要、关键词"
- "对账官网列表，补齐缺失条目"
- "给每篇论文标注 track（main/conference、datasets、position、journal 等）"
- "批量跑多个年份（例如 NeurIPS-21 到 NeurIPS-25）"

Core script:

- `paper-search/scripts/fetch_conference_papers.py`

## Input and output contract

Input:

- Required target token: `VENUE-YY` or `VENUE-YYYY`
- Example: `AAAI-26`, `CVPR-2025`, `NeurIPS-25`

Output:

- One JSON file (do not split by track)
- Per-paper fields include:
  - `paper_title`
  - `authors`
  - `institutions`
  - `abstract`
  - `keywords`
  - `presentation_level` (`poster` / `oral` / `bestpaper`)
  - `track` (normalized slug)
  - `track_display_name` (human-readable)
  - `track_group` (`main` or `other`)

Top-level fields include:

- `query`, `source`, `generated_at_utc`, `paper_count`
- `track_counts`, `track_group_counts`
- `papers`
- Optional `reconciliation` (when `--reconcile-url` used)
- Optional `official_tracks` (NeurIPS official mapping metadata)

## End-to-end pipeline

1. Parse target
- Parse `VENUE-YY` into `venue_code + year` (for example `NeurIPS-25 -> NEURIPS + 2025`).
- Validate year range and token format.

2. Load presentation overrides (optional)
- If `--overrides` is provided, read title-level overrides to set `presentation_level`.

3. Preload official NeurIPS track index (NeurIPS only)
- Fetch `https://neurips.cc/static/virtual/data/neurips-{year}-orals-posters.json`.
- Build title -> track mapping from official `sourceurl`.
- Track examples:
  - `conference`
  - `datasets_and_benchmarks_track`
  - `position_paper_track`
  - journal tracks (`journal_track_jmlr`, `journal_track_tmlr`, `journal_track_annals_of_statistics`, etc.)

4. Fetch provider data
- `provider=openalex`: OpenAlex only.
- `provider=openreview`: OpenReview only (accepted-paper logic).
- `provider=auto`: OpenAlex first, fallback/switch to OpenReview when count is low.

5. Normalize provider records
- Normalize authors/institutions and deduplicate while preserving order.
- Rebuild abstract from OpenAlex `abstract_inverted_index`.
- Extract keywords from `keywords`, fallback to ranked concepts.
- Normalize track fields via unified setter:
  - `track`
  - `track_display_name`
  - `track_group`

6. Reconcile against external checklist (optional)
- With `--reconcile-url`, parse external paper list (typically NeurIPS virtual `papers.html`).
- Noise cleaning:
  - remove navigation and non-paper anchors
  - filter non-paper categories like `session` / `town-hall`
- Compare normalized titles:
  - `matched`
  - `missing_in_provider`
  - `extra_in_provider`
- With `--reconcile-include-missing`, append placeholders for missing titles.

7. Apply official NeurIPS track mapping (if loaded)
- Match by normalized title and overwrite provider/default track with official track.
- Attach `official_track_source_url` per matched paper.
- If official catalog has `conference`, remap residual legacy `main` -> `conference`.

8. Finalize and export
- Sort papers by normalized title.
- Build `track_counts` and `track_group_counts`.
- Write single JSON output.

## Track naming rules

NeurIPS uses official tracks first, then fallback heuristics only if unmatched.

- `track` is the machine-readable identifier (slug).
- `track_display_name` is for humans.
- `track_group` is coarse classification:
  - `main`: conference/main body
  - `other`: datasets, position, journal, challenge, workshop-like tracks

## Commands

```bash
python3 paper-search/scripts/fetch_conference_papers.py CVPR-26 \
  --output CVPR-26.json \
  --api-key "$OPENALEX_API_KEY"

python3 paper-search/scripts/fetch_conference_papers.py NeurIPS-25 \
  --output NeurIPS-25.json \
  --provider openreview \
  --reconcile-url https://neurips.cc/virtual/2025/papers.html \
  --reconcile-include-missing

python3 paper-search/scripts/fetch_conference_papers.py NeurIPS-25 \
  --output NeurIPS-25.json \
  --provider auto \
  --api-key "$OPENALEX_API_KEY" \
  --no-progress
```

Batch rerun NeurIPS 2021-2025:

```bash
for y in 21 22 23 24 25; do
  python3 paper-search/scripts/fetch_conference_papers.py "NeurIPS-$y" \
    --output "NeurIPS-$y.json" \
    --provider openreview \
    --reconcile-url "https://neurips.cc/virtual/20${y}/papers.html" \
    --reconcile-include-missing
done
```

Use `--source-id` to force a specific OpenAlex source when venue name matching is ambiguous:

```bash
python3 paper-search/scripts/fetch_conference_papers.py CVPR-26 \
  --source-id https://openalex.org/S4306400393 \
  --source-name "IEEE/CVF Conference on Computer Vision and Pattern Recognition" \
  --api-key "$OPENALEX_API_KEY" \
  --output cvpr26.json
```

## Data quality details

- OpenReview acceptance counts can be lower than official acceptance announcements.
- Reconciliation can still show `missing` after cleanup when sources differ in inclusion policy.
- Official NeurIPS track index may include items not returned by OpenReview.
- External reconciliation parser is heuristic and may require future category cleanup as website structure changes.

## Operational rules

- Treat `paper_title`, `authors`, and `abstract` as primary fields from OpenAlex metadata.
- De-duplicate institutions while preserving order of first appearance.
- Derive keywords from OpenAlex `keywords`; fallback to top concepts when needed.
- When OpenAlex count is unexpectedly low for supported venues, use OpenReview fallback (`--provider auto`).
- Use `--provider openreview` to force OpenReview accepted-paper retrieval for venues with known OpenReview IDs.
- Use `--reconcile-url` for external checklist reconciliation.
- Add `--reconcile-include-missing` to append missing titles as placeholder records.
- Default `presentation_level` to `poster`; override to `oral` or `bestpaper` through a JSON overrides file.
- Keep unresolved fields as empty values instead of fabricating content.

## Resources

### scripts/

- `scripts/fetch_conference_papers.py`: Fetch venue-year papers and export normalized JSON.

### references/

- `references/presentation_overrides_template.json`: Template for manual oral/bestpaper overrides.
- `references/presentation_overrides.json`: Optional manual overrides data.
