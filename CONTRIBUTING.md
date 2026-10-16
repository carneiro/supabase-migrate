# Contributing to supabase-migrate

Thanks for helping make Supabase project migrations more reliable. Bug reports, documentation improvements, compatibility fixes, and tested resource adapters are welcome. Keep code, documentation, issues, and pull requests in English.

## Scope

The project has one primary command: `./migrate`. Planning, comparison, and recovery support that migration workflow. Keep behavior generic across applications: do not add organization names, hard-coded project references, business-specific table lists, or custom data transformations. Continuous replication and incremental update application are outside the current scope.

Discuss substantial changes in an issue before implementing them. For bugs, include reproducible steps, expected/actual behavior, operating system, Python/PostgreSQL/Supabase CLI versions, and a minimal synthetic fixture. Never attach real credentials, database dumps, function bundles, or private run directories. Report vulnerabilities using [SECURITY.md](SECURITY.md).

## Local setup

Fork and clone the repository, then create a branch. Python 3.10+ and Bash are sufficient for unit tests; there are no third-party Python dependencies to install.

```bash
python3 -m unittest discover -s tests -v
bash -n migrate
./migrate --help
```

Unit tests mock database/API operations and need no Supabase credentials. CI runs them on Python 3.10, 3.12, and 3.13 and includes a separate PostgreSQL integration job.

## Architecture

| File | Responsibility |
| --- | --- |
| `migrate`, `supamigrate/__main__.py` | Shell entry point and sanitized error handling. |
| `supamigrate/core.py` | HTTP, subprocesses, project validation, private JSON, and hashes. |
| `supamigrate/database.py` | PostgreSQL inventory, snapshots, dumps, and transactional restore. |
| `supamigrate/services.py` | Storage, Edge Functions, and Management API configuration. |
| `supamigrate/config_contracts.json` | Writable configuration shapes derived from Supabase OpenAPI. |
| `supamigrate/runner.py` | Preflight, confirmation, backup, orchestration, and recovery. |
| `supamigrate/reporting.py` | Private, value-free resource reports and comparisons. |

Use the standard library where practical. Preserve existing JSON keys/state names unless a format change is explicitly documented. When updating configuration contracts, cite the upstream OpenAPI revision and test filtering of read-only and sensitive fields.

## Safety expectations

- Keep all source connections read-only and credentials out of command arguments and public errors.
- Keep target confirmation, preflight blockers, backup verification, and recovery paths intact.
- Do not remove a blocker merely to make a migration pass. A new resource adapter needs capture, restore, verification, recovery, and failure tests.
- Do not claim global atomicity or successful verification for unobserved resources.
- Never invoke migrated Functions automatically; they can have external effects.
- Use synthetic data and generic identifiers in examples and tests.

## PostgreSQL integration tests

Use an isolated, disposable PostgreSQL **17** server listening on `127.0.0.1:5432`, with matching client tools on `PATH`. The fixture uses user `postgres` and password `migration-test-only`; it creates `supabase_auth_admin`, `migration_fixture_source`, and `migration_fixture_target`. Start with a fresh cluster and do not use a local application database server. Never point these tests at production.

```bash
MIGRATE_TEST_POSTGRES=1 python3 -m unittest discover -s tests -p test_postgres_integration.py -v
```

The tests exercise source write protection, shared snapshots, Auth foreign keys, RLS, Storage ownership, extension handling, internal Realtime preservation, and rollback. They are skipped unless explicitly enabled. See `.github/workflows/tests.yml` for the disposable CI setup. These fixtures do not replace hosted Supabase validation; follow [docs/VALIDATION.md](docs/VALIDATION.md) for changes to platform behavior.

## Pull requests

Explain the problem, resulting behavior, relevant limitations, and validation performed. Include regression tests for behavior or safety changes; documentation-only changes do not need new tests. Update help text and documentation when changing flags or coverage. Keep each pull request focused and ensure existing tests pass.

By contributing, you agree that your contributions are licensed under the repository's [MIT License](LICENSE). You must have the right to contribute the submitted work. Follow our [Code of Conduct](CODE_OF_CONDUCT.md).
