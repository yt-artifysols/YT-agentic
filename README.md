# yt-agentic

Agentic AI control system that produces videos for up to 20 YouTube channels with a mandatory
human approval gate. See `CLAUDE.md` for invariants and commands, `docs/adr/` for decisions and
`docs/threat-model.md` for the threat model.

```bash
make setup      # create .venv, install deps, install pre-commit hooks
make up         # local Postgres (pgvector) + Temporal
make test-all   # lint, typecheck, generated-file and migration checks, tests, DB guard tests
```
