#!/usr/bin/python3 -IS
"""ui-trace: time forge's built-in tools from its own status lines.

    FORGE_UI_TRACE=/tmp/trace/ui.jsonl ui-trace forge -p "<prompt>"

Forge prints one status line as each tool starts ("● [HH:MM:SS] Read
foo.txt", "Create …", "Replace …", "Search for …", "Update Todos …"). With
AGENT_TRACE=1 the entrypoint runs forge through this wrapper: forge gets a
pty of its own (it refuses to run without one), every byte it writes is
relayed unchanged to our stdout, and each status line is appended to
$FORGE_UI_TRACE as JSON with the wall-clock nanosecond at which its first
byte arrived. scripts/trace-render.py turns those into measured tool starts;
a tool ends at the next status line or the next model request.

Best effort by design: exit status and signals pass through, a failed log
write never affects forge, and with FORGE_UI_TRACE unset this is a plain
exec. Stdlib only, no privileges. Status titles hold file paths and shell
commands — the log is as sensitive as tools.jsonl.
Regression test: scripts/test-jail-image.sh.
"""
import errno
import fcntl
import json
import os
import pty
import re
import select
import signal
import sys
import termios
import time

LOG = os.environ.get("FORGE_UI_TRACE")
if len(sys.argv) < 2:
    sys.exit("usage: ui-trace <command> [args...]")
if not LOG:
    os.execvp(sys.argv[1], sys.argv[1:])

TITLE_MAX = 300
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>]")
STATUS = re.compile(r"●\s*\[(\d\d:\d\d:\d\d)\]\s*(.*)")
# Title prefix -> forge tool name (forge 2.13.18, crates/forge_app/src/fmt/fmt_input.rs).
KINDS = [
    (re.compile(r"Read Todos\b"), "todo_read"),
    (re.compile(r"Read\b"), "read"),
    (re.compile(r"(Create|Overwrite)\b"), "write"),
    (re.compile(r"Replace\b.*\(\d+ edits\)$"), "multi_patch"),
    (re.compile(r"Replace( All)?\b"), "patch"),
    (re.compile(r"Search for\b"), "fs_search"),
    (re.compile(r"Codebase Search\b"), "sem_search"),
    (re.compile(r"Remove\b"), "remove"),
    (re.compile(r"Undo\b"), "undo"),
    (re.compile(r"Execute \["), "shell"),
    (re.compile(r"GET\b"), "fetch"),
    (re.compile(r"Follow-up\b"), "followup"),
    (re.compile(r"Skill\b"), "skill"),
    (re.compile(r"Update Todos\b"), "todo_write"),
    (re.compile(r"Task\b|\S+ \[Agent\]"), "task"),
    (re.compile(r"MCP\b"), "mcp"),
]


def kind_of(title):
    for pat, kind in KINDS:
        if pat.match(title):
            return kind
    return None  # Initialize / Finished / Migrated … — kept as run events


try:
    log_fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
except OSError:
    log_fd = None


def record(t_ns, text):
    m = STATUS.search(text)
    if not m or log_fd is None:
        return
    title = m.group(2).strip()
    rec = {"t_ns": t_ns, "clock": m.group(1), "kind": kind_of(title), "title": title[:TITLE_MAX]}
    try:
        os.write(log_fd, (json.dumps(rec) + "\n").encode())
    except OSError:
        pass


def copy_winsize(fd):
    for src in (sys.stdout, sys.stdin):
        try:
            if src.isatty():
                fcntl.ioctl(fd, termios.TIOCSWINSZ, fcntl.ioctl(src.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
                return
        except (OSError, ValueError):
            pass


pid, master = pty.fork()
if pid == 0:
    try:
        os.execvp(sys.argv[1], sys.argv[1:])
    finally:
        os._exit(127)

copy_winsize(master)
for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(sig, lambda s, _f: os.kill(pid, s))
signal.signal(signal.SIGWINCH, lambda _s, _f: copy_winsize(master))

stdin_fd = None
old_tty = None
try:
    # Take over the terminal (raw mode + keystroke relay) only when our process
    # group owns it. A background group — e.g. under `timeout` without
    # --foreground — that touches the tty is stopped by SIGTTOU/SIGTTIN, and
    # forge's output would then sit unread (and un-timestamped) until something
    # sent SIGCONT. Not owning the terminal just means no keystroke relay.
    if sys.stdin.isatty() and os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp():
        stdin_fd = sys.stdin.fileno()
        old_tty = termios.tcgetattr(stdin_fd)
        import tty
        tty.setraw(stdin_fd)
except (OSError, ValueError, termios.error):
    stdin_fd = old_tty = None


def relay(data):
    global stdout_open
    view = memoryview(data)
    while stdout_open and view:
        try:
            view = view[os.write(1, view):]
        except OSError:
            stdout_open = False  # reader went away; keep draining forge


stdout_open = True
partial, partial_t = b"", 0
status = None
try:
    while True:
        fds = [master] + ([stdin_fd] if stdin_fd is not None else [])
        ready, _, _ = select.select(fds, [], [], 0.2)
        if stdin_fd in ready:
            try:
                data = os.read(stdin_fd, 4096)
            except OSError:
                data = b""
            if data:
                os.write(master, data)
            else:
                stdin_fd = None
        if master in ready:
            try:
                data = os.read(master, 65536)
            except OSError as e:
                if e.errno != errno.EIO:
                    raise
                data = b""
            if not data:
                break
            now = time.time_ns()
            relay(data)
            chunk = partial + data
            lines = re.split(rb"[\r\n]", chunk)
            starts = [partial_t if partial else now] + [now] * (len(lines) - 1)
            for line, t in zip(lines[:-1], starts):
                if line:
                    record(t, ANSI.sub("", line.decode("utf-8", "replace")))
            partial = lines[-1]
            partial_t = starts[-1] if partial else 0
        elif status is None:
            done, st = os.waitpid(pid, os.WNOHANG)
            if done:
                status = st
                # forge exited but something may still hold the pty; drain briefly
                while select.select([master], [], [], 0.05)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    relay(data)
                break
finally:
    if old_tty is not None:
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)  # never stop on the way out
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_tty)
        except (OSError, termios.error):
            pass

if partial:
    record(partial_t, ANSI.sub("", partial.decode("utf-8", "replace")))
if status is None:
    _, status = os.waitpid(pid, 0)
code = os.waitstatus_to_exitcode(status)
sys.exit(128 - code if code < 0 else code & 0xFF)
