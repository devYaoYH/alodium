"""
config — where the repository is, and where the passphrase comes from.

The decisions here (which env file wins, local path vs backend URL, which
passphrase source takes precedence) are pure functions over a dict; the two
effects they need — running the password command, reading a file — are
injected, so the tests never touch the Keychain.
"""

import os
from dataclasses import dataclass
from pathlib import Path


class BackupError(Exception):
    """A condition the operator has to fix. Always exits non-zero.

    `code` carries restic's own exit status through when restic is what
    failed, so a scheduler sees 12 ("wrong password") rather than a flat 1.
    It is never 0: a backup that raised did not happen.
    """

    def __init__(self, message, code: int = 1):
        super().__init__(message)
        self.code = code if code != 0 else 1


# Credentials restic's backends read. Only these are forwarded into the
# container; never the whole environment.
BACKEND_ENV = (
    "B2_ACCOUNT_ID", "B2_ACCOUNT_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_DEFAULT_REGION", "AWS_SESSION_TOKEN",
    "RESTIC_REST_USERNAME", "RESTIC_REST_PASSWORD",
)

# Deciding what to back up means parsing the compose tree, and compose needs
# the local layer git never sees: .env for the ${VARS}, secrets/<app>.env for
# every env_file.
LOCAL_LAYER = (".env", "secrets")


def preflight_local_layer(repo_root, exists=None) -> None:
    """Refuse to run from a checkout without the local layer.

    Without this, compose dies with "stat …/secrets/radicale.env: no such file
    or directory", which sends the reader hunting for a missing secret instead
    of telling them they are in the wrong checkout (a git worktree, usually).
    """
    exists = exists or (lambda p: Path(p).exists())
    for name in LOCAL_LAYER:
        if not exists(Path(repo_root) / name):
            raise BackupError(
                f"missing '{name}' in {repo_root} — backup.sh needs the local layer "
                f"(.env, secrets/) and must run from the live checkout, not a git "
                f"worktree.")


def find_env_file(alodium_home, repo_root, exists=None) -> tuple[Path, list[Path]]:
    """(chosen env file, ignored candidates).

    ~/.alodium/backup.env is the documented home; scripts/backup.env stays
    supported for the pre-~/.alodium layout. If both exist the ~/.alodium one
    wins and the other is called out rather than silently ignored.
    """
    exists = exists or (lambda p: Path(p).exists())
    candidates = [Path(alodium_home) / "backup.env", Path(repo_root) / "scripts" / "backup.env"]
    found = [c for c in candidates if exists(c)]
    if not found:
        raise BackupError(
            f"no backup.env found. Copy scripts/backup.env.example to "
            f"{candidates[0]} (chmod 600) and fill it in.")
    return found[0], found[1:]


@dataclass
class Repository:
    value: str          # exactly what the operator configured
    is_local: bool
    path: Path | None   # host directory to bind-mount at /repo, when local


def classify_repository(value: str, home: str) -> Repository:
    """A local directory gets bind-mounted; a backend URL is passed through.

    P0 is a local directory. Anything with a backend prefix (b2:, s3:, sftp:,
    rest:) is forwarded untouched with its provider credentials.
    """
    if not value:
        raise BackupError("backup.env sets no RESTIC_REPOSITORY")
    if value.startswith(("/", "~/", "./", "../")):
        expanded = value.replace("~", home, 1) if value.startswith("~/") else value
        return Repository(value, True, Path(expanded))
    if ":" in value:
        return Repository(value, False, None)
    raise BackupError(
        f"RESTIC_REPOSITORY='{value}' is neither an absolute path nor a restic "
        f"backend URL")


def resolve_passphrase(env: dict, run_command=None, read_file=None, home="") -> str:
    """The passphrase, resolved ON THE HOST, in documented precedence order.

    RESTIC_PASSWORD_COMMAND wins because the preferred source is the macOS
    Keychain, and `security` exists here, not in the restic image. The result
    is handed to the container as a mounted file, so it never reaches argv and
    never reaches the container's environment where `docker inspect` would
    print it.
    """
    command = env.get("RESTIC_PASSWORD_COMMAND", "")
    passfile = env.get("RESTIC_PASSWORD_FILE", "")
    literal = env.get("RESTIC_PASSWORD", "")

    if command:
        value = (run_command or _default_run_command)(command)
        source = f"passphrase lookup failed: {command}"
    elif passfile:
        expanded = passfile.replace("~", home, 1) if passfile.startswith("~/") else passfile
        value = (read_file or _default_read_file)(expanded)
        source = f"passphrase file is empty or unreadable: {passfile}"
    elif literal:
        value = literal
        source = "RESTIC_PASSWORD is set but empty"
    else:
        raise BackupError(
            "backup.env supplies no passphrase — set RESTIC_PASSWORD_COMMAND "
            "(preferred), RESTIC_PASSWORD_FILE, or RESTIC_PASSWORD")

    value = (value or "").strip("\n")
    if not value:
        raise BackupError(
            f"{source}. For the Keychain path, create the item first: "
            f'security add-generic-password -a "$USER" -s alodium-restic -w')
    return value


def _default_run_command(command: str) -> str:
    import subprocess
    proc = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    if proc.returncode != 0:
        # stderr, not stdout: the command failing is the useful message.
        print(proc.stderr.strip(), file=__import__("sys").stderr)
        return ""
    return proc.stdout


def _default_read_file(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def backend_env(env: dict) -> list[str]:
    """`-e NAME=value` flags for the backend credentials that are actually set."""
    flags: list[str] = []
    for name in BACKEND_ENV:
        if env.get(name):
            flags += ["-e", f"{name}={env[name]}"]
    return flags


def alodium_home(environ=None) -> Path:
    """The node's host-side storage root.

    Outside the checkout on purpose: the checkout is replaceable, this is not.
    Overridable for drills and tests.
    """
    environ = os.environ if environ is None else environ
    return Path(environ.get("ALODIUM_HOME") or (Path(environ.get("HOME", "")) / ".alodium"))
