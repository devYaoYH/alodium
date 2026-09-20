"""
runner — the edge. Everything that shells out lives here and nowhere else.

Docker is the only thing that can read a named volume on every platform we
care about (macOS, Windows/WSL2, Linux), so restic runs where the data is:
inside a container, with each volume bind-mounted READ-ONLY at /data/<volume>.

The predecessor to this module derived host paths from `docker volume inspect
… .Mountpoint`, i.e. /var/lib/docker/volumes/…. On Docker Desktop that path
exists only inside the LinuxKit VM, so a `-d` test dropped EVERY volume, restic
snapshotted the pg dumps alone — and the script printed "backup complete".
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from .config import BackupError

# Pinned by digest like every other image on this node. This is a multi-arch
# manifest list (linux/amd64, arm64, arm, 386) — checked deliberately, because
# an arm64-only pin would break a restore onto an amd64 machine, which is the
# entire point of having backups (coordination#71; PR 6 audits the other pins).
RESTIC_IMAGE = ("restic/restic:0.19.0@sha256:"
                "7f44e0057b82348597568ea209360762d0b38f8e1dbc8ad859661ac1055e45f2")

# The SQLite toolbox: a throwaway container that mounts each production volume
# READ-ONLY and asks SQLite itself for a consistent snapshot. Same pin the
# registry service already runs, so the backup path introduces no new image to
# audit, and Python's stdlib sqlite3 is a full SQLite (VACUUM INTO,
# integrity_check) with no apk install at backup time.
SQLITE_IMAGE = ("python:3.12-alpine@sha256:"
                "6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df")

# Used only to read a dump back with `pg_restore -l`. Same pin as every
# Postgres on this node, so pg_restore always matches the pg_dump that wrote
# the file. Never given a production volume, never started as a server.
POSTGRES_IMAGE = ("postgres:16-alpine@sha256:"
                  "16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229")


def _run(argv, **kw):
    return subprocess.run(argv, **kw)


class Docker:
    """Read-only queries about the node, plus pg_dump. Never starts anything."""

    def __init__(self, repo_root: Path, run=_run):
        self.repo_root = Path(repo_root)
        self._run = run

    # --- compose -----------------------------------------------------------

    def all_profiles(self) -> str:
        """Every profile declared anywhere in the compose tree, comma-joined.

        Profile-gated services are invisible to a bare `compose config`, so we
        enumerate them all — same trick, same reason, as deploy.sh step 4b.
        """
        names = set()
        files = [self.repo_root / "docker-compose.yml"]
        files += sorted(self.repo_root.glob("apps/*/compose.yaml"))
        for path in files:
            try:
                text = path.read_text()
            except OSError:
                continue
            for line in text.splitlines():
                if "profiles:" not in line or "[" not in line:
                    continue
                inside = line[line.index("[") + 1:line.rindex("]")] if "]" in line else ""
                for name in inside.split(","):
                    name = name.strip().strip('"').strip("'")
                    if name:
                        names.add(name)
        if names:
            return ",".join(sorted(names))
        proc = self._run(["docker", "compose", "config", "--profiles"],
                         cwd=self.repo_root, capture_output=True, text=True)
        return ",".join(proc.stdout.split()) if proc.returncode == 0 else ""

    def compose_config(self, profiles: str) -> dict:
        env = dict(os.environ, COMPOSE_PROFILES=profiles)
        proc = self._run(["docker", "compose", "config", "--format", "json"],
                         cwd=self.repo_root, env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            raise BackupError("docker compose config failed — refusing to guess "
                              "what to back up")
        return json.loads(proc.stdout)

    def project_services(self) -> set[str]:
        """Services with a container in this project — running, exited or created."""
        proc = self._run(["docker", "compose", "ps", "-a", "--services"],
                         cwd=self.repo_root, capture_output=True, text=True)
        return set(proc.stdout.split()) if proc.returncode == 0 else set()

    # --- daemon ------------------------------------------------------------

    def volumes(self) -> set[str]:
        proc = self._run(["docker", "volume", "ls", "--format", "{{.Name}}"],
                         capture_output=True, text=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            raise BackupError("docker volume ls failed — is the daemon running?")
        return set(proc.stdout.split())

    def running_containers(self) -> set[str]:
        proc = self._run(["docker", "ps", "--format", "{{.Names}}"],
                         capture_output=True, text=True)
        return set(proc.stdout.split()) if proc.returncode == 0 else set()

    def pg_dump(self, spec, path) -> bool:
        """Dump one database to `path`. False on any failure; caller unlinks.

        `-Fc`: custom format, compressed, and restorable selectively. The plain
        SQL it replaces was 603.73 MB for litellm alone and dominated the whole
        snapshot; the same data is 150.95 MB here. It also buys the readback
        below — pg_restore can list a custom dump's table of contents, which a
        plain .sql file offers no equivalent of short of loading it.
        """
        with open(path, "wb") as out:
            proc = self._run(
                ["docker", "exec", spec.container, "pg_dump", "-Fc", "-U", spec.user,
                 spec.database],
                stdout=out)
        return proc.returncode == 0


class Dumps:
    """The dump drivers: the two container runs, and pg_dump's readback.

    Each driver takes the specs for its kind and returns
    {filename: DumpResult}, which is all node_backup.dumps.execute_dumps needs
    — so the classification and the degrade/skip bookkeeping stay pure and the
    tests never start a container.

    Verification lives HERE, inside the drivers, on purpose: a dump that was
    written but cannot be read back reports FAILED exactly like one that was
    never written, so it lands in the same degraded list. "Produced but
    unusable" does not get a second mechanism.
    """

    def __init__(self, repo_root: Path, project: str, run=_run,
                 sqlite_image: str = SQLITE_IMAGE,
                 postgres_image: str = POSTGRES_IMAGE):
        self.repo_root = Path(repo_root)
        self.project = project
        self._run = run
        self.sqlite_image = sqlite_image
        self.postgres_image = postgres_image

    # --- postgres ----------------------------------------------------------

    def postgres(self, specs, dump_dir) -> dict:
        """pg_dump each database, then read every written archive back."""
        from .dumps import DumpResult, FAILED, OK       # local: avoid a cycle

        results = {}
        written = []
        for spec in specs:
            path = Path(dump_dir) / spec.filename
            if self._pg_dump_600(spec, path):
                written.append(spec.filename)
            else:
                results[spec.filename] = DumpResult(
                    FAILED,
                    f"pg_dump failed against the running container "
                    f"'{spec.container}'")
        if not written:
            return results

        for filename, (ok, detail) in self._pg_restore_list(dump_dir, written).items():
            results[filename] = (DumpResult(OK, detail) if ok
                                 else DumpResult(FAILED, detail))
        return results

    def _pg_dump_600(self, spec, path) -> bool:
        ok = Docker(self.repo_root, self._run).pg_dump(spec, path)
        try:
            Path(path).chmod(0o600)
        except OSError:
            pass
        return ok

    def _pg_restore_list(self, dump_dir, filenames) -> dict:
        """{filename: (ok, detail)} from `pg_restore -l`.

        pg_dump exiting 0 says the dump was written, not that it can be read.
        Listing a custom-format archive's table of contents parses the whole
        file, so a truncated or corrupt dump fails here rather than at restore
        time — the same bargain integrity_check makes for the SQLite dumps.
        """
        script = (
            'for f in "$@"; do\n'
            '  if entries=$(pg_restore -l "/dumps/$f" 2>/dev/null | grep -c "^[0-9]"); then\n'
            '    printf "OK\\t%s\\t%s\\n" "$f" "$entries"\n'
            '  else\n'
            '    printf "FAIL\\t%s\\t%s\\n" "$f" "$(pg_restore -l "/dumps/$f" 2>&1 | head -1)"\n'
            '  fi\n'
            'done\n')
        proc = self._run(
            ["docker", "run", "--rm", "-v", f"{dump_dir}:/dumps:ro",
             "--entrypoint", "sh", self.postgres_image, "-c", script, "sh", *filenames],
            capture_output=True, text=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            raise BackupError("the pg_restore readback container failed outright — "
                              "the Postgres dumps are unverified")
        out = {}
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            status, filename, detail = parts
            if status == "OK" and detail.isdigit() and int(detail) > 0:
                out[filename] = (True, f"{detail} archive entries; pg_restore -l ok")
            else:
                out[filename] = (
                    False,
                    f"written by pg_dump but unreadable by pg_restore: "
                    f"{detail or 'no table of contents'}")
        for filename in filenames:
            out.setdefault(filename, (False, "pg_restore printed no verdict for it"))
        return out

    # --- sqlite ------------------------------------------------------------

    def sqlite(self, specs, dump_dir) -> dict:
        """One container run for every SQLite database.

        Each source volume is mounted READ-ONLY at /src/<volume>; the staging
        dump directory is the only writable path. See sqlite_snapshot.py for
        why VACUUM INTO on a read-only handle is the method and what was
        rejected.
        """
        from .dumps import DumpResult, FAILED, MISSING, OK   # local: avoid a cycle

        dump_dir = Path(dump_dir)
        spec_file = dump_dir.parent / "sqlite-spec.json"
        spec_file.write_text(json.dumps(
            [{"volume": s.volume, "path": s.path, "file": s.filename} for s in specs]))
        spec_file.chmod(0o600)

        mounts = []
        for volume in dict.fromkeys(s.volume for s in specs):
            mounts += ["-v", f"{self.project}_{volume}:/src/{volume}:ro"]
        program = self.repo_root / "scripts" / "node_backup" / "sqlite_snapshot.py"

        proc = self._run(
            ["docker", "run", "--rm", *mounts,
             "-v", f"{dump_dir}:/dumps",
             "-v", f"{program}:/sqlite_snapshot.py:ro",
             "-v", f"{spec_file}:/spec.json:ro",
             self.sqlite_image, "python3", "/sqlite_snapshot.py", "/spec.json"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout + proc.stderr)
            raise BackupError("the sqlite dump container failed outright — no SQLite "
                              "database was snapshotted")

        results = {}
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status, filename = parts[0], parts[1]
            rest = parts[2:]
            if status == "OK" and len(rest) == 3:
                size, tables, rows = rest
                results[filename] = DumpResult(
                    OK, f"{size} bytes, {tables} tables, {rows} rows; integrity_check ok")
            elif status == "MISSING":
                results[filename] = DumpResult(
                    MISSING, f"{rest[0] if rest else 'the file'} is not in its volume; "
                             f"the app has not created it yet")
            else:
                results[filename] = DumpResult(
                    FAILED, rest[0] if rest else "the sqlite driver gave no reason")
        return results


class Restic:
    """restic, in a container. Holds the mounts and env every call needs.

    `base_mounts` is what restic needs to reach its repository. The production
    data mounts are passed per-call, so `init`, `forget` and `prune` never see
    a production volume at all.
    """

    def __init__(self, base_mounts: list[str], env_flags: list[str],
                 image: str = RESTIC_IMAGE, run=_run):
        self.base_mounts = base_mounts
        self.env_flags = env_flags
        self.image = image
        self._run = run

    def argv(self, restic_args: list[str], data_mounts: list[str] | None = None) -> list[str]:
        return (["docker", "run", "--rm", *self.base_mounts, *(data_mounts or []),
                 *self.env_flags, self.image] + restic_args)

    def __call__(self, restic_args: list[str], data_mounts: list[str] | None = None) -> int:
        return self._run(self.argv(restic_args, data_mounts)).returncode

    def check(self, restic_args: list[str], data_mounts: list[str] | None = None) -> None:
        code = self(restic_args, data_mounts)
        if code != 0:
            raise BackupError(f"restic {restic_args[0]} failed (exit {code})", code=code)
