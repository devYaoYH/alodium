"""
dumps — which databases get dumped, how, and whether the result is real.

A copy of a live database file is a copy of whatever was on disk
mid-transaction, so backing up a volume is not backing up the database inside
it. Every database on this node is therefore dumped through its own engine,
and every dump is read back before the snapshot is allowed to report success.

WHAT gets dumped is declared by the app that owns the data — `[lifecycle]
dump` in manifest/<app>.toml — for the same reason the volume list is
generated from `[lifecycle] backup`: the manifest is the inventory
(docs/DESIGN.md). The key is MANDATORY and has no default. An app holding no
database writes `dump = []` and means it, so "declared none" and "nobody
decided" can never look alike.

Pure functions over plain data, like plan.py: parsing is a dict, classification
is three sets, execution takes injected drivers. The decisions a backup gets
wrong are checkable in milliseconds; scripts/node_backup/runner.py owns the
subprocess calls, and scripts/node_backup/sqlite_snapshot.py is the program
that runs inside the throwaway SQLite container.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .plan import PROJECT

# --------------------------------------------------------------------------
# Specs
# --------------------------------------------------------------------------
#
# `service` is the compose service that owns the data. It is what separates
# "this app has never run here, skip cleanly" from "this app has run and its
# dump is missing, which is a degraded backup" — exactly as in plan.py, and
# deliberately the same vocabulary.
#
# `filename` is declared rather than derived from the database name. Two apps
# can both own a database called "app", and a silently colliding output name
# means one dump quietly overwriting another inside the snapshot.


@dataclass(frozen=True)
class PostgresDump:
    """One Postgres database, dumped from its running container."""
    service: str
    filename: str
    container: str
    user: str
    database: str
    kind: str = "postgres"

    @property
    def label(self) -> str:
        return self.database


@dataclass(frozen=True)
class SqliteDump:
    """One SQLite database, read out of its volume without touching the app."""
    service: str
    filename: str
    volume: str
    path: str
    kind: str = "sqlite"

    @property
    def label(self) -> str:
        return self.filename


# The CORE stack declares here rather than in a manifest, for exactly the
# reason plan.CORE_VOLUMES is a literal list: litellm, forgejo and pocket-id
# are not apps ON the node, they are the node. They ship no manifest/*.toml by
# design — registry/registry.py aggregates that directory as the node's service
# catalogue, and a manifest/core.toml would advertise "forgejo" as a
# registered, callable app with a `needs` block it does not have.
# manifest/node.yaml was the other candidate and is worse: it is local-layer
# and untracked, so the declarations would exist on this node only.
CORE_DUMPS = [
    PostgresDump("litellm-db", "litellm.dump", "litellm-db", "litellm", "litellm"),
    # Forgejo runs SQLite in journal_mode=DELETE here, not WAL — measured, not
    # assumed. It is no safer for it: a rollback-journal database copied
    # mid-transaction is torn in its own way. VACUUM INTO covers both.
    SqliteDump("forgejo", "gitea.db", "forgejo_data", "gitea/gitea.db"),
    SqliteDump("pocket-id", "pocket-id.db", "pocketid_data", "pocket-id.db"),
]


# --------------------------------------------------------------------------
# Manifest parsing
# --------------------------------------------------------------------------

KIND_FIELDS = {
    "postgres": ("container", "user", "database"),
    "sqlite": ("volume", "path"),
}
COMMON_FIELDS = ("service", "file")

# Two error categories, and they must not be collapsed. "This manifest has no
# dump declaration" and "this manifest's dump declaration could not be parsed"
# send the operator to different fixes, and at 3am the log line is all they
# have.
UNDECLARED = "undeclared"
MALFORMED = "malformed"


@dataclass(frozen=True)
class DumpError:
    category: str        # UNDECLARED | MALFORMED
    manifest: str        # the file to go and edit
    message: str         # what is wrong, in full

    def __str__(self) -> str:
        return f"{self.manifest}: {self.message}"


def _parse_entry(entry, where: str, manifest: str):
    """One `dump = [...]` table -> (spec, error). Exactly one is None."""
    if not isinstance(entry, dict):
        return None, DumpError(MALFORMED, manifest, f"{where} is not a table")

    kind = entry.get("kind")
    if kind not in KIND_FIELDS:
        return None, DumpError(
            MALFORMED, manifest,
            f"{where}: kind={kind!r}, expected one of {sorted(KIND_FIELDS)}")

    fields = COMMON_FIELDS + KIND_FIELDS[kind]
    missing = [k for k in fields if not entry.get(k)]
    if missing:
        return None, DumpError(
            MALFORMED, manifest, f"{where}: kind={kind} is missing {', '.join(missing)}")

    unknown = sorted(set(entry) - {"kind"} - set(fields))
    if unknown:
        return None, DumpError(
            MALFORMED, manifest, f"{where}: unknown key(s) {', '.join(unknown)}")

    filename = entry["file"]
    if "/" in filename or filename.startswith("."):
        return None, DumpError(
            MALFORMED, manifest,
            f"{where}: file={filename!r} must be a plain filename")

    if kind == "postgres":
        return PostgresDump(entry["service"], filename, entry["container"],
                            entry["user"], entry["database"]), None
    return SqliteDump(entry["service"], filename, entry["volume"], entry["path"]), None


def manifest_dumps(manifest_dir) -> tuple[list, list]:
    """(specs, errors) from every real app manifest.

    Both are returned rather than raising, because the two callers want
    different severities and that split is deliberate:

      - verify-config.sh hard-fails on any error. That is the point where the
        mistake is cheap to fix, and where "we forgot" gets caught.
      - the backup DEGRADES on an error. Refusing to protect the other twelve
        apps because one manifest is incomplete is the worse outcome, and a
        degraded run already means exactly "there is data here we did not
        capture". It also makes the mandatory key non-breaking for a manifest
        that arrives out of band.
    """
    specs: list = []
    errors: list = []
    seen: dict = {}

    for path in sorted(Path(manifest_dir).glob("*.toml")):
        if path.name.endswith(".example.toml"):
            continue  # documentation, not an installed app
        name = path.name
        try:
            data = tomllib.loads(path.read_text())
        except (tomllib.TOMLDecodeError, OSError) as exc:
            errors.append(DumpError(MALFORMED, name, f"unreadable — {exc}"))
            continue

        lifecycle = data.get("lifecycle")
        if lifecycle is None or "dump" not in lifecycle:
            errors.append(DumpError(
                UNDECLARED, name,
                "has no [lifecycle] dump (declare it, or declare it empty with "
                "a reason)"))
            continue

        declared = lifecycle["dump"]
        if not isinstance(declared, list):
            errors.append(DumpError(
                MALFORMED, name, "[lifecycle] dump must be a list of tables"))
            continue

        for i, entry in enumerate(declared):
            spec, error = _parse_entry(entry, f"dump[{i}]", name)
            if error:
                errors.append(error)
                continue
            if spec.filename in seen:
                errors.append(DumpError(
                    MALFORMED, name,
                    f"dump file {spec.filename!r} is already declared by "
                    f"{seen[spec.filename]} — two dumps cannot share one name "
                    f"in the snapshot"))
                continue
            seen[spec.filename] = name
            specs.append(spec)

    return specs, errors


def declared_dumps(manifest_dir, core=None) -> tuple[list, list]:
    """Core specs first, then the manifest-declared ones."""
    core = CORE_DUMPS if core is None else list(core)
    specs, errors = manifest_dumps(manifest_dir)
    core_names = {s.filename for s in core}
    for spec in specs:
        if spec.filename in core_names:
            errors.append(DumpError(
                MALFORMED, "core",
                f"dump file {spec.filename!r} is already declared by the core "
                f"stack in node_backup/dumps.py"))
    return core + specs, errors


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

@dataclass
class DumpPlan:
    to_run: list = field(default_factory=list)
    skipped: list = field(default_factory=list)    # never ran here; clean
    degraded: list = field(default_factory=list)   # ran here, can't dump


def plan_dumps(specs, running_containers, ran_services, existing_volumes=(),
               project: str = PROJECT) -> DumpPlan:
    """Three outcomes, mirroring plan.plan_volumes deliberately:

        dumpable here                    -> dump it, then verify it
        has run on this node, but down   -> degraded
        never run on this node           -> clean skip

    What "dumpable" MEANS differs by kind, and the asymmetry is deliberate —
    please do not "fix" it into consistency:

      - POSTGRES speaks over a socket inside its container, and only the
        running engine can produce a consistent dump. So the container must be
        RUNNING; a database whose service has run here but is down is degraded,
        because there is data we could not capture.

      - SQLITE is a file in a volume we mount READ-ONLY. We never touch the
        app, so what matters is that the VOLUME exists. A stopped app is the
        EASY case, not a degraded one. Classifying it on container state would
        mark the node degraded every time the operator shuts Docker down to
        reclaim RAM — which on this node is a normal, deliberate state rather
        than an incident (coordination#71, "the daemon is not always on").
    """
    plan = DumpPlan()
    running_containers = set(running_containers)
    ran_services = set(ran_services)
    existing_volumes = set(existing_volumes)

    for spec in specs:
        if spec.kind == "postgres":
            available = spec.container in running_containers
            gone = (f"{spec.label} — service '{spec.service}' has run on this node "
                    f"but its container is not running")
        else:
            available = f"{project}_{spec.volume}" in existing_volumes
            gone = (f"{spec.label} — service '{spec.service}' has run on this node "
                    f"but volume {project}_{spec.volume} does not exist")

        if available:
            plan.to_run.append(spec)
        elif spec.service in ran_services:
            plan.degraded.append(gone)
        else:
            plan.skipped.append(f"{spec.label} (service '{spec.service}' has never run)")
    return plan


def declaration_outcomes(errors) -> list[str]:
    """Manifest errors, as degraded reasons — one line per manifest, by category.

    A missing declaration and an unparseable one read differently on purpose.
    """
    out = []
    for error in errors:
        if error.category == UNDECLARED:
            out.append(f"{error.manifest} — manifest has no dump declaration, so "
                       f"any database this app owns is NOT backed up: {error.message}")
        else:
            out.append(f"{error.manifest} — manifest's dump declaration could not be "
                       f"parsed, so it was not run: {error.message}")
    return out


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

OK = "ok"
MISSING = "missing"       # nothing there to dump
FAILED = "failed"         # produced nothing usable; degrade


@dataclass(frozen=True)
class DumpResult:
    status: str
    detail: str = ""


def execute_dumps(plan: DumpPlan, dump_dir, run_postgres, run_sqlite,
                  unlink) -> tuple[list, list, list]:
    """Run the planned dumps. Returns (written, degraded, skipped).

    The drivers are injected and batched — one container run for every SQLite
    database, one `pg_restore -l` readback for every Postgres dump — so this
    stays a pure fold over `{filename: DumpResult}` and the tests never need a
    daemon. Verification lives inside the drivers and reports FAILED, so a dump
    that does not read back lands in the SAME degraded list as a dump that was
    never taken. There is no second mechanism for "produced but unusable".

    MISSING is a clean skip, not a degrade: the volume is there and the app has
    run, but it has never created that database file. "Installed, never
    initialized" is not a hole in the backup.
    """
    results: dict = {}
    pg = [s for s in plan.to_run if s.kind == "postgres"]
    sq = [s for s in plan.to_run if s.kind == "sqlite"]
    if pg:
        results.update(run_postgres(pg, dump_dir))
    if sq:
        results.update(run_sqlite(sq, dump_dir))

    written: list = []
    degraded: list = []
    skipped: list = []
    for spec in plan.to_run:
        result = results.get(spec.filename) or DumpResult(
            FAILED, "the dump driver returned no result for it")
        if result.status == OK:
            written.append(spec.filename)
        elif result.status == MISSING:
            skipped.append(f"{spec.label} ({result.detail})")
        else:
            # A half-written or unverifiable dump is worse than none: it would
            # restore as a truncated database and look like data.
            unlink(Path(dump_dir) / spec.filename)
            degraded.append(f"{spec.label} — {result.detail}")
    return written, degraded, skipped


__all__ = [
    "PostgresDump", "SqliteDump", "CORE_DUMPS", "DumpError",
    "UNDECLARED", "MALFORMED", "manifest_dumps", "declared_dumps",
    "DumpPlan", "plan_dumps", "declaration_outcomes",
    "OK", "MISSING", "FAILED", "DumpResult", "execute_dumps",
]
