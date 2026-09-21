"""
runner — the edge. Everything that shells out lives here and nowhere else.

Four external tools, each optional in a different way: `caddy` and
`shellcheck` live in the jail image and are legitimately absent on a laptop
(SKIP, not FAIL); `git` is how the lint and test lists are built; `python3`
runs each unit test in its own process, so one test's crash cannot take the
gate with it.

Everything here is READ-ONLY with respect to the node. This gate inspects
config text: it never starts, stops, rebuilds or recreates a container, never
touches a volume, and never reaches the network. The caddy check copies the
tree into a private mktemp dir and validates THAT, so even a tool that wanted
to rewrite its input cannot reach the checkout.

`run` and `which` are injected, so test_runner.py asserts the argv and the
verdict mapping without any of these tools installed.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .report import FAIL, OK, SKIP, Result

# Dummy env so interpolation resolves; validation is about structure, not real
# values. A real secret must never be needed to lint the door.
CADDY_ENVFILE = ("NODE_DOMAIN=localhost\n"
                 "ACME_EMAIL=op@example.com\n"
                 "EXTRA_TRUSTED_RANGES=192.0.2.0/32\n"
                 "RADICALE_WEB_AUTH=ZHVtbXk6ZHVtbXk=\n"
                 "RADICALE_OPERATOR_EMAIL=op@example.com\n")

# caddy validate narrates on stderr even when it succeeds; these lines are
# noise around the real error.
CADDY_NOISE = re.compile(r"using config|maintenance|shutting down|^\{.*level.:.info",
                         re.IGNORECASE)


def _run(argv, **kw):
    return subprocess.run(argv, **kw)


def filter_caddy_log(text: str) -> list[str]:
    """Drop caddy's own chatter, keep the diagnosis."""
    return [line for line in text.splitlines() if not CADDY_NOISE.search(line)]


class Tools:
    """The subprocess edge, with `run` and `which` injectable for tests."""

    def __init__(self, repo_root, run=_run, which=shutil.which):
        self.repo_root = Path(repo_root)
        self._run = run
        self._which = which

    def _capture(self, argv, **kw):
        """Run argv from the repo root, merging stdout+stderr like `>log 2>&1`.

        The merge is done by the CHILD (stderr=STDOUT), not by concatenating
        two pipes afterwards: shellcheck writes an encoding warning to stderr
        in the middle of its findings, and gluing the streams end to end moved
        that line to the bottom of the report — a small thing that would have
        made the transcripts differ.
        """
        proc = self._run(argv, cwd=str(self.repo_root), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, **kw)
        return proc.returncode, (proc.stdout or "")

    # --- 1. caddy: assemble the whole door and validate --------------------

    def caddy_validate(self) -> Result:
        """The FULL assembled Caddyfile (root + every apps/*/route.caddy) adapts.

        The root Caddyfile imports app routes by ABSOLUTE path
        (import /srv/apps/*/route.caddy — where prod mounts them). In the temp
        tree they live elsewhere, so the import is rewritten to point there —
        otherwise the glob matches nothing, the routes are silently skipped,
        and a broken route.caddy validates as "OK" against an empty door. That
        is the subtle trap: without the rewrite, the linter passes broken
        configs.
        """
        if not self._which("caddy"):
            return Result(SKIP, "SKIP: no caddy binary here (present in the jail "
                                "image; install caddy to run this locally)")

        td = Path(tempfile.mkdtemp(prefix="alodium-verify."))
        try:
            shutil.copytree(self.repo_root / "caddy", td / "caddy")
            shutil.copytree(self.repo_root / "apps", td / "apps")
            caddyfile = td / "caddy" / "Caddyfile"
            caddyfile.write_text(
                caddyfile.read_text().replace("/srv/apps/", f"{td}/apps/"))
            envfile = td / "envfile"
            envfile.write_text(CADDY_ENVFILE)

            code, log = self._capture(
                ["caddy", "validate", "--config", str(caddyfile),
                 "--adapter", "caddyfile", "--envfile", str(envfile)])
            if code == 0:
                return Result(OK, "OK: config adapts and validates")
            return Result(FAIL, "FAIL: caddy validate errored —",
                          detail=tuple(filter_caddy_log(log)), detail_tail=12)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    # --- 4. label definitions ----------------------------------------------

    def ensure_tier_labels(self) -> Result:
        """`ensure-tier-labels.sh --verify` — quoting/encoding of the definitions.

        --verify is the script's own read-only mode: it parses the label table
        and applies nothing.
        """
        script = self.repo_root / "scripts" / "ensure-tier-labels.sh"
        if not script.is_file():
            return Result(SKIP, "SKIP: no scripts/ensure-tier-labels.sh")
        code, log = self._capture(["bash", "scripts/ensure-tier-labels.sh", "--verify"])
        if code == 0:
            return Result(OK, "OK: all label definitions parse correctly")
        return Result(FAIL, "FAIL: label definition errors —",
                      detail=tuple(log.splitlines()))

    # --- 5. shell lint ------------------------------------------------------

    def tracked_shell_scripts(self):
        """`git ls-files 'scripts/*.sh' 'host/**/*.sh'`, or None if git failed."""
        code, out = self._capture(["git", "ls-files", "scripts/*.sh", "host/**/*.sh"])
        if code != 0:
            return None
        return out.splitlines()

    def shellcheck(self, files) -> Result:
        """-S error: fail the gate on errors, not on style.

        The existing scripts predate this check and use intentional patterns;
        a gate that cried about style would be turned off, and then it would
        catch nothing at all.
        """
        if not self._which("shellcheck"):
            return Result(SKIP, "SKIP: no shellcheck here (present in the jail image)")
        code, log = self._capture(["shellcheck", "-S", "error", *files])
        if code == 0:
            return Result(OK, "OK: no shellcheck errors")
        return Result(FAIL, "FAIL: shellcheck errors —",
                      detail=tuple(log.splitlines()), detail_head=20)

    # --- 5b. unit tests for the Python under scripts/ -----------------------

    def tracked_py_tests(self):
        """`git ls-files 'scripts/**/test_*.py'` — half of the discovery union."""
        code, out = self._capture(["git", "ls-files", "scripts/**/test_*.py"])
        if code != 0:
            return []
        return out.splitlines()

    def run_py_test(self, path: str) -> Result:
        """One test file, in its own process, from the repo root."""
        code, log = self._capture(["python3", path])
        if code == 0:
            return Result(OK, f"OK: {path}")
        return Result(FAIL, f"FAIL: {path} —",
                      detail=tuple(log.splitlines()), detail_tail=20)
