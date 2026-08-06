# Collector and gate reference

## Registry

| Venues | Production module | Primary provider |
|---|---|---|
| AAAI | `tools.aaai_collect` | AAAI/OpenAlex |
| ACL | `tools.acl_collect` | ACL Anthology |
| AISTATS, ICML | venue-specific modules | PMLR |
| CVPR, ICCV, ECCV | `tools.cvpr_collect` | CVF/ECVA |
| IJCAI | `tools.ijcai_collect` | IJCAI |
| KDD, TPAMI | venue-specific modules | DBLP/OpenAlex |
| ICDE, VLDB, SIGIR, ACMMM, WWW | `tools.dblp_expand_collect` | DBLP/OpenAlex |
| ICLR, NeurIPS | `janussearch.collectors.generic` | OpenReview |

Always let `tools.corpus plan` resolve the actual module. Do not call implementation files from a Skill path.

## Gates

Hard failures:

- duplicate normalized titles;
- authors coverage below the configured threshold;
- abstract coverage below the configured threshold;
- invalid JSON/schema or failed staging operation.

Warnings by default:

- official paper-count mismatch;
- official track-count mismatch;
- official presentation-level mismatch.

`--strict-official-alignment` promotes all three warning categories to hard failures.

## Recovery

Keep collected snapshots and reports under the run directory.

- If preparation or staging validation fails, preserve the snapshot, staging tree, reports, and manifest; do not publish canonical JSON. Fix the source or transformation and rerun preparation/validation from the same snapshot when practical.
- If catalog build or validation fails after canonical publication, freeze the batch and report the partial state accurately: canonical JSON is already published, while the previous SQLite catalog remains available because catalog replacement is atomic. Do not claim that canonical publication was rolled back.
