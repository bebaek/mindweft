# Backfill existing personal agents

This runbook backfills the stable `user:personal-assistant` agent and the built-in
`shared:mindweft-user-mcp` capability for existing user execution configurations. The migration
creates a `user:mindweft-user-tools` capability profile and attaches it to the personal assistant.
It is idempotent: users with an explicit `defaults.agent_ref` are not replaced, and rerunning the
command does not create duplicate resources.

## Safety

- Run against the admin SQLite database used by the deployment.
- Obtain `MINDWEFT_ADMIN_ENCRYPTION_KEY` from the deployment secret manager; do not place it in
  the command line, shell history, logs, or this repository.
- Start with `--dry-run` and a single tenant or user canary.
- The command uses the stored config version as an optimistic-concurrency guard. A concurrent
  update is reported as a conflict and is not overwritten.
- Invalid configs are reported and skipped. They require manual remediation.
- The command does not print config contents or credentials.

## Prerequisites

```bash
export MINDWEFT_ADMIN_DB_PATH=/srv/mindweft/.data/admin.db
export MINDWEFT_ADMIN_ENCRYPTION_KEY='loaded from the deployment secret manager'
```

The database must be offline or otherwise protected from SQLite-level operational conflicts while
running the migration. Keep the application available only if the deployment has validated its
SQLite locking and backup procedure.

## Dry run

Run a canary first:

```bash
uv run python scripts/backfill_personal_agents.py \
  --tenant-id tenant-1 \
  --dry-run
```

Then inspect the summary, for example:

```json
{"already_default": 12, "conflicts": 0, "dry_run": true, "invalid": 0, "scanned": 20, "updated": 8}
```

A dry run does not write configs. `updated` is the number of configs that would receive the
personal assistant default.

## Apply

Apply the canary after reviewing the dry-run result:

```bash
uv run python scripts/backfill_personal_agents.py \
  --tenant-id tenant-1 \
  --batch-size 100
```

For a specific user:

```bash
uv run python scripts/backfill_personal_agents.py \
  --tenant-id tenant-1 \
  --user-id user-1
```

After the canary is verified, run without filters in bounded batches:

```bash
uv run python scripts/backfill_personal_agents.py --batch-size 100
```

## Verification

For a migrated user, verify through an authenticated API client:

```bash
curl -H 'Authorization: Bearer …' \
  https://minigent.example/me/execution-config

curl -H 'Authorization: Bearer …' \
  https://minigent.example/me/agents
```

Confirm that `defaults.agent_ref` is `user:personal-assistant`, the personal assistant references
`user:mindweft-user-tools`, and that profile references `shared:mindweft-user-mcp`. A new thread
should expose the `minigent_user_mcp.*` tools and use the personal execution options. Do not paste
authenticated responses into tickets or logs.

## Handling results

- `invalid > 0`: stop and remediate the affected configs before retrying them.
- `conflicts > 0`: investigate active writers, then rerun for the affected tenant/user. The
  command never overwrites a newer version.
- `updated == 0` with `already_default == scanned`: the migration is complete for that scope.
- Non-zero exit status: do not assume completion; retain the JSON summary and investigate.

There is no destructive rollback. The forward fix is to restore the affected config from the
SQLite backup if necessary, or use the normal principal-scoped config update path to remove the
bootstrap agent and reset the default under normal review. Preserve the backup and encryption key
separately until verification is complete.
