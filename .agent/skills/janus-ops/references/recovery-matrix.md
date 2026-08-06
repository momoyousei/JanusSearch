# Operations recovery matrix

## Dependency order

```text
data/raw -> data/papers.db + FTS -> Chroma vectors -> topics -> markdown caches -> evaluation
```

Repair from left to right. A changed upstream fingerprint invalidates downstream evaluation status.

## Evidence paths

| Capability | Primary report |
|---|---|
| Catalog | `artifacts/m2/validate_report.json` |
| Projections | `artifacts/m3/validate_report.json` |
| Evaluation | `artifacts/evaluate/eval_report.json` |
| Run audit | `artifacts/runs/<run_id>/manifest.json` |

Legacy `artifacts/m4/eval_report.json` may remain for compatibility, but a legacy PASS without current input fingerprints is stale by definition.

## Failure ordering

1. Preserve the exact error and command.
2. Run the relevant doctor profile.
3. Identify the earliest failed dependency.
4. Apply only the explicit minimal repair.
5. Validate that dependency and every affected downstream layer.
6. Run offline evaluation; run online evaluation only when explicitly requested.

Do not delete Chroma as a first response. Prefer its incremental ID/fingerprint behavior and rerun the failed command. Destructive rebuilds require explicit scope and evidence.

