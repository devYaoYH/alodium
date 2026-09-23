"""
info — config/homepage/static/deploy-info.json, the node's deploy stamp.

The homepage's lower-left badge reads this file, and scripts/deploy-watch.sh
reads it to decide whether to deploy again. That second reader is why the two
commit fields are not redundant and why this module is pure and tested:

  `commit`           what THIS run deployed or TRIED to (the widget links it)
  `deployed_commit`  the last commit that deployed SUCCESSFULLY (ok | warning)

The watcher keys off `deployed_commit`. Get the carry-forward wrong and a
failed deploy either looks deployed — so the node sits broken and nothing
retries — or a successful one does not, so every two-minute heartbeat redeploys
forever. Both are silent. Hence `deployed_commit_for` below, which is a pure
function of (status, previous file contents) with a test per branch.
"""

import json

OK = "ok"
WARNING = "warning"
FAILED = "failed"


def final_status(messages) -> str:
    """`ok` for a clean run, `warning` if anything was recorded along the way.

    The bash spells this `[[ -s "$DEPLOY_MSGS" ]]` — ANY recorded line, at any
    level, downgrades the run. A hard abort never reaches here; it writes
    `failed` from the error handler instead.
    """
    return WARNING if messages else OK


def deployed_commit_for(status: str, commit: str, previous) -> str:
    """What `deployed_commit` becomes, given this run's status and the old file.

    A successful run (ok or warning) deployed this commit, so it advances.
    A FAILED run must not advance it: it carries the previous value forward so
    the watcher retries instead of believing the broken tip is live.

    `previous` is the parsed old file, or None when it was missing or
    unreadable (first deploy ever, or the file was lost) — an unreadable file
    yields "", which the watcher reads as "nothing deployed yet" and retries.
    That is the safe direction: a spurious retry costs one idempotent deploy,
    a spurious "already deployed" costs an outage nobody is told about.

    The `"deployed_commit" in previous` test is a compatibility branch for
    files written before the field existed: those count their `commit` only if
    that run had not failed.
    """
    if status != FAILED:
        return commit
    previous = previous or {}
    if "deployed_commit" in previous:
        return previous["deployed_commit"] or ""
    return previous.get("commit", "") if previous.get("status") != FAILED else ""


def message_entries(messages) -> list[dict]:
    """[(level, text)] -> the JSON array the homepage renders.

    The bash stashed messages in a TSV file and re-read it here, so a text
    containing a tab or a newline came back split. That round-trip is
    reproduced rather than skipped: the messages carry `tail -1` of a failing
    script's stderr, and if one of those ever does arrive multi-line, the
    homepage should show what it showed before this port, not something new.
    """
    out = []
    for level, text in messages:
        for line in f"{level}\t{text}".split("\n"):
            if not line:
                continue
            level_part, _, text_part = line.partition("\t")
            out.append({"level": level_part, "text": text_part})
    return out


def commit_url(domain: str, repo: str, commit: str) -> str:
    """The Forgejo commit link the badge points at. Defaults match the bash's
    `${NODE_DOMAIN:-localhost}` / `${NODE_CONFIG_REPO:-operator/node-config}`,
    so a deploy run without .env still writes a well-formed file."""
    return f"https://git.{domain or 'localhost'}/{repo or 'operator/node-config'}/commit/{commit}"


def build(status, timestamp, commit, short_hash, url, messages, previous) -> dict:
    """The whole artifact. Key order is the bash's, so a `git diff` of the file
    across the port shows only the values that actually moved."""
    return {
        "timestamp": timestamp,
        "commit": commit,
        "short_hash": short_hash,
        "url": url,
        "status": status,
        "deployed_commit": deployed_commit_for(status, commit, previous),
        "messages": message_entries(messages),
    }


def render(info: dict) -> str:
    """`json.dumps(..., indent=2)` plus the trailing newline `print` added."""
    return json.dumps(info, indent=2) + "\n"


__all__ = ["OK", "WARNING", "FAILED", "final_status", "deployed_commit_for",
           "message_entries", "commit_url", "build", "render"]
