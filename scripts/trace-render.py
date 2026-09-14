#!/usr/bin/env python3
"""Render an agent run trace (AGENT_TRACE=1) as a wall-clock timeline.

    ./scripts/trace-render.py traces/<run>      # writes trace.json + trace.html, prints a summary

Joins these sources on the run name (= LiteLLM key alias = container name):
  - LiteLLM spend logs, session key:<run>: every model request — start, first
    token, end, tokens, cost, and the tool calls it returned.
  - tools.jsonl from agent/trace-sh.py: every shell command forge ran, with
    measured wall time, exit status, CPU, peak RSS and block IO.
  - events.jsonl (entrypoint phases), state.json (container start/finish) and
    forge.db (tool results, for output sizes) — copied out by run-task.sh.

Tool calls without a measurement — forge's built-ins (read, patch, fs_search,
...) always, shell calls when the run was not traced — span the gap before the
next model request, marked inferred. A directory holding only run.json
({"run": "<run name>"}) therefore renders any past run from spend logs alone. Host-side and ring 0: it reads the LiteLLM database through docker
exec, and the output holds commands and tool arguments — keep it operator-only.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

VIEWER = Path(__file__).resolve().parent / "trace-viewer.html"

LANES = [
    ("setup", "container setup"),
    ("llm", "model requests"),
    ("llm-side", "model side requests"),
    ("shell", "shell tools"),
    ("builtin", "unmeasured tools"),
    ("harness", "harness shell"),
]
# Overlapping spans charge wall-clock to the lane listed first, so the
# breakdown sums to the run's wall time exactly once.
CHARGE_ORDER = ["llm", "llm-side", "shell", "builtin", "setup", "harness"]
CATEGORY = {
    "llm": "model wait", "llm-side": "model wait", "shell": "shell tools",
    "builtin": "unmeasured tools (inferred)", "setup": "container setup",
    "harness": "harness shell", None: "harness / unaccounted",
}
CATEGORY_ORDER = ["model wait", "shell tools", "unmeasured tools (inferred)",
                  "container setup", "harness shell", "harness / unaccounted"]
# Setup spans end at an entrypoint mark; each is named for what led up to it.
SETUP_LABEL = {"entrypoint_start": "container start",
               "workspace_ready": "workspace clone + setup",
               "harness_exec": "harness environment"}
CMD_MAX = 4096  # trace-sh truncates recorded commands to this

SQL = r"""
select coalesce(json_agg(r order by r.start_us), '[]') from (
  select request_id,
         (extract(epoch from "startTime") * 1e6)::bigint           as start_us,
         (extract(epoch from "completionStartTime") * 1e6)::bigint as first_token_us,
         (extract(epoch from "endTime") * 1e6)::bigint             as end_us,
         coalesce(nullif(model_group, ''), model)                  as model,
         prompt_tokens, completion_tokens, spend, status,
         coalesce(proxy_server_request ? 'tools', false)           as has_tools,
         (select json_agg(json_build_object(
                    'name', tc->'function'->>'name',
                    'args', left(tc->'function'->>'arguments', 4096)))
            from jsonb_array_elements(
                   case when jsonb_typeof(response->'choices'->0->'message'->'tool_calls') = 'array'
                        then response->'choices'->0->'message'->'tool_calls'
                        else '[]'::jsonb end) tc)                  as tool_calls
    from "LiteLLM_SpendLogs"
   where session_id = :'sid'
) r;
"""


def rfc3339_us(s):
    """Docker's nanosecond RFC 3339 timestamp -> epoch microseconds (None for unset)."""
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", s or "")
    if not m or m[1].startswith("0001-"):
        return None
    tz = "+00:00" if m[3] == "Z" else m[3]
    return int(datetime.fromisoformat(m[1] + tz).timestamp()) * 1_000_000 + int((m[2] or "0")[:6].ljust(6, "0"))


def read_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def read_jsonl(path):
    out = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # a torn last line from a killed run
    return out


def args_key(name, args):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            pass
    return name, json.dumps(args, sort_keys=True, separators=(",", ":"))


def first_line(cmd, n=90):
    lines = (cmd or "").strip().splitlines()
    return (lines[0][:n] + ("…" if len(lines) > 1 or len(lines[0]) > n else "")) if lines else "(empty)"


def fetch_model_requests(run, container):
    cmd = ["docker", "exec", "-i", container, "psql", "-U", "litellm", "-d", "litellm",
           "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-v", f"sid=key:{run}"]
    try:
        p = subprocess.run(cmd, input=SQL, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], f"could not query LiteLLM spend logs: {e}"
    if p.returncode != 0:
        tail = (p.stderr.strip().splitlines() or ["unknown error"])[-1]
        return [], f"could not query LiteLLM spend logs ({container}): {tail}"
    return json.loads(p.stdout.strip() or "[]"), None


def forge_tool_results(db):
    """(tool, args) -> [{result_chars, is_error}] in call order, across sub-agent conversations."""
    results = {}
    if not db.exists():
        return results, None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("select context from conversations order by created_at").fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        return results, f"could not read {db.name}: {e}"
    for (ctx,) in rows:
        calls = {}
        for m in json.loads(ctx or "{}").get("messages", []):
            msg = m.get("message", {})
            for tc in (msg.get("text") or {}).get("tool_calls") or []:
                calls[tc.get("call_id")] = (tc.get("name"), tc.get("arguments"))
            tool = msg.get("tool")
            if tool:
                name, args = calls.get(tool.get("call_id"), (tool.get("name"), None))
                out = tool.get("output") or {}
                size = sum(len(v.get("text", "")) for v in out.get("values") or [] if isinstance(v, dict))
                results.setdefault(args_key(name, args), []).append(
                    {"result_chars": size, "is_error": bool(out.get("is_error"))})
    return results, None


def charge(spans, t0, t1):
    """Split [t0, t1] into category totals (us), charging overlaps by CHARGE_ORDER."""
    rank = {lane: i for i, lane in enumerate(CHARGE_ORDER)}
    edges = sorted([(s["start"], 1, s["lane"]) for s in spans] + [(s["end"], -1, s["lane"]) for s in spans])
    active = dict.fromkeys(CHARGE_ORDER, 0)
    totals, prev = {}, t0
    for t, delta, lane in edges + [(t1, 0, None)]:
        if t > prev:
            top = min((l for l, n in active.items() if n > 0), key=rank.get, default=None)
            totals[CATEGORY[top]] = totals.get(CATEGORY[top], 0) + (t - prev)
            prev = t
        if lane:
            active[lane] += delta
    return totals


def build(trace_dir, container):
    run_meta = read_json(trace_dir / "run.json", {})
    run = run_meta.get("run") or trace_dir.name
    state = read_json(trace_dir / "state.json", {})
    started, finished = rfc3339_us(state.get("StartedAt")), rfc3339_us(state.get("FinishedAt"))
    warnings, spans = [], []

    def add(lane, start, end, label, **detail):
        spans.append({"lane": lane, "start": start, "end": max(end, start), "label": label, "detail": detail})
        return spans[-1]

    # Container setup: docker start, then each entrypoint phase.
    marks = [(started, "container_start")] if started else []
    marks += sorted((e["t_ns"] // 1000, e["event"]) for e in read_jsonl(trace_dir / "events.jsonl") if "t_ns" in e)
    for (a, _), (b, event) in zip(marks, marks[1:]):
        add("setup", a, b, SETUP_LABEL.get(event, event))

    # Model requests. Forge also sends a tool-less side request per run.
    reqs, err = fetch_model_requests(run, container)
    if err:
        warnings.append(err)
    elif not reqs:
        warnings.append(f"No LiteLLM spend-log rows for session key:{run} yet. LiteLLM writes "
                        "spend logs in batches — re-render in a minute.")
    for r in reqs:
        s = add("llm" if r.get("has_tools") else "llm-side", r["start_us"], r["end_us"], r.get("model") or "model",
                tokens_in=r.get("prompt_tokens"), tokens_out=r.get("completion_tokens"),
                cost_usd=r.get("spend"), status=r.get("status"),
                tool_calls=[tc["name"] for tc in r.get("tool_calls") or []])
        ft = r.get("first_token_us")
        if ft and r["start_us"] <= ft <= r["end_us"]:
            s["first_token"] = ft
            s["detail"]["first_token_ms"] = (ft - r["start_us"]) / 1000

    results, err = forge_tool_results(trace_dir / "forge.db")
    if err:
        warnings.append(err)

    def take_result(name, args):
        lst = results.get(args_key(name, args))
        return lst.pop(0) if lst else None

    # Each tool-calling response owns the gap until the next model request.
    # Calls start unmeasured; shell calls are matched to trace-sh records
    # below, and whatever stays unmeasured is drawn inferred across the gap.
    main = [r for r in reqs if r.get("has_tools")]
    run_end = finished or max((s["end"] for s in spans), default=0)
    gaps, shell_calls = [], []
    for i, r in enumerate(main):
        if not r.get("tool_calls"):
            continue
        lo = r["end_us"]
        hi = main[i + 1]["start_us"] if i + 1 < len(main) else max(run_end, lo)
        gap = {"lo": lo, "hi": hi, "calls": []}
        for tc in r["tool_calls"]:
            call = {"tool": tc["name"], "args": (tc.get("args") or "")[:400],
                    "result": take_result(tc["name"], tc.get("args")), "measured": False}
            if tc["name"] == "shell":
                try:
                    call["cmd"] = json.loads(tc.get("args") or "{}").get("command")
                except (json.JSONDecodeError, AttributeError):
                    call["cmd"] = None  # arguments longer than the query's 4096-char cut
                call.update(lo=lo, hi=hi)
                shell_calls.append(call)
            gap["calls"].append(call)
        gaps.append(gap)

    # Shell records: match to the model's shell calls, else it is forge's own
    # housekeeping (git probes and the like).
    fallback = {}  # command -> forge-store key; only used when there is no model lane
    if not main:
        for key in results:
            args = json.loads(key[1]) if key[0] == "shell" else None
            if isinstance(args, dict):
                fallback.setdefault((args.get("command") or "")[:CMD_MAX], key)
    used = set()
    for rec in read_jsonl(trace_dir / "tools.jsonl"):
        start = rec["start_ns"] // 1000
        end = start + int(rec["wall_ms"] * 1000)
        cmd = rec.get("cmd", "")
        detail = {k: rec[k] for k in ("exit", "cpu_user_ms", "cpu_sys_ms", "max_rss_kb", "inblock", "oublock", "pid")
                  if k in rec}
        detail["command"] = cmd + ("…" if rec.get("cmd_len", 0) > len(cmd) else "")
        match = next((j for j, c in enumerate(shell_calls)
                      if j not in used and c["cmd"] is not None and c["cmd"][:CMD_MAX] == cmd
                      and c["lo"] - 2_000_000 <= start <= c["hi"] + 2_000_000), None)
        if match is not None:
            used.add(match)
            shell_calls[match]["measured"] = True
            detail.update(shell_calls[match]["result"] or {})
            add("shell", start, end, first_line(cmd), **detail)
        elif cmd in fallback:
            pending = results[fallback[cmd]]
            detail.update(pending.pop(0) if pending else {})
            add("shell", start, end, first_line(cmd), **detail)
        else:
            add("harness", start, end, first_line(cmd), **detail)

    for gap in gaps:
        todo = [c for c in gap["calls"] if not c["measured"]]
        if todo:
            add("builtin", gap["lo"], gap["hi"], ", ".join(c["tool"] for c in todo), inferred=True,
                calls=[{"tool": c["tool"], "args": c["args"], **(c["result"] or {})} for c in todo])

    starts =[s["start"] for s in spans] + ([started] if started else [])
    ends = [s["end"] for s in spans] + ([finished] if finished else [])
    if not starts:
        sys.exit(f"trace-render: nothing to render in {trace_dir} (no state.json, events, tools or model rows)")
    t0, t1 = min(starts), max(ends)
    if not run_meta.get("model"):  # a past run rendered from spend logs alone
        models = [r["model"] for r in reqs if r.get("model")]
        if models:
            run_meta["model"] = max(set(models), key=models.count)
    wall = max(t1 - t0, 1)

    spans.sort(key=lambda s: (s["start"], CHARGE_ORDER.index(s["lane"])))
    rel = lambda us: round((us - t0) / 1000, 1)
    out_spans = []
    for s in spans:
        o = {"lane": s["lane"], "start_ms": rel(s["start"]), "end_ms": rel(s["end"]), "label": s["label"],
             "detail": {k: v for k, v in s["detail"].items() if v not in (None, [], "")}}
        if "first_token" in s:
            o["first_token_ms"] = rel(s["first_token"])
        out_spans.append(o)

    totals = charge(spans, t0, t1)
    tools = [i for i, s in enumerate(out_spans) if s["lane"] in ("shell", "builtin")]
    llm = [s for s in out_spans if s["lane"] in ("llm", "llm-side")]
    ttfts = sorted(s["detail"]["first_token_ms"] for s in llm if "first_token_ms" in s["detail"])
    durs = sorted(s["end_ms"] - s["start_ms"] for s in llm)
    median = lambda xs: xs[len(xs) // 2] if xs else None
    return {
        "run": run,
        "meta": {**run_meta, "started_at": state.get("StartedAt"), "finished_at": state.get("FinishedAt"),
                 "exit_code": state.get("ExitCode")},
        "wall_ms": wall / 1000,
        "lanes": [{"id": l, "name": n} for l, n in LANES if any(s["lane"] == l for s in spans)],
        "spans": out_spans,
        "summary": {
            "categories": [{"name": c, "ms": totals[c] / 1000, "pct": 100 * totals[c] / wall}
                           for c in CATEGORY_ORDER if totals.get(c)],
            "slowest": sorted(tools, key=lambda i: out_spans[i]["start_ms"] - out_spans[i]["end_ms"])[:15],
            "model": {"requests": len(llm), "side_requests": sum(s["lane"] == "llm-side" for s in llm),
                      "first_token_p50_ms": median(ttfts), "duration_p50_ms": median(durs),
                      "tokens_in": sum(s["detail"].get("tokens_in") or 0 for s in llm),
                      "tokens_out": sum(s["detail"].get("tokens_out") or 0 for s in llm),
                      "cost_usd": sum(s["detail"].get("cost_usd") or 0 for s in llm)},
        },
        "warnings": warnings,
    }


def fmt(ms):
    if ms is None:
        return "–"
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{int(ms // 60_000)}m{int(ms / 1000 % 60):02d}s"


def main():
    ap = argparse.ArgumentParser(description="Render an AGENT_TRACE=1 run as a wall-clock timeline.")
    ap.add_argument("trace_dir", type=Path, help="traces/<run> as written by run-task.sh")
    ap.add_argument("--litellm-db-container", default=os.environ.get("LITELLM_DB_CONTAINER", "litellm-db"))
    a = ap.parse_args()
    if not a.trace_dir.is_dir():
        sys.exit(f"trace-render: no such directory: {a.trace_dir}")

    data = build(a.trace_dir, a.litellm_db_container)
    (a.trace_dir / "trace.json").write_text(json.dumps(data, indent=1))
    page = VIEWER.read_text().replace("__TRACE_DATA__", json.dumps(data).replace("</", "<\\/"))
    html_path = a.trace_dir / "trace.html"
    html_path.write_text(page)

    m, spans = data["summary"]["model"], data["spans"]
    print(f"{data['run']}  {data['meta'].get('harness', '?')} / {data['meta'].get('model', '?')}  "
          f"wall {fmt(data['wall_ms'])}")
    for c in data["summary"]["categories"]:
        print(f"  {c['name']:<28} {fmt(c['ms']):>8}  {c['pct']:5.1f}%")
    print(f"model: {m['requests']} requests ({m['side_requests']} side), first token p50 "
          f"{fmt(m['first_token_p50_ms'])}, duration p50 {fmt(m['duration_p50_ms'])}, ${m['cost_usd']:.4f}")
    if data["summary"]["slowest"]:
        print("slowest tool calls:")
        for i in data["summary"]["slowest"][:8]:
            s = spans[i]
            kind = "shell" if s["lane"] == "shell" else "inferred"
            print(f"  {fmt(s['end_ms'] - s['start_ms']):>8}  {kind:<9}  {s['label']}")
    for w in data["warnings"]:
        print(f"warning: {w}")
    print(f"wrote {html_path}")


if __name__ == "__main__":
    main()
