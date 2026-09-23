"""
host — the edge. Subprocesses, docker queries and the detached spawn live here
and nowhere else in node_host; the job modules receive a `Host` and tests hand
them a fake one.

Two portability traps from the bash are closed here:

  - Detaching. The dispatcher spawns each issue run in its own session so it
    outlives the pass (launchd kills the job's process group on exit). macOS
    has no `setsid` binary, so the bash already shelled into python for
    `start_new_session=True`. On Windows that flag is ignored; the equivalent
    is DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP.
  - `docker ps -q | grep -c .` and `docker inspect ... || true`: the output is
    parsed here, with the bash's "a failure reads as empty" semantics.
"""

import os
import subprocess
import sys
import time


class Host:
    def __init__(self, repo_root, env):
        self.repo_root = str(repo_root)
        self.env = env

    # --- processes --------------------------------------------------------

    def run_combined(self, argv) -> tuple:
        """`OUT=$(cmd 2>&1)`: (exit status, stdout+stderr as text)."""
        try:
            proc = subprocess.run(list(argv), cwd=self.repo_root, env=self.env,
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            # bash: "cannot execute" is status 126, "not found" 127.
            return (127 if isinstance(exc, FileNotFoundError) else 126), f"{argv[0]}: {exc}\n"
        return proc.returncode, proc.stdout.decode("utf-8", errors="replace")

    def run_quiet(self, argv) -> int:
        """`cmd >/dev/null 2>&1`; the exit status only."""
        try:
            return subprocess.run(list(argv), cwd=self.repo_root, env=self.env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode
        except OSError:
            return 127

    def spawn_detached(self, argv, log_path) -> None:
        """Start `argv` in its own session with stdout+stderr appended to
        `log_path`, and return without waiting. Raises OSError if it could not
        be started — the one failure the caller can still act on."""
        if len(argv) > 1 and argv[0] == sys.executable and not os.path.isfile(argv[1]):
            # A missing script would start an interpreter that dies at once, so
            # the run never begins — report it as the spawn failure it is.
            raise FileNotFoundError(argv[1])
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kw["start_new_session"] = True
        with open(log_path, "ab") as log:
            subprocess.Popen(list(argv), cwd=self.repo_root, env=self.env,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kw)

    def sleep(self, seconds) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.time()

    # --- docker (read-only) -------------------------------------------------

    def _docker_stdout(self, argv) -> str:
        try:
            proc = subprocess.run(["docker"] + list(argv), env=self.env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
        except OSError:
            return ""
        return proc.stdout.decode("utf-8", errors="replace")

    def running_tenants(self) -> int:
        """`docker ps --filter "name=task-issue-work" -q 2>/dev/null | grep -c .`"""
        out = self._docker_stdout(["ps", "--filter", "name=task-issue-work", "-q"])
        return sum(1 for line in out.split("\n") if line)

    def image_id(self, image: str) -> str:
        """`$(docker inspect --format '{{.Id}}' IMAGE 2>/dev/null || true)`"""
        return self._docker_stdout(["inspect", "--format", "{{.Id}}", image]).rstrip("\n")

    # --- the sibling scripts ------------------------------------------------

    def script(self, name: str) -> list:
        """argv for a sibling under scripts/. A .py runs under THIS interpreter
        (no shebang or PATH lookup, so it works where there is no bash); a .sh
        runs as the bash did."""
        path = os.path.join(self.repo_root, "scripts", name)
        if name.endswith(".py"):
            return [sys.executable, path]
        return [path]
