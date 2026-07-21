#!/usr/bin/env python3
"""Idempotent production bootstrap for the Odoo container.

Run by the entrypoint BEFORE the Odoo server starts, whenever the deployment
injects agent credentials (``ODOO_AGENT_PASSWORD``) or forces it with
``ODOO_BOOTSTRAP=1``. It removes the last manual post-deploy steps:

  1. Create the production database if it does not exist (``list_db = False``
     disables the web database manager, so nothing else can).
  2. Install the ``odoo_ai_ops`` module if it is not installed.
  3. Create/refresh the agent's technical user (``ODOO_AGENT_LOGIN``, default
     ``ai_ops_agent``): set its password from ``ODOO_AGENT_PASSWORD`` and grant
     the **AI Ops Agent (Technical)** group. The password is (re)applied on
     every boot, so rotating the ``odoo_agent_password`` secret propagates on
     the next deploy.

Every step is a no-op when its outcome is already in place, so running this on
each task boot is cheap (a few SQL probes). Failures exit non-zero: the
container dies and the ECS deployment circuit breaker aborts + rolls back the
deploy - same fail-closed pattern as the master-password guard.

Concurrency: the database-existence check + CREATE DATABASE are serialized via
a Postgres advisory lock held by a short transaction (safe through PgBouncer's
transaction pooling; kept short because of its 90s idle-transaction timeout).
The odoo service runs desired_count=1, so parallel module installs do not occur
in practice; if a scale-up ever races one, the loser exits non-zero and its
replacement task finds the work already done.

DB connection parameters resolve like the stock image entrypoint: env vars
HOST/PORT/USER/PASSWORD first, then the values in odoo.conf.
"""

import configparser
import os
import subprocess
import sys
import time

import psycopg2

ODOO_CONF = os.environ.get("ODOO_RC", "/etc/odoo/odoo.conf")
# Arbitrary constant ("AIOPS" as hex) identifying the bootstrap advisory lock.
ADVISORY_LOCK_KEY = 0x41494F5053
DB_WAIT_SECONDS = 60


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def fatal(msg: str) -> None:
    print(f"[bootstrap] FATAL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def load_conf() -> configparser.SectionProxy:
    parser = configparser.ConfigParser()
    parser.read(ODOO_CONF)
    return parser["options"] if "options" in parser else parser["DEFAULT"]


def db_params(conf) -> dict:
    """DB connection params, env-first exactly like the stock entrypoint."""

    def pick(env_key: str, conf_key: str, default: str) -> str:
        value = os.environ.get(env_key) or conf.get(conf_key, "")
        return value if value and value.lower() != "false" else default

    return {
        "host": pick("HOST", "db_host", "localhost"),
        "port": pick("PORT", "db_port", "5432"),
        "user": pick("USER", "db_user", "odoo"),
        "password": pick("PASSWORD", "db_password", ""),
    }


def connect(params: dict, dbname: str, autocommit: bool = True):
    conn = psycopg2.connect(dbname=dbname, connect_timeout=10, **params)
    conn.autocommit = autocommit
    return conn


def wait_for_postgres(params: dict):
    """Return a connection to the maintenance DB, retrying while it comes up."""
    deadline = time.monotonic() + DB_WAIT_SECONDS
    while True:
        try:
            return connect(params, "postgres")
        except psycopg2.OperationalError as exc:
            if time.monotonic() >= deadline:
                fatal(f"Postgres unreachable after {DB_WAIT_SECONDS}s: {exc}")
            log("waiting for Postgres...")
            time.sleep(3)


def ensure_database(params: dict, db_name: str) -> None:
    """Create ``db_name`` if missing (advisory-locked against a booting twin)."""
    admin = wait_for_postgres(params)
    try:
        # The lock must live in a transaction to survive PgBouncer's
        # transaction pooling; keep that transaction short (idle txns are
        # killed after 90s) by using a second autocommit connection for the
        # CREATE DATABASE, which cannot run inside a transaction block.
        locker = connect(params, "postgres", autocommit=False)
        try:
            with locker.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
                with admin.cursor() as probe:
                    probe.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
                    exists = probe.fetchone() is not None
                if exists:
                    log(f"database '{db_name}' already exists.")
                else:
                    log(f"creating database '{db_name}'...")
                    with admin.cursor() as create:
                        # Same shape Odoo's own db-create uses.
                        create.execute(
                            f'CREATE DATABASE "{db_name}" ENCODING \'unicode\' '
                            "LC_COLLATE 'C' TEMPLATE template0"
                        )
            locker.commit()  # releases the advisory lock
        finally:
            locker.close()
    finally:
        admin.close()


def odoo_cli(params: dict, db_name: str, args: list[str]) -> None:
    cmd = [
        "odoo",
        "-c",
        ODOO_CONF,
        "-d",
        db_name,
        "--db_host",
        params["host"],
        "--db_port",
        params["port"],
        "--db_user",
        params["user"],
        "--db_password",
        params["password"],
        "--no-http",
        "--stop-after-init",
        *args,
    ]
    shown = list(cmd)
    shown[shown.index("--db_password") + 1] = "***"
    log("running: " + " ".join(shown))
    result = subprocess.run(cmd)  # noqa: S603 - fixed argv, no shell
    if result.returncode != 0:
        fatal(f"odoo {' '.join(args)} exited with {result.returncode}")


def module_state(params: dict, db_name: str) -> str:
    """'uninitialized' (no registry), 'installed', or the module's state."""
    conn = connect(params, db_name)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_tables WHERE tablename = 'ir_module_module'")
            if cur.fetchone() is None:
                return "uninitialized"
            cur.execute("SELECT state FROM ir_module_module WHERE name = 'odoo_ai_ops'")
            row = cur.fetchone()
            return row[0] if row else "unavailable"
    finally:
        conn.close()


def ensure_module(params: dict, db_name: str) -> None:
    state = module_state(params, db_name)
    if state == "uninitialized":
        log("empty database - initializing with base + odoo_ai_ops (no demo data)...")
        odoo_cli(params, db_name, ["-i", "base,odoo_ai_ops"])
    elif state != "installed":
        log(f"odoo_ai_ops is '{state}' - installing...")
        odoo_cli(params, db_name, ["-i", "odoo_ai_ops"])
    else:
        log("odoo_ai_ops already installed.")


def ensure_agent_user(params: dict, db_name: str, login: str, password: str) -> None:
    """Create/refresh the agent's technical user inside the registry."""
    import odoo
    from odoo.modules.registry import Registry

    odoo.tools.config.parse_config(
        [
            "-c",
            ODOO_CONF,
            "-d",
            db_name,
            "--db_host",
            params["host"],
            "--db_port",
            params["port"],
            "--db_user",
            params["user"],
            "--db_password",
            params["password"],
            "--no-http",
        ]
    )
    registry = Registry(db_name)
    with registry.cursor() as cr:
        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
        group = env.ref("odoo_ai_ops.group_ai_ops_agent")
        users = env["res.users"].with_context(active_test=False, no_reset_password=True)
        user = users.search([("login", "=", login)], limit=1)
        if user:
            log(f"refreshing agent user '{login}' (password + group).")
            user.write({"password": password, "active": True})
        else:
            log(f"creating agent user '{login}'.")
            user = users.create(
                {
                    "name": "AI Ops Agent",
                    "login": login,
                    "password": password,
                    "group_ids": [(4, group.id)],
                }
            )
        if group not in user.group_ids:
            user.write({"group_ids": [(4, group.id)]})
        cr.commit()


def main() -> None:
    conf = load_conf()
    db_name = os.environ.get("ODOO_DB_NAME") or conf.get("db_name", "") or "odoo"
    params = db_params(conf)
    agent_login = os.environ.get("ODOO_AGENT_LOGIN", "ai_ops_agent")
    agent_password = os.environ.get("ODOO_AGENT_PASSWORD", "")

    log(f"target database: '{db_name}' via {params['host']}:{params['port']}")
    ensure_database(params, db_name)
    ensure_module(params, db_name)
    if agent_password:
        ensure_agent_user(params, db_name, agent_login, agent_password)
    else:
        log("ODOO_AGENT_PASSWORD not set - skipping agent-user provisioning.")
    log("bootstrap complete.")


if __name__ == "__main__":
    main()
