# Deprecated model scripts

Quarantined files:

- `archive/deprecated/model_migrations/DEPRECATED_DO_NOT_RUN_update_openai_models.ps1`
- `archive/deprecated/model_migrations/DEPRECATED_DO_NOT_RUN_migrate_to_openai.ps1`

They contain stale daily-review settings or legacy `gpt-5-nano` /
`gpt-5-mini` migration assumptions. Do not execute them in production.

Current routing is classifier `gpt-5.4-nano`, normal/daily
`gpt-5.4-mini`, important `gpt-5.6-luna`, and weekly
`gpt-5.6-terra`. `gpt-5.6-sol` remains manual-only.

Preview the safe targeted updater:

```powershell
.\production\set_current_openai_models.ps1
```

Apply only after review:

```powershell
.\production\set_current_openai_models.ps1 -Apply
```

It changes only the listed model keys and creates an `.env` backup.
Quarantined files may be read for rollback research but must not be run.

