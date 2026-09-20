"""
report — the OK / SKIP / FAIL vocabulary, and how a verdict reaches the eye.

The gate's contract with everything that reads it (the operator, the agent in
tasks/issue-work.md, CI) is three words and an exit code:

  OK    the check ran and passed
  SKIP  the check could not run here — a missing tool, a file not deployed
        yet. A SKIP is NOT a pass and must never mask a FAIL, but on its own
        it does not fail the gate: the caddy and shellcheck binaries live in
        the jail image, so a local run legitimately skips them.
  FAIL  the check ran and rejected the tree. Any FAIL => exit 1.

The rendering is deliberately literal rather than pretty. Each field below
reproduces one behaviour of the bash original — including where a heredoc's
stdout landed unindented above its own verdict line — because the port's claim
is a byte-identical transcript, and a "tidier" line here would cost the ability
to prove that with diff.
"""

from dataclasses import dataclass, field

OK = "OK"
SKIP = "SKIP"
FAIL = "FAIL"


@dataclass
class Result:
    """One verdict, plus exactly how the bash original laid it out."""

    status: str
    note: str | None = None
    # Lines the bash heredoc wrote to stdout, which appeared VERBATIM and
    # unindented above the note. Preserved so the transcript matches.
    passthrough: tuple = ()
    # Lines the bash version captured in a log file and re-printed indented.
    detail: tuple = ()
    detail_indent: str = "    "
    detail_head: int | None = None
    detail_tail: int | None = None

    def render(self) -> list[str]:
        out = [str(line) for line in self.passthrough]
        if self.note is not None:
            out.append("  " + self.note)
        detail = [str(line) for line in self.detail]
        if self.detail_head is not None:
            detail = detail[: self.detail_head]
        if self.detail_tail is not None:
            detail = detail[-self.detail_tail:] if self.detail_tail else []
        out += [self.detail_indent + line for line in detail]
        return out


@dataclass
class Section:
    """A `== title ==` block. One section can hold several verdicts.

    The unit-test section emits one per test file, which is why this is a list
    and not a single Result.
    """

    title: str
    results: list = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(r.status == FAIL for r in self.results)

    def render(self) -> list[str]:
        out = ["", "== %s ==" % self.title]
        for result in self.results:
            out += result.render()
        return out


def exit_code(sections) -> int:
    """Any FAIL anywhere => 1. SKIP and OK => 0.

    Mirrors the bash `FAIL=1` flag, which was set (never cleared) by each
    failing section and used verbatim as the exit status.
    """
    return 1 if any(s.failed for s in sections) else 0


def verdict_line(sections) -> str:
    if exit_code(sections) == 0:
        return "verify-config: PASS"
    return "verify-config: FAIL (fix the above before pushing)"


def guarded(note_on_crash, fn, *args, detail_indent="    ", **kwargs) -> Result:
    """Run a check; turn an unexpected exception into a FAIL, never a pass.

    The bash original got this for free: a heredoc that raised printed its
    traceback to the section's log and exited non-zero, so a malformed
    config/litellm.yaml failed the gate rather than skipping it. Keeping that
    shape matters more than tidy errors — the one outcome a gate may never
    produce is a silent pass.
    """
    try:
        return fn(*args, **kwargs)
    except Exception:                                        # noqa: BLE001
        import traceback
        lines = "".join(traceback.format_exc()).rstrip("\n").split("\n")
        return Result(FAIL, note_on_crash, detail=tuple(lines),
                      detail_indent=detail_indent)
