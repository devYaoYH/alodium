#!/usr/bin/env python3
"""
Offline tests for node_verify.provenance — every first-party image rebuildable.

The trap this catches: an image that runs once from a hand-built :local tag and
then silently ossifies, with no source anyone can point at. The exempt classes
matter as much as the trap — a check that flags upstream images gets disabled.

  - the trap FAILS: a sovereign-node/ image with :local, or with no tag at all
  - [build].ref must be a 40-char hex SHA, the same rule build-mirrored.sh
    applies at deploy time — `HEAD`, a short sha and a branch name all FAIL
  - a `build:` context in the app's compose fragment IS provenance
  - exempt and stay exempt: env-var images, @sha256: pins, upstream images,
    a concrete version tag, app.example.toml, non-.toml files
  - an unparsable manifest FAILS with the filename
  - an unparsable compose fragment does not count as provenance
  - EQUIVALENCE: this tree's recorded manifests reproduce bash's verdict

Run:  python3 scripts/node_verify/test_provenance.py     (from the repo root)
      ./scripts/verify-config.sh                         (runs it with the rest)
PyYAML + stdlib only.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from node_verify import provenance                              # noqa: E402
from node_verify.report import FAIL as R_FAIL, OK               # noqa: E402

FAIL = 0
SHA = "a" * 40


def check(name, cond, detail=""):
    global FAIL
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        FAIL += 1


def manifest(name="snake", image="ghcr.io/sovereign-node/snake:local", build=None):
    text = f'[app]\nname = "{name}"\nimage = "{image}"\n'
    if build is not None:
        text += "[build]\n" + "".join(f'{k} = "{v}"\n' for k, v in build.items())
    return [(f"{name}.toml", text)]


NO_COMPOSE = lambda name: None                                  # noqa: E731
BUILDS = lambda name: {"services": {"app": {"build": "."}}}      # noqa: E731


# ---- 1. the trap -----------------------------------------------------------

errs = provenance.errors(manifest(), NO_COMPOSE)
check("trap: a :local first-party image with no provenance FAILS", len(errs) == 1,
      detail=str(errs))
check("trap: the message names the manifest, the image and both remedies",
      errs and errs[0].startswith("snake.toml: image ")
      and "cannot be reproduced" in errs[0]
      and "[build] section with pinned repo+ref" in errs[0]
      and "build: context" in errs[0], detail=str(errs))

check("trap: an untagged first-party image FAILS too",
      len(provenance.errors(manifest(image="ghcr.io/sovereign-node/snake"),
                            NO_COMPOSE)) == 1)
check("trap: an empty tag FAILS too",
      len(provenance.errors(manifest(image="ghcr.io/sovereign-node/snake:"),
                            NO_COMPOSE)) == 1)


# ---- 2. [build].ref must be a real commit ----------------------------------

check("build: a 40-char hex ref is provenance",
      provenance.errors(manifest(build={"repo": "x", "ref": SHA}), NO_COMPOSE) == [])

for bad in ("HEAD", "main", "a" * 39, "a" * 41, "g" * 40, ""):
    errs = provenance.errors(manifest(build={"repo": "x", "ref": bad}), NO_COMPOSE)
    check(f"build: ref {bad!r} is rejected",
          len(errs) == 1 and "is not a 40-char hex SHA" in errs[0], detail=str(errs))

errs = provenance.errors(manifest(build={"repo": "x", "ref": "HEAD"}), NO_COMPOSE)
check("build: a bad ref reports ONCE, not also as missing provenance",
      len(errs) == 1, detail=str(errs))
check("build: the bad-ref message quotes the ref it found",
      '[build].ref "HEAD"' in errs[0], detail=str(errs))


# ---- 3. a compose build context is provenance ------------------------------

check("compose: a build: context in the fragment is provenance",
      provenance.errors(manifest(), BUILDS) == [])
check("compose: a fragment with no build: is not",
      len(provenance.errors(manifest(), lambda n: {"services": {"app": {"image": "x"}}})) == 1)
check("compose: an unparsable fragment (None) is not provenance",
      len(provenance.errors(manifest(), NO_COMPOSE)) == 1)
check("compose: compose_declares_build reads every service, not just the first",
      provenance.compose_declares_build(
          {"services": {"a": {"image": "x"}, "b": {"build": "."}}}) is True)
check("compose: a junk fragment is not a build",
      provenance.compose_declares_build("not a mapping") is False
      and provenance.compose_declares_build(None) is False)


# ---- 4. the exempt classes -------------------------------------------------

for image, why in [
    ("${SNAKE_IMAGE}", "an env-var reference"),
    ("$SNAKE_IMAGE", "a bare env-var reference"),
    ("ghcr.io/sovereign-node/snake@sha256:" + "b" * 64, "a digest pin"),
    ("ghcr.io/sovereign-node/snake:0.6.0", "a concrete version tag"),
    ("docker.io/library/postgres:16-alpine", "an upstream image"),
    ("postgres:local", "an upstream image with a :local tag"),
]:
    check(f"exempt: {why} is not flagged",
          provenance.errors(manifest(image=image), NO_COMPOSE) == [],
          detail=image)

check("exempt: app.example.toml is not inventory",
      provenance.errors([("app.example.toml",
                          '[app]\nname="x"\nimage="ghcr.io/sovereign-node/x:local"\n')],
                        NO_COMPOSE) == [])
check("exempt: a non-.toml file in manifest/ is ignored",
      provenance.errors([("node.example.yaml", "not: toml")], NO_COMPOSE) == [])
check("exempt: a manifest with no image declares no image",
      provenance.errors([("x.toml", '[app]\nname = "x"\n')], NO_COMPOSE) == [])
check("exempt: a manifest with no name is skipped",
      provenance.errors([("x.toml", '[app]\nimage = "ghcr.io/sovereign-node/x:local"\n')],
                        NO_COMPOSE) == [])


# ---- 5. a broken manifest ---------------------------------------------------

errs = provenance.errors([("broken.toml", "[app\nname = ")], NO_COMPOSE)
check("broken: an unparsable manifest FAILS with its filename",
      len(errs) == 1 and errs[0].startswith("broken.toml: failed to parse TOML — "),
      detail=str(errs))


# ---- 6. the section verdict ------------------------------------------------

r = provenance.check_provenance(manifest(build={"repo": "x", "ref": SHA}), NO_COMPOSE)
check("verdict: clean is OK", r.status == OK)
check("verdict: the OK note and the stdout line are both unchanged",
      r.note == "OK: all first-party images have build provenance"
      and r.passthrough == ("OK: every first-party image has build provenance",),
      detail=str(r))

r = provenance.check_provenance(manifest(), NO_COMPOSE)
check("verdict: the trap FAILS the section", r.status == R_FAIL)
check("verdict: the FAIL note is unchanged", r.note == "FAIL: build provenance errors —")
check("verdict: nothing is printed to stdout on failure", r.passthrough == ())
check("verdict: each error prints with the FAIL prefix bash used",
      all(line.startswith("  FAIL: ") for line in r.detail), detail=str(r.detail))


# ---- 7. equivalence with the merged bash implementation --------------------

BASELINE = json.loads((HERE / "testdata" / "bash_baseline.json").read_text())
INPUTS = BASELINE["inputs"]
want = BASELINE["bash_verdicts"]["build provenance"]

entries = [(name, text) for name, text in INPUTS["manifests"]]
lookup = lambda name: (                                          # noqa: E731
    {"services": {"app": {"build": "."}}}
    if INPUTS["compose_declares_build"].get(name) else {"services": {"app": {}}})

r = provenance.check_provenance(entries, lookup)
check("equiv: same status as bash", r.status == want["status"],
      detail=str(r.detail))
check("equiv: same note as bash", r.note == want["note"])
check("equiv: same stdout line as bash", list(r.passthrough) == want["passthrough"])
check("equiv: the recorded tree still declares 14 manifests",
      len(entries) == 14, detail=str(len(entries)))


print()
if FAIL == 0:
    print("test_provenance: PASS")
    sys.exit(0)
print(f"test_provenance: FAIL ({FAIL} failures)")
sys.exit(1)
