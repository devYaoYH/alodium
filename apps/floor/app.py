#!/usr/bin/env python3
"""floor — the factory-floor aggregator.

Read-only, stdlib-only. A background poller keeps one JSON snapshot of the
node warm; the HTTP side serves it plus the static isometric site. Nothing
here mutates anything anywhere: every upstream call is a GET with a weak or
absent credential, and every absent credential degrades to "that signal is
not on the floor" rather than an error.

Sources (all optional except the first two being *useful*):
  registry      http://registry:8090/v1/services     topology, no creds
  docker-proxy  http://docker-proxy:2375/containers  who is running, no creds
  forgejo       commits / PRs / issues               FLOOR_GIT_TOKEN
  litellm       /key/info spend self-report          FLOOR_*_LLM_KEY

Surface:
  GET /healthz          liveness
  GET /v1/floor         rooms + edges + sources (the map)
  GET /v1/activity      recent events + statuses (the motion), ?since=<id>
  GET /*                the isometric site (static, from ./site)
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "8080"))
POLL_SECONDS = int(os.environ.get("FLOOR_POLL_SECONDS", "10"))
SITE_DIR = Path(__file__).parent / "site"

REGISTRY_URL = os.environ.get("FLOOR_REGISTRY_URL", "http://registry:8090")
DOCKER_URL = os.environ.get("FLOOR_DOCKER_URL", "http://docker-proxy:2375")
FORGEJO_URL = os.environ.get("FLOOR_FORGEJO_URL", "http://forgejo:3000")
LITELLM_URL = os.environ.get("FLOOR_LITELLM_URL", "http://litellm:4000")

GIT_TOKEN = os.environ.get("FLOOR_GIT_TOKEN", "")
LLM_KEYS = {  # tenant label -> budgeted virtual key (self-report only)
    "agent-dev": os.environ.get("FLOOR_AGENT_LLM_KEY", ""),
    "assistant": os.environ.get("FLOOR_ASSISTANT_LLM_KEY", ""),
}
WATCHED_REPOS = [r for r in (
    os.environ.get("FLOOR_NODE_CONFIG_REPO", "operator/node-config"),
    os.environ.get("FLOOR_COORDINATION_REPO", "operator/coordination"),
) if r]

# --- the semantic map ---------------------------------------------------------
# Core plane rooms (things that exist as containers but publish no manifest).
# Registry manifests are the source of truth for apps; this catalog only
# covers the node's fixed skeleton, so a NEW app needs zero edits here — it
# gets a room the moment its manifest lands (unknown kinds become a generic
# workshop; see site/assets/ASSETS.md for adding a bespoke archetype).
CORE_ROOMS = {
    "caddy":        {"wing": "gate", "archetype": "gatehouse", "size": "large",
                     "label": "Front Door", "blurb": "reverse proxy — the only exposed thing"},
    "pocket-id":    {"wing": "gate", "archetype": "identity", "size": "medium",
                     "label": "Identity Desk", "blurb": "passkey-only OIDC for everyone inside"},
    "oauth2-proxy": {"wing": "gate", "archetype": "stamp", "size": "small",
                     "label": "Visitor Stamps", "blurb": "forward-auth shim for apps without OIDC"},
    "litellm":      {"wing": "core", "archetype": "reactor", "size": "large",
                     "label": "Inference Reactor", "blurb": "LLM gateway — keys, budgets, audit"},
    "litellm-db":   {"wing": "core", "archetype": "vault", "size": "small",
                     "label": "Reactor Vault", "blurb": "LiteLLM's postgres, private network"},
    "forgejo":      {"wing": "core", "archetype": "archive", "size": "large",
                     "label": "The Archive", "blurb": "git — source of truth and change ledger"},
    "registry":     {"wing": "core", "archetype": "catalog", "size": "small",
                     "label": "Card Catalog", "blurb": "service discovery: what exists, what can I call"},
    "homepage":     {"wing": "ops", "archetype": "console", "size": "medium",
                     "label": "Control Room", "blurb": "trusted-people dashboard"},
    "docker-proxy": {"wing": "ops", "archetype": "periscope", "size": "small",
                     "label": "Watch Booth", "blurb": "read-only container status feed"},
    "doorbell-runner": {"wing": "ops", "archetype": "bell", "size": "small",
                        "label": "Doorbell", "blurb": "assigned-issue dispatch trigger"},
    "agent":        {"wing": "bay", "archetype": "workbench", "size": "medium",
                     "label": "Resident Engineer", "blurb": "agent-dev: proposes through PRs only"},
    "assistant":    {"wing": "bay", "archetype": "frontdesk", "size": "medium",
                     "label": "Assistant", "blurb": "conversational tenant — reads and files issues"},
    "search-egress": {"wing": "gate", "archetype": "dock", "size": "small",
                      "label": "Search Chute", "blurb": "the only search service allowed outside"},
}

# App manifests → archetype, by keyword over name/description. First hit wins;
# no hit → "workshop" (the generic room every new node starts as).
ARCHETYPE_RULES = [
    (("calendar", "caldav", "radicale"), "calendar"),
    (("feed", "rss", "miniflux"), "antenna"),
    (("note", "memo"), "pinboard"),
    (("chat", "webui", "conversation"), "switchboard"),
    (("game", "snake", "arcade"), "arcade"),
    (("search",), "radar"),
    (("mail", "bridge", "drive"), "mailroom"),
    (("floor", "factory"), "drafting"),
    (("git", "forge"), "archive"),
    (("llm", "model", "inference"), "reactor"),
]

# needs.<key> in a manifest → an edge to this room, drawn automatically.
NEEDS_TARGETS = {
    "llm": "litellm",
    "git": "forgejo",
    "calendar": "radicale",
    "feeds": "miniflux",
    "search": "search-broker",
    "notes": "memos",
}

# Fixed skeleton wiring the manifests cannot express (mirrors docker-compose
# networks; changes rarely, reviewed like any config change).
CORE_EDGES = [
    ("caddy", "pocket-id", "ingress"),
    ("homepage", "forgejo", "watch"),
    ("homepage", "litellm", "watch"),
    ("homepage", "docker-proxy", "watch"),
    ("floor", "registry", "watch"),
    ("floor", "docker-proxy", "watch"),
    ("litellm", "litellm-db", "data"),
    ("litellm", "internet", "egress"),
    ("miniflux", "internet", "egress"),
    ("search-broker", "search-egress", "data"),
    ("search-egress", "internet", "egress"),
    ("doorbell-runner", "forgejo", "watch"),
    ("agent", "litellm", "llm"),
    ("agent", "forgejo", "git"),
    ("agent", "registry", "watch"),
    ("assistant", "litellm", "llm"),
    ("assistant", "forgejo", "git"),
]


def http_json(url, headers=None, timeout=5):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def classify(name, description):
    hay = f"{name} {description or ''}".lower()
    for keywords, archetype in ARCHETYPE_RULES:
        if any(k in hay for k in keywords):
            return archetype
    return "workshop"


class State:
    """One lock, one snapshot, one event ring. The poller writes; HTTP reads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.snapshot = {"rooms": [], "edges": [], "sources": {}, "generated_at": 0}
        self.events = deque(maxlen=300)
        self.next_event_id = 1
        # poller memory for change detection
        self.seen_shas = {}       # repo -> set of commit shas
        self.seen_issues = {}     # repo -> {number: updated_at}
        self.pr_counts = {}       # repo -> open PR count
        self.llm_spend = {}       # tenant -> last spend
        self.container_states = {}  # name -> state

    def emit(self, kind, src, dst, label):
        with self.lock:
            self.events.append({
                "id": self.next_event_id, "ts": int(time.time()),
                "kind": kind, "from": src, "to": dst, "label": label,
            })
            self.next_event_id += 1


STATE = State()


def poll_registry(sources):
    try:
        services = http_json(f"{REGISTRY_URL}/v1/services")
        sources["registry"] = "ok"
        return [s for s in services if "error" not in s]
    except (urllib.error.URLError, OSError, ValueError) as exc:
        sources["registry"] = f"unreachable: {exc}"
        return []


def poll_docker(sources):
    try:
        raw = http_json(f"{DOCKER_URL}/containers/json?all=1")
        sources["docker"] = "ok"
        out = {}
        for c in raw:
            # only this stack's containers — an unrelated container on the
            # host is not a room (compose run/exec tenants carry it too)
            project = (c.get("Labels") or {}).get("com.docker.compose.project", "")
            if project and not project.startswith("sovereign-node"):
                continue
            name = (c.get("Names") or ["/?"])[0].lstrip("/")
            out[name] = {"state": c.get("State", "unknown"), "status": c.get("Status", "")}
        return out
    except (urllib.error.URLError, OSError, ValueError) as exc:
        sources["docker"] = f"unreachable: {exc}"
        return {}


def poll_forgejo(sources):
    if not GIT_TOKEN:
        sources["forgejo"] = "no credential (FLOOR_GIT_TOKEN unset)"
        return
    headers = {"Authorization": f"token {GIT_TOKEN}"}
    errors = []
    for repo in WATCHED_REPOS:
        try:
            base = f"{FORGEJO_URL}/api/v1/repos/{repo}"
            short = repo.split("/", 1)[-1]

            commits = http_json(f"{base}/commits?limit=5&stat=false", headers)
            seen = STATE.seen_shas.setdefault(repo, set())
            first_scan = not seen
            for c in commits:
                sha = c.get("sha", "")
                if sha and sha not in seen:
                    seen.add(sha)
                    if not first_scan:
                        who = (c.get("commit", {}).get("author", {}) or {}).get("name", "someone")
                        msg = (c.get("commit", {}).get("message", "") or "").splitlines()[0][:60]
                        STATE.emit("git", "agent", "forgejo", f"{who}: {msg} → {short}")

            pulls = http_json(f"{base}/pulls?state=open&limit=20", headers)
            prev = STATE.pr_counts.get(repo)
            if prev is not None and len(pulls) > prev:
                title = pulls[0].get("title", "a proposal")[:60] if pulls else "a proposal"
                STATE.emit("pr", "forgejo", "homepage", f"new proposal: {title}")
            STATE.pr_counts[repo] = len(pulls)

            issues = http_json(f"{base}/issues?state=open&limit=10&type=issues", headers)
            known = STATE.seen_issues.setdefault(repo, {})
            first_scan = not known
            for issue in issues:
                num, updated = issue.get("number"), issue.get("updated_at", "")
                if num is not None and known.get(num) != updated:
                    if not first_scan and num in known:
                        STATE.emit("issue", "assistant", "forgejo",
                                   f"issue update: {issue.get('title', '')[:60]}")
                    elif not first_scan:
                        STATE.emit("issue", "assistant", "forgejo",
                                   f"new issue: {issue.get('title', '')[:60]}")
                    known[num] = updated
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(f"{repo}: {exc}")
    sources["forgejo"] = "; ".join(errors) if errors else "ok"


def poll_litellm(sources):
    live = 0
    for tenant, key in LLM_KEYS.items():
        if not key:
            continue
        try:
            info = http_json(f"{LITELLM_URL}/key/info",
                             {"Authorization": f"Bearer {key}"})
            spend = float((info.get("info") or {}).get("spend") or 0.0)
            prev = STATE.llm_spend.get(tenant)
            if prev is not None and spend > prev:
                room = "agent" if tenant == "agent-dev" else "assistant"
                STATE.emit("llm", room, "litellm",
                           f"{tenant} inference · +${spend - prev:.4f}")
            STATE.llm_spend[tenant] = spend
            live += 1
        except (urllib.error.URLError, OSError, ValueError):
            pass
    if not any(LLM_KEYS.values()):
        sources["litellm"] = "no credential (FLOOR_*_LLM_KEY unset)"
    else:
        sources["litellm"] = "ok" if live else "error: keys set but /key/info failed"


def build_snapshot(services, containers, sources):
    rooms, edges, known = [], [], set()

    def add_room(name, wing, archetype, size, label, blurb, ring=None, needs=None):
        if name in known:
            return
        known.add(name)
        c = containers.get(name)
        rooms.append({
            "name": name, "wing": wing, "archetype": archetype, "size": size,
            "label": label, "blurb": blurb, "ring": ring, "needs": needs or {},
            "state": (c or {}).get("state", "unknown" if containers else "unpolled"),
            "status": (c or {}).get("status", ""),
        })

    # The internet is a place on this floor: the loading dock outside the gate.
    add_room("internet", "outside", "worldgate", "medium", "Loading Dock",
             "the world outside — everything crossing here is deliberate")

    # Skeleton rooms appear even when their container is stopped or absent —
    # the agent bay's benches sit dark until a tenant session lights them up.
    for name, spec in CORE_ROOMS.items():
        add_room(name, spec["wing"], spec["archetype"], spec["size"],
                 spec["label"], spec["blurb"])

    for svc in services:
        name = svc.get("name")
        if not name or name in known:
            continue
        add_room(name, "apps", classify(name, svc.get("description")), "medium",
                 name.replace("-", " ").title(), svc.get("description") or "",
                 ring=svc.get("ring"), needs=svc.get("needs") or {})
        edges.append({"from": "caddy", "to": name, "kind": "ingress"})
        for need_key in (svc.get("needs") or {}):
            target = NEEDS_TARGETS.get(need_key)
            if target:
                edges.append({"from": name, "to": target, "kind": need_key})

    # containers nobody claimed: ephemeral tenants and -db sidecars
    for name in sorted(containers):
        if name in known:
            continue
        if name.endswith("-db"):
            parent = name[:-3]
            add_room(name, "apps", "vault", "small",
                     name.replace("-", " ").title(), "private database, one client")
            if parent in known:
                edges.append({"from": parent, "to": name, "kind": "data"})
        else:
            add_room(name, "bay", "pad", "small", name,
                     "ephemeral tenant — one container, one task, one budget")
            edges.append({"from": name, "to": "litellm", "kind": "llm"})
            edges.append({"from": name, "to": "forgejo", "kind": "git"})

    edges.extend({"from": a, "to": b, "kind": k}
                 for a, b, k in CORE_EDGES if a in known and b in known)
    # every skeleton room with a door gets an ingress edge
    for name in ("forgejo", "litellm", "homepage", "registry"):
        if name in known:
            edges.append({"from": "caddy", "to": name, "kind": "ingress"})
    edges.append({"from": "internet", "to": "caddy", "kind": "ingress"})

    # de-dup
    uniq, seen_e = [], set()
    for e in edges:
        if e["from"] not in known or e["to"] not in known:
            continue
        sig = (e["from"], e["to"], e["kind"])
        if sig not in seen_e:
            seen_e.add(sig)
            uniq.append(e)

    running = sum(1 for r in rooms if r["state"] == "running")
    return {
        "node": os.environ.get("NODE_DOMAIN") or "this node",
        "generated_at": int(time.time()),
        "rooms": rooms, "edges": uniq, "sources": sources,
        "running": running, "total": len(rooms),
    }


def poller():
    while True:
        sources = {}
        try:
            services = poll_registry(sources)
            containers = poll_docker(sources)
            # container lifecycle changes are events too
            for name, c in containers.items():
                prev = STATE.container_states.get(name)
                if prev is not None and prev != c["state"]:
                    STATE.emit("lifecycle", "docker-proxy", name,
                               f"{name}: {prev} → {c['state']}")
                STATE.container_states[name] = c["state"]
            poll_forgejo(sources)
            poll_litellm(sources)
            snap = build_snapshot(services, containers, sources)
            with STATE.lock:
                STATE.snapshot = snap
        except Exception as exc:  # the floor must never die to a poll bug
            print(f"[floor] poll error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".svg": "image/svg+xml", ".md": "text/plain", ".png": "image/png",
        ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    server_version = "sovereign-floor/0"

    def _send(self, code, body, ctype="application/json", cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode())

    def do_GET(self):  # noqa: N802 (http.server API)
        path, _, query = self.path.partition("?")
        if path == "/healthz":
            self._send(200, b"ok\n", "text/plain")
        elif path == "/v1/floor":
            with STATE.lock:
                self._json(200, STATE.snapshot)
        elif path == "/v1/activity":
            since = 0
            for part in query.split("&"):
                if part.startswith("since="):
                    try:
                        since = int(part[6:])
                    except ValueError:
                        pass
            with STATE.lock:
                events = [e for e in STATE.events if e["id"] > since]
                latest = STATE.next_event_id - 1
                sources = dict(STATE.snapshot.get("sources", {}))
                states = {r["name"]: r["state"]
                          for r in STATE.snapshot.get("rooms", [])}
            self._json(200, {"events": events[-100:], "latest_id": latest,
                             "sources": sources, "states": states})
        else:
            self._static(path)

    def _static(self, path):
        rel = path.lstrip("/") or "index.html"
        target = (SITE_DIR / rel).resolve()
        if not target.is_relative_to(SITE_DIR.resolve()) or not target.is_file():
            self._json(404, {"error": "not found"})
            return
        self._send(200, target.read_bytes(),
                   MIME.get(target.suffix, "application/octet-stream"),
                   cache=target.suffix == ".svg")

    def log_message(self, fmt, *args):
        print(f"[floor] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    threading.Thread(target=poller, daemon=True).start()
    print(f"[floor] listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
