# Collector and gate reference

## Registry

| Venues | Production module | Primary provider |
|---|---|---|
| AAAI | `tools.aaai_collect` | AAAI/OpenAlex |
| ACL | `tools.acl_collect` | ACL Anthology |
| AISTATS | venue-specific module | PMLR |
| CVPR, ICCV | `tools.cvpr_collect` | CVF OpenAccess |
| ICLR, ICML, NeurIPS, ECCV | `janussearch.collectors.virtual` | official virtual events + abstracts |
| IJCAI | `tools.ijcai_collect` | IJCAI |
| KDD, TPAMI | venue-specific modules | DBLP/OpenAlex |
| ICDE, SIGIR, ACMMM, WWW | `tools.dblp_expand_collect` | DBLP/OpenAlex |
| VLDB | `tools.pvldb_collect` | PVLDB official page; DBLP identifiers |

Always let `tools.corpus plan` resolve the actual module. Do not call implementation files from a Skill path.

## Collection outcomes

- `collected`: one or more paper JSON files exist and may proceed to prepare.
- `no_update`: the official source is valid but has no released target data; stop successfully before prepare.
- `incomplete_source`: pagination, HTTP, authentication, fingerprint, or completeness checks failed; freeze the venue and keep canonical unchanged.

For 2026, ICLR uses only the Conference source group. ICML may use only the approved pinned snapshot at commit `2cf625b555c51e61086a3b009c59d47e768466cf`, matching its recorded SHA-256 and canonical-subset gate. CVPR 2026 uses final CVF OpenAccess. VLDB 2026 uses PVLDB Volume 19 and excludes Front Matter.

## Gates

Hard failures:

- duplicate normalized titles;
- authors coverage below the configured threshold;
- abstract coverage below the configured threshold;
- invalid JSON/schema or failed staging operation.
- incomplete pagination or source fingerprint mismatch;
- duplicate/ambiguous reconciliation mapping;
- any deletion absent from the versioned approval policy.

Warnings by default:

- official paper-count mismatch;
- official track-count mismatch;
- official presentation-level mismatch.

`--strict-official-alignment` promotes all three warning categories to hard failures.

## Recovery

Keep collected snapshots, `.janus-collection.json`, per-record reconciliation reports, and validation reports under the run directory.

- If preparation or staging validation fails, preserve the snapshot, staging tree, reports, and manifest; do not publish canonical JSON. Fix the source or transformation and rerun preparation/validation from the same snapshot when practical.
- If catalog build or validation fails after canonical publication, freeze the batch and report the partial state accurately: canonical JSON is already published, while the previous SQLite catalog remains available because catalog replacement is atomic. Do not claim that canonical publication was rolled back.
