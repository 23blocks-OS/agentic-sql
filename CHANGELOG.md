# Changelog

All notable changes to this skill are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.1.0] - 2026-10-10

### Fixed
- Replaced a wrong CVE reference (it was an unrelated LangGraph SQLite checkpoint issue) with the Datadog Security Labs case of the archived Postgres MCP server (2025-08-21).

### Changed
- The parser layer is now a single-statement allow-list over the whole tree: root must be SELECT, write/DDL/COPY/SET/transaction nodes rejected anywhere, function calls limited to a project allow-list.
- Timeouts moved from a `SET` prefix to the read-only role; one statement per round trip, fresh connection per query.
- Added `STATEMENT_TIMEOUT` and `DISPLAY_ROWS` variables; no hard-coded 30s or 50 remain.
- The wrapper is now a stated prerequisite, and `run.md` describes what it must do. Removed the claim of a sibling wrapper repo.
- Saved queries use schema-qualified names instead of `SET search_path`.
- Structure: one term per concept (SQL gate, wrapper, library, INDEX, read-only role), Contents lists in long reference files, no cross-links between reference files, academic citations reduced to one Background line, investigation checklist added.

### Added
- Role hardening: NOSUPERUSER NOBYPASSRLS, CONNECT only on the target database, default privileges, `pg_read_all_data` caveat, no server-file grants.
- "Data handling" section: untrusted results, no personal data in files or logs, secrets via environment or `aim-secret`, name the environment, prefer a read replica.

### Security
- Examples scrubbed of real-looking names, IDs and company names.
