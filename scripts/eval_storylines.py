"""Unattended storyline evaluation: seed stories, grow them with expand_frontier, render every storyline.

    uv run --project backend python scripts/eval_storylines.py run [--iterations N] [--plan plan.json] [--judge]
    uv run --project backend python scripts/eval_storylines.py judge eval-out/<stamp>

`run` starts a private backend (port 8766, CYOA_LLM=openai unless --llm says otherwise) whose
story and checkpoint databases live in the output folder, so the dev database is never touched.
For each premise in scripts/eval-premises.json it creates a story, writes the opening passage,
runs expand_frontier with the skip_review_interrupt policy under a pacing plan (--plan, a JSON
PacingPlan; default: the backend's default plan), then writes:

    eval-out/<stamp>/<n>-<slug>.md     every root-to-leaf storyline as continuous prose, plus stats
    eval-out/<stamp>/<n>-<slug>.json   the same paths as data (what `judge` reads)
    eval-out/<stamp>/index.md          one row per story
    eval-out/<stamp>/backend.log       the private backend's output

`--judge` (or the `judge` subcommand afterwards) asks the model at CYOA_OPENAI_URL to flag
contradictions in each storyline and appends its verdicts to the story's report. See
wiki/cyoa-wiki/runbooks/storyline-eval.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
PREMISES = ROOT / "scripts" / "eval-premises.json"
OPENAI_URL = os.environ.get("CYOA_OPENAI_URL", "http://localhost:8080/v1")


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


# --- private backend -------------------------------------------------------------


class Backend:
    """A cyoa backend in a subprocess, on its own port and databases; stopped on exit."""

    def __init__(self, out: Path, port: int, llm: str, concurrency: int | None = None):
        self.base = f"http://127.0.0.1:{port}/api"
        env = {**os.environ, "CYOA_LLM": llm, "CYOA_RELOAD": "0", "CYOA_PORT": str(port),
               "CYOA_DB": str(out / "cyoa.db"), "CYOA_AGENT_DB": str(out / "cyoa-agent.db")}
        if concurrency is not None:
            env["CYOA_AGENT_CONCURRENCY"] = str(concurrency)
        self.logf = open(out / "backend.log", "w")
        self.proc = subprocess.Popen([sys.executable, "-c", "from cyoa.main import run; run()"],
                                     env=env, cwd=out, stdout=self.logf, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        self.http = httpx.Client(base_url=self.base, timeout=30)

    def wait_ready(self, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise SystemExit(f"backend exited with {self.proc.returncode}; see {self.logf.name}")
            try:
                if self.http.get("/health").json().get("ok"):
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        raise SystemExit(f"backend not ready after {timeout}s; see {self.logf.name}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.logf.close()

    def call(self, method: str, path: str, **kw: Any) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text}")
        return r.json() if r.content else None


def check_model_server() -> str:
    try:
        ids = [m["id"] for m in httpx.get(f"{OPENAI_URL}/models", timeout=5).json()["data"]]
    except Exception as e:
        raise SystemExit(f"model server not reachable at {OPENAI_URL}: {e}")
    return ", ".join(ids) or "(no models listed)"


# --- one story -------------------------------------------------------------------


def seed(be: Backend, premise: dict[str, Any]) -> tuple[int, int]:
    story = be.call("POST", "/stories", json={"title": premise["title"], "description": premise["description"]})
    sid, start = story["id"], story["start_passage_id"]
    be.call("PATCH", f"/stories/{sid}/passages/{start}",
            json={"title": premise["start_title"], "body": premise["start_body"]})
    return sid, start


def expand(be: Backend, sid: int, start: int, budget: dict[str, Any], steer: str | None, policy: dict[str, Any]) -> dict[str, Any]:
    run = be.call("POST", f"/stories/{sid}/runs", json={
        "workflow": "expand_frontier", "scope": [start], "budget": budget, "steer": steer,
        "policy": {"skip_review_interrupt": True, **policy}})
    deadline = time.time() + budget["max_seconds"] + 120
    last = None
    while True:
        run = be.call("GET", f"/stories/{sid}/runs/{run['id']}")
        if run["progress"] != last:
            last = run["progress"]
            log(f"  run {run['id']}: {run['status']} {last}")
        if run["status"] == "paused":  # not expected under skip_review_interrupt; keep going anyway
            be.call("POST", f"/stories/{sid}/runs/{run['id']}/resume", json={})
        elif run["status"] != "running":
            return run
        if time.time() > deadline:
            log("  timed out; cancelling and reporting what exists")
            return be.call("POST", f"/stories/{sid}/runs/{run['id']}/cancel")
        time.sleep(2)


def retry_reasons(be: Backend, sid: int, run_id: int) -> dict[str, int]:
    """Count the run's retry events by phase and reason. The event log lives only in the
    backend process, so it is read here, before the private backend stops."""
    counts: dict[str, int] = {}
    r = be.http.get(f"/stories/{sid}/runs/{run_id}/events", timeout=30)
    for line in r.text.splitlines():
        if line.startswith("data: "):
            ev = json.loads(line[6:])
            if ev.get("type") == "retry":
                key = f"{ev.get('phase')}: {ev.get('reason')}"
                counts[key] = counts.get(key, 0) + 1
    return counts


def audit_stats(db_path: Path, story_id: int) -> dict[str, Any]:
    """Refinement telemetry for one story, read straight from the private backend's
    sqlite file (a real file at <out>/cyoa.db, unlike the transient retry event log)."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM audits WHERE story_id = ?", (story_id,))]
    conn.close()
    if not rows:
        return {}
    by_auditor: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_auditor.setdefault(r["auditor"], []).append(r)
    top_issues: dict[str, int] = {}
    for r in rows:
        for i in json.loads(r["issues"]):
            key = f"{r['auditor']}: {i['problem'][:60]}"
            top_issues[key] = top_issues.get(key, 0) + 1
    return {
        "first_pass_rate": {a: round(sum(1 for r in rs if r["round"] == 1 and r["verdict"] == "pass")
                                     / max(1, sum(1 for r in rs if r["round"] == 1)), 2)
                           for a, rs in by_auditor.items()},
        "rounds_per_step": max((r["round"] for r in rows), default=0),
        "dropped": sum(r["dropped"] for r in rows),
        "top_issues": sorted(top_issues.items(), key=lambda kv: -kv[1])[:10],
    }


def storylines(graph: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Every root-to-leaf path from the start passage as [{passage, choice-taken-out-of-it}] steps.
    A path stops at a passage with no targeted choices, or before revisiting one (cycle)."""
    passages = {p["id"]: p for p in graph["passages"]}
    out: dict[int, list[dict[str, Any]]] = {}
    for c in graph["choices"]:
        if c["to_passage"] is not None:
            out.setdefault(c["from_passage"], []).append(c)
    paths: list[list[dict[str, Any]]] = []

    def walk(pid: int, trail: list[dict[str, Any]], seen: set[int]) -> None:
        nxt = [c for c in out.get(pid, []) if c["to_passage"] not in seen]
        if not nxt:
            paths.append(trail + [{"passage": passages[pid], "choice": None}])
            return
        for c in nxt:
            walk(c["to_passage"], trail + [{"passage": passages[pid], "choice": c["text"]}], seen | {c["to_passage"]})

    start = graph["story"]["start_passage_id"]
    if start in passages:
        walk(start, [], {start})
    return paths


def render_path(i: int, path: list[dict[str, Any]]) -> str:
    lines = [f"## Storyline {i}", "", "_" + " → ".join(s["passage"]["title"] for s in path) + "_", ""]
    for s in path:
        p = s["passage"]
        lines += [f"### {p['title']}", "", p["body"].strip() or "_(empty body)_", ""]
        if s["choice"]:
            lines += [f"> **▶ {s['choice']}**", ""]
        elif p.get("is_ending"):
            lines += ["**— THE END —**", ""]
        else:
            lines += ["_(unfinished: no choices and not an ending)_", ""]
    return "\n".join(lines)


def write_report(out: Path, stem: str, premise: dict[str, Any], run: dict[str, Any],
                 graph: dict[str, Any], problems: dict[str, Any], paths: list[list[dict[str, Any]]],
                 retries: dict[str, int], audits: dict[str, Any]) -> dict[str, Any]:
    agent = [p for p in graph["passages"] if p["provenance"] == "agent"]
    depths = [len(p) for p in paths]
    unresolved = sum(1 for p in graph["passages"] if (p.get("meta") or {}).get("audit", {}).get("verdict") == "unresolved")
    stats = {"passages": len(graph["passages"]), "agent_passages": len(agent), "choices": len(graph["choices"]),
             "storylines": len(paths),
             "endings": len(problems.get("endings", [])), "unfinished": len(problems.get("unfinished", [])), "min_len": min(depths, default=0), "max_len": max(depths, default=0),
             # bodies with under 40 characters of text once ellipses are stripped: the model's "..." placeholders
             "stub_bodies": sum(1 for p in graph["passages"] if len(re.sub(r"[.…\s]", "", p["body"])) < 40),
             "stub_choices": sum(1 for c in graph["choices"] if len(re.sub(r"[.…\s]", "", c["text"])) < 3),
             "retries": sum(retries.values()), "unresolved": unresolved, "dropped": audits.get("dropped", 0),
             "run_status": run["status"], "run_error": run["error"], "progress": run["progress"]}
    md = [f"# {premise['title']}", "", f"**Premise:** {premise['description']}", ""]
    if premise.get("steer"):
        md += [f"**Steer:** {premise['steer']}", ""]
    md += ["| stat | value |", "|---|---|"] + [f"| {k} | {v} |" for k, v in stats.items()] + [""]
    md += ["**Engine problems:** " + json.dumps({k: v for k, v in problems.items() if v}), ""]
    if retries:
        md += ["**Retries by reason:**", ""] + [f"- {n}× {k}" for k, n in sorted(retries.items(), key=lambda kv: -kv[1])] + [""]
    if audits:
        md += ["**Audit stats:**", "", f"- rounds per step (max): {audits['rounds_per_step']}",
              f"- dropped (unverifiable) issues: {audits['dropped']}",
              f"- first-pass rate: {', '.join(f'{a}={r}' for a, r in audits['first_pass_rate'].items())}"]
        if audits["top_issues"]:
            md += ["- top issues:"] + [f"  - {n}× {k}" for k, n in audits["top_issues"]]
        md += [""]
    md += [render_path(i, p) for i, p in enumerate(paths, 1)]
    (out / f"{stem}.md").write_text("\n".join(md))
    data = {"premise": premise, "stats": stats, "retries": retries,
            "paths": [[{"title": s["passage"]["title"], "body": s["passage"]["body"], "choice": s["choice"]} for s in p]
                      for p in paths]}
    (out / f"{stem}.json").write_text(json.dumps(data, indent=2))
    return stats


# --- judge -----------------------------------------------------------------------

JUDGE_PROMPT = """You are checking one storyline from a choose-your-own-adventure book for internal consistency.
Read it as a single continuous story: each passage follows from the choice printed before it.

Flag only real problems: contradictions (a fact, name, object, injury, location or relationship that
changes without cause), passages that ignore the choice that led to them, references to things never
introduced, and changes of person or tense. Do not critique style or suggest improvements.

Answer with JSON only: {{"consistent": true|false, "readability": 1-5, "issues": [{{"passage": "<title>", "problem": "<one sentence>"}}]}}

STORY PREMISE: {premise}

STORYLINE:
{text}"""


def judge_path(http: httpx.Client, premise: str, path: list[dict[str, Any]]) -> dict[str, Any]:
    text = "\n\n".join(f"[{s['title']}]\n{s['body']}" + (f"\n(Choice taken: {s['choice']})" if s["choice"] else "")
                       for s in path)
    r = http.post(f"{OPENAI_URL}/chat/completions", json={
        "model": os.environ.get("CYOA_OPENAI_MODEL", "default"), "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": JUDGE_PROMPT.format(premise=premise, text=text)}]})
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content[content.index("{"): content.rindex("}") + 1])
    except ValueError:
        return {"consistent": None, "readability": None, "issues": [{"passage": "-", "problem": f"unparseable: {content[:200]}"}]}


def judge_dir(out: Path) -> None:
    http = httpx.Client(timeout=600)
    for f in sorted(out.glob("*.json")):
        data = json.loads(f.read_text())
        log(f"judging {f.stem}: {len(data['paths'])} storylines")
        md = ["", "---", "", "## Judge verdicts (model-graded; a signal, not proof)", ""]
        for i, path in enumerate(data["paths"], 1):
            v = judge_path(http, data["premise"]["description"], path)
            md.append(f"- **Storyline {i}:** consistent={v.get('consistent')} readability={v.get('readability')}")
            md += [f"  - _{x.get('passage')}_: {x.get('problem')}" for x in v.get("issues") or []]
            log(f"  storyline {i}: consistent={v.get('consistent')} issues={len(v.get('issues') or [])}")
        report = out / f"{f.stem}.md"
        text = report.read_text().split("\n---\n\n## Judge verdicts")[0]
        report.write_text(text + "\n".join(md) + "\n")


# --- commands --------------------------------------------------------------------


def cmd_run(a: argparse.Namespace) -> None:
    premises = json.loads(PREMISES.read_text())[: a.iterations]
    out = Path(a.out or ROOT / "eval-out" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if a.llm == "openai":
        log(f"model server {OPENAI_URL}: {check_model_server()}")
    policy = json.loads(a.policy)
    refining = policy.get("refine", {}).get("rounds", 0) > 0
    # Refinement multiplies call count per step (refinement-loops.md's "Time" section);
    # give a refined run more room unless the caller set --max-seconds explicitly.
    max_seconds = a.max_seconds if a.max_seconds is not None else (7200 * 5 if refining else 7200)
    # The run's budget is a pacing plan (concepts/act-pacing.md): the file given, or the
    # backend's default, read from the first story once it exists.
    plan = json.loads(Path(a.plan).read_text()) if a.plan else None
    budget: dict[str, Any] = {}
    log(f"output {out}; plan {a.plan or '(default)'}; policy {policy}; concurrency {a.concurrency or '(default)'}; "
        f"{len(premises)} premises x {a.repeat} repeat(s)")
    be = Backend(out, a.port, a.llm, a.concurrency)
    rows = []
    try:
        be.wait_ready()
        for n, premise in enumerate(premises, 1):
            for k in range(1, a.repeat + 1):
                stem = f"{n}-{slug(premise['title'])}" + (f"-{k}" if a.repeat > 1 else "")
                log(f"[{n}/{len(premises)}{f' rep {k}/{a.repeat}' if a.repeat > 1 else ''}] {premise['title']}")
                t0 = time.time()
                sid, start = seed(be, premise)
                if not budget:
                    budget = {**(plan or be.call("GET", f"/stories/{sid}/pacing")), "max_seconds": max_seconds}
                    log(f"  plan: {json.dumps(budget)}")
                run = expand(be, sid, start, budget, premise.get("steer"), policy)
                retries = retry_reasons(be, sid, run["id"])
                audits = audit_stats(out / "cyoa.db", sid)
                graph = be.call("GET", f"/stories/{sid}/graph")
                problems = be.call("GET", f"/stories/{sid}/problems")
                paths = storylines(graph)
                stats = write_report(out, stem, premise, run, graph, problems, paths, retries, audits)
                stats["seconds"] = round(time.time() - t0)
                rows.append((stem, stats))
                log(f"  {stats['run_status']}: {stats['agent_passages']} passages, {stats['storylines']} storylines, "
                    f"{stats['seconds']}s" + (f", error: {stats['run_error']}" if stats["run_error"] else ""))
    finally:
        be.stop()
    idx = ["# Storyline eval", "", f"Plan: `{json.dumps(budget)}`, policy: `{json.dumps(policy)}`, llm: `{a.llm}`", "",
           "| story | status | passages | storylines | endings | unfinished | length | stub bodies | stub choices | retries | unresolved | dropped | seconds | error |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    idx += [f"| [{stem}]({stem}.md) | {s['run_status']} | {s['agent_passages']} | {s['storylines']} | {s['endings']} | {s['unfinished']} | "
            f"{s['min_len']}–{s['max_len']} | {s['stub_bodies']} | {s['stub_choices']} | {s['retries']} | {s['unresolved']} | {s['dropped']} | "
            f"{s['seconds']} | {s['run_error'] or ''} |" for stem, s in rows]
    (out / "index.md").write_text("\n".join(idx) + "\n")
    if a.judge:
        judge_dir(out)
    log(f"done: {out / 'index.md'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="seed, expand and report each premise")
    r.add_argument("--iterations", type=int, default=3, help="how many premises from eval-premises.json")
    r.add_argument("--plan", help="a pacing plan as JSON (models.PacingPlan: acts, ending); default: the backend's")
    r.add_argument("--max-seconds", type=int, default=None, help="per run, a guard not a target "
                    "(default: 7200, or 36000 when policy.refine.rounds > 0)")
    r.add_argument("--policy", default="{}", help="JSON merged into the run's policy, e.g. "
                    '\'{"refine": {"rounds": 2}}\' (skip_review_interrupt is always on)')
    r.add_argument("--repeat", type=int, default=1, help="run every premise this many times (for comparing arms)")
    r.add_argument("--concurrency", type=int, default=None, help="CYOA_AGENT_CONCURRENCY for the private backend "
                    "(default: whatever the environment already has, normally 1); raise to match a load-balancing "
                    "proxy's pool size so generation steps actually fan out concurrently instead of just being permitted to")
    r.add_argument("--llm", default="openai", choices=["openai", "ollama"])
    r.add_argument("--port", type=int, default=8766)
    r.add_argument("--out", help="output folder (default eval-out/<stamp>)")
    r.add_argument("--judge", action="store_true", help="also model-grade each storyline")
    j = sub.add_parser("judge", help="model-grade the storylines in an existing output folder")
    j.add_argument("out", type=Path)
    a = ap.parse_args()
    if a.cmd == "run":
        cmd_run(a)
    else:
        judge_dir(a.out.resolve())


if __name__ == "__main__":
    main()
