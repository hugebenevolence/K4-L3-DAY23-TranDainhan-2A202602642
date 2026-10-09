"""Run a PostgreSQL 16 PITR drill against the containers in stretch/README.md."""

import json
import pathlib
import subprocess
import time


PRIMARY = "day23-postgres"
RESTORE = "day23-postgres-restore"
OUT = pathlib.Path("reports/stretch/postgres-pitr.json")


def docker(*args, input_text=None):
    result = subprocess.run(["docker", *args], input=input_text, text=True,
                            capture_output=True, check=True)
    return result.stdout.strip()


def sql(statement, container=PRIMARY):
    return docker("exec", container, "psql", "-U", "postgres", "-d", "postgres",
                  "-Atqc", statement)


def wait_for_restore(seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if docker("exec", RESTORE, "pg_isready", "-U", "postgres").endswith("accepting connections"):
                return
        except subprocess.CalledProcessError:
            pass
        time.sleep(0.5)
    raise TimeoutError("restored PostgreSQL did not become ready")


def main():
    sql("CREATE TABLE IF NOT EXISTS dr_meta (name text PRIMARY KEY, note text NOT NULL)")
    sql("INSERT INTO dr_meta VALUES ('baseline','base backup') ON CONFLICT (name) DO NOTHING")
    docker("exec", "-u", "postgres", "-e", "PGPASSWORD=labpass", PRIMARY,
           "pg_basebackup", "-h", "127.0.0.1", "-U", "postgres", "-D", "/backup/base",
           "-Fp", "-Xs")

    sql("INSERT INTO dr_meta VALUES ('before_target','recover me')")
    target = sql("SELECT clock_timestamp()")
    time.sleep(1)
    sql("INSERT INTO dr_meta VALUES ('after_target','exclude me')")
    archived_before = int(sql("SELECT archived_count FROM pg_stat_archiver"))
    sql("SELECT pg_switch_wal()")
    deadline = time.monotonic() + 30
    while int(sql("SELECT archived_count FROM pg_stat_archiver")) <= archived_before:
        if time.monotonic() > deadline:
            raise TimeoutError("WAL segment was not archived")
        time.sleep(0.5)

    recovery = ("restore_command = 'cp /archive/%f %p'\n"
                f"recovery_target_time = '{target}'\n"
                "recovery_target_action = 'promote'\n")
    docker("exec", "-i", "-u", "postgres", PRIMARY, "sh", "-c",
           "cat >> /backup/base/postgresql.auto.conf", input_text=recovery)
    docker("exec", "-u", "postgres", PRIMARY, "touch", "/backup/base/recovery.signal")

    started = time.monotonic()
    docker("run", "-d", "--name", RESTORE, "--network", "day23-stretch",
           "--mount", "source=day23-pg-backup,target=/var/lib/postgresql/data",
           "--mount", "source=day23-pg-archive,target=/archive",
           "-e", "PGDATA=/var/lib/postgresql/data/base", "postgres:16-alpine")
    try:
        wait_for_restore()
        rows = sql("SELECT name FROM dr_meta ORDER BY name", RESTORE).splitlines()
        rto = round(time.monotonic() - started, 2)
        if rows != ["baseline", "before_target"]:
            raise AssertionError(f"wrong PITR point: {rows}")
        result = {"target_time_utc": target, "rows_at_target": rows,
                  "excluded_row": "after_target", "postgres_restore_rto_s": rto,
                  "archived_wal_files": docker("exec", PRIMARY, "sh", "-c", "ls /archive | wc -l")}
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))
    finally:
        subprocess.run(["docker", "rm", "-f", RESTORE], capture_output=True)


if __name__ == "__main__":
    main()
