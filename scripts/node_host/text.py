"""
text — the small text transforms the bash did with pipes, as the pipes did them.

Each is shell behavior that shows up verbatim in a coordination comment or the
audit log, so it is reproduced and tested against recorded pipeline output
(testdata/bash_baseline.json) rather than approximated:

  capture       `$(cmd)` — strips ALL trailing newlines
  tail_lines    `... | tail -N`, then captured
  clean_run     dispatch-run's failure tail: ANSI stripped, CRs dropped,
                progress chatter and blank lines filtered, last 12 lines
  audit_line    the JSON-ish line appended to .task-dispatch/dispatch-audit.log
"""

import re

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_NOISE = re.compile(r"Processing|Reasoning|Ctrl\+C", re.IGNORECASE)
_BLANK = re.compile(r"^[ \t\n\v\f\r]*$")


def capture(text: str) -> str:
    """What `$(...)` keeps: the output minus every trailing newline."""
    return text.rstrip("\n")


def lines(text: str) -> list:
    """Lines as `tail`/`grep` count them: a final line without a newline is
    still a line, and a trailing newline does not start an empty one."""
    if text == "":
        return []
    out = text.split("\n")
    if text.endswith("\n"):
        out.pop()
    return out


def tail_lines(text: str, n: int) -> str:
    """`$(printf '%s' "$text" | tail -n)`."""
    return capture("\n".join(lines(text)[-n:]))


def clean_run(text: str, n: int = 12) -> str:
    """dispatch-run's failure tail:

        printf '%s' "$OUT" | sed 's/\\x1b\\[[0-9;]*[A-Za-z]//g' | tr -d '\\r' \\
          | grep -viE 'Processing|Reasoning|Ctrl\\+C' | grep -v '^[[:space:]]*$' | tail -12
    """
    stripped = _ANSI.sub("", text).replace("\r", "")
    kept = [line for line in lines(stripped)
            if not _NOISE.search(line) and not _BLANK.match(line)]
    return capture("\n".join(kept[-n:]))


def audit_line(ts: str, issue, action: str, run: str, detail: str) -> str:
    """printf '{"ts":"%s","issue":%s,"action":"%s","run":"%s","detail":"%s"}\\n'
    with `"${detail//\\"/\\'}"`. Inside double quotes bash 3.2 keeps the
    backslash, so a `"` in the detail is written as `\\'` (recorded, not
    guessed: testdata/bash_baseline.json). Not full JSON escaping, and not
    upgraded here: readers of the log know this shape."""
    detail = detail.replace('"', "\\'")
    return (f'{{"ts":"{ts}","issue":{issue},"action":"{action}",'
            f'"run":"{run}","detail":"{detail}"}}\n')
