"""
policy — how a snapshot is tagged, and which retention pool it belongs to.

Pure: every function returns the exact restic argv, so the tests pin the flags
themselves rather than a description of them. That matters here — the bug this
module exists to prevent was a single missing tag.
"""

from .plan import PROJECT

TAG_COMPLETE = "complete"
TAG_PARTIAL = "partial"

KEEP_DAILY = 7
KEEP_WEEKLY = 4
KEEP_MONTHLY = 6
KEEP_PARTIAL_LAST = 3


def snapshot_tags(project: str, degraded: bool) -> list[str]:
    """Tag flags for `restic backup`.

    Every snapshot carries a class tag — `complete` or `partial` — and the
    `complete` tag is NOT redundant, however much it looks it. Do not
    "simplify" it away: retention keeps the NEWEST snapshot in each period, so
    without a tag that distinguishes the classes, a degraded run at 14:00 makes
    that day's newest snapshot the partial one, and the next clean run's
    `forget` deletes the complete 03:00 snapshot in its favour.
    """
    return ["--tag", project, "--tag", TAG_PARTIAL if degraded else TAG_COMPLETE]


def backup_args(project: str, targets: list[str], degraded: bool) -> list[str]:
    """Full argv for `restic backup`.

    `--host` is not cosmetic: without it restic records the container's random
    hostname, every snapshot lands in its own retention group, and `forget`
    never expires anything. A fixed name also survives a restore onto another
    machine.
    """
    return (["backup", *targets, "--host", project]
            + snapshot_tags(project, degraded)
            + ["--exclude-caches"])


def retention_calls(project: str, degraded: bool) -> list[list[str]]:
    """Retention, as two disjoint pools and one prune. Empty when degraded.

    A degraded run expires nothing: skipping retention there is the cheap half
    of the fix, but the eviction it prevents actually happens on the NEXT clean
    run, which is what the class tags are for.

    `--group-by host` (not host,tags or host,paths): one host, one backup job,
    one history. Grouping by paths would fragment it every time an app is
    added; grouping by tags would give the partial pool its own group of every
    age. Instead the pools are selected explicitly by tag — `--tag "a,b"` means
    AND — so the real policy can only ever be satisfied by a complete snapshot,
    and a partial can never cause a complete one to be deleted.

    Snapshots written before this scheme existed carry neither class tag and
    match neither pool, so they are left alone. That is academic for the real
    repository, which has not been created yet.
    """
    if degraded:
        return []
    return [
        # The real policy. Complete snapshots only.
        ["forget", "--host", project, "--tag", f"{project},{TAG_COMPLETE}",
         "--group-by", "host",
         "--keep-daily", str(KEEP_DAILY),
         "--keep-weekly", str(KEEP_WEEKLY),
         "--keep-monthly", str(KEEP_MONTHLY)],
        # Partials are kept only so a restore has something recent to fall back
        # on while the node is half up. Bounded, so a week of degraded runs
        # cannot fill the disk.
        ["forget", "--host", project, "--tag", f"{project},{TAG_PARTIAL}",
         "--group-by", "host", "--keep-last", str(KEEP_PARTIAL_LAST)],
        # One prune for both, rather than once per pool.
        #
        # Note for coordination#71: this runs on every clean backup, which is
        # fine for a local P0 repository but wrong once the node's storage
        # credential is append-only. PR 4 moves it out of the backup path.
        ["prune"],
    ]


def data_mounts(volumes: list[str], dump_dir: str) -> tuple[list[str], list[str]]:
    """(docker -v flags, restic target paths) for the production data.

    Each volume is mounted READ-ONLY at a predictable /data/<volume>, so a path
    in the snapshot names the volume it restores into. Nothing here may write
    to a production volume: every data mount is :ro, always.
    """
    mounts: list[str] = []
    targets: list[str] = []
    for vol in volumes:
        mounts += ["-v", f"{vol}:/data/{vol}:ro"]
        targets.append(f"/data/{vol}")
    mounts += ["-v", f"{dump_dir}:/data/dumps:ro"]
    targets.append("/data/dumps")
    return mounts, targets


__all__ = [
    "PROJECT", "TAG_COMPLETE", "TAG_PARTIAL",
    "snapshot_tags", "backup_args", "retention_calls", "data_mounts",
]
