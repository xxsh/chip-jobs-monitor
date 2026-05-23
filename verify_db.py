"""Equivalence check for the db.py port vs db.mjs.

Three layers:
  A. Pure-helper parity (mirrors db.test.mjs).
  B. Content-hash / cancellation-key parity vs Node across real snapshots.
  C. Live dual-persist: run db.mjs and db.py against two throwaway MySQL DBs
     from the same fixture, then diff every table row-by-row.

Requires a local MySQL (root via /tmp/mysql.sock) and node. Temp DBs are
dropped before and after. Exit 0 = equivalent, 1 = mismatch.

Usage: python verify_db.py
"""

import glob
import json
import os
import subprocess
import sys

import pymysql

import db

HERE = os.path.dirname(os.path.abspath(__file__))
DB_NODE = "nvidia_jobs_verify_node"
DB_PY = "nvidia_jobs_verify_py"
SOCKET = os.environ.get("MYSQL_SOCKET_PATH", "/tmp/mysql.sock")

failures = []


def check(label, ok):
    if not ok:
        failures.append(label)
        print(f"  FAIL {label}")


# ---------------------------------------------------------------- A. pure helpers
def section_helpers():
    print("A. pure-helper parity")
    c = db.mysql_config_from_env({})
    check("config: unconfigured", c["configured"] is False and c["database"] == "nvidia_jobs_monitor")

    c = db.mysql_config_from_env(
        {"MYSQL_USER": "root", "MYSQL_SOCKET_PATH": "/tmp/mysql.sock", "MYSQL_DATABASE": "nvidia_jobs_monitor"}
    )
    check(
        "config: socket",
        c["configured"]
        and c["config"]["user"] == "root"
        and c["config"]["unix_socket"] == "/tmp/mysql.sock"
        and c["config"]["database"] == "nvidia_jobs_monitor",
    )

    c = db.mysql_config_from_env({"MYSQL_DSN": "mysql://u:p@example.com:3307/somedb"})
    check(
        "config: dsn",
        c["configured"]
        and c["config"]["user"] == "u"
        and c["config"]["password"] == "p"
        and c["config"]["host"] == "example.com"
        and c["config"]["port"] == 3307
        and c["database"] == "somedb",
    )

    check("jobKey: id", db.job_key({"id": 42, "jr": "JR42", "name": "Role"}) == "42")
    check("jobKey: jr", db.job_key({"jr": "JR42", "name": "Role"}) == "JR42")
    check(
        "jobKey: fallback",
        db.job_key({"title": "Role", "posted": "2026-05-20T00:00:00"}) == "Role|2026-05-20T00:00:00",
    )

    a = {"id": 1, "jr": "JR1", "name": "Role", "locations": ["China, Shanghai"], "requirements": ["Python"], "preferred": []}
    b = {"preferred": [], "requirements": ["Python"], "locations": ["China, Shanghai"], "name": "Role", "jr": "JR1", "id": 1}
    check("jobContentHash: order-independent", db.job_content_hash(a) == db.job_content_hash(b))

    check("toMysqlDate", db.to_mysql_date("2026-05-20T00:00:00") == "2026-05-20")
    check("toMysqlDateTime", db.to_mysql_datetime("2026-05-20T01:02:03") == "2026-05-20 01:02:03")
    check("slugify", db.slugify("Shanghai, China") == "shanghai-china")
    check(
        "cancellationKey: order-independent",
        db.cancellation_key({"jr": "JR1", "title": "A"}) == db.cancellation_key({"title": "A", "jr": "JR1"}),
    )


# ---------------------------------------------------------------- B. hash parity vs node
NODE_HASH_DUMP = r"""
import fs from 'fs';
import { jobContentHash, cancellationKey } from '%s/db.mjs';
const [mode, path] = process.argv.slice(-2);
const data = JSON.parse(fs.readFileSync(path, 'utf8'));
if (mode === 'jobs') for (const j of data) console.log(jobContentHash(j));
else for (const c of (data.canceledJobs || [])) console.log(cancellationKey(c));
""" % HERE


def node_hashes(mode, path):
    out = subprocess.run(
        ["node", "--input-type=module", "-e", NODE_HASH_DUMP, "--", mode, path],
        capture_output=True, text=True, cwd=HERE,
    )
    if out.returncode != 0:
        raise RuntimeError(f"node hash dump failed: {out.stderr}")
    return out.stdout.splitlines()


def section_hash_parity():
    print("B. content-hash / cancellation-key parity vs node")
    snaps = sorted(glob.glob(os.path.join(HERE, "snapshots", "2026-*_shanghai-china.json")))[-5:]
    total_jobs = 0
    for snap in snaps:
        jobs = json.load(open(snap, encoding="utf-8"))
        py = [db.job_content_hash(j) for j in jobs]
        check(f"jobHash {os.path.basename(snap)}", py == node_hashes("jobs", snap))
        total_jobs += len(jobs)

    reports = sorted(glob.glob(os.path.join(HERE, "reports", "2026-*_shanghai-china.json")))[-5:]
    total_cancels = 0
    for rep in reports:
        data = json.load(open(rep, encoding="utf-8"))
        py = [db.cancellation_key(c) for c in (data.get("canceledJobs") or [])]
        check(f"cancelKey {os.path.basename(rep)}", py == node_hashes("cancel", rep))
        total_cancels += len(data.get("canceledJobs") or [])
    print(f"   ({total_jobs} job hashes, {total_cancels} cancellation keys checked)")


# ---------------------------------------------------------------- C. live dual-persist + diff
NODE_PERSIST = r"""
import fs from 'fs';
import { persistDailyRunFromEnv } from '%s/db.mjs';
const [snapPath, reportPath, location, runDate, profilePath] = process.argv.slice(-5);
const currentJobs = JSON.parse(fs.readFileSync(snapPath, 'utf8'));
const report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
const res = await persistDailyRunFromEnv({
  location, runDate, currentJobs, report,
  snapshotFile: snapPath, reportFile: reportPath,
  profileCachePath: profilePath || null,
});
process.stderr.write(JSON.stringify(res) + '\n');
""" % HERE

TABLES = ["runs", "jobs", "job_snapshots", "scores", "cancellations"]
SKIP_COLS = {"id", "created_at", "updated_at"}


def admin_conn():
    return pymysql.connect(user="root", unix_socket=SOCKET, charset="utf8mb4", autocommit=True)


def drop_dbs():
    with admin_conn() as c, c.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{DB_NODE}`")
        cur.execute(f"DROP DATABASE IF EXISTS `{DB_PY}`")


def dump_table(database, table):
    conn = pymysql.connect(user="root", unix_socket=SOCKET, database=database, charset="utf8mb4")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                [database, table],
            )
            cols = [r[0] for r in cur.fetchall() if r[0] not in SKIP_COLS]
            collist = ", ".join(f"`{c}`" for c in cols)
            cur.execute(f"SELECT {collist} FROM `{table}` ORDER BY id")
            return cols, cur.fetchall()
    finally:
        conn.close()


def section_live(fixture):
    print("C. live dual-persist + table diff")
    snap, report, location, run_date, profile = fixture
    drop_dbs()
    try:
        # Node side
        env = {**os.environ, "MYSQL_USER": "root", "MYSQL_SOCKET_PATH": SOCKET, "MYSQL_DATABASE": DB_NODE}
        n = subprocess.run(
            ["node", "--input-type=module", "-e", NODE_PERSIST, "--", snap, report, location, run_date, profile or ""],
            capture_output=True, text=True, cwd=HERE, env=env,
        )
        if n.returncode != 0:
            check("node persist", False)
            print("   node stderr:", n.stderr[-500:])
            return
        node_res = n.stderr.strip().splitlines()[-1]

        # Python side
        os.environ["MYSQL_USER"] = "root"
        os.environ["MYSQL_SOCKET_PATH"] = SOCKET
        os.environ["MYSQL_DATABASE"] = DB_PY
        py_res = db.persist_daily_run_from_env(
            location=location,
            run_date=run_date,
            current_jobs=json.load(open(snap, encoding="utf-8")),
            report=json.load(open(report, encoding="utf-8")),
            snapshot_file=snap,
            report_file=report,
            profile_cache_path=profile or None,
        )
        print(f"   node -> {node_res}")
        print(f"   py   -> {json.dumps(py_res)}")

        for table in TABLES:
            cols_n, rows_n = dump_table(DB_NODE, table)
            cols_p, rows_p = dump_table(DB_PY, table)
            if cols_n != cols_p:
                check(f"{table}: columns", False)
                continue
            if len(rows_n) != len(rows_p):
                check(f"{table}: row count ({len(rows_n)} vs {len(rows_p)})", False)
                continue
            mismatch = None
            for i, (rn, rp) in enumerate(zip(rows_n, rows_p)):
                if rn != rp:
                    diffcols = [cols_n[j] for j in range(len(rn)) if rn[j] != rp[j]]
                    mismatch = f"row {i} cols={diffcols}"
                    break
            check(f"{table}: {len(rows_n)} rows identical", mismatch is None)
            if mismatch:
                print(f"      {table} {mismatch}")
                for j in range(len(rows_n[i])):
                    if rows_n[i][j] != rows_p[i][j]:
                        print(f"        {cols_n[j]}: node={rows_n[i][j]!r} py={rows_p[i][j]!r}")
    finally:
        drop_dbs()


def main():
    section_helpers()
    section_hash_parity()

    snap = sorted(glob.glob(os.path.join(HERE, "snapshots", "2026-*_shanghai-china.json")))[-1]
    report = sorted(glob.glob(os.path.join(HERE, "reports", "2026-*_shanghai-china.json")))[-1]
    profile = os.path.join(HERE, "scorer", "cache", "profile_latest.json")
    fixture = (snap, report, "Shanghai, China", os.path.basename(snap)[:10], profile if os.path.exists(profile) else "")
    print(f"   fixture: {os.path.basename(snap)} + {os.path.basename(report)} (run_date {fixture[3]})")
    section_live(fixture)

    print()
    if failures:
        print(f"FAIL — {len(failures)} mismatch(es)")
        return 1
    print("OK — db.py is equivalent to db.mjs (helpers, hashes, and live persisted rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
