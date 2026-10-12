# Contributing to agentic-sql

Thanks for helping. This is a Claude Code skill: most of it is Markdown (`SKILL.md` and `reference/`), plus one Python script, `scripts/safe_psql.py`.

## Proposing a change

1. Open an issue first for anything larger than a typo, so we can agree on the direction.
2. Fork, branch, and keep the change small and focused.
3. Update `CHANGELOG.md`. If the behavior of the skill changes, bump `metadata.version` in `SKILL.md`.
4. Open a pull request that says what changed and why.

## Running the tests

```bash
pip install -r requirements.txt pytest
pytest tests
```

The end-to-end tests start a throwaway local Postgres and are skipped when none is installed. Any change to `scripts/safe_psql.py` needs a test, especially one that tries to get a write past the gate.

## Writing guidelines

- Say what the skill does and when to use it; do not add rules the model already follows.
- Never put real credentials, connection strings or personal data in examples.
