# Fleet configuration

One YAML file per cluster (`clusters/<id>.yaml`) and per channel (`channels/<slug>.yaml`).
Adding a channel is a new file plus `ytctl channel onboard <slug>`; no code changes.

```
config/
  clusters/   <- your real clusters
  channels/   <- your real channels
  examples/   <- annotated samples (loaded by CI so they never rot)
```

- `ytctl channel validate` checks every file and the rules between them (20 channel cap,
  quota arithmetic per Google project, rollout order, no credential-shaped strings).
- `ytctl channel apply [--dry-run]` applies the files to the database, idempotently.
- Brand and policy become knowledge **drafts**; a human approves them in the approval hub.
- A cluster with `requires_cluster` stays dark until that cluster has run live for `gate_days`
  and an admin signed it off (enforced by the database; see docs/runbooks/cluster-rollout.md).
- Secrets never go in these files: credentials are derived vault references
  (`vault://youtube-refresh-token-<slug>`), whose values are entered in Secret Manager.
