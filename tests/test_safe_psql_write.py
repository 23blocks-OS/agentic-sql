import json, os, pty, shutil, socket, subprocess, sys, tempfile, time, pathlib, select
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import safe_psql_write as w  # noqa: E402
import safe_psql as sp  # noqa: E402

WRITE = str(ROOT / "scripts" / "safe_psql_write.py")
READ = str(ROOT / "scripts" / "safe_psql.py")
AGENT_VARS = w.AGENT_ENV_MARKERS

SETTINGS_BASE = """
default_max_rows: 25
approval_ttl_minutes: 10
environments:
  production: {confirm_by_typing: true}
tables:
  customers: {insert: true, update: [plan, email], max_rows: 25}
  notes: {insert: true, update: true, delete: true, max_rows: 25}
"""


def write_settings(tmp, hard_delete=False, extra=""):
    p = tmp / "write.yaml"
    p.write_text(f"allow_hard_delete: {'true' if hard_delete else 'false'}\n" + SETTINGS_BASE + extra)
    return str(p)


def S(hard_delete=False):
    d = pathlib.Path(tempfile.mkdtemp(prefix="wset_"))
    return w.load_settings(write_settings(d, hard_delete))


# ------------------------------------------------------------------ the rules
OK = [
    "INSERT INTO customers (name, plan) VALUES (:n, 'free')",
    "UPDATE customers SET plan = :p WHERE id = :id",
    "UPDATE customers SET plan = 'pro', email = :e WHERE id IN (SELECT id FROM customers WHERE name = :n)",
    "UPDATE public.customers SET plan = 'pro' WHERE id = 1",
    "INSERT INTO notes (body) VALUES ('x') ON CONFLICT DO NOTHING",
    "UPDATE notes SET body = lower(body) WHERE id = 1",
]
BAD = {
    "select": "SELECT 1",
    "two statements": "UPDATE customers SET plan = 'a' WHERE id = 1; UPDATE customers SET plan = 'b' WHERE id = 2",
    "drop": "DROP TABLE customers",
    "truncate": "TRUNCATE customers",
    "update without where": "UPDATE customers SET plan = 'pro'",
    "delete without where": "DELETE FROM notes",
    "unlisted table": "UPDATE audit SET msg = 'x' WHERE id = 1",
    "unlisted column": "UPDATE customers SET name = 'x' WHERE id = 1",
    "returning": "UPDATE customers SET plan = 'a' WHERE id = 1 RETURNING *",
    "update from": "UPDATE customers c SET plan = 'a' FROM notes n WHERE c.id = n.id",
    "delete using": "DELETE FROM notes USING customers WHERE notes.id = customers.id",
    "on conflict do update": "INSERT INTO notes (body) VALUES ('x') ON CONFLICT (id) DO UPDATE SET body = 'y'",
    "write inside WITH": "WITH x AS (DELETE FROM notes WHERE id = 1 RETURNING id) UPDATE customers SET plan = 'a' WHERE id IN (SELECT id FROM x)",
    "insert without column list": "INSERT INTO customers VALUES (1, 'a')",
    "insert not allowed table-level": "INSERT INTO customers (name) VALUES (pg_read_file('/etc/passwd'))",
    "dangerous function in set": "UPDATE notes SET body = pg_read_file('/etc/passwd') WHERE id = 1",
    "dangerous function in where": "UPDATE notes SET body = 'x' WHERE pg_terminate_backend(1)",
    "merge": "MERGE INTO notes n USING customers c ON n.id = c.id WHEN MATCHED THEN DELETE",
    "copy": "COPY notes TO PROGRAM 'id'",
    "set": "SET ROLE postgres",
    "commit trick": "COMMIT; DROP SCHEMA public CASCADE",
    "quoted mixed-case table": 'UPDATE "Customers" SET plan = \'a\' WHERE id = 1',
    "quoted mixed-case column": 'UPDATE customers SET "Plan" = \'a\' WHERE id = 1',
    "quoted mixed-case insert column": 'INSERT INTO customers ("Name") VALUES (\'a\')',
    "empty": "",
    "garbage": "UPDAT customers",
}


def test_quoted_lowercase_names_still_pass():
    assert w.gate_write('UPDATE "customers" SET "plan" = 1 WHERE id = 1', S())["table"] == "public.customers"


@pytest.mark.parametrize("sql", OK)
def test_allowed_statements_pass(sql):
    info = w.gate_write(sql, S())
    assert info["kind"] in ("INSERT", "UPDATE")


@pytest.mark.parametrize("name", sorted(BAD))
def test_bad_statements_are_refused(name):
    with pytest.raises(w.Refused):
        w.gate_write(BAD[name], S())


def test_delete_is_off_by_default():
    with pytest.raises(w.Refused) as e:
        w.gate_write("DELETE FROM notes WHERE id = 1", S(hard_delete=False))
    assert "allow_hard_delete" in str(e.value)


def test_delete_needs_both_the_global_switch_and_the_table():
    assert w.gate_write("DELETE FROM notes WHERE id = 1", S(hard_delete=True))["kind"] == "DELETE"
    with pytest.raises(w.Refused):  # customers has no delete: true
        w.gate_write("DELETE FROM customers WHERE id = 1", S(hard_delete=True))


def test_no_settings_file_means_nothing_is_writable(tmp_path, monkeypatch):
    monkeypatch.delenv("SAFE_PSQL_WRITE_SETTINGS", raising=False)
    monkeypatch.setenv("SAFE_PSQL_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(w.Refused):
        w.load_settings(None)


def test_bound_parameters_never_spliced():
    info = w.gate_write("UPDATE customers SET plan = :p WHERE id = :id", S())
    sql, names = w.bind(info["tree"])
    assert names == ["id", "p"] and "%(p)s" in sql and ":p" not in sql


# ------------------------------------------------------------------ real Postgres
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

    def psql(sql, db="app"):
        subprocess.run([PG + "/psql", "-h", "127.0.0.1", "-p", str(port), "-U", "admin", "-d", db, "-v", "ON_ERROR_STOP=1", "-q", "-At", "-c", sql], check=True, capture_output=True)

    def q(sql, db="app"):
        return subprocess.run([PG + "/psql", "-h", "127.0.0.1", "-p", str(port), "-U", "admin", "-d", db, "-v", "ON_ERROR_STOP=1", "-q", "-At", "-c", sql],
                              check=True, capture_output=True, text=True).stdout.strip()

    psql("CREATE DATABASE app", "postgres")
    psql("CREATE TABLE customers(id int primary key, name text, email text, plan text, secret text)")
    psql("CREATE TABLE notes(id serial primary key, body text)")
    psql("CREATE TABLE audit(id int primary key, msg text)")
    for r, pw in (("writer", "w"), ("writer_broad", "b"), ("reader", "r"), ("nowrite", "n")):
        psql(f"CREATE ROLE {r} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB PASSWORD '{pw}'", "postgres")
        psql(f"GRANT CONNECT ON DATABASE app TO {r}", "postgres")
        psql(f"GRANT USAGE ON SCHEMA public TO {r}")
        psql(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {r}")
    psql("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO writer, writer_broad")
    psql("GRANT INSERT, UPDATE ON customers TO writer, writer_broad")
    psql("GRANT INSERT, UPDATE, DELETE ON notes TO writer, writer_broad")
    psql("GRANT UPDATE ON audit TO writer_broad")  # a login that can write a table that is not on the list
    psql("GRANT SELECT ON ALL TABLES IN SCHEMA public TO reader")
    # a careful setup: column-level grants only
    psql("CREATE ROLE writer_cols LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB PASSWORD 'c'", "postgres")
    psql("GRANT CONNECT ON DATABASE app TO writer_cols", "postgres")
    psql("GRANT USAGE ON SCHEMA public TO writer_cols")
    psql("GRANT SELECT ON ALL TABLES IN SCHEMA public TO writer_cols")
    psql("GRANT UPDATE (plan, email) ON customers TO writer_cols")
    psql("GRANT INSERT (body) ON notes TO writer_cols")
    psql("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO writer_cols")
    yield {"port": port, "psql": psql, "q": q, "url": lambda u, p: f"postgresql://{u}:{p}@127.0.0.1:{port}/app"}
    subprocess.run([PG + "/pg_ctl", "-D", d + "/data", "-m", "immediate", "stop"], capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def env(pg, tmp_path):
    pg["psql"]("TRUNCATE customers, notes, audit RESTART IDENTITY")
    pg["psql"]("INSERT INTO customers SELECT g, 'c'||g, 'c'||g||'@x.test', 'free', 'sec'||g FROM generate_series(1,40) g")
    pg["psql"]("INSERT INTO notes(body) SELECT 'n'||g FROM generate_series(1,5) g")
    pg["psql"]("INSERT INTO audit VALUES (1,'a')")
    home = tmp_path / "home"
    settings = write_settings(tmp_path)
    e = {k: v for k, v in os.environ.items() if k not in AGENT_VARS}
    e.update({"SAFE_PSQL_HOME": str(home), "W_URL": pg["url"]("writer", "w"), "BROAD_URL": pg["url"]("writer_broad", "b"),
              "R_URL": pg["url"]("reader", "r"), "NOWRITE_URL": pg["url"]("nowrite", "n"), "COLS_URL": pg["url"]("writer_cols", "c")})
    return {"env": e, "settings": settings, "home": home, "tmp": tmp_path, "pg": pg}


def run(env, *args, agent=False, settings=None):
    e = dict(env["env"])
    if agent:
        e["CLAUDECODE"] = "1"
    cmd = [sys.executable, WRITE, "--settings", settings or env["settings"], *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=e, stdin=subprocess.DEVNULL, start_new_session=True)


def propose(env, sql, *extra, url="W_URL", environment="staging"):
    return run(env, "propose", "--env", url, "--environment", environment, "--sql", sql, *extra)


def rid_of(p):
    for tok in p.stdout.split():
        if tok.startswith("w-"):
            return tok.rstrip(":")
    raise AssertionError("no request id in: " + p.stdout + p.stderr)


def approve_in_terminal(env, rid, typed, settings=None, extra_env=None):
    """Run `approve` on a real pseudo-terminal, as a person at a terminal would."""
    e = dict(env["env"]); e.update(extra_env or {})
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(sys.executable, [sys.executable, WRITE, "--settings", settings or env["settings"], "approve", rid], e)
    out = b""
    deadline = time.time() + 20
    answered = False
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if b"rejects:" in out and not answered:
                os.write(fd, (typed + "\n").encode()); answered = True
        else:
            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if done:
                return os.waitstatus_to_exitcode(status), out.decode(errors="replace")
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


def cust(env, i, col):
    return env["pg"]["q"](f"SELECT {col} FROM customers WHERE id = {i}")


def test_propose_changes_nothing_and_prints_no_values(env):
    p = propose(env, "UPDATE customers SET plan = :p WHERE id = :id", "--param", "p=pro", "--param", "id=7", "--reason", "support case S12")
    assert p.returncode == 0, p.stderr
    assert "1 row(s) would change" in p.stdout and "plan" in p.stdout
    assert "pro" not in p.stdout.replace("approve", "").replace("propose", "").replace("professional", "")  # the value is not echoed
    assert "c7@x.test" not in p.stdout
    assert cust(env, 7, "plan") == "free"


def test_full_flow_propose_approve_apply(env):
    p = propose(env, "UPDATE customers SET plan = :p WHERE id = :id", "--param", "p=pro", "--param", "id=7")
    rid = rid_of(p)
    assert run(env, "apply", rid).returncode == 6              # not approved yet
    assert cust(env, 7, "plan") == "free"
    code, shown = approve_in_terminal(env, rid, "yes")
    assert code == 0, shown
    assert "free" in shown and "pro" in shown                    # the person sees before and after
    a = run(env, "apply", rid)
    assert a.returncode == 0, a.stderr
    assert cust(env, 7, "plan") == "pro"
    assert run(env, "apply", rid).returncode == 6              # cannot be applied twice


def test_wrong_answer_rejects(env):
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3"))
    code, _ = approve_in_terminal(env, rid, "maybe")
    assert code == 6
    assert run(env, "apply", rid).returncode == 6
    assert cust(env, 3, "plan") == "free"


def test_production_requires_typing_the_environment_name(env):
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3", environment="production"))
    code, _ = approve_in_terminal(env, rid, "yes")
    assert code == 6 and cust(env, 3, "plan") == "free"
    rid2 = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3", environment="production"))
    code2, _ = approve_in_terminal(env, rid2, "production")
    assert code2 == 0
    assert run(env, "apply", rid2).returncode == 0 and cust(env, 3, "plan") == "pro"


def test_approval_needs_a_terminal(env):
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3"))
    p = run(env, "approve", rid)  # no controlling terminal, like an agent's shell command
    assert p.returncode == 6 and "terminal" in p.stderr
    assert run(env, "apply", rid).returncode == 6


def test_approval_refused_inside_an_agent_session_even_with_a_terminal(env):
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3"))
    code, out = approve_in_terminal(env, rid, "yes", extra_env={"CLAUDECODE": "1"})
    assert code == 6 and cust(env, 3, "plan") == "free"
    assert run(env, "apply", rid).returncode == 6


def test_a_changed_request_is_refused(env):
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3"))
    assert approve_in_terminal(env, rid, "yes")[0] == 0
    f = env["home"] / "requests" / f"{rid}.json"
    d = json.loads(f.read_text()); d["params"] = {"x": "y"}; d["sql"] = "UPDATE customers SET plan = 'free' WHERE id = 4"; f.write_text(json.dumps(d))
    a = run(env, "apply", rid)
    assert a.returncode == 6 and "differs" in a.stderr
    assert cust(env, 4, "plan") == "free" and cust(env, 3, "plan") == "free"


def test_expired_request_is_refused(env):
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3"))
    f = env["home"] / "requests" / f"{rid}.json"
    d = json.loads(f.read_text()); d["expires"] = time.time() - 5; f.write_text(json.dumps(d))
    code, _ = approve_in_terminal(env, rid, "yes")
    assert code == 6
    assert run(env, "apply", rid).returncode == 6


def test_approval_expires_before_apply(env):
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3"))
    assert approve_in_terminal(env, rid, "yes")[0] == 0
    f = env["home"] / "requests" / f"{rid}.json"
    d = json.loads(f.read_text()); d["apply_by"] = time.time() - 1; f.write_text(json.dumps(d))
    assert run(env, "apply", rid).returncode == 6 and cust(env, 3, "plan") == "free"


def test_row_count_changed_between_propose_and_apply_rolls_back(env):
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE plan = 'free' AND id <= 3"))
    assert approve_in_terminal(env, rid, "yes")[0] == 0
    env["pg"]["psql"]("UPDATE customers SET plan = 'free' WHERE id = 4")  # no effect on the match (id > 3)
    env["pg"]["psql"]("UPDATE customers SET plan = 'gold' WHERE id = 2")  # one row leaves the match
    a = run(env, "apply", rid)
    assert a.returncode == 7 and "Rolled back" in a.stderr
    assert cust(env, 1, "plan") == "free" and cust(env, 3, "plan") == "free"


def test_too_many_rows_refused_at_propose(env):
    p = propose(env, "UPDATE customers SET plan = 'pro' WHERE id <= 30")
    assert p.returncode == 2 and "more than 25" in p.stderr
    assert cust(env, 1, "plan") == "free"


def test_zero_rows_is_not_a_request(env):
    p = propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 9999")
    assert p.returncode == 2 and "0 rows" in p.stderr


def test_insert_flow_and_dry_run_does_not_insert(env):
    p = propose(env, "INSERT INTO notes (body) VALUES (:b)", "--param", "b=hello")
    assert p.returncode == 0, p.stderr
    rid = rid_of(p)
    assert env["pg"]["q"]("SELECT count(*) FROM notes") == "5"
    assert approve_in_terminal(env, rid, "yes")[0] == 0
    assert run(env, "apply", rid).returncode == 0
    assert env["pg"]["q"]("SELECT count(*) FROM notes WHERE body = 'hello'") == "1"


def test_delete_only_when_enabled(env):
    p = propose(env, "DELETE FROM notes WHERE id = 1")
    assert p.returncode == 2 and "allow_hard_delete" in p.stderr
    s2 = write_settings(env["tmp"], hard_delete=True)
    p2 = run(env, "propose", "--env", "W_URL", "--sql", "DELETE FROM notes WHERE id = 1", settings=s2)
    assert p2.returncode == 0, p2.stderr
    rid = rid_of(p2)
    assert approve_in_terminal(env, rid, "yes", settings=s2)[0] == 0
    assert run(env, "apply", rid, settings=s2).returncode == 0
    assert env["pg"]["q"]("SELECT count(*) FROM notes WHERE id = 1") == "0"


def test_login_that_can_write_unlisted_tables_is_refused(env):
    p = propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3", url="BROAD_URL")
    assert p.returncode == 3 and "audit" in p.stderr


def test_a_read_only_login_is_not_a_write_login(env):
    p = propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3", url="NOWRITE_URL")
    assert p.returncode == 3 and "no write privilege" in p.stderr


def test_superuser_login_is_refused(env):
    e = dict(env["env"]); e["ADMIN_URL"] = f"postgresql://admin@127.0.0.1:{env['pg']['port']}/app"
    env2 = dict(env); env2["env"] = e
    p = propose(env2, "UPDATE customers SET plan = 'pro' WHERE id = 3", url="ADMIN_URL")
    assert p.returncode == 3 and "superuser" in p.stderr


def test_read_door_refuses_a_login_that_can_write(env):
    e = dict(env["env"])
    p = subprocess.run([sys.executable, READ, "--env", "W_URL", "--sql", "SELECT 1"], capture_output=True, text=True, env=e)
    assert p.returncode == 3 and "read login must have SELECT only" in p.stderr
    ok = subprocess.run([sys.executable, READ, "--env", "R_URL", "--sql", "SELECT count(*) FROM customers"], capture_output=True, text=True, env=e)
    assert ok.returncode == 0, ok.stderr


def test_log_has_no_values_and_records_the_approver(env):
    rid = rid_of(propose(env, "UPDATE customers SET email = :e WHERE id = :id", "--param", "e=secret.person@x.test", "--param", "id=7"))
    assert approve_in_terminal(env, rid, "yes")[0] == 0
    assert run(env, "apply", rid).returncode == 0
    log = (env["home"] / "write.log").read_text()
    assert "secret.person" not in log and "c7@x.test" not in log
    events = [json.loads(l)["event"] for l in log.splitlines()]
    assert events == ["proposed", "approved", "applied"]
    assert '"param_names": ["e", "id"]' in log or '"param_names": ["id", "e"]' in log


def test_values_are_dropped_from_the_request_file_once_finished(env):
    rid = rid_of(propose(env, "UPDATE customers SET email = :e WHERE id = :id", "--param", "e=zed@x.test", "--param", "id=7"))
    assert approve_in_terminal(env, rid, "yes")[0] == 0
    assert run(env, "apply", rid).returncode == 0
    assert "zed@x.test" not in (env["home"] / "requests" / f"{rid}.json").read_text()


def test_request_file_is_private(env):
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3"))
    mode = (env["home"] / "requests" / f"{rid}.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_reject_command(env):
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3"))
    assert run(env, "reject", rid).returncode == 0
    assert run(env, "apply", rid).returncode == 6


def test_param_injection_is_just_text(env):
    p = propose(env, "UPDATE customers SET email = :e WHERE id = :id", "--param", "e=x'; DROP TABLE customers; --", "--param", "id=7")
    rid = rid_of(p)
    assert approve_in_terminal(env, rid, "yes")[0] == 0
    assert run(env, "apply", rid).returncode == 0
    assert env["pg"]["q"]("SELECT count(*) FROM customers") == "40"
    assert cust(env, 7, "email") == "x'; DROP TABLE customers; --"


def test_connection_url_never_appears_in_output(env):
    e = dict(env["env"]); e["BAD_URL"] = "postgresql://user:hunter2@127.0.0.1:1/db"
    env2 = dict(env); env2["env"] = e
    p = propose(env2, "UPDATE customers SET plan = 'pro' WHERE id = 3", url="BAD_URL")
    assert p.returncode == 4 and "hunter2" not in p.stdout + p.stderr


def test_piping_yes_into_approve_does_not_approve(env):
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3"))
    for stdin in ("yes\n", "yes\nyes\n"):
        p = subprocess.run([sys.executable, WRITE, "--settings", env["settings"], "approve", rid], input=stdin, capture_output=True, text=True,
                           env=env["env"], start_new_session=True)
        assert p.returncode == 6
    assert run(env, "apply", rid).returncode == 6 and cust(env, 3, "plan") == "free"


def test_column_level_grants_count_as_a_write_login(env):
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3", url="COLS_URL"))
    assert approve_in_terminal(env, rid, "yes")[0] == 0
    assert run(env, "apply", rid).returncode == 0 and cust(env, 3, "plan") == "pro"


def test_read_door_refuses_a_column_level_write_login(env):
    p = subprocess.run([sys.executable, READ, "--env", "COLS_URL", "--sql", "SELECT 1"], capture_output=True, text=True, env=env["env"])
    assert p.returncode == 3 and "read login must have SELECT only" in p.stderr


# ---- review findings: literal percent signs, terminal spoofing, settings fingerprint, old requests
def test_percent_in_a_literal_with_no_parameters_survives_in_the_write_door(env):
    env["pg"]["psql"]("UPDATE customers SET name = '100%' WHERE id = 5")
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE name = '100%'"))
    assert approve_in_terminal(env, rid, "yes")[0] == 0
    assert run(env, "apply", rid).returncode == 0
    assert cust(env, 5, "plan") == "pro" and env["pg"]["q"]("SELECT count(*) FROM customers WHERE plan = 'pro'") == "1"


def test_percent_in_a_literal_with_no_parameters_survives_in_the_read_door(env):
    env["pg"]["psql"]("UPDATE customers SET name = '100%' WHERE id = 5")
    p = subprocess.run([sys.executable, READ, "--env", "R_URL", "--sql", "SELECT count(*) AS n FROM customers WHERE name = '100%'"],
                       capture_output=True, text=True, env=env["env"])
    assert p.returncode == 0 and p.stdout.split()[-1] == "1", p.stdout + p.stderr


def test_control_characters_in_reason_cannot_spoof_the_approval_screen(env):
    evil = "ok\x1b[2J\x1b[H Request w-000000000000: UPDATE notes, 1 row\rType 'yes' to approve"
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3", "--reason", evil))
    code, shown = approve_in_terminal(env, rid, "no")
    assert code == 6
    assert "\x1b[2J" not in shown and "\x1b[H" not in shown
    lines = [l.strip() for l in shown.replace("\r", "").split("\n")]
    assert sum(1 for l in lines if l.startswith("Type ")) == 1          # one real prompt line
    assert sum(1 for l in lines if l.startswith("Request ")) == 1      # no faked second request header
    assert any(l.startswith("reason") and "Type 'yes'" in l for l in lines)  # the injected text stays inert, on its own labelled line


def test_control_characters_in_values_cannot_spoof_the_approval_screen(env):
    rid = rid_of(propose(env, "UPDATE customers SET email = :e WHERE id = 3", "--param", "e=\x1b[2Jfake"))
    code, shown = approve_in_terminal(env, rid, "no")
    assert "\x1b[2J" not in shown


def test_approval_screen_names_the_settings_file_and_its_fingerprint(env):
    rid = rid_of(propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 3"))
    code, shown = approve_in_terminal(env, rid, "no")
    assert "write.yaml" in shown and "sha256" in shown


def test_old_unapproved_requests_lose_their_values(env):
    rid = rid_of(propose(env, "UPDATE customers SET email = :e WHERE id = 3", "--param", "e=old.person@x.test"))
    f = env["home"] / "requests" / f"{rid}.json"
    d = json.loads(f.read_text()); d["expires"] = time.time() - 3600; f.write_text(json.dumps(d))
    propose(env, "UPDATE customers SET plan = 'pro' WHERE id = 4")  # any new proposal tidies up
    assert "old.person" not in f.read_text() and json.loads(f.read_text())["status"] == "expired"


def _status_request(tmp_path, status="pending", ttl=600):
    home = tmp_path / "home"
    (home / "requests").mkdir(parents=True, mode=0o700)
    rid = "w-0123456789ab"
    req = {"id": rid, "status": status, "kind": "UPDATE", "table": "public.customers", "rows": 3, "environment": "staging",
           "expires": time.time() + ttl, "reason": "secret reason", "params": {"p": "secret value"}}
    (home / "requests" / (rid + ".json")).write_text(json.dumps(req))
    e = {k: v for k, v in os.environ.items() if k not in AGENT_VARS}
    e["SAFE_PSQL_HOME"] = str(home)
    return rid, e


def _status(e, *args):
    return subprocess.run([sys.executable, WRITE, "status", *args], capture_output=True, text=True, env=e, stdin=subprocess.DEVNULL)


def test_status_default_output_is_one_line_and_unchanged(tmp_path):
    rid, e = _status_request(tmp_path)
    p = _status(e, rid)
    assert p.returncode == 0
    assert p.stdout.startswith(f"request {rid}: pending · UPDATE public.customers · 3 row(s) · expires in ")
    assert p.stdout.count("\n") == 1 and "{" not in p.stdout


def test_status_json_has_the_fields_and_no_values(tmp_path):
    rid, e = _status_request(tmp_path, status="approved")
    p = _status(e, rid, "--json")
    assert p.returncode == 0 and p.stdout.count("\n") == 1
    d = json.loads(p.stdout)
    assert set(d) == {"id", "status", "kind", "table", "rows", "environment", "expires_in"}
    assert d["id"] == rid and d["status"] == "approved" and d["rows"] == 3 and 0 < d["expires_in"] <= 600
    assert "secret" not in p.stdout


def test_status_json_after_expiry_reports_zero(tmp_path):
    rid, e = _status_request(tmp_path, ttl=-5)
    d = json.loads(_status(e, rid, "--json").stdout)
    assert d["expires_in"] == 0
    assert "expired" in _status(e, rid).stdout


def test_status_json_unknown_request_is_refused(tmp_path):
    _, e = _status_request(tmp_path)
    p = _status(e, "w-ffffffffffff", "--json")
    assert p.returncode == 2 and p.stdout == ""
