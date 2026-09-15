#!/usr/bin/python3 -IS
"""trace-sh: $SHELL shim that times the agent's shell tool calls.

The entrypoint points SHELL here when AGENT_TRACE=1 (forge harness): forge
runs its `shell` tool as `$SHELL -c <command>`, so every command passes
through. Each one runs under the real /bin/sh; on exit one JSON line goes to
$TOOL_TRACE_LOG with wall time, exit status, and the rusage of the shell plus
everything it waited for (CPU time, largest single process RSS, block IO).

Best-effort by design: tracing never changes a command's exit status or
output, and with TOOL_TRACE_LOG unset this is a plain exec of /bin/sh.

Why Python: interpreter startup costs ~9 ms per command (bare /bin/sh:
0.3 ms). Against recorded task runs that is <0.25% of wall-clock, and the
clock starts after startup, so recorded durations exclude it. A compiled
shim is the upgrade path if tracing ever becomes always-on.
Regression test: scripts/test-jail-image.sh.
"""
import json
import os
import signal
import sys
import time

REAL = "/bin/sh"
CMD_MAX = 4096          # cap per-record command text; heredoc writes get huge

argv = [REAL] + sys.argv[1:]
log = os.environ.get("TOOL_TRACE_LOG")
if not log:
    os.execv(REAL, argv)

start_ns = time.time_ns()
t0 = time.monotonic_ns()
pid = os.fork()
if pid == 0:
    try:
        os.execv(REAL, argv)
    finally:
        os._exit(127)


def forward(sig, _frame):
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


for s in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(s, forward)

_, status, ru = os.wait4(pid, 0)          # retried on EINTR (PEP 475)
wall_ms = (time.monotonic_ns() - t0) / 1e6
code = os.waitstatus_to_exitcode(status)
if code < 0:
    code = 128 - code                     # killed by signal N -> 128+N, as a shell reports it

args = sys.argv[1:]
cmd = args[args.index("-c") + 1] if "-c" in args[:-1] else " ".join(args)
record = {
    "start_ns": start_ns,
    "wall_ms": round(wall_ms, 1),
    "exit": code,
    "pid": pid,
    "cpu_user_ms": round(ru.ru_utime * 1000, 1),
    "cpu_sys_ms": round(ru.ru_stime * 1000, 1),
    "max_rss_kb": ru.ru_maxrss,
    "inblock": ru.ru_inblock,
    "oublock": ru.ru_oublock,
    "cmd": cmd[:CMD_MAX],
    "cmd_len": len(cmd),
}
try:
    # One O_APPEND write per record, so parallel tool calls never interleave.
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(record) + "\n").encode())
    finally:
        os.close(fd)
except OSError:
    pass
sys.exit(code & 0xFF)
