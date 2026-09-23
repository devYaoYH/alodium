"""
frontmatter — `front <file> <key>`, the awk one-liner three scripts shared:

    awk -v k="$2" 'NR>1 && /^---$/{exit}
                   $1==k":"{sub(/^[^:]*: */,""); sub(/[[:space:]]*#.*$/,"");
                            sub(/[[:space:]]+$/,""); print}' "$1"

This is SECURITY-relevant, not formatting: `front brief dispatch` == "auto" is
the gate that decides whether a filed issue can make the host run a brief. So
it is reproduced exactly, quirks included, and test_frontmatter.py checks it
against outputs recorded from the awk itself (testdata/bash_baseline.json):

  - The scan stops at the first `---` AFTER line 1, so a brief without
    frontmatter is scanned to the end: a body line `dispatch: auto` counts.
  - `$1` is the first blank-separated field, so leading indentation matches
    and `dispatch:auto` (no space) does not.
  - Everything from the first `#` is a comment, even mid-value.
  - A repeated key prints every match, one per line; the caller's `!= "auto"`
    comparison then fails, which is the safe outcome and stays that way.
"""

import re

_FIELD_SEP = re.compile(r"[ \t]+")
_KEY_PREFIX = re.compile(r"^[^:]*: *")
_COMMENT = re.compile(r"[ \t\n\v\f\r]*#.*$")
_TRAILING = re.compile(r"[ \t\n\v\f\r]+$")


def _first_field(line: str) -> str:
    stripped = line.lstrip(" \t")
    return _FIELD_SEP.split(stripped, 1)[0] if stripped else ""


def front(text: str, key: str) -> str:
    """The value(s) of `key`, as `$(front file key)` would have captured them."""
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    values = []
    for nr, line in enumerate(lines, 1):
        if nr > 1 and line == "---":
            break
        if _first_field(line) == key + ":":
            value = _KEY_PREFIX.sub("", line, count=1)
            value = _COMMENT.sub("", value, count=1)
            value = _TRAILING.sub("", value, count=1)
            values.append(value)
    return "\n".join(values).rstrip("\n")


def front_file(path, key: str) -> str:
    """`front` over a file; a missing/unreadable file reads as empty, as awk's
    error went to stderr and `$(...)` captured nothing."""
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            return front(f.read(), key)
    except OSError:
        return ""
