import os, sys, subprocess, shutil, socket, tempfile, time, pathlib
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import safe_psql as sp  # noqa: E402

OK_QUERIES = [
    "SELECT 1",
    "SELECT 1;",
    "SELECT count(*), sum(x) FROM t WHERE y = :name",
    "SELECT x::int, CAST(y AS text), now(), date_trunc('day', ts) FROM t",
    "WITH a AS (SELECT id FROM t) SELECT * FROM a JOIN u USING (id)",
    "SELECT a FROM t UNION SELECT b FROM u",
    "SELECT lower(a), upper(b), coalesce(a, b) FROM t ORDER BY 1 LIMIT 5",
    "SELECT * FROM generate_series(1, 3)",
    "SELECT * FROM t WHERE a = 'x;y'",
    "SELECT row_number() OVER (PARTITION BY a ORDER BY b) FROM t",
    "SELECT jsonb_build_object('a', 1), data->>'k' FROM t",
]

ATTACKS = {
    "datadog commit trick": "COMMIT; DROP SCHEMA public CASCADE",
    "two selects": "SELECT 1; SELECT 2",
    "select then delete": "SELECT 1; DELETE FROM t",
    "write inside WITH": "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
    "update inside WITH": "WITH x AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM x",
    "insert inside WITH": "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
    "select into": "SELECT * INTO newt FROM t",
    "for update": "SELECT * FROM t FOR UPDATE",
    "for share": "SELECT * FROM t FOR SHARE",
    "explain analyze delete": "EXPLAIN ANALYZE DELETE FROM t",
    "plain explain": "EXPLAIN SELECT 1",
    "copy to program": "COPY t TO PROGRAM 'id'",
    "copy from": "COPY t FROM '/etc/passwd'",
    "set role": "SET ROLE postgres",
    "reset": "RESET ALL",
    "show": "SHOW all",
    "begin": "BEGIN",
    "commit": "COMMIT",
    "rollback": "ROLLBACK",
    "do block": "DO $$ BEGIN PERFORM 1; END $$",
    "call": "CALL p()",
    "listen": "LISTEN x",
    "notify": "NOTIFY x",
    "vacuum": "VACUUM",
    "insert": "INSERT INTO t VALUES (1)",
    "update": "UPDATE t SET a = 1",
    "delete": "DELETE FROM t",
    "merge": "MERGE INTO t USING u ON true WHEN MATCHED THEN DELETE",
    "drop": "DROP TABLE t",
    "truncate": "TRUNCATE t",
    "alter role": "ALTER ROLE x SUPERUSER",
    "grant": "GRANT ALL ON t TO x",
    "create table as": "CREATE TABLE z AS SELECT 1",
    "terminate backend": "SELECT pg_terminate_backend(1)",
    "cancel backend": "SELECT pg_cancel_backend(1)",
    "read file": "SELECT pg_read_file('/etc/passwd')",
    "read file, quoted and cased": 'SELECT "pg_catalog"."Pg_Read_File"(\'/etc/passwd\')',
    "read binary file": "SELECT pg_read_binary_file('/etc/passwd')",
    "ls dir in FROM": "SELECT * FROM pg_ls_dir('/')",
    "dblink": "SELECT dblink('host=x', 'select 1')",
    "dblink exec": "SELECT dblink_exec('host=x', 'drop table t')",
    "lo import": "SELECT lo_import('/etc/passwd')",
    "lo export": "SELECT lo_export(1, '/tmp/x')",
    "set_config": "SELECT set_config('default_transaction_read_only', 'off', false)",
    "nextval": "SELECT nextval('s')",
    "setval": "SELECT setval('s', 1)",
    "pg_sleep": "SELECT pg_sleep(100)",
    "pg_sleep in subquery": "SELECT (SELECT pg_sleep(10))",
    "query_to_xml": "SELECT query_to_xml('delete from t', true, true, '')",
    "function hidden in WHERE": "SELECT 1 FROM t WHERE pg_terminate_backend(pid)",
    "function hidden in ORDER BY": "SELECT 1 FROM t ORDER BY pg_sleep(1)",
    "function hidden in CTE": "WITH a AS (SELECT pg_read_file('/x')) SELECT * FROM a",
    "empty": "",
    "only comment": "-- nothing",
    "garbage": "SELEC 1 FRM",
}


@pytest.mark.parametrize("sql", OK_QUERIES)
def test_normal_queries_pass(sql):
    out, _ = sp.gate(sql, set(sp.DEFAULT_ALLOWED_FUNCTIONS))
    assert out.upper().lstrip().startswith(("SELECT", "WITH"))


@pytest.mark.parametrize("name", sorted(ATTACKS))
def test_attack_is_refused(name):
    with pytest.raises(sp.Refused):
        sp.gate(ATTACKS[name], set(sp.DEFAULT_ALLOWED_FUNCTIONS))


def test_limit_is_added():
    out, _ = sp.gate("SELECT * FROM t", set(), max_rows=100)
    assert "LIMIT 100" in out


def test_big_limit_is_capped():
    out, _ = sp.gate("SELECT * FROM t LIMIT 999999", set(), max_rows=100)
    assert "LIMIT 100" in out and "999999" not in out


def test_small_limit_is_kept():
    out, _ = sp.gate("SELECT * FROM t LIMIT 5", set(), max_rows=100)
    assert "LIMIT 5" in out


def test_non_numeric_limit_is_replaced():
    out, _ = sp.gate("SELECT * FROM t LIMIT (SELECT 99999999)", set(), max_rows=100)
    assert "LIMIT 100" in out


def test_params_become_bound_placeholders_and_are_never_spliced():
    out, names = sp.gate("SELECT * FROM t WHERE a = :a AND b = :b", set())
    assert names == ["a", "b"]
    assert "%(a)s" in out and "%(b)s" in out
    assert ":a" not in out


def test_percent_in_literal_survives_parameter_binding():
    out, _ = sp.gate("SELECT * FROM t WHERE a LIKE '%x%' AND b = :b", set())
    assert "%%x%%" in out and "%(b)s" in out


def test_project_allow_list_extends_defaults(tmp_path):
    f = tmp_path / "fn.txt"
    f.write_text("my_safe_fn  # reviewed\n")
    allowed = sp.load_allowed(str(f))
    out, _ = sp.gate("SELECT my_safe_fn(a) FROM t", allowed)
    assert "my_safe_fn" in out.lower()
    with pytest.raises(sp.Refused):
        sp.gate("SELECT my_safe_fn(a) FROM t", set(sp.DEFAULT_ALLOWED_FUNCTIONS))


def test_the_parser_does_not_type_the_dangerous_functions():
    # If a sqlglot upgrade starts typing one of these, it would skip the Anonymous check.
    import sqlglot
    from sqlglot import exp
    for fn in ["pg_terminate_backend(1)", "pg_read_file('x')", "dblink('a','b')", "lo_import('x')", "query_to_xml('a',true,true,'')"]:
        node = sqlglot.parse_one("SELECT " + fn, read="postgres").find(exp.Func)
        assert isinstance(node, exp.Anonymous), fn


DANGEROUS = ["pg_terminate_backend(1)", "pg_cancel_backend(1)", "pg_read_file('x')", "pg_read_binary_file('x')",
             "pg_ls_dir('/')", "pg_stat_file('x')", "dblink('a','b')", "dblink_exec('a','b')", "lo_import('x')",
             "lo_export(1,'x')", "lo_unlink(1)", "set_config('a','b',false)", "nextval('s')", "setval('s',1)",
             "pg_sleep(1)", "query_to_xml('a',true,true,'')", "pg_reload_conf()", "pg_rotate_logfile()",
             "pg_switch_wal()", "pg_create_restore_point('x')", "txid_current()", "pg_advisory_lock(1)"]
POSITIONS = ["SELECT {f}", "SELECT 1 FROM t WHERE {f}", "SELECT 1 FROM t ORDER BY {f}", "SELECT 1 FROM t GROUP BY {f}",
             "SELECT 1 FROM t HAVING {f}", "WITH a AS (SELECT {f}) SELECT * FROM a", "SELECT (SELECT {f})",
             "SELECT 1 FROM t JOIN u ON {f}", "SELECT CASE WHEN {f} THEN 1 END", "SELECT coalesce({f}, 1)",
             "SELECT 1 UNION SELECT {f}", "SELECT * FROM t LIMIT 1 OFFSET (SELECT {f})", "SELECT {f}::text",
             "SELECT lower(({f})::text)", "SELECT * FROM {f}"]


@pytest.mark.parametrize("f", DANGEROUS)
def test_dangerous_function_is_refused_in_every_position(f):
    for pos in POSITIONS:
        sql = pos.format(f=f)
        with pytest.raises(sp.Refused):
            sp.gate(sql, set(sp.DEFAULT_ALLOWED_FUNCTIONS))
        for variant in (sql.replace(f.split("(")[0], '"pg_catalog"."' + f.split("(")[0].upper() + '"', 1),
                        sql.replace(f.split("(")[0], 'pg_catalog.' + f.split("(")[0], 1)):
            with pytest.raises(sp.Refused):
                sp.gate(variant, set(sp.DEFAULT_ALLOWED_FUNCTIONS))


def test_cli_refusal_exit_code_and_reason():
    p = subprocess.run([sys.executable, str(ROOT / "scripts/safe_psql.py"), "--check-only", "--sql", "COMMIT; DROP TABLE t"],
                       capture_output=True, text=True)
    assert p.returncode == 2 and "exactly one statement" in p.stderr


def test_cli_check_only_prints_sql():
    p = subprocess.run([sys.executable, str(ROOT / "scripts/safe_psql.py"), "--check-only", "--sql", "SELECT 1"],
                       capture_output=True, text=True)
    assert p.returncode == 0 and "LIMIT 1000" in p.stdout


def test_cli_missing_param_is_a_usage_error():
    p = subprocess.run([sys.executable, str(ROOT / "scripts/safe_psql.py"), "--check-only", "--sql", "SELECT :x"],
                       capture_output=True, text=True)
    assert p.returncode == 5


def test_connection_url_never_appears_in_error_output():
    env = dict(os.environ, T_URL="postgresql://user:hunter2@127.0.0.1:1/db")
    p = subprocess.run([sys.executable, str(ROOT / "scripts/safe_psql.py"), "--env", "T_URL", "--sql", "SELECT 1"],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 4
    assert "hunter2" not in p.stdout + p.stderr


# ---- end to end against a real throwaway Postgres (skipped when none is installed) ----
def _find_pg():
    for base in ("/opt/homebrew/opt/postgresql@16/bin", "/opt/homebrew/opt/postgresql@17/bin", "/usr/lib/postgresql/16/bin", "/usr/lib/postgresql/15/bin"):
        if os.path.exists(base + "/initdb"):
            return base
    p = shutil.which("initdb")
    return os.path.dirname(p) if p else None


PG = _find_pg()


@pytest.fixture(scope="module")
def pg():
    if not PG:
        pytest.skip("no local Postgres to test against")
    d = tempfile.mkdtemp(prefix="safe_psql_pg_")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    subprocess.run([PG + "/initdb", "-D", d + "/data", "-U", "admin", "--auth=trust", "-E", "UTF8"], check=True, capture_output=True)
    subprocess.run([PG + "/pg_ctl", "-D", d + "/data", "-o", f"-p {port} -k {d} -c listen_addresses=127.0.0.1", "-l", d + "/log", "-w", "start"], check=True, capture_output=True)
    base = f"host=127.0.0.1 port={port} "
    def psql(sql, db="postgres"):
        subprocess.run([PG + "/psql", "-h", "127.0.0.1", "-p", str(port), "-U", "admin", "-d", db, "-v", "ON_ERROR_STOP=1", "-q", "-c", sql], check=True, capture_output=True)
    psql("CREATE DATABASE app")
    psql("CREATE TABLE people(id int primary key, name text, secret text); INSERT INTO people SELECT g, 'p'||g, 's'||g FROM generate_series(1,2500) g", "app")
    psql("CREATE ROLE reader LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB PASSWORD 'r'")
    psql("GRANT CONNECT ON DATABASE app TO reader; GRANT USAGE ON SCHEMA public TO reader; GRANT SELECT ON ALL TABLES IN SCHEMA public TO reader", "app")
    psql("CREATE ROLE sneaky LOGIN CREATEDB PASSWORD 's'")
    psql("GRANT CONNECT ON DATABASE app TO sneaky", "app")
    psql("CREATE ROLE filer LOGIN PASSWORD 'f'; GRANT pg_read_server_files TO filer; GRANT CONNECT ON DATABASE app TO filer", "app")
    yield {"port": port, "url": lambda u, p: f"postgresql://{u}:{p}@127.0.0.1:{port}/app", "psql": psql}
    subprocess.run([PG + "/pg_ctl", "-D", d + "/data", "-m", "immediate", "stop"], capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


def run_cli(url, *args):
    env = dict(os.environ, T_URL=url)
    return subprocess.run([sys.executable, str(ROOT / "scripts/safe_psql.py"), "--env", "T_URL", *args], capture_output=True, text=True, env=env)


def test_e2e_select_works_with_param_and_limit(pg):
    p = run_cli(pg["url"]("reader", "r"), "--sql", "SELECT id, name FROM people WHERE id = :id", "--param", "id=7", "--format", "csv")
    assert p.returncode == 0, p.stderr
    assert "7,p7" in p.stdout


def test_e2e_row_cap_and_display_cap(pg):
    p = run_cli(pg["url"]("reader", "r"), "--sql", "SELECT id FROM people", "--max-rows", "300", "--display-rows", "10")
    assert p.returncode == 0, p.stderr
    assert "10 of 300 rows shown" in p.stdout


def test_e2e_param_injection_is_just_a_string(pg):
    p = run_cli(pg["url"]("reader", "r"), "--sql", "SELECT count(*) FROM people WHERE name = :n", "--param", "n=x'; DROP TABLE people; --")
    assert p.returncode == 0, p.stderr
    assert run_cli(pg["url"]("reader", "r"), "--sql", "SELECT count(*) FROM people").stdout.split()[-1] == "2500"


def test_e2e_statement_timeout_applies(pg):
    p = run_cli(pg["url"]("reader", "r"), "--sql", "SELECT count(*) FROM generate_series(1, 400000000)", "--timeout-ms", "300")
    assert p.returncode == 4 and "timeout" in p.stderr.lower()


def test_e2e_superuser_is_refused(pg):
    p = run_cli(pg["url"]("admin", ""), "--sql", "SELECT 1")
    assert p.returncode == 3 and "superuser" in p.stderr


def test_e2e_createdb_role_is_refused(pg):
    p = run_cli(pg["url"]("sneaky", "s"), "--sql", "SELECT 1")
    assert p.returncode == 3 and "createdb" in p.stderr


def test_e2e_file_reading_member_is_refused(pg):
    p = run_cli(pg["url"]("filer", "f"), "--sql", "SELECT 1")
    assert p.returncode == 3 and "pg_read_server_files" in p.stderr


def test_e2e_reader_cannot_write_even_if_the_gate_were_bypassed(pg):
    import psycopg
    with psycopg.connect(pg["url"]("reader", "r")) as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM people")


def test_e2e_log_has_no_values(pg, tmp_path):
    log = tmp_path / "log.jsonl"
    p = run_cli(pg["url"]("reader", "r"), "--sql", "SELECT name FROM people WHERE id = :id", "--param", "id=7", "--log", str(log), "--query-id", "Q1")
    assert p.returncode == 0
    line = log.read_text()
    assert '"query_id": "Q1"' in line and '"params": ["id"]' in line and "p7" not in line
