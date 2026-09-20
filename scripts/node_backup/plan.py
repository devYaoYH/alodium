"""
plan — what gets backed up, and what a missing thing means.

Pure functions over plain data: a parsed compose config, the set of volumes
docker reports, the set of services that have a container in this project. No
subprocess, no docker, no filesystem except through injected callables. That is
the point: `include vs skip vs missing` and `dump vs degraded vs never-ran` are
the decisions a backup gets wrong, and they are checkable in milliseconds.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT = "sovereign-node"

# Core volumes: the stack itself. Not optional, not profile-gated; a missing
# one is a hard failure.
CORE_VOLUMES = [
    "forgejo_data",    # node-config, coordination, every issue and PR
    "litellm_db",      # virtual keys and spend history
    "radicale_data",   # calendars and contacts
    "caddy_data",      # the internal CA — restoring it is how TLS survives
    "pocketid_data",   # identity: SQLite + passkey public halves
]


@dataclass(frozen=True)
class DumpSpec:
    """One Postgres database to dump, and the container to dump it from."""
    container: str
    service: str
    user: str
    database: str
    filename: str


# Dump Postgres rather than trusting a copy of live files. The list is still
# hardcoded here; moving it into each app manifest — and adding the missing
# redash and egress-audit dumps, `pg_dump -Fc`, and SQLite consistency — is
# PR 2 of coordination#71. This list is the seam it replaces: PR 2 builds these
# from the manifests instead of spelling them out.
DUMP_SPECS = [
    DumpSpec("litellm-db", "litellm-db", "litellm", "litellm", "litellm.sql"),
    DumpSpec("miniflux-db", "miniflux-db", "miniflux", "miniflux", "miniflux.sql"),
    DumpSpec("search-audit-db", "search-audit-db", "search_audit_owner",
             "search_audit", "search_audit.sql"),
]


# --------------------------------------------------------------------------
# Volumes
# --------------------------------------------------------------------------

@dataclass
class VolumePlan:
    include: list[str] = field(default_factory=list)   # full docker volume names
    skipped: list[str] = field(default_factory=list)   # human-readable reasons
    missing: list[str] = field(default_factory=list)   # non-empty => fatal

    @property
    def fatal(self) -> bool:
        return bool(self.missing) or not self.include


def manifest_backup_volumes(manifest_dir: Path) -> list[str]:
    """Every volume any app manifest declares under [lifecycle].backup.

    The manifest is the inventory (DESIGN.md). Add an app, declare its backup,
    and the include list follows; nothing to remember.
    """
    out: list[str] = []
    for path in sorted(Path(manifest_dir).glob("*.toml")):
        if path.name.endswith(".example.toml"):
            continue
        data = tomllib.loads(path.read_text())
        for vol in data.get("lifecycle", {}).get("backup", []):
            out.append(vol.removeprefix(f"{PROJECT}_"))
    return out


def declared_volumes(app_volumes, core=None) -> list[str]:
    """Core volumes plus the manifest-declared ones, deduped, order preserved.

    radicale_data is both core and manifest-declared, so dedupe is not
    cosmetic — it would otherwise be mounted twice.
    """
    core = CORE_VOLUMES if core is None else core
    seen, out = set(), []
    for vol in list(core) + list(app_volumes):
        if vol not in seen:
            seen.add(vol)
            out.append(vol)
    return out


def volume_owners(compose_config: dict) -> dict[str, list[str]]:
    """volume name -> the compose services that mount it.

    Built from `docker compose config --format json` across ALL profiles, so
    profile-gated services are visible. A declared volume that no service
    mounts is a manifest that has drifted from the compose tree, and drift is
    exactly how a volume silently stops being backed up.
    """
    owners: dict[str, list[str]] = {name: [] for name in compose_config.get("volumes", {})}
    for service, spec in sorted((compose_config.get("services") or {}).items()):
        for mount in spec.get("volumes") or []:
            if mount.get("type") == "volume" and mount.get("source") in owners:
                owners[mount["source"]].append(service)
    return owners


def plan_volumes(declared, owners, existing_volumes, ran_services,
                 project: str = PROJECT) -> VolumePlan:
    """Classify every declared volume: include, skip, or missing.

    `existing_volumes` are full docker names (sovereign-node_foo) that exist.
    `ran_services` are compose services with a container in this project —
    running, exited or merely created. That is "has this app's profile ever
    run", and it is what separates "not installed yet, skip" from "installed,
    and its data is GONE".
    """
    plan = VolumePlan()
    existing_volumes = set(existing_volumes)
    ran_services = set(ran_services)

    for vol in declared:
        full = f"{project}_{vol}"

        if vol not in owners:
            plan.missing.append(
                f"{full} — declared for backup, but no compose service defines it "
                f"(manifest drift)")
            continue

        if full in existing_volumes:
            plan.include.append(full)
            continue

        # The volume does not exist. Either the app has never run (fine — say so
        # out loud), or it has run and its data is missing (never fine).
        ran = next((svc for svc in owners[vol] if svc in ran_services), None)
        if ran:
            plan.missing.append(
                f"{full} — service '{ran}' has run on this node, but the volume "
                f"does not exist")
        else:
            svcs = ",".join(owners[vol]) or "no service"
            plan.skipped.append(f"{full} (profile never run: {svcs})")

    return plan


# --------------------------------------------------------------------------
# Dumps
# --------------------------------------------------------------------------

@dataclass
class DumpPlan:
    to_run: list[DumpSpec] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)    # never ran here; clean
    degraded: list[str] = field(default_factory=list)   # ran here, can't dump


def plan_dumps(specs, running_containers, ran_services) -> DumpPlan:
    """Three outcomes, and the distinction is the point.

    The operator stops Docker to reclaim RAM, so a partially-up node is a
    normal state here, not an edge case — and a dump that is missing because
    its database was down must never ride inside a snapshot that reports
    success. Mirrors the volume logic deliberately:

        running                          -> dump it
        has run on this node, but down   -> degraded
        never run on this node           -> clean skip
    """
    plan = DumpPlan()
    running_containers = set(running_containers)
    ran_services = set(ran_services)

    for spec in specs:
        if spec.container in running_containers:
            plan.to_run.append(spec)
        elif spec.service in ran_services:
            plan.degraded.append(
                f"{spec.database} — service '{spec.service}' has run on this node "
                f"but its container is not running")
        else:
            plan.skipped.append(
                f"{spec.database} (service '{spec.service}' has never run)")
    return plan


def execute_dumps(plan: DumpPlan, dump_dir, pg_dump, unlink) -> tuple[list[str], list[str]]:
    """Run the planned dumps. Returns (written filenames, new degraded reasons).

    `pg_dump(spec, path) -> bool` and `unlink(path)` are injected so this is
    testable without a daemon. A failing pg_dump against a *running* container
    is degraded too, and the half-written file is deleted: a truncated dump
    restores as a database and looks like data.
    """
    written: list[str] = []
    degraded: list[str] = []
    for spec in plan.to_run:
        path = Path(dump_dir) / spec.filename
        if pg_dump(spec, path):
            written.append(spec.filename)
        else:
            unlink(path)
            degraded.append(
                f"{spec.database} — pg_dump failed against the running container "
                f"'{spec.container}'")
    return written, degraded
