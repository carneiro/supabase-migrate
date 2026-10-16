# Security policy

This project is experimental. Security fixes target the current default branch; there is no maintained release support matrix yet.

## Reporting a vulnerability

Do not disclose credentials, private data, exploitable details, or run artifacts in a public issue. If GitHub private vulnerability reporting is enabled for this repository, use **Security → Report a vulnerability**. If that option is unavailable, open a minimal issue asking maintainers for a private reporting channel without disclosing vulnerability details. No response-time guarantee is currently offered.

In a private report, include affected versions, impact, minimal reproduction steps using synthetic data, and any suggested mitigation. Never send live tokens, database passwords, or production backups.

## Handling migration artifacts

The tool has administrative access to both projects and replaces supported target resources. Run it only from a trusted machine. Prefer hidden credential prompts or a secret manager; do not place credentials in shell history, issues, or commits.

`.migrate/` contains unencrypted backups, user data, password hashes, configuration values, and function bundles. Private filesystem permissions are not encryption. Use protected disks, restrict access, and delete artifacts according to your retention policy after validating migration and recovery needs.

Final reports omit data/configuration values but still reveal project references, resource names, secret names, and counts. Review them before sharing. Do not upload an entire run directory for troubleshooting.

If credentials or sensitive artifacts have already been published, removing them from the current tree is not enough: revoke or rotate exposed credentials and address copies in repository history and other locations before public release.
