#!/usr/bin/env python3
"""
Offline tests for node_host.lock — the pass lock both host jobs take.

One test per branch the bash had:

  mkdir succeeds                     -> acquired, released on exit
  fresh lock held by another pass    -> held (exit 0, lock untouched)
  lock older than the threshold      -> stolen (rmdir + mkdir)
  exactly at the threshold           -> held (`-gt`, not `-ge`)
  a FILE squatting on the lock path  -> held (the bash's `-d` was false)
  a release after a steal            -> the directory is gone

and the cutover property that made the mechanism worth keeping: a lock taken
by `mkdir` (as the bash did) is honoured by PassLock, and vice versa.

Run:  python3 scripts/node_host/test_lock.py   (from the repo root)
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_host import lock                                     # noqa: E402

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


def aged(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


with tempfile.TemporaryDirectory() as t:
    p = Path(t) / "pass.lock"

    print("acquire / release")
    with lock.PassLock(p, 1800) as lk:
        check("free lock is acquired", lk.acquire() == lock.ACQUIRED)
        check("the lock is a directory", p.is_dir())
        other = lock.PassLock(p, 1800)
        check("a second pass sees it held", other.acquire() == lock.HELD)
        other.release()
        check("releasing an un-held lock leaves it alone", p.is_dir())
    check("exit releases it", not p.exists())

    print("stale steal")
    p.mkdir()
    aged(p, 1801)
    lk = lock.PassLock(p, 1800)
    check("older than threshold -> stolen", lk.acquire() == lock.STOLEN)
    lk.release()
    check("stolen lock is released on exit", not p.exists())

    now = time.time()
    p.mkdir()
    os.utime(p, (now - 1800, now - 1800))
    lk = lock.PassLock(p, 1800, clock=lambda: now)
    check("exactly at the threshold -> held (-gt)", lk.acquire() == lock.HELD)
    os.rmdir(p)

    print("odd states")
    p.write_text("not a dir")
    aged(p, 99999)
    check("a file on the path -> held, untouched", lock.PassLock(p, 1).acquire() == lock.HELD
          and p.is_file())
    p.unlink()

    print("cutover: bash's mkdir lock and PassLock honour each other")
    subprocess.run(["mkdir", str(p)], check=True)
    check("PassLock sees a shell-made lock as held", lock.PassLock(p, 1800).acquire() == lock.HELD)
    os.rmdir(p)
    lk = lock.PassLock(p, 1800)
    lk.acquire()
    check("shell mkdir fails on a PassLock",
          subprocess.run(["mkdir", str(p)], stderr=subprocess.DEVNULL).returncode != 0)
    lk.release()

    print("is_stale")
    check("1801 > 1800", lock.is_stale(1801, 1800))
    check("1800 is not > 1800", not lock.is_stale(1800, 1800))

print(f"\n{'PASS' if FAIL == 0 else f'FAIL ({FAIL})'}")
sys.exit(1 if FAIL else 0)
