# Changelog

All notable changes to this skill are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.3.0] - 2026-10-12

### Added
- **The write door** (`scripts/safe_psql_write.py`, command `safe-psql-write`, `/agentic-sql write`). One `INSERT` or `UPDATE` (`DELETE` only when the settings turn hard delete on; off by default), `WHERE` required, only tables and columns listed in a settings file, a dry run that is rolled back, and a request that a person approves in a real terminal before `apply` runs it once. The approval is tied to the exact statement, expires, and is refused inside an agent session. The log holds no values. Reference: `reference/write.md`; example settings: `examples/write.example.yaml`. Design: AI Maestro backlog F037.
- The approval screen strips control and escape characters from everything an agent can influence (reason, values, rows), names the settings file with its fingerprint, and requests nobody approved in time lose their values (deleted after 7 days).
- 62 tests for the write door, including approval through a pseudo-terminal and runs against a throwaway Postgres.

### Fixed
- **`safe-psql` (read) mangled a literal `%` when the query had no parameters.** The checked statement writes `%` as `%%`, and without a parameter dict the database received `%%`, so `WHERE name = '100%'` compared against `100%%` and matched the wrong rows (`LIKE '%x%'` happened to still work). A parameter dict is now always passed.

### Changed
- `safe-psql` (read) now also refuses a database login that can write any table, including column-level grants, so the read and write logins cannot be mixed up.
- `requirements.txt` gains `pyyaml`.

## [1.2.1] - 2026-10-12

### Changed
- Commands and the prerequisite now use `safe-psql`, a launcher AI Maestro installs on the PATH that runs the wrapper in a private Python environment. The manual setup for use without AI Maestro is documented.

## [1.2.0] - 2026-10-10

### Added
- `scripts/safe_psql.py`, the wrapper the skill relied on, and `requirements.txt` (sqlglot, psycopg 3). One statement per call, SELECT only, the whole tree checked, functions limited to an allow-list, the statement sent is regenerated from the checked tree, row limit enforced, parameters bound by the server, timeouts and read-only set at connect time, and it refuses to run as a role that could write or read server files.
- `tests/test_safe_psql.py`: 109 tests, including the Datadog `COMMIT; ...` trick, writes hidden in `WITH`, `SELECT INTO`, locking clauses, `COPY`, 22 dangerous functions in 15 query positions (also schema-qualified and quoted), and end-to-end runs against a throwaway local Postgres.

### Changed
- The prerequisite now says to install the requirements and use a read-only role, instead of "bring your own wrapper".

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
