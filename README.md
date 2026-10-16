# supabase-migrate

Migrate supported resources from one hosted Supabase project to another with a single command:

```bash
./migrate
```

A standalone, dependency-free Python tool for project-to-project migration, with preflight checks, target backups, snapshot verification, and recovery. It has no application-specific table lists, business rules, or organization configuration. This is an independent community project, not an official Supabase product.

**Status: experimental.** Automated unit tests and a disposable PostgreSQL integration workflow are included. Full end-to-end validation against two hosted Supabase projects is still required. The tool does not promise a complete platform clone or a globally atomic migration.

## Requirements

- Linux, macOS, or WSL with Bash and Python **3.10+**. Native Windows is not supported.
- PostgreSQL client tools (`psql`, `pg_dump`, `pg_restore`) matching the server major version.
- Supabase CLI supporting `functions download/deploy --use-api` and `--project-ref`. The existing implementation was developed against CLI **2.120.0**; required flags are checked at runtime.
- A [Supabase Personal Access Token](https://supabase.com/dashboard/account/tokens) with access to both projects, configuration/key/function read access, and target write access.
- Both database connection URLs, with passwords, from the Dashboard **Connect** panel. Use a direct connection or **Session pooler on port 5432**, not the transaction pooler.
- Local disk space for both snapshots, downloaded files, and verification, on a protected filesystem.

No Python packages or Docker are required to run the tool. Dependencies are not installed automatically. `--plan` and `--diff` only need Python, Bash, and `psql`.

## Quick start

1. Clone this repository and open its directory.
2. Run `./migrate --plan` and enter the source and target project references, then the token and database URLs at hidden prompts.
3. Review the inventory, estimate, limitations, and blockers. Resolve compatibility issues before migrating.
4. Take an independent backup and isolate the target from clients, jobs, and writes.
5. Run `./migrate`, review the plan again, and type `MIGRATE <TARGET_PROJECT_REF>` when prompted.
6. Review the final report, configure manual secrets and OAuth callbacks, and validate the application before switching clients.

**Migration replaces supported content on the target.** The source project is not modified by this tool. There is no `--yes` flag: migration and recovery require an interactive terminal and explicit target confirmation.

You may provide project references as arguments:

```bash
./migrate --plan --source SOURCE_PROJECT_REF --target TARGET_PROJECT_REF
./migrate --source SOURCE_PROJECT_REF --target TARGET_PROJECT_REF
```

References are the 20 lowercase letters in a project's Supabase URL, not display names. Credentials can optionally come from `SUPABASE_ACCESS_TOKEN`, `SOURCE_DB_URL`, and `TARGET_DB_URL`. Use hidden prompts or your secret manager to avoid putting credentials in shell history. The CLI receives the token through its environment, not command arguments. The tool does not run `supabase link` or change another directory's project link.

## Commands

`migrate` is the sole entry point. These options support planning, verification, and recovery of a migration:

```bash
# Inventory and estimate only; no dumps or remote mutations.
./migrate --plan

# Compare current project states, including database checksums; no remote writes.
./migrate --diff

# Estimate using a nominal throughput of 25 MiB/s (default: 10).
./migrate --throughput-mib 25

# Store private artifacts in a directory with permissions 0700.
./migrate --work-dir /path/to/private/migration-runs

# Refuse immediately because global atomicity is unavailable.
./migrate --require-atomic

# Recover a target from a previous run; prompts for RECOVER <TARGET_PROJECT_REF>.
./migrate --recover .migrate/YYYYmmddTHHMMSSZ-run-id

./migrate --help
```

`--plan`, `--diff`, and `--recover` are mutually exclusive. `--diff` reports differences; it does not apply incremental updates or provide continuous replication.

## How migration works

Inventory → estimate → confirmation → target backup → source capture → drift checks → target Functions/Storage cleanup → transactional database restore → configuration → Storage files/owners → Edge Functions → verification.

Cleanup starts only after backups have been checked. Source database connections set `default_transaction_read_only=on`; the source HTTP client only permits GET. Object reads may update platform access telemetry. Neither project is deleted or recreated.

Each database capture uses an exported PostgreSQL snapshot shared by dumps and checksums. The source can remain online, but **writes after the snapshot are not migrated**. Storage has no snapshot shared with PostgreSQL: inventory and object versions are checked again after download, and detected drift aborts the run. This is not CDC or distributed snapshot consistency.

Database snapshot transactions close before file and bundle downloads. Restore creates application schemas first, loads Auth and application data next, and applies constraints/triggers afterwards to preserve references to `auth.users`.

Estimates are heuristic ranges, not an SLA. They account for database size, object count/size, functions, backup, restore, and verification. Checksums read all migratable table rows and consume source I/O. Metadata stays in memory; files stream to disk.

## Coverage and limitations

| Resource | Behavior |
| --- | --- |
| Application schemas, tables, data, indexes, views, sequences, types, SQL functions, triggers, grants, and RLS | Native dump/restore, including owners. Managed schemas are not dropped. |
| CLI migration history (`supabase_migrations`) | Included in the application dump. |
| Auth | Table data, including users, UUIDs, and password hashes; managed schema retained. Incompatible versions or table structures block migration. |
| Auth/Storage customizations | Policies, triggers, ACLs, and RLS flags. Custom functions/tables in these schemas block migration. Managed role default privileges must match. |
| Realtime | Application publications, table membership, and configuration exposed by the Management API. `supabase_realtime` migrates; internal `supabase_realtime_*` publications and managed partitions remain on the target. Active connections/messages are not copied. |
| Standard Storage | Buckets, object bytes, IDs, owners, timestamps, and `user_metadata`. Physical versions/ETags generated on the target are retained. Bytes are verified using SHA-256. |
| Edge Functions | CLI download/deploy, preserving name and `verify_jwt`. Requires `index.ts` and no declared import map. Unsupported bundles block before cleanup. |
| Edge Function secrets | Names only in reports. Values are not exported, created, or deleted. Existing target secrets remain and need review. |
| Auth, SMTP/OAuth, Storage, Realtime, and Data API configuration | Writable fields from the bundled OpenAPI-derived contract. Detected masked values block migration. |
| Custom roles without LOGIN | Attributes and memberships; incompatible, privileged, target-only, or non-exportable password roles block migration. |
| Extensions | Must exist in both projects with matching versions. Platform/extensions are not implicitly replaced. |
| Cron; populated Vault/pgsodium; vector/analytics Storage; pending multipart uploads; SSO/third-party Auth; subscriptions; FDW; large objects; custom event triggers | Block migration and require dedicated adapters and validation. They are not silently skipped. |
| API/JWT keys, project ID/URL, domains/DNS, region, plan, compute, network/SSL, backups/PITR, platform logs, external integrations | Target identity/infrastructure is retained. External configuration is not cloned. |

Ordinary application tables and queues pass through native dump/restore. Extensions with external storage, protected schemas, or special requirements need separate validation; there is no universal extension migration guarantee.

Source JWTs do not become valid on the target just because Auth records were copied. OAuth providers must allow the target callback. Project references in arbitrary SQL, URLs, data, and code are not rewritten automatically. Review them before cutover.

Manual secrets produce the state `completed_pending_manual_secrets`. Functions may not work until secrets are configured. Validate application behavior before directing clients to the target.

## Failure and recovery

Database restore uses `psql --single-transaction -v ON_ERROR_STOP=1`: a failed database transaction does not leave a partial database import. Storage, configuration, and Functions have immediately visible effects and do not share that transaction. The target must remain isolated throughout the operation; operator confirmation cannot guarantee isolation across all services.

After mutation begins, an error triggers an attempt to restore the target backup. Successful recovery still returns an error and records `failed_recovered`. Failed recovery records `recovery_failed`; preserve the backups and use `--recover`. After SIGKILL or a machine failure, recovery is manual. Destructive phases are not blindly resumed. Secret values are never changed by the tool.

A local lock prevents concurrent runs for the same target in the same work directory. It is not distributed: use one operator per target.

## Private artifacts and reports

Runs are saved under `.migrate/<run>/`, ignored by Git, with private directory/file permissions (0700/0600). **Backups and bundles contain application data, password hashes, and possibly configuration credentials. They are not encrypted at rest by this tool.** Use protected storage and a retention policy. Never upload run directories to an issue or pull request. Hash manifests detect accidental corruption, not an attacker who can also rewrite the manifest.

Migration, planning, and comparison produce `final-report.md` and `final-report.json`; reports are also attempted after failures once a run directory exists. Reports include per-resource status, counts, manual secret names, and four comparisons:

- Captured snapshot versus verified target: evidence for the completed copy.
- Current source versus current target: differences observed at the end.
- Snapshot versus current source: changes after capture.
- Previous target versus current target: effects of the operation.

Observations have separate timestamps. Current differences do not automatically invalidate a verified snapshot copy. `--plan` does not calculate row counts/checksums and reports `data_not_checked`. `--diff` reads all migratable rows; schedule it according to application load.

Comparison details omit row values, configuration values, SQL, owners, and object paths. Storage objects use opaque path-derived identifiers. Details are limited to 200 keys per category, with full counts and truncation indicators. Reports still contain project references, schema/resource names, and secret names; review before sharing.

Current Storage comparison checks metadata only; migration verifies bytes with SHA-256. Edge bundle hashes are informational because bundles may embed project IDs. Functions are never invoked automatically.

| Exit code | Meaning |
| --- | --- |
| `0` | Requested operation and final reporting completed. A successful diff can still contain differences; a successful migration may need manual secrets. |
| `1` | Execution failed or was interrupted, even if automatic recovery succeeded. |
| `2` | Argument parsing error, or an otherwise completed operation whose final reporting/comparison failed. |

Incomplete comparisons report `not_collected`. A reporting failure does not roll back a completed migration: inspect `migration_phase` and `migration_completed` to distinguish copy failure from incomplete reporting.

Recovery preserves the original report and writes a new one under `<run>/recovery-<id>/`. It observes only the recovered target because it does not request source credentials. Run `./migrate --diff` separately to compare both projects.

## Development and contributing

```bash
python3 -m unittest discover -s tests -v
bash -n migrate
./migrate --help
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture, tests, and pull request guidance; [VALIDATION.md](docs/VALIDATION.md) for disposable Supabase acceptance testing; and [SECURITY.md](SECURITY.md) for vulnerability reporting and artifact handling. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).

Reference contracts: [official backup/restore guide](https://supabase.com/docs/guides/platform/migrating-within-supabase/backup-restore), [Management API](https://supabase.com/docs/reference/api/introduction), and [OpenAPI source](https://github.com/supabase/supabase/blob/master/apps/docs/spec/api_v1_openapi.json). Supabase APIs evolve; the bundled contract is not a guarantee of compatibility with every project or future API version.

## License

[MIT](LICENSE). Contributions are accepted under the same license.
