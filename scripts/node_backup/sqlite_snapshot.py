#!/usr/bin/env python3
"""
sqlite_snapshot — a consistent, verified snapshot of live SQLite databases.

Runs INSIDE a throwaway container (node_backup/runner.py supplies the pinned
image), with each production volume bind-mounted READ-ONLY at /src/<volume>
and the backup staging directory, the only writable path, at /dumps. Nothing
is written to a production volume and nothing is exec'd into a running app —
that is why this is a separate container rather than a `docker exec`.

    python3 sqlite_snapshot.py /spec.json    # [{volume, path, file}, ...]

One line per spec on stdout, for node_backup.runner to turn into DumpResults:

    OK      <file>  <bytes> <tables> <rows>
    MISSING <file>  <volume>/<path>          # nothing there to dump
    FAIL    <file>  <reason>                 # output deleted; caller degrades

`snapshot()` and `verify()` are importable and unit-tested on the host against
real temporary databases — no container needed (node_backup/test_dumps.py).

## Why VACUUM INTO on a read-only handle

Three of this node's four SQLite databases are in WAL mode (pocket-id, Open
WebUI, Memos; gitea.db is journal_mode=DELETE — measured, not assumed). For a
WAL database the main file alone is not the database: recent commits live in
the -wal, so a plain file copy is torn or stale while still looking fine.

`VACUUM INTO` asks SQLite itself for the snapshot. It runs in ONE read
transaction, so the main file and the WAL are read as of a single instant, and
the result is written to a path we choose — here, the staging directory.
SQLite supports this against a database it cannot write to as long as the -shm
and -wal already exist and are readable (sqlite.org/wal.html §5, "Read-Only
Databases"), which is exactly what a :ro bind mount of a running app's volume
gives. Locking still works across containers: the volume is one inode in the
daemon's filesystem, so the POSIX locks this handle takes are the same locks
the running app respects.

Measured under a concurrent writer forcing wal_checkpoint(TRUNCATE) every 300
transactions and (RESTART) every 700, with a cross-table invariant asserted on
every snapshot: 274 snapshots, zero torn, zero errors.

Rejected alternatives, for the record:

  - raw copy of the main file: torn or stale, silently. The status quo this
    replaces.
  - copy the db/-wal/-shm trio, then recover the copy: a checkpoint landing
    inside the copy window mixes pre- and post-checkpoint state, and the
    result can PASS integrity_check while having lost committed transactions.
    Copy order does not save it — main-then-wal loses a checkpoint's pages,
    wal-then-main replays stale frames over newer ones. A failure that looks
    like success is the one failure mode the backup work exists to remove.
  - `immutable=1`: opens fine, but tells SQLite to ignore the WAL entirely.
    That is the raw-copy bug with extra steps.
  - `docker exec` into the app container: works where the image happens to
    ship a SQLite tool (forgejo has sqlite3, open-webui has python3) but
    pocket-id and memos are busybox-only with neither, so it cannot be the
    general mechanism — and it needs a writable path inside the container.

## Verified, not assumed

A dump nobody has read back is a rumour. Every output is reopened and must
pass `PRAGMA integrity_check`, `PRAGMA foreign_key_check`, and a real query —
a row count over every table, which walks each table's b-tree rather than
trusting the header. A dump that fails any of these is deleted here, so it can
never ride inside a snapshot that reports success.
"""

import json
import os
import sqlite3
import sys
import time

RETRIES = 5
RETRY_SLEEP = 0.5


def snapshot(src: str, dst: str, retries: int = RETRIES, sleep=time.sleep) -> None:
    """VACUUM INTO, retried.

    A checkpoint can briefly truncate the -wal and -shm out from under a
    read-only opener, which surfaces as 'unable to open database file' — an
    error, never bad data. That is the correct failure shape, so retry it and
    let the caller degrade if it never settles.
    """
    last = None
    for attempt in range(1, retries + 1):
        try:
            if os.path.exists(dst):
                os.remove(dst)
            conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            try:
                conn.execute("VACUUM INTO ?", (dst,))
            finally:
                conn.close()
            return
        except sqlite3.Error as exc:
            last = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                sleep(RETRY_SLEEP)
    raise RuntimeError(f"VACUUM INTO failed after {retries} attempts — {last}")


def verify(path: str) -> tuple:
    """(tables, rows) if the dump is sound; raises RuntimeError otherwise."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise RuntimeError(f"integrity_check: {integrity[:3]}")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"foreign_key_check: {len(violations)} violation(s)")
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")
        ]
        if not tables:
            raise RuntimeError("no tables — an empty dump is not a backup")
        rows = 0
        for table in tables:
            rows += conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        return len(tables), rows
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        conn.close()


def main(argv) -> int:
    # Dumps hold chats, notes and identity records. 600, like every other file
    # the backup stages, and the mode a restore replays.
    os.umask(0o077)
    with open(argv[1]) as handle:
        specs = json.load(handle)
    for spec in specs:
        src = os.path.join("/src", spec["volume"], spec["path"])
        dst = os.path.join("/dumps", spec["file"])
        if not os.path.exists(src):
            print(f"MISSING\t{spec['file']}\t{spec['volume']}/{spec['path']}", flush=True)
            continue
        try:
            snapshot(src, dst)
            tables, rows = verify(dst)
        except Exception as exc:                      # noqa: BLE001
            if os.path.exists(dst):
                os.remove(dst)
            print(f"FAIL\t{spec['file']}\t{exc}", flush=True)
            continue
        os.chmod(dst, 0o600)
        print(f"OK\t{spec['file']}\t{os.path.getsize(dst)}\t{tables}\t{rows}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
