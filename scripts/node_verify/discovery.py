"""
discovery — which files each section looks at.

Small, dull, and its own module because one of these lists has already lied.
`git ls-files 'scripts/**/test_*.py'` alone silently skipped a brand-new test
file while the section printed PASS: the old fallback only fired when the git
list was ENTIRELY empty, so adding a test beside existing ones meant the gate
ran every test but the new one. A gate that quietly checks less than you think
is the exact failure this section exists to catch, so the union below is
load-bearing and test_runner.py pins it.

Read-only filesystem globbing, no subprocess — the git call that feeds
`union_tests` lives in `runner`.
"""

from pathlib import Path

# Section 2's file set, in the order `ls` produced it (sorted as one list, not
# per-glob), so a failure is reported against the same file first.
YAML_GLOBS = ("docker-compose.yml", "docker-compose.staging.yml",
              "apps/*/compose.yaml", "config/*.yaml")

TEST_GLOB = "scripts/*/test_*.py"


def yaml_files(repo_root) -> list[str]:
    """Repo-relative paths of every YAML file the strict parse covers."""
    root = Path(repo_root)
    found = set()
    for pattern in YAML_GLOBS:
        for path in root.glob(pattern):
            if path.is_file():
                found.add(str(path.relative_to(root)))
    return sorted(found)


def disk_tests(repo_root) -> list[str]:
    """`ls scripts/*/test_*.py` — what is on disk, tracked or not."""
    root = Path(repo_root)
    return sorted(str(p.relative_to(root)) for p in root.glob(TEST_GLOB) if p.is_file())


def union_tests(git_lines, disk_lines) -> list[str]:
    """Tracked tests UNION on-disk tests, deduped and sorted (`sort -u`).

    Both halves matter. git ls-files misses a test that has not been added
    yet; the disk glob misses nothing here today but is one directory layout
    away from doing so. Running a test twice is free; not running one is how
    this gate printed PASS over an untested change.
    """
    return sorted({line.strip() for line in list(git_lines) + list(disk_lines)
                   if line.strip()})


def shell_files(git_lines, fallback_lines) -> list[str]:
    """Shell scripts to lint: what git tracks, else the plain scripts/ glob.

    `git_lines` is None when the git call FAILED; the fallback exists for a
    checkout with no git (the jail image's copy). This mirrors the bash
    `$(git ls-files … || ls scripts/*.sh)` exactly, including the case it gets
    wrong: a git call that SUCCEEDS with an empty list does not fall back, it
    lints nothing — see the PR's "found, not fixed" list.
    """
    if git_lines is None:
        return [line.strip() for line in fallback_lines if line.strip()]
    return [line.strip() for line in git_lines if line.strip()]
