#!/usr/bin/env python3
"""
Encrypted backup of every node volume — restic, in a container.
This box is your identity; a backup you haven't restored is a rumor.

    ./scripts/backup.sh passphrase set   # once: into the platform keyring
    ./scripts/backup.sh init     # once, after ~/.alodium/backup.env exists
    ./scripts/backup.sh          # then schedule it (PR 4 owns scheduling)
    ./scripts/backup.sh passphrase check # which source resolves; never prints it

The decisions live in scripts/node_backup/{plan,dumps,policy,config}.py as
pure functions with offline tests; this file is the wiring between them and
scripts/node_backup/runner.py, which is the only place that shells out, and
keyring_store.py, the only place that touches the platform secret store.

Nothing here may write to a production volume. Every data mount is :ro.
"""

import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from node_backup import config, dumps, plan, policy               # noqa: E402
from node_backup.config import BackupError                        # noqa: E402
from node_backup.runner import Docker, Dumps, Restic, RESTIC_IMAGE  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def log(msg):
    print(f"backup: {msg}", flush=True)


def err(msg):
    print(f"backup: {msg}", file=sys.stderr, flush=True)


def source_env_file(path: Path) -> dict:
    """Read backup.env, which is a shell file (`export X="$HOME/..."`).

    It is documented and shipped as shell, so it is read as shell rather than
    re-specified as an ini file that would silently mis-parse an operator's
    existing one. Everything it exports lands in the returned dict.
    """
    proc = subprocess.run(
        ["bash", "-c",
         'set -a; . "$1"; python3 -c "import os,json;print(json.dumps(dict(os.environ)))"',
         "bash", str(path)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise BackupError(f"could not read {path}")
    return json.loads(proc.stdout)


def build_restic(cfg_env: dict, repo, cache_dir: Path, pass_file: Path) -> Restic:
    base_mounts = ["-v", f"{pass_file}:/secrets/restic-pass:ro",
                   "-v", f"{cache_dir}:/cache"]
    env_flags = ["-e", "RESTIC_PASSWORD_FILE=/secrets/restic-pass",
                 "-e", "RESTIC_CACHE_DIR=/cache"]
    if repo.is_local:
        base_mounts += ["-v", f"{repo.path}:/repo"]
        env_flags += ["-e", "RESTIC_REPOSITORY=/repo"]
    else:
        env_flags += ["-e", f"RESTIC_REPOSITORY={repo.value}"]
        env_flags += config.backend_env(cfg_env)
    return Restic(base_mounts, env_flags, RESTIC_IMAGE)


def optional_env(alodium_home: Path) -> dict:
    """backup.env if there is one, else {} — `passphrase` works before it exists."""
    try:
        env_file, _ = config.find_env_file(alodium_home, REPO_ROOT)
    except BackupError:
        return {}
    return source_env_file(env_file)


def manage_passphrase(args, alodium_home: Path) -> int:
    """`backup.sh passphrase set [--generate]` and `backup.sh passphrase check`.

    The passphrase is read with getpass (the terminal, no echo) and written
    in-process through keyring, so it never appears in argv, in a subprocess
    or in a log. `check` resolves it exactly the way a backup would and
    reports only where it came from.
    """
    from node_backup import keyring_store

    action = args[0] if args else ""
    flags = args[1:]
    cfg_env = optional_env(alodium_home)
    override = config.passphrase_source(cfg_env)
    account = keyring_store.default_account()
    where = f"service '{keyring_store.SERVICE}', account '{account}'"

    if action == "check":
        config.resolve_passphrase(cfg_env, home=os.environ.get("HOME", ""))
        if override == config.KEYRING_SOURCE:
            log(f"passphrase ok — platform keyring ({keyring_store.backend_name()}), {where}")
        else:
            log(f"passphrase ok — {override} in backup.env (overrides the platform keyring)")
        return 0

    if action != "set" or not set(flags) <= {"--generate"}:
        raise BackupError("usage: backup.sh passphrase set [--generate] | passphrase check")

    generate = "--generate" in flags
    # Shown once so it can be written on paper. Only to a terminal: a
    # redirected stdout is a file or a log, and that is where it must not go.
    if generate and not sys.stdout.isatty():
        raise BackupError("--generate prints the passphrase once and needs a terminal")

    log(f"platform keyring: {keyring_store.backend_name()}")
    # Replacing the entry is how a repository gets locked out: restic keeps
    # the old key, the store forgets it. That takes a deliberate step outside
    # this script, not a re-run of it.
    if keyring_store.get(keyring_store.SERVICE, account):
        raise BackupError(
            f"the platform keyring already holds a passphrase ({where}). Refusing to "
            f"replace it: any repository initialised with it would become "
            f"unreadable. To rotate, `restic key add` first; then remove the old "
            f"entry with your platform's credential manager and re-run this.")

    if generate:
        value = keyring_store.generate()
    else:
        value = getpass.getpass("restic passphrase: ")
        if not value:
            raise BackupError("empty passphrase — nothing stored")
        if getpass.getpass("again: ") != value:
            raise BackupError("the two entries differ — nothing stored")

    keyring_store.put(keyring_store.SERVICE, account, value)
    if keyring_store.get(keyring_store.SERVICE, account) != value:
        raise BackupError(f"stored, but reading it back returned something else ({where})")
    log(f"stored in the platform keyring ({where}) and read back")
    if generate:
        print(f"\n    {value}\n")
        print("Write this down and keep the paper off this machine. Losing it loses "
              "every backup; restic cannot recover it.")
    if override != config.KEYRING_SOURCE:
        err(f"WARN backup.env sets {override}, which overrides the keyring — "
            f"remove it for this entry to be used")
    return 0


def main(argv) -> int:
    subcommand = argv[1] if len(argv) > 1 else ""
    os.chdir(REPO_ROOT)

    # Managing the passphrase needs neither the stack nor the local layer:
    # it is the step before `init`, and it runs from any checkout.
    if subcommand == "passphrase":
        return manage_passphrase(argv[2:], config.alodium_home())

    config.preflight_local_layer(REPO_ROOT)

    home = os.environ.get("HOME", "")
    alodium_home = config.alodium_home()
    env_file, ignored = config.find_env_file(alodium_home, REPO_ROOT)
    for other in ignored:
        err(f"WARN ignoring {other} — {env_file} takes precedence")
    cfg_env = source_env_file(env_file)

    repo = config.classify_repository(cfg_env.get("RESTIC_REPOSITORY", ""), home)
    cache_dir = Path(cfg_env.get("RESTIC_CACHE_DIR") or (alodium_home / "cache" / "restic"))

    alodium_home.mkdir(parents=True, exist_ok=True)
    alodium_home.chmod(0o700)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # The passphrase is resolved on the HOST and handed to the container as a
    # mounted file: never argv, never the container's environment.
    stage = Path(tempfile.mkdtemp(prefix="alodium-backup."))
    stage.chmod(0o700)
    try:
        pass_file = stage / "restic-pass"
        passphrase = config.resolve_passphrase(cfg_env, home=home)
        pass_file.write_text(passphrase + "\n")
        pass_file.chmod(0o600)

        restic = build_restic(cfg_env, repo, cache_dir, pass_file)

        # Creating the repository is an explicit, operator-typed step. Nothing
        # on the normal path creates one: `restic backup` against a missing
        # repository must fail, not quietly start a second, empty history next
        # to the real one.
        if subcommand == "init":
            if repo.is_local:
                repo.path.mkdir(parents=True, exist_ok=True)
            restic.check(["init"])
            log(f"repository initialized: {repo.value}")
            return 0
        if subcommand:
            raise BackupError(f"unknown subcommand '{subcommand}' (expected none, 'init' or 'passphrase')")

        if repo.is_local and not (repo.path / "config").is_file():
            raise BackupError(f"no restic repository at {repo.path} — run "
                              f"./scripts/backup.sh init first")

        return run_backup(restic, stage)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def run_backup(restic: Restic, stage: Path) -> int:
    docker = Docker(REPO_ROOT)

    compose_config = docker.compose_config(docker.all_profiles())
    owners = plan.volume_owners(compose_config)
    declared = plan.declared_volumes(plan.manifest_backup_volumes(REPO_ROOT / "manifest"))
    # Asked once and shared: the volume plan and the dump plan classify against
    # the same view of the node, so a volume cannot be present for one and
    # absent for the other.
    volumes = docker.volumes()
    ran_services = docker.project_services()
    vplan = plan.plan_volumes(declared, owners, volumes, ran_services)

    for skipped in vplan.skipped:
        log(f"skip    {skipped}")
    if vplan.missing:
        for missing in vplan.missing:
            err(f"ERROR missing volume {missing}")
        raise BackupError(
            f"{len(vplan.missing)} declared volume(s) missing — refusing to take a "
            f"snapshot that pretends they are backed up")
    if not vplan.include:
        raise BackupError(
            "computed volume set is empty — that is the bug this script was "
            "rewritten to make impossible, not a backup")
    for vol in vplan.include:
        log(f"include {vol}")

    # Dumps stage in a private mktemp dir (700) removed in the finally above,
    # so a crash no longer leaves LiteLLM keys and user data readable in /tmp.
    # 0600 on the files is also the mode a restore replays.
    dump_dir = stage / "dumps"
    dump_dir.mkdir()
    dump_dir.chmod(0o700)

    # What gets dumped is declared by the app that owns the data. A manifest
    # that declares nothing DEGRADES the run rather than aborting it: refusing
    # to protect the other twelve apps because one manifest is incomplete is
    # the worse outcome, and "degraded" already means exactly "there is data
    # here we did not capture". verify-config.sh blocks it pre-merge, where it
    # is cheap to fix.
    specs, decl_errors = dumps.declared_dumps(REPO_ROOT / "manifest")
    dplan = dumps.plan_dumps(specs, docker.running_containers(),
                             ran_services, volumes)
    drivers = Dumps(REPO_ROOT, plan.PROJECT)
    written, failed, uninitialized = dumps.execute_dumps(
        dplan, dump_dir,
        run_postgres=drivers.postgres,
        run_sqlite=drivers.sqlite,
        unlink=lambda path: Path(path).unlink(missing_ok=True))
    degraded = dumps.declaration_outcomes(decl_errors) + dplan.degraded + failed

    for skipped in dplan.skipped + uninitialized:
        log(f"skip    dump {skipped}")
    log(f"dumps   {' '.join(written) if written else '<none>'}")
    for reason in degraded:
        err(f"DEGRADED dump missing: {reason}")

    mounts, targets = policy.data_mounts(vplan.include, str(dump_dir))
    restic.check(policy.backup_args(plan.PROJECT, targets, bool(degraded)), mounts)

    calls = policy.retention_calls(plan.PROJECT, bool(degraded))
    if not calls:
        log("retention skipped — nothing was expired")
        raise BackupError(
            f"degraded run: snapshot taken and tagged '{policy.TAG_PARTIAL}', "
            f"{len(degraded)} dump(s) missing (listed above). Volumes are backed "
            f"up; the databases above are not.")
    for call in calls:
        restic.check(call)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"complete: {stamp}  volumes={len(vplan.include)} "
        f"skipped={len(vplan.skipped)} dumps={len(written)}")
    return 0


if __name__ == "__main__":
    # The "never exit 0 without completing" guarantee. main() returns 0 from
    # exactly one place; every other route out of this process — a raised
    # BackupError, an unexpected exception, a signal — lands here and exits
    # non-zero. The bash predecessor needed an explicit flag for this because
    # macOS bash 3.2 aborts `set -u` violations with status ZERO; in Python it
    # is structural, but the guarantee is the same and it is the whole point.
    try:
        code = main(sys.argv)
    except BackupError as exc:
        err(f"ERROR {exc}")
        err(f"FAILED (exit {exc.code})")
        sys.exit(exc.code)
    except KeyboardInterrupt:
        err("FAILED (interrupted)")
        sys.exit(130)
    except BaseException:                                   # noqa: BLE001
        import traceback
        traceback.print_exc()
        err("FAILED (unexpected error above)")
        sys.exit(1)
    if code != 0:
        err(f"FAILED (exit {code})")
    sys.exit(code)
