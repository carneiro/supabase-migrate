# Validation with disposable Supabase projects

A complete migration has not yet been validated against two hosted Supabase projects. The PostgreSQL CI fixture tests isolated restore and rollback behavior; it does not establish compatibility with all remote resources or permissions.

Use two disposable projects without production data, with the same PostgreSQL major version, extension versions, and internal Auth/Storage versions. Check target storage quotas, upload size limits, and Function limits.

1. Create source tables with circular foreign keys, partitions, sequences/identity columns, types, indexes, views, restrictive/permissive policies, SQL functions, and triggers. Include identifiers with spaces and quotes.
2. Create test Auth users and identities. Configure the same test provider and register the target callback. Include application data referencing `auth.users`.
3. Create public/private buckets, empty and binary files, Unicode paths and spaces, UUID/text owners, MIME restrictions, and bucket size limits.
4. Enable Realtime on selected tables. Deploy Functions with and without `verify_jwt`, a distinct display name, and a dependency on a manually configured secret.
5. Put different data in the target. Isolate it from clients, writes, and jobs. Keep an independent backup.
6. Run `./migrate --plan`. Check inventory, estimates, limitations, and blockers before proceeding.
7. Run `./migrate --diff` to record the initial state, then `./migrate`. Inspect both final reports, distinguishing verified snapshot content from later source writes. Validate password login, UUIDs, hashes/counts, RLS with different users, memberships/ACLs, Realtime, authorized/denied downloads, and file ownership.
8. Configure target secrets manually through the Dashboard/CLI. Invoke Functions with appropriate authentication and check their behavior. Invocation is not automated because Functions may cause external effects.
9. In a separate run, inject an API failure after cleanup. Verify a nonzero exit code, recovery of previous target data/files/Functions/configuration, and `failed_recovered` state.
10. Test abrupt interruption and `./migrate --recover <run-directory>`. Verify that corrupted backups are rejected and failed recovery retains `recovery_failed` state.
11. Change a Storage object during capture: migration must stop before cleanup. Change target data during backup: migration must also stop.
12. Exercise blockers: identical project references, a database URL for another project, port 6543, incompatible versions/extensions, populated Vault, pending multipart uploads, SSO, Cron, import maps, and custom LOGIN roles.

Do not remove a blocker merely to make migration pass. Add the corresponding adapter and validate capture/restore/verify/recover in both directions.

Technical completion requires an intact backup and verification of supported resources. Application cutover also requires secrets, callbacks, URLs, and functional tests. A completed copy is not an atomic application cutover.

Record tool versions, synthetic fixtures, results, and unresolved limitations in the pull request or release notes. Do not attach private artifacts or credentials.
