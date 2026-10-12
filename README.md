# agentic-sql: Claude Code skill for safe, library-first SQL on production databases

[![Claude Code skill](https://img.shields.io/badge/Claude_Code-skill-D97757)](https://docs.claude.com/en/docs/claude-code/skills)
[![License: MIT](https://img.shields.io/github/license/23blocks-OS/agentic-sql)](LICENSE)

**agentic-sql is an agent skill (`SKILL.md`) that lets Claude Code run SQL against production or staging Postgres safely.** It checks a saved query library first, sends only read-only queries through a three-layer SQL gate, and saves useful findings back to the library before the session ends.

It works with any Postgres and any framework you investigate through `psql`. It is for data lookups, support-ticket investigations and data audits. It is not for sandbox databases, schema migrations or code that only builds queries.

## What it does

- **Library first.** `find` searches your saved queries before any new SQL is written, so the same query is not re-derived for every ticket.
- **One SQL gate, three layers.** A read-only Postgres role, a parser check (SQLGlot: one statement, `SELECT` only, whole tree checked, function allow-list), and a `LIMIT` added when the query has none.
- **Ships its own wrapper.** `scripts/safe_psql.py` (launched as `safe-psql`) applies the gate, binds parameters on the server and refuses to run as a role that could write.
- **Schema doc as source of truth.** Tables, join keys and gotchas live in a schema doc the agent reads before writing a query.
- **Curation is a workflow step.** `curate` runs before the final report, so reusable queries are saved instead of lost in terminal history.

## Install

### Option 1: through the AI Maestro plugin

The skill is part of the `ai-maestro` plugin from [ai-maestro-plugins](https://github.com/23blocks-OS/ai-maestro-plugins):

```bash
claude plugin marketplace add 23blocks-OS/ai-maestro-plugins
claude plugin install ai-maestro@ai-maestro-plugins
```

AI Maestro's installer and updater also put `safe-psql` on your PATH, in a private Python environment with the packages from `requirements.txt`. Claude Code namespaces plugin skills, so the command may appear as `/ai-maestro:agentic-sql`.

### Option 2: standalone

```bash
git clone https://github.com/23blocks-OS/agentic-sql.git ~/.claude/skills/agentic-sql
python3 -m venv ~/.aimaestro/venvs/agentic-sql
~/.aimaestro/venvs/agentic-sql/bin/pip install -r ~/.claude/skills/agentic-sql/requirements.txt
```

Without AI Maestro there is no `safe-psql` launcher. Wherever the skill says `safe-psql`, run `~/.aimaestro/venvs/agentic-sql/bin/python ~/.claude/skills/agentic-sql/scripts/safe_psql.py`, or put a small script with that command on your PATH. If the wrapper cannot run, the skill stops instead of falling back to raw `psql`.

You also need a Postgres role with `SELECT` and `USAGE` only (the wrapper refuses superusers and other roles that could write). Role setup is in [reference/run.md](reference/run.md).

## Usage

```
/agentic-sql audit                       # once, to check library and schema doc paths
/agentic-sql find "customer by email"    # always first
/agentic-sql run Q12                     # run a saved query through the gate
/agentic-sql curate                      # before ending the session
```

Under the hood, `run` calls the wrapper:

```bash
safe-psql --env ACME_DATABASE_URL --query-id Q12 \
  --sql "SELECT id, email FROM users WHERE id = :id" --param id=7
safe-psql --check-only --sql "..."       # gate only, no database
```

## Sub-commands

| Command | What it does |
|---|---|
| `/agentic-sql find <topic>` | Search the library index for queries matching `<topic>`. Returns ranked matches with their purpose, schema references and parameters. **Always run this first.** |
| `/agentic-sql audit` | Health-check the library: count queries, flag orphan `.sql` files not in the index, flag queries with no schema reference, list queries last touched more than 90 days ago. |
| `/agentic-sql add` | Guided workflow for saving a new query: prompts for purpose, business case, environment, schema reference and parameters; formats the header; saves to `{QUERY_DIR}` as `kebab-case-name.sql`; updates `{INDEX_PATH}`. |
| `/agentic-sql run <query-id-or-path>` | Execute a saved query (by ID like `Q12`) or a freshly written one through `{SAFE_PSQL_WRAPPER}`, which runs the SQL gate, injects `LIMIT` and connects as `{READ_ONLY_ROLE}`. |
| `/agentic-sql curate` | Post-investigation review. Surface any SQL run this session that was not from the library, decide which is reusable, promote it via `add`. **Run before ending the conversation.** |
| `/agentic-sql schema <table-or-section>` | View or update `{SCHEMA_DOC_PATH}` for a table or section. When a new query touches an undocumented table, run this first, then add the query. |

## How it works and why it is safe

The gate has three layers, and all are required:

1. **Read-only role.** The agent connects as a Postgres role with only `SELECT` and `USAGE`, with timeouts set on the role.
2. **Parser check.** Exactly one statement, root `SELECT`, the whole syntax tree checked, functions limited to an allow-list. The statement sent to the database is regenerated from the checked tree, not your original text.
3. **Row limit.** `LIMIT {MAX_ROWS}` is added when the query has none, one statement per round trip, a fresh connection per query.

Read-only transactions alone have been bypassed: Anthropic's archived reference Postgres MCP server accepted multiple statements, so `COMMIT; <write>` in one string ended the read-only transaction ([Datadog Security Labs, 2025-08-21](https://securitylabs.datadoghq.com/articles/mcp-vulnerability-case-study-SQL-injection-in-the-postgresql-mcp-server/)). The wrapper has a test suite, including that trick, writes hidden in `WITH`, `SELECT INTO`, `COPY` and dangerous functions in many query positions. Gate rules and role setup: [reference/run.md](reference/run.md).

Query results are treated as untrusted data. The skill does not write result rows or personal data to files or logs, and keeps connection strings in environment variables or a secret store.

## First-time setup

On first invocation, the skill auto-extracts these from `CLAUDE.md` if present and asks for any missing values:

| Variable | Purpose | Default if missing |
|---|---|---|
| `{QUERY_DIR}` | Where saved queries live (relative to project root) | `sql_library/queries/` |
| `{INDEX_PATH}` | Markdown index of the library | `sql_library/INDEX.md` |
| `{SCHEMA_DOC_PATH}` | Schema reference doc | `sql_library/SCHEMA.md` |
| `{DB_ENVS}` | Named environments and their connection variables | (asks) |
| `{READ_ONLY_ROLE}` | Postgres role with SELECT-only grants | `agentic_sql_ro` |
| `{SAFE_PSQL_WRAPPER}` | Wrapper that applies the SQL gate; ships with the skill | `safe-psql` (runs `scripts/safe_psql.py`) |
| `{MAX_ROWS}` | Default LIMIT injected when query has none | `1000` |
| `{STATEMENT_TIMEOUT}` | `statement_timeout` set on the read-only role | `30s` |
| `{DISPLAY_ROWS}` | Rows shown before offering more | `50` |

Values persist to `.claude/agentic-sql.yml` for the session.

## What matters

1. **The library is a search corpus, not a folder.** If `find` does not surface the right query, the library failed even if the query exists.
2. **The schema doc is the source of truth, not the live DB schema.** `\d table` gives columns but no gotchas, no join keys, no "this counter drifts under condition X" notes.
3. **The SQL gate is non-negotiable.** All three layers, every time.
4. **Curation runs before the final report.** Cemri et al. ([arxiv 2503.13657](https://arxiv.org/pdf/2503.13657)) call "diffusion of responsibility" a leading multi-agent failure: if curation is not an explicit step, it becomes nobody's job. The skill makes it a step.
5. **No field fallbacks.** Never use one column as a substitute for another inside a query. A NULL in the right field is a data finding to surface, not paper over.

## What not to do

- Do not use this skill for application code changes. It is an investigation tool.
- Do not use it for destructive operations (UPDATE, DELETE, INSERT). Those belong in `scripts/` as explicit one-off scripts with dry-run gating.
- Do not skip `find` because you already know the query.
- Do not save a query without a schema reference line in the header.
- Do not defer curation to the end of the week. The session ends, the context dies, the queries do not get saved.

## Query categories the library should cover

These are the categories that tend to recur in real production work. Use them as the taxonomy when seeding a fresh library.

| Category | Example queries | Why it's worth saving |
|---|---|---|
| **Entity lookup** | "Find project / user / order by name pattern" | First step of every support ticket |
| **Identity reconciliation** | Cross-block identity joins (e.g. talent in tenant DB vs. search index vs. auth provider) | Catches drift between microservice copies |
| **Audit (read-only)** | Whitespace pollution, duplicate emails, forged document numbers, denormalized counter drift | Health checks before / after cleanups |
| **Cross-block joins** | When data spans multiple databases / tenants and the join key is `api_access_key` or similar | Pre-derived join paths avoid hallucinating columns |
| **Aggregation by time bucket** | Daily failure-rate breakdowns, regression-onset detection | Standard shape for "when did X start" investigations |
| **Magic-link / token resolution** | Resolve a customer's broken link back to who issued it, when, where | Recurring support ticket shape |
| **Test-fixture selection** | Find a test account / project / PCU with a specific shape for E2E tests | QA team asks for these repeatedly |
| **Collision / deduplication triage** | "All rows sharing canonical email/document with activity per row" | Required BEFORE any cleanup that might merge |

## Run the tests

```bash
pip install -r requirements.txt pytest
pytest tests
```

The end-to-end tests start a throwaway local Postgres and are skipped when none is installed.

## File structure

```
agentic-sql/
├── SKILL.md                # skill manifest (YAML frontmatter + workflow)
├── README.md               # this file
├── CHANGELOG.md
├── LICENSE                 # MIT
├── requirements.txt        # sqlglot, psycopg
├── scripts/safe_psql.py    # the read-only SQL gate wrapper
├── tests/test_safe_psql.py
└── reference/              # one playbook per sub-command
    ├── audit.md  find.md  add.md  run.md  curate.md  schema.md
```

## Works with

- Postgres (the wrapper uses SQLGlot's PostgreSQL dialect and `psycopg`)
- Any framework (Rails, Django, Node.js and others); the skill is the investigation discipline, not application code
- Multi-database and multi-tenant setups, built for shared-cluster microservice architectures (the 23blocks platform was the original context)

## Part of the AI Maestro ecosystem

agentic-sql ships in the `ai-maestro` plugin for [AI Maestro](https://ai-maestro.23blocks.com), a dashboard and orchestrator for fleets of AI coding agents ([source on GitHub](https://github.com/23blocks-OS/ai-maestro)). It also works on its own in any Claude Code setup.

## Related skills

- [agentic-support](https://github.com/23blocks-OS/agentic-support): numbered support cases and runbooks. Its runbooks hand database lookups to agentic-sql.
- [agentic-seo](https://github.com/23blocks-OS/agentic-seo): SEO audit and implementation for websites.

## Changelog

See [CHANGELOG.md](CHANGELOG.md). Contributions are welcome; see [CONTRIBUTING.md](.github/CONTRIBUTING.md).

## References

- [Vanna.AI](https://vanna.ai/) — saved-query RAG pattern
- [AutoLink (arxiv 2511.17190)](https://arxiv.org/pdf/2511.17190) — iterative schema linking
- [LinkAlign (arxiv 2503.18596)](https://arxiv.org/pdf/2503.18596) — multi-database schema retrieval
- [SQLGlot](https://github.com/tobymao/sqlglot) — SQL parser / AST library used by the safety wrapper
- [LangChain SQL Toolkit](https://docs.langchain.com/oss/javascript/langchain/sql-agent) — two-tier safety layer pattern
- [Cemri et al., "Why Do Multi-Agent LLM Systems Fail?" (arxiv 2503.13657)](https://arxiv.org/pdf/2503.13657) — multi-agent failure mode taxonomy
- [Anthropic — Writing Tools for Agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [Anthropic — Equipping Agents with Skills](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)

## License

MIT. See [LICENSE](LICENSE).

---

Built by [23blocks](https://23blocks.com).
