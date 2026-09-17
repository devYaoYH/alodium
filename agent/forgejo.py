#!/usr/bin/env python3
"""
forgejo — a minimal CLI for the Forgejo operations an agent runs a lot.

Why it exists: agents wrote all their Forgejo work as raw `curl` plus
`python3 -c` pipelines. 46% of shell calls in 77 dispatched runs hit the
API directly, ~23% of writes failed, and the failures clustered around
shell-quoting of JSON bodies containing backticks / parens / newlines /
fenced code. This helper moves JSON encoding, label-id lookup and
endpoint shape into a single place so callers pass typed flags and read
a file with `--body-file`, never a JSON string through the shell.

Subcommands cover exactly the calls the briefs and skills already
ask for:

    issue view  <n> [--json]
    issue comment <n> --file <path>
    issue label <n> <name>
    pr list [--open|--closed|--all] [--repo owner/name]
    pr view  <n> [--repo owner/name] [--json]
    pr create --title T --body-file F [--head B] [--base main] [--repo owner/name]
    pr request-review <n> <user> [--repo owner/name]

Output is compact text by default; pass `--json` to get the raw API
response (for callers that want to parse it further). Errors are
non-zero exit + a one-line message that never includes the token.

Auth: `$AGENT_FORGEJO_TOKEN`. Base: `http://forgejo:3000`.
Default repo for `issue` subcommands: `$COORDINATION_REPO`.
Default repo for `pr`   subcommands: `$NODE_CONFIG_REPO`.
Override with `--repo owner/name`.

Stdlib only — the jail image has python3 with no extra packages.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional, Tuple

API_BASE = "http://forgejo:3000"
TIMEOUT = 30  # seconds; the agent's network is internal so 30s is generous

# NEVER printed anywhere. If you need to debug auth, log the request
# line minus the Authorization header (we already do that on error).
TOKEN_ENV = "AGENT_FORGEJO_TOKEN"
COORD_ENV = "COORDINATION_REPO"
NODE_ENV = "NODE_CONFIG_REPO"


# ----------------------------- HTTP plumbing --------------------------------

def _token() -> str:
    tok = os.environ.get(TOKEN_ENV, "")
    if not tok:
        die(f"{TOKEN_ENV} is not set in the environment; no auth, no API call.")
    return tok


def _repo_for(sub: str, override: Optional[str]) -> str:
    """Pick the repo based on the subcommand; allow --repo override."""
    if override:
        return override
    env = NODE_ENV if sub.startswith("pr") else COORD_ENV
    val = os.environ.get(env, "")
    if not val:
        die(f"--repo not given and ${env} is not set; cannot guess the repo.")
    return val


def _request(
    method: str,
    path: str,
    query: Optional[dict] = None,
    body: Optional[dict] = None,
) -> Tuple[int, Any]:
    """
    Make an API call. Returns (http_status, parsed_json_or_text).

    - 2xx with JSON body → (status, dict/list)
    - 2xx with empty body → (status, None)
    - non-2xx              → (status, parsed body or raw text)
    Network errors raise; we catch at the call site to format nicely.
    """
    url = API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None
    headers = {
        "Authorization": f"token {_token()}",
        "Accept": "application/json",
        "User-Agent": "agent-dev/forgejo-cli",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        # Forgejo returns a JSON error body on 4xx/5xx — surface it so the
        # operator can see WHY; we redact anything that looks like a token
        # before printing.
        raw = e.read() if e.fp else b""
        return e.code, _parse_body(raw)
    except urllib.error.URLError as e:
        die(f"connection error: {e.reason}")

    if not raw:
        return status, None
    return status, _parse_body(raw)


def _parse_body(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # Not JSON — return text so the caller can show it.
        return raw.decode("utf-8", errors="replace")


def _request_raw(
    method: str,
    path: str,
    query: Optional[dict] = None,
) -> Tuple[int, bytes]:
    """
    Make an API call and return raw bytes (no JSON parsing).
    Used for binary downloads like attachment content.
    """
    url = API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {
        "Authorization": f"token {_token()}",
        "Accept": "application/octet-stream",
        "User-Agent": "agent-dev/forgejo-cli",
    }
    req = urllib.request.Request(url, data=None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b""
        return e.code, raw
    except urllib.error.URLError as e:
        die(f"connection error: {e.reason}")


def _check(status: int, payload: Any, op: str) -> Any:
    """Turn non-2xx into a die(); return the JSON payload on success."""
    if 200 <= status < 300:
        return payload
    # Build a single-line message; never include the token. The token
    # never enters our logs because we read it from env and only put it
    # in an HTTP header, not in any string we display.
    msg = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    die(f"{op}: HTTP {status}: {_one_line(msg)}")


def _one_line(s: str) -> str:
    """Collapse whitespace so error messages stay single-line."""
    return " ".join(s.split())


# ----------------------------- output formatting ----------------------------

def fmt_issue(issue: dict, comments: list) -> str:
    """Compact text view of an issue + its comments."""
    n = issue.get("number", "?")
    title = issue.get("title", "")
    state = issue.get("state", "")
    author = (issue.get("user") or {}).get("login", "")
    created = issue.get("created_at", "")
    labels = ", ".join(l.get("name", "") for l in issue.get("labels") or [])
    body = issue.get("body") or ""

    out = [
        f"#{n}  {title}",
        f"state: {state}  author: {author}  created: {created}  labels: {labels or '(none)'}",
        "",
        body,
    ]
    if comments:
        out.append("")
        out.append("---")
        out.append(f"## Comments ({len(comments)})")
        out.append("")
        for c in comments:
            cid = c.get("id", "?")
            cauthor = (c.get("user") or {}).get("login", "")
            cdate = c.get("created_at", "")
            cbody = c.get("body") or ""
            out.append(f"[{cid}] {cauthor}  {cdate}")
            out.append(cbody)
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def fmt_pr_list(prs: list) -> str:
    if not prs:
        return "(no PRs)\n"
    lines = []
    for pr in prs:
        n = pr.get("number", "?")
        title = pr.get("title", "")
        head = (pr.get("head") or {}).get("label", "")
        base = (pr.get("base") or {}).get("label", "")
        state = pr.get("state", "")
        out = f"#{n}  {title}  ({base} <- {head})  [{state}]"
        lines.append(out)
    return "\n".join(lines) + "\n"


def fmt_pr(pr: dict) -> str:
    n = pr.get("number", "?")
    title = pr.get("title", "")
    state = pr.get("state", "")
    author = (pr.get("user") or {}).get("login", "")
    base = (pr.get("base") or {}).get("label", "")
    head = (pr.get("head") or {}).get("label", "")
    url = pr.get("html_url", "")
    body = pr.get("body") or ""
    out = [
        f"#{n}  {title}",
        f"state: {state}  author: {author}  base: {base}  head: {head}",
        f"url: {url}",
        "",
        body,
    ]
    return "\n".join(out).rstrip() + "\n"


# ----------------------------- subcommands ----------------------------------

def cmd_issue_view(args: argparse.Namespace) -> int:
    repo = _repo_for("issue", args.repo)
    path = f"/api/v1/repos/{repo}/issues/{args.n}"
    s, issue = _request("GET", path)
    issue = _check(s, issue, f"GET issue {args.n}")

    # Comments — paginated; pull them all (coordination issues have <10 in practice).
    comments: list = []
    page = 1
    while True:
        s, payload = _request(
            "GET", f"/api/v1/repos/{repo}/issues/{args.n}/comments",
            query={"page": page, "limit": 50},
        )
        payload = _check(s, payload, f"GET comments for issue {args.n}")
        if not isinstance(payload, list) or not payload:
            break
        comments.extend(payload)
        if len(payload) < 50:
            break
        page += 1

    if args.json:
        print(json.dumps({"issue": issue, "comments": comments}, indent=2))
    else:
        sys.stdout.write(fmt_issue(issue, comments))
    return 0


def cmd_issue_comment(args: argparse.Namespace) -> int:
    repo = _repo_for("issue", args.repo)
    body = _read_body_file(args.file)
    payload = {"body": body}
    s, resp = _request(
        "POST", f"/api/v1/repos/{repo}/issues/{args.n}/comments", body=payload
    )
    resp = _check(s, resp, f"comment on issue {args.n}")
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        cid = resp.get("id", "?") if isinstance(resp, dict) else "?"
        print(f"commented on issue {args.n} (comment id {cid})")
    return 0


def cmd_issue_label(args: argparse.Namespace) -> int:
    repo = _repo_for("issue", args.repo)
    # Resolve label id (Forgejo's create API takes IDs, not names).
    s, labels = _request("GET", f"/api/v1/repos/{repo}/labels", query={"limit": 100})
    labels = _check(s, labels, "list labels")
    label_id = None
    if isinstance(labels, list):
        for l in labels:
            if l.get("name", "").lower() == args.name.lower():
                label_id = l.get("id")
                break
    if label_id is None:
        die(f"label '{args.name}' not found in {repo}")
    s, resp = _request(
        "POST", f"/api/v1/repos/{repo}/issues/{args.n}/labels",
        body={"labels": [label_id]},
    )
    resp = _check(s, resp, f"label issue {args.n} with '{args.name}'")
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        print(f"labeled issue {args.n} with '{args.name}' (id {label_id})")
    return 0


def cmd_pr_list(args: argparse.Namespace) -> int:
    repo = _repo_for("pr", args.repo)
    state = args.state
    s, prs = _request("GET", f"/api/v1/repos/{repo}/pulls", query={"state": state, "limit": 50})
    prs = _check(s, prs, f"list PRs in {repo}")
    if args.json:
        print(json.dumps(prs, indent=2))
    else:
        sys.stdout.write(fmt_pr_list(prs if isinstance(prs, list) else []))
    return 0


def cmd_pr_view(args: argparse.Namespace) -> int:
    repo = _repo_for("pr", args.repo)
    s, pr = _request("GET", f"/api/v1/repos/{repo}/pulls/{args.n}")
    pr = _check(s, pr, f"GET PR {args.n}")
    if args.json:
        print(json.dumps(pr, indent=2))
    else:
        sys.stdout.write(fmt_pr(pr))
    return 0


def cmd_pr_create(args: argparse.Namespace) -> int:
    repo = _repo_for("pr", args.repo)
    body = _read_body_file(args.body_file)
    payload = {
        "title": args.title,
        "body": body,
        "head": args.head,
        "base": args.base or "main",
    }
    s, pr = _request("POST", f"/api/v1/repos/{repo}/pulls", body=payload)
    pr = _check(s, pr, f"create PR in {repo}")
    if args.json:
        print(json.dumps(pr, indent=2))
    else:
        n = pr.get("number", "?") if isinstance(pr, dict) else "?"
        url = pr.get("html_url", "") if isinstance(pr, dict) else ""
        print(f"opened PR #{n} in {repo}: {url}")
    return 0


def cmd_pr_request_review(args: argparse.Namespace) -> int:
    repo = _repo_for("pr", args.repo)
    # Forgejo returns a LIST (not an object) on this endpoint — a fact the
    # old `curl | python3` pipeline got wrong repeatedly. We don't parse,
    # we just check the status.
    s, resp = _request(
        "POST",
        f"/api/v1/repos/{repo}/pulls/{args.n}/requested_reviewers",
        body={"reviewers": [args.user]},
    )
    resp = _check(s, resp, f"request review on PR {args.n} for '{args.user}'")
    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        print(f"requested '{args.user}' as reviewer for PR {args.n}")
    return 0


# ----------------------------- attachment subcommands -------------------------

ATTACHMENT_HOST_RE = r"https://git\.localhost/attachments/"
UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def _resolve_uuid(issue: int, raw: str, repo: str) -> Tuple[str, int]:
    """
    Return (uuid, size) for an attachment identifier.

    raw can be:
      - a numeric id (e.g. "6")
      - a UUID (e.g. "2ed9e20e-...")
      - a full URL (e.g. "https://git.localhost/attachments/<uuid>")
    """
    import re

    # Full URL — extract uuid
    if raw.startswith("https://git.localhost/attachments/"):
        raw = raw.replace("https://git.localhost/attachments/", "")
        # Strip trailing slash or query if present
        raw = raw.split("?")[0].rstrip("/")

    # Numeric — list attachments and find by id
    if raw.isdigit():
        s, atts = _request("GET", f"/api/v1/repos/{repo}/issues/{issue}/attachments")
        atts = _check(s, atts, f"list attachments for issue {issue}")
        if not isinstance(atts, list):
            die(f"expected a list of attachments, got {type(atts).__name__}")
        aid = int(raw)
        for a in atts:
            if a.get("id") == aid:
                uuid = a.get("uuid", "")
                size = a.get("size", 0)
                if not uuid:
                    die(f"attachment {aid} has no uuid field")
                return uuid, size
        die(f"no attachment with id {aid} found on issue {issue}")

    # Plain uuid — validate format
    if re.match(UUID_RE, raw):
        # We still need the size; grab the metadata from the API
        s, atts = _request("GET", f"/api/v1/repos/{repo}/issues/{issue}/attachments")
        atts = _check(s, atts, f"list attachments for issue {issue}")
        size = 0
        for a in (atts if isinstance(atts, list) else []):
            if a.get("uuid", "") == raw:
                size = a.get("size", 0)
                break
        return raw, size

    die(f"unrecognised attachment identifier: {raw[:80]}")


def cmd_attachment_list(args: argparse.Namespace) -> int:
    repo = _repo_for("issue", args.repo)
    s, atts = _request("GET", f"/api/v1/repos/{repo}/issues/{args.n}/attachments")
    atts = _check(s, atts, f"list attachments for issue {args.n}")
    if not isinstance(atts, list):
        die(f"expected list, got {type(atts).__name__}")

    if args.json:
        print(json.dumps(atts, indent=2))
        return 0

    if not atts:
        print(f"(no attachments on issue {args.n})")
        return 0

    print(f"Attachments on issue #{args.n}:")
    for a in atts:
        aid = a.get("id", "?")
        aname = a.get("name", "")
        auuid = a.get("uuid", "")
        asize = a.get("size", 0)
        furl = a.get("download_url", "")
        print(f"  [{aid}] {aname or '(unnamed)'}")
        print(f"        uuid: {auuid}")
        print(f"        size: {asize} bytes")
        if furl:
            print(f"        url:  {furl}")
    return 0


def cmd_attachment_fetch(args: argparse.Namespace) -> int:
    repo = _repo_for("issue", args.repo)
    uuid, size = _resolve_uuid(args.n, args.identifier, repo)

    # Download raw bytes from /attachments/{uuid}
    url = f"/attachments/{uuid}"
    s, raw_body = _request_raw("GET", url)
    _check(s, raw_body, f"download attachment {uuid}")

    if not isinstance(raw_body, bytes):
        die(f"expected binary data, got {type(raw_body).__name__}")

    if args.out:
        with open(args.out, "wb") as f:
            f.write(raw_body)
        print(f"downloaded attachment {uuid} ({size} bytes) to {args.out}")
    else:
        # Write to stdout for piping. sys.stdout.buffer exists in real
        # terminals; in test contexts (StringIO) we decode.
        buf = getattr(sys.stdout, "buffer", None)
        if isinstance(raw_body, bytes) and buf is not None:
            buf.write(raw_body)
        elif isinstance(raw_body, bytes):
            sys.stdout.write(raw_body.decode("utf-8", errors="replace"))
        else:
            sys.stdout.write(raw_body)
    return 0


# ----------------------------- helpers --------------------------------------

def _read_body_file(path: str) -> str:
    """Read the comment/PR body from a file. UTF-8. Fail loudly if absent.

    This is the load-bearing piece: callers write the body to a file (e.g.
    `cat <<'EOF' > /tmp/c.md`) and pass `--file /tmp/c.md`, so the shell
    never has to quote a JSON string. Newlines, parens, backticks and
    fenced blocks all pass through cleanly.
    """
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8")
    except FileNotFoundError:
        die(f"body file not found: {path}")
    except IsADirectoryError:
        die(f"body file is a directory: {path}")
    except PermissionError:
        die(f"body file not readable: {path}")


def die(msg: str, code: int = 1) -> None:
    """Print a single-line error and exit non-zero. Never prints the token."""
    sys.stderr.write(f"forgejo: {msg}\n")
    sys.exit(code)


# ----------------------------- argparse -------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="forgejo",
        description="Forgejo CLI helper for the agent jail — see header.",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    # ---- issue ----
    iv = sub.add_parser("issue", help="issue operations on the coordination repo")
    issue_sub = iv.add_subparsers(dest="issue_cmd", required=True, metavar="OP")

    iv_view = issue_sub.add_parser("view", help="show an issue and its comments")
    iv_view.add_argument("n", type=int, help="issue number")
    iv_view.add_argument("--json", action="store_true", help="raw JSON")
    iv_view.add_argument("--repo", help="owner/name (default $COORDINATION_REPO)")
    iv_view.set_defaults(func=cmd_issue_view)

    iv_comment = issue_sub.add_parser("comment", help="add a comment from a file")
    iv_comment.add_argument("n", type=int, help="issue number")
    iv_comment.add_argument("--file", required=True, help="path to UTF-8 file with comment body")
    iv_comment.add_argument("--json", action="store_true", help="raw JSON")
    iv_comment.add_argument("--repo", help="owner/name (default $COORDINATION_REPO)")
    iv_comment.set_defaults(func=cmd_issue_comment)

    iv_label = issue_sub.add_parser("label", help="add a label by name (resolves id)")
    iv_label.add_argument("n", type=int, help="issue number")
    iv_label.add_argument("name", help="label name (e.g. handoff)")
    iv_label.add_argument("--json", action="store_true", help="raw JSON")
    iv_label.add_argument("--repo", help="owner/name (default $COORDINATION_REPO)")
    iv_label.set_defaults(func=cmd_issue_label)

    # ---- pr ----
    pr = sub.add_parser("pr", help="PR operations on the node-config repo")
    pr_sub = pr.add_subparsers(dest="pr_cmd", required=True, metavar="OP")

    pr_list = pr_sub.add_parser("list", help="list PRs")
    pr_list.add_argument("--state", choices=("open", "closed", "all"), default="open",
                         help="filter by state (default open)")
    pr_list.add_argument("--json", action="store_true", help="raw JSON")
    pr_list.add_argument("--repo", help="owner/name (default $NODE_CONFIG_REPO)")
    pr_list.set_defaults(func=cmd_pr_list)

    pr_view = pr_sub.add_parser("view", help="show a PR")
    pr_view.add_argument("n", type=int, help="PR number")
    pr_view.add_argument("--json", action="store_true", help="raw JSON")
    pr_view.add_argument("--repo", help="owner/name (default $NODE_CONFIG_REPO)")
    pr_view.set_defaults(func=cmd_pr_view)

    pr_create = pr_sub.add_parser("create", help="open a PR")
    pr_create.add_argument("--title", required=True, help="PR title")
    pr_create.add_argument("--body-file", required=True,
                           help="UTF-8 file with PR body (never quoted by the shell)")
    pr_create.add_argument("--head", required=True, help="branch name in the fork")
    pr_create.add_argument("--base", default="main", help="base branch (default main)")
    pr_create.add_argument("--json", action="store_true", help="raw JSON")
    pr_create.add_argument("--repo", help="owner/name (default $NODE_CONFIG_REPO)")
    pr_create.set_defaults(func=cmd_pr_create)

    pr_review = pr_sub.add_parser("request-review", help="ask someone to review a PR")
    pr_review.add_argument("n", type=int, help="PR number")
    pr_review.add_argument("user", help="username (e.g. operator)")
    pr_review.add_argument("--json", action="store_true", help="raw JSON")
    pr_review.add_argument("--repo", help="owner/name (default $NODE_CONFIG_REPO)")
    pr_review.set_defaults(func=cmd_pr_request_review)

    # ---- attachment ----
    att = sub.add_parser("attachment", help="attachment operations on coordination issues")
    att_sub = att.add_subparsers(dest="att_cmd", required=True, metavar="OP")

    att_list = att_sub.add_parser("list", help="list attachments on an issue")
    att_list.add_argument("n", type=int, help="issue number")
    att_list.add_argument("--json", action="store_true", help="raw JSON")
    att_list.add_argument("--repo", help="owner/name (default $COORDINATION_REPO)")
    att_list.set_defaults(func=cmd_attachment_list)

    att_fetch = att_sub.add_parser("fetch", help="download an attachment by id, uuid, or URL")
    att_fetch.add_argument("n", type=int, help="issue number")
    att_fetch.add_argument("identifier", help="attachment id, uuid, or full URL")
    att_fetch.add_argument("--out", help="output file path (default: stdout)")
    att_fetch.add_argument("--repo", help="owner/name (default $COORDINATION_REPO)")
    att_fetch.set_defaults(func=cmd_attachment_fetch)

    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
