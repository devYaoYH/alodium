#!/usr/bin/env python3
"""
Offline config verification — a pure text check, no daemon, no data, no
network. Runs the same validations the operator runs when reviewing a config
PR, so the agent can self-check BEFORE pushing (the jail image carries the
caddy binary + shellcheck for exactly this). Also runnable by the operator
and by CI.

    ./scripts/verify-config.sh            # validate everything

Each section is a function over parsed data in scripts/node_verify/, with
offline tests for its pass AND fail branch; scripts/node_verify/runner.py is
the only module that shells out. This file is the wiring: it loads the files,
calls each check in order, prints the transcript and assembles the exit code.

Nothing here may mutate the node. It reads config text and nothing else.
"""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from node_verify import checks, containment, discovery, provenance, report  # noqa: E402
from node_verify.report import Section, guarded                             # noqa: E402
from node_verify.runner import Tools                                        # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_yaml(path: Path):
    """A parsed YAML file, or None when it is not there.

    None means "not deployed on this node yet" and each check turns it into a
    SKIP. A file that exists but does not parse raises, and `guarded` turns
    that into a FAIL — never a skip.
    """
    if not path.is_file():
        return None
    with open(path) as f:
        return yaml.safe_load(f)


def manifest_entries(manifest_dir: Path):
    """(filename, text) for everything in manifest/, in directory order."""
    if not manifest_dir.is_dir():
        return []
    return [(p.name, p.read_text()) for p in sorted(manifest_dir.iterdir())
            if p.is_file()]


def compose_doc_for(repo_root: Path):
    """Lookup used by the provenance check: app name -> parsed compose, or None.

    A fragment that does not parse comes back as None, exactly as before: a
    `build:` context inside an unparsable file is not evidence of anything,
    and section 2 is what reports the parse error.
    """
    def lookup(name):
        path = repo_root / "apps" / name / "compose.yaml"
        if not path.is_file():
            return None
        try:
            return yaml.safe_load(path.read_text())
        except Exception:                                    # noqa: BLE001
            return None
    return lookup


def build_sections(repo_root: Path, tools: Tools):
    """Yield each section as it is decided, so the transcript streams.

    Every check runs inside `guarded`, loading included: a config file that
    exists but does not parse must FAIL the gate with its traceback, never
    take the process down before the later sections have run.
    """
    # --- 1. Caddy: assemble the whole door and validate --------------------
    yield Section("caddy validate (full assembled Caddyfile)",
                  [guarded("FAIL: caddy validate errored —", tools.caddy_validate)])

    # --- 2. YAML parses strictly -------------------------------------------
    yield Section("yaml parse", [guarded(
        None,
        lambda: checks.check_yaml(discovery.yaml_files(repo_root),
                                  lambda rel: (repo_root / rel).read_text()),
        detail_indent="  ")])

    # --- 3. Dispatch tiers reconciled with litellm -------------------------
    yield Section("dispatch tiers vs litellm", [guarded(
        "FAIL: dispatch-tiers.yaml references models not in litellm.yaml —",
        lambda: checks.check_tiers(
            load_yaml(repo_root / "config" / "dispatch-tiers.yaml"),
            load_yaml(repo_root / "config" / "litellm.yaml")))])

    # --- 4. Label definitions valid (quoting/encoding) ---------------------
    yield Section("label definitions",
                  [guarded("FAIL: label definition errors —", tools.ensure_tier_labels)])

    # --- 5. Shell lint ------------------------------------------------------
    yield Section("shellcheck", [guarded(
        "FAIL: shellcheck errors —",
        lambda: tools.shellcheck(discovery.shell_files(
            tools.tracked_shell_scripts(),
            sorted(str(p.relative_to(repo_root))
                   for p in repo_root.glob("scripts/*.sh")))))])

    # --- 5b. Unit tests for the Python under scripts/ ----------------------
    # Offline, stdlib-only, no daemon: the decisions this node gets wrong are
    # pure functions, so they are checked here rather than by fault-injecting
    # against a live Docker host. Add a test_*.py next to any new module and
    # it runs. Tracked AND on-disk, unioned — see discovery.union_tests.
    py_tests = discovery.union_tests(tools.tracked_py_tests(),
                                     discovery.disk_tests(repo_root))
    if not py_tests:
        results = [report.Result(report.SKIP, "SKIP: no scripts/**/test_*.py found")]
    else:
        results = [guarded(f"FAIL: {t} —", tools.run_py_test, t) for t in py_tests]
    yield Section("python unit tests (scripts/)", results)

    # --- 6. Copilot containment invariants ---------------------------------
    yield Section("copilot containment", [guarded(
        "FAIL: copilot containment violated —",
        lambda: containment.check_copilot(
            load_yaml(repo_root / "apps" / "copilot" / "compose.yaml")))])

    # --- 7. Build provenance ------------------------------------------------
    yield Section("build provenance", [guarded(
        "FAIL: build provenance errors —",
        lambda: provenance.check_provenance(
            manifest_entries(repo_root / "manifest"), compose_doc_for(repo_root)))])

    # --- 8. Model pricing pinned -------------------------------------------
    yield Section("model pricing", [guarded(
        "FAIL: unpriced model deployment —",
        lambda: checks.check_pricing(load_yaml(repo_root / "config" / "litellm.yaml")))])


def main(argv) -> int:
    repo_root = REPO_ROOT
    sections = []
    for section in build_sections(repo_root, Tools(repo_root)):
        sections.append(section)
        for line in section.render():
            print(line, flush=True)
    print()
    print(report.verdict_line(sections))
    return report.exit_code(sections)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt:
        sys.exit(130)
