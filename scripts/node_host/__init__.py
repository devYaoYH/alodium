"""
node_host — what the host-side jobs share, so none of them re-derives it.

The dispatcher (scripts/task_dispatcher.py), the per-issue run it spawns
(scripts/dispatch_run.py) and the auto-deploy watcher (scripts/deploy_watch.py)
all used to open with the same bash preamble: `set -a; source .env`, a `mkdir`
pass lock aged with `stat -f %m || stat -c %Y`, an `A()` curl wrapper pinned to
127.0.0.1, and an `awk` frontmatter reader copied between three scripts. Each
of those was a place where macOS and Linux disagreed, which is why they live
here, once, in the standard library:

  envfile      .env as compose writes it, parsed rather than sourced
  lock         the pass lock: os.mkdir, atomic on every OS
  forgejo      the Forgejo API on git.<domain>, pinned to 127.0.0.1:443
  frontmatter  a brief's `key: value` header, exactly as the awk read it
  text         output tails, the run-output filter, the audit-line format
  jail         the "Jail summary" both dispatch paths post
  host         the edge: subprocesses, docker queries, the detached spawn
  testkit      the scenario worlds and fakes the equivalence tests replay

`host` and the transport in `forgejo` are the only code that touches the
outside world; everything else is a pure function with offline tests.

Stdlib only — the node's python3 is what runs this, with no pip.
"""
