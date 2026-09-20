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
        """Dump one database to `path`. False on any failure; caller unlinks."""
        with open(path, "wb") as out:
            proc = self._run(
                ["docker", "exec", spec.container, "pg_dump", "-U", spec.user,
                 spec.database],
                stdout=out)
        return proc.returncode == 0


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
