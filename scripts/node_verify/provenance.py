"""
provenance — every first-party image must be reproducible.

Scans manifest/*.toml for sovereign-node/ images that have no version tag, no
[build] section, and no build: context in their app's compose fragment — the
classic "runs once from a hand-built :local image, then silently ossifies"
trap. Exempt: env-var references, @sha256:-pinned images, and non-sovereign-node/
upstream images (those are reproducible by digest/tag upstream).

Pure: manifests arrive as (filename, text) pairs and the compose tree as an
injected lookup, so the exempt classes and the trap class are all fixtures in
test_provenance.py.
"""

import re
import tomllib

from .report import FAIL, OK, Result

# Same rule _build_mirrored_parse.py uses — build-mirrored.sh rejects anything
# that isn't a 40-char hex SHA, so verify-config must agree pre-merge or a
# bad ref passes the gate and only blows up at deploy time.
SHA_RE = re.compile(r"[0-9a-f]{40}")

SKIP_EXAMPLE = True  # app.example.toml is not a real app


def compose_declares_build(compose_doc) -> bool:
    """True if any service in the fragment builds from a context."""
    if not compose_doc or not isinstance(compose_doc, dict):
        return False
    for svc in (compose_doc.get("services", {}) or {}).values():
        if isinstance(svc, dict) and "build" in svc:
            return True
    return False


def errors(manifest_entries, compose_doc_for) -> list[str]:
    """Every manifest whose first-party image could not be rebuilt from source.

    `manifest_entries` is an iterable of (filename, text) in directory order;
    `compose_doc_for(app_name)` returns the app's parsed compose fragment, or
    None when there is none (or it does not parse — the bash original swallowed
    a broken fragment here too, so a build: context in an unparsable file does
    not count as provenance).
    """
    out = []

    for entry, text in manifest_entries:
        if not entry.endswith(".toml"):
            continue
        if SKIP_EXAMPLE and entry == "app.example.toml":
            continue

        try:
            m = tomllib.loads(text)
        except Exception as e:                               # noqa: BLE001
            out.append(f"{entry}: failed to parse TOML — {e}")
            continue

        app = m.get("app", {})
        name = app.get("name", "")
        image = app.get("image", "")

        if not image or not name:
            continue

        # Skip env-var references (e.g. GOG_BRIDGE_IMAGE)
        if image.startswith("$") or "${" in image:
            continue

        # Skip images pinned by digest
        if "@sha256:" in image:
            continue

        # Skip non-first-party images (not from the sovereign-node org)
        if "sovereign-node/" not in image:
            continue

        # Check tag: extract the part after the last colon. A versioned tag
        # (e.g. :0.6.0, :0.22.7) is fine — the operator manages it. Flag
        # :local, no tag, or empty tag.
        tag = image.split(":")[-1] if ":" in image else None
        is_floating = (tag is None) or (tag == "") or (tag == "local")

        if not is_floating:
            # Has a concrete version tag — not in the floating-trap class
            continue

        build = m.get("build")
        has_build = bool(build)

        # If [build] is declared, [build].ref MUST be a 40-char hex SHA — the
        # same rule scripts/_build_mirrored_parse.py enforces at deploy time. A
        # mis-pinned ref used to sail through verify-config (it only checked
        # provenance, not the ref format), which is how a stray HEAD literal
        # for egress-broker reached review.
        if has_build and isinstance(build, dict):
            ref = build.get("ref", "")
            if not SHA_RE.fullmatch(ref or ""):
                out.append(
                    f'{entry}: [build].ref "{ref}" is not a 40-char hex SHA — '
                    f'pin it to a real commit (or remove the [build] section and '
                    f'use compose `build: .` instead, like floor).')
                continue

        if has_build:
            # [build] section exists (and ref validated above) — provenance is
            # declared
            continue

        if compose_declares_build(compose_doc_for(name)):
            # compose fragment has build: context — image is built locally
            continue

        out.append(
            f'{entry}: image "{image}" is a first-party (sovereign-node/) image '
            f'with no version tag, no [build] section, and no build: context in '
            f'apps/{name}/compose.yaml — this image cannot be reproduced. '
            f'Add a [build] section with pinned repo+ref, or add build: context '
            f'to the compose fragment.')

    return out


def check_provenance(manifest_entries, compose_doc_for) -> Result:
    errs = errors(manifest_entries, compose_doc_for)
    if errs:
        return Result(FAIL, "FAIL: build provenance errors —",
                      detail=tuple(f"  FAIL: {e}" for e in errs))
    return Result(OK, "OK: all first-party images have build provenance",
                  passthrough=("OK: every first-party image has build provenance",))
