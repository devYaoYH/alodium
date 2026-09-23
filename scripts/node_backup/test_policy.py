#!/usr/bin/env python3
"""
Offline tests for node_backup.policy and node_backup.config.

policy returns restic argv, so these assert the flags themselves — the bug
this module exists to prevent was one missing tag, and a test that only
checked "retention ran" would not have caught it.

Covers:

  - a clean run is tagged `complete`, a degraded run `partial`
  - --host and --exclude-caches are on every backup
  - retention is two disjoint pools (complete: 7/4/6, partial: keep-last 3)
    plus exactly one prune, and NOTHING when the run is degraded
  - the complete pool's selector can never match a partial snapshot
  - every production data mount is :ro, at /data/<volume>, dumps included
  - passphrase precedence: command > file > literal (> keyring, see
    test_keyring.py), and empty is an error
  - repository classification: local path vs backend URL vs nonsense
  - the local-layer preflight message

Run:  python3 scripts/node_backup/test_policy.py    (from the repo root)
      ./scripts/verify-config.sh                    (runs it with the rest)
Stdlib only.
"""

import importlib
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
importlib.import_module("node_backup")
policy = importlib.import_module("node_backup.policy")
config = importlib.import_module("node_backup.config")

FAIL = 0


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


P = "sovereign-node"


# ---- 1. tagging ------------------------------------------------------------

check("tags: a clean run is tagged complete",
      policy.snapshot_tags(P, degraded=False) == ["--tag", P, "--tag", "complete"],
      detail=str(policy.snapshot_tags(P, False)))
check("tags: a degraded run is tagged partial",
      policy.snapshot_tags(P, degraded=True) == ["--tag", P, "--tag", "partial"])
check("tags: the two classes are never both present",
      "partial" not in policy.snapshot_tags(P, False)
      and "complete" not in policy.snapshot_tags(P, True))


# ---- 2. backup argv --------------------------------------------------------

args = policy.backup_args(P, ["/data/a", "/data/dumps"], degraded=False)
check("backup: starts with the subcommand and the targets",
      args[:3] == ["backup", "/data/a", "/data/dumps"], detail=str(args))
check("backup: pins --host so retention groups survive a random container name",
      args[args.index("--host") + 1] == P)
check("backup: keeps --exclude-caches", "--exclude-caches" in args)
check("backup: a degraded snapshot is still taken",
      policy.backup_args(P, ["/data/a"], degraded=True)[0] == "backup")


# ---- 3. retention pools ----------------------------------------------------

calls = policy.retention_calls(P, degraded=False)
check("retention: three calls — two forgets and one prune",
      [c[0] for c in calls] == ["forget", "forget", "prune"], detail=str([c[0] for c in calls]))

complete_call, partial_call, prune_call = calls
check("retention: the real policy selects complete only",
      complete_call[complete_call.index("--tag") + 1] == f"{P},complete",
      detail=str(complete_call))
check("retention: the real policy is 7 daily / 4 weekly / 6 monthly",
      complete_call[complete_call.index("--keep-daily") + 1] == "7"
      and complete_call[complete_call.index("--keep-weekly") + 1] == "4"
      and complete_call[complete_call.index("--keep-monthly") + 1] == "6")
check("retention: partials are bounded by --keep-last 3",
      partial_call[partial_call.index("--tag") + 1] == f"{P},partial"
      and partial_call[partial_call.index("--keep-last") + 1] == "3")
check("retention: partials get no daily/weekly/monthly policy of their own",
      not any(f.startswith("--keep-daily") for f in partial_call))
check("retention: both pools group by host alone",
      all(c[c.index("--group-by") + 1] == "host" for c in (complete_call, partial_call)))
check("retention: exactly one prune", [c[0] for c in calls].count("prune") == 1)
check("retention: --prune is not smuggled onto a forget",
      not any("--prune" in c for c in calls))

# The regression this whole class scheme exists for: a partial must never be
# able to satisfy — and therefore evict from — the complete pool.
check("retention: the complete selector cannot match a partial snapshot",
      "partial" not in complete_call[complete_call.index("--tag") + 1])
check("retention: the two selectors are disjoint",
      complete_call[complete_call.index("--tag") + 1]
      != partial_call[partial_call.index("--tag") + 1])

check("retention: a degraded run expires nothing at all",
      policy.retention_calls(P, degraded=True) == [])


# ---- 4. data mounts --------------------------------------------------------

mounts, targets = policy.data_mounts(["sovereign-node_forgejo_data",
                                      "sovereign-node_caddy_data"], "/tmp/x/dumps")
vflags = [m for m in mounts if m != "-v"]
check("mounts: every production mount is read-only",
      all(m.endswith(":ro") for m in vflags), detail=str(vflags))
check("mounts: volumes land at /data/<volume>",
      "sovereign-node_forgejo_data:/data/sovereign-node_forgejo_data:ro" in vflags)
check("mounts: the dump dir rides along, also read-only",
      "/tmp/x/dumps:/data/dumps:ro" in vflags)
check("mounts: targets match the mount points",
      targets == ["/data/sovereign-node_forgejo_data",
                  "/data/sovereign-node_caddy_data", "/data/dumps"], detail=str(targets))
check("mounts: no writable data mount can be produced",
      not any(m.endswith(":rw") for m in vflags))


# ---- 5. passphrase precedence ---------------------------------------------

env_all = {"RESTIC_PASSWORD_COMMAND": "cmd",
           "RESTIC_PASSWORD_FILE": "/f",
           "RESTIC_PASSWORD": "literal"}
check("passphrase: the command wins over file and literal",
      config.resolve_passphrase(env_all, run_command=lambda c: "from-cmd\n",
                                read_file=lambda p: "from-file") == "from-cmd")
check("passphrase: the file wins over the literal",
      config.resolve_passphrase({"RESTIC_PASSWORD_FILE": "/f", "RESTIC_PASSWORD": "lit"},
                                read_file=lambda p: "from-file\n") == "from-file")
check("passphrase: the literal is the last resort",
      config.resolve_passphrase({"RESTIC_PASSWORD": "lit"}) == "lit")
check("passphrase: ~ in the file path is expanded",
      config.resolve_passphrase({"RESTIC_PASSWORD_FILE": "~/p"},
                                read_file=lambda p: p, home="/Users/x") == "/Users/x/p")


def raises(fn, needle):
    try:
        fn()
    except config.BackupError as exc:
        return needle in str(exc)
    return False


# With no override the keyring is the source, so "no source at all" now means
# no override AND an empty keyring. Injected, so no real store is asked.
check("passphrase: no source at all is an error",
      raises(lambda: config.resolve_passphrase({}, keyring_get=lambda s, a: None,
                                               account="u"),
             "supplies no passphrase"))
check("passphrase: a command that returns nothing is an error, not an empty key",
      raises(lambda: config.resolve_passphrase({"RESTIC_PASSWORD_COMMAND": "c"},
                                               run_command=lambda c: ""), "lookup failed"))
# The recipe for storing a passphrase is now `backup.sh passphrase set` on
# every platform, not the macOS-only `security add-generic-password`.
check("passphrase: the empty-result message points at the recipe for storing one",
      raises(lambda: config.resolve_passphrase({"RESTIC_PASSWORD_COMMAND": "c"},
                                               run_command=lambda c: "\n"),
             "backup.sh passphrase set"))
check("passphrase: trailing newline is stripped, inner content is not",
      config.resolve_passphrase({"RESTIC_PASSWORD": "a b\n"}) == "a b")


# ---- 5b. BackupError never carries a zero exit code ------------------------

check("error: restic's own exit code is carried through",
      config.BackupError("x", code=12).code == 12)
check("error: the default is 1", config.BackupError("x").code == 1)
check("error: a zero code is coerced to 1 — a backup that raised did not happen",
      config.BackupError("x", code=0).code == 1)


# ---- 6. repository classification -----------------------------------------

r = config.classify_repository("/Users/x/.alodium/backups/restic", home="/Users/x")
check("repo: an absolute path is local and gets bind-mounted",
      r.is_local and str(r.path).endswith("/backups/restic"))
check("repo: ~ is expanded",
      str(config.classify_repository("~/.alodium/backups/restic", "/Users/x").path)
      == "/Users/x/.alodium/backups/restic")
b2 = config.classify_repository("b2:bucket:sovereign-node", home="/Users/x")
check("repo: a backend URL is not local and is passed through verbatim",
      (not b2.is_local) and b2.path is None and b2.value == "b2:bucket:sovereign-node")
check("repo: an empty repository is an error",
      raises(lambda: config.classify_repository("", "/h"), "no RESTIC_REPOSITORY"))
check("repo: a bare word is an error, not a relative guess",
      raises(lambda: config.classify_repository("restic-dev", "/h"), "neither an absolute path"))


# ---- 7. backend credential forwarding -------------------------------------

flags = config.backend_env({"B2_ACCOUNT_ID": "id", "B2_ACCOUNT_KEY": "",
                            "AWS_ACCESS_KEY_ID": "k", "FORGEJO_TOKEN": "secret"})
check("backend: only set credentials are forwarded",
      flags == ["-e", "B2_ACCOUNT_ID=id", "-e", "AWS_ACCESS_KEY_ID=k"], detail=str(flags))
check("backend: unrelated secrets are never forwarded",
      not any("FORGEJO" in f for f in flags))


# ---- 8. local-layer preflight ---------------------------------------------

check("preflight: a checkout with the local layer passes",
      config.preflight_local_layer("/repo", exists=lambda p: True) is None)
check("preflight: a worktree without .env is refused with a useful message",
      raises(lambda: config.preflight_local_layer("/wt", exists=lambda p: p.name != ".env"),
             "not a git worktree"))
check("preflight: a missing secrets/ is caught too",
      raises(lambda: config.preflight_local_layer("/wt", exists=lambda p: p.name != "secrets"),
             "secrets"))


# ---- 9. env-file precedence ------------------------------------------------

chosen, ignored = config.find_env_file("/home/.alodium", "/repo", exists=lambda p: True)
check("env: ~/.alodium/backup.env wins", str(chosen) == "/home/.alodium/backup.env")
check("env: the losing candidate is reported, not silently dropped",
      [str(p) for p in ignored] == ["/repo/scripts/backup.env"], detail=str(ignored))
only_legacy, ignored2 = config.find_env_file(
    "/home/.alodium", "/repo", exists=lambda p: p.name == "backup.env" and "scripts" in str(p))
check("env: the legacy path still works alone",
      str(only_legacy) == "/repo/scripts/backup.env" and ignored2 == [])
check("env: no env file at all is an error naming the documented home",
      raises(lambda: config.find_env_file("/home/.alodium", "/repo", exists=lambda p: False),
             "/home/.alodium/backup.env"))


print()
if FAIL == 0:
    print("test_policy: PASS")
    sys.exit(0)
print(f"test_policy: FAIL ({FAIL} failures)")
sys.exit(1)
