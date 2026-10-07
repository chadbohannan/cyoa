"""Short real-model check of the passage-to-passage handoff in expand_frontier.

Copies the story DB to a scratch file (your data is never written), runs one small
expand_frontier (one act of two choices, then the ending level; refine off) from a story's
start with either the handoff prompts (`new`) or the previous frontier_passage / frontier_ending
texts (`legacy`), and prints every parent-ending -> child-opening transition plus crude
flow numbers: how much of the parent's last lines the child re-tells, and how alike siblings are.

    cd backend && CYOA_LLM=openai uv run python ../scripts/flow_probe.py --variant new|legacy --story 2 --out /tmp/x.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

LEGACY = {
    "frontier_passage": (
        "The reader took the choice '{choice}' from PASSAGE and it has no destination yet. Write the destination passage: "
        "a title and body that follow directly from that choice. Write about {length}. This storyline has at most {remaining} passages left, counting this "
        "one, and this part of the story has {act_remaining}: pace it toward the next turn as those numbers shrink. "
        "Its direction: {act_guidance} If you are given an ends_story field, set it only when this passage is a "
        "natural conclusion of what the reader chose, a good end or a bad one.",
        ("choice", "remaining", "act_remaining", "length", "act_guidance")),
    "frontier_ending": (
        "The reader took the choice '{choice}' from PASSAGE, and the passage it leads to is the last in this storyline. "
        "Write it: a title and body of about {length} that follow directly from that choice and bring the story to a "
        "conclusion consistent with everything the reader has seen. {act_guidance} Resolve or clearly close the "
        "central question; do not end on a cliffhanger or offer further choices.",
        ("choice", "length", "act_guidance")),
}
STOP = set("the a an and or but of to in on at for with from by as is was were be been it its it's this that these those "
           "you your yours he she they them his her their there then than so if not no into out up down over under again".split())


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z']+", text.lower()) if w not in STOP]


def grams(text: str, n: int = 3) -> set[tuple[str, ...]]:
    w = re.findall(r"[a-z']+", text.lower())
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--variant", choices=["new", "legacy"], required=True)
    ap.add_argument("--story", type=int, default=2)
    ap.add_argument("--db", default="cyoa.db")
    ap.add_argument("--acts", type=int, default=1, help="acts of two choices before the ending level")
    ap.add_argument("--paths", help="write each new storyline (start to leaf, full text) to this file")
    ap.add_argument("--rounds", type=int, default=0, help="refine rounds (0 = off)")
    ap.add_argument("--out")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="flowprobe-"))
    shutil.copy(args.db, tmp / "cyoa.db")
    os.environ["CYOA_DB"] = str(tmp / "cyoa.db")
    os.environ["CYOA_AGENT_DB"] = str(tmp / "agent.db")
    from cyoa import agents, db, prompts, runs  # after the env is set

    if args.variant == "legacy":
        for key, (text, ph) in LEGACY.items():
            prompts.PROMPTS[key] = dataclasses.replace(prompts.PROMPTS[key], default=text, placeholders=ph)
    with db.transaction() as conn:
        start = conn.execute("SELECT start_passage_id FROM stories WHERE id = ?", (args.story,)).fetchone()[0]
        before = {r[0] for r in conn.execute("SELECT id FROM passages WHERE story_id = ?", (args.story,))}
    acts = [{**agents.DEFAULT_PLAN["acts"][min(i, 2)], "branching": 2, "ends_story": False} for i in range(args.acts)]
    budget = {"acts": acts, "ending": agents.DEFAULT_PLAN["ending"], "max_seconds": 900}
    t = time.time()
    run = runs.manager.start(args.story, "expand_frontier", [start], {}, budget,
                             {"refine": {"rounds": args.rounds}, "skip_review_interrupt": True})
    while runs.manager.get(run["id"])["status"] == "running":
        time.sleep(1)
    run = runs.manager.get(run["id"])
    with db.transaction() as conn:
        ps = {r["id"]: dict(r) for r in conn.execute("SELECT id, title, body, is_ending FROM passages WHERE story_id = ?", (args.story,))}
        ch = [dict(r) for r in conn.execute("SELECT from_passage, to_passage, text FROM choices WHERE story_id = ?", (args.story,))]
    new = [c for c in ch if c["to_passage"] not in before and c["to_passage"] in ps]
    rows = []
    for c in new:
        par, kid = ps[c["from_passage"]], ps[c["to_passage"]]
        tail = " ".join(par["body"].split())[-300:]
        head = " ".join(kid["body"].split())[:300]
        rows.append({"parent": par["title"], "choice": c["text"], "child": kid["title"], "ending": bool(kid["is_ending"]),
                     "parent_tail": tail, "child_head": head,
                     # how much of the parent's closing the child's opening re-tells (content words, 3-grams)
                     "retell_words": round(len(set(words(head)) & set(words(tail))) / max(1, len(set(words(head)))), 2),
                     "retell_3g": round(len(grams(head) & grams(par["body"])) / max(1, len(grams(head))), 2),
                     "child_words": len(kid["body"].split())})
    audits = []
    if args.rounds:
        with db.transaction() as conn:
            audits = [dict(r) for r in conn.execute(
                "SELECT passage_id, step, round, auditor, verdict, issues, dropped, draft FROM audits WHERE story_id = ? ORDER BY id", (args.story,))]
        print(f"\nAUDITS ({len(audits)} rows)")
        for a in audits:
            iss = json.loads(a["issues"])
            words_in_draft = len(json.loads(a["draft"]).get("body", "").split())
            print(f"  {a['step']:<17} r{a['round']} {a['auditor']:<17} {a['verdict']:<6} issues={len(iss)} dropped={a['dropped']} draft_words={words_in_draft}"
                  + "".join(f"\n      - [{i['severity']}] {i['problem']} :: \"{i['quote'][:90]}\"" for i in iss))
    sib = []
    by_parent: dict[int, list[dict]] = {}
    for c in new:
        by_parent.setdefault(c["from_passage"], []).append(ps[c["to_passage"]])
    for kids in by_parent.values():
        for i in range(len(kids)):
            for j in range(i + 1, len(kids)):
                sib.append(jaccard(set(words(kids[i]["body"])), set(words(kids[j]["body"]))))
    summary = {"variant": args.variant, "status": run["status"], "error": run["error"], "seconds": round(time.time() - t),
               "new_passages": len(new), "progress": run["progress"],
               "mean_retell_words": round(sum(r["retell_words"] for r in rows) / max(1, len(rows)), 2),
               "mean_retell_3g": round(sum(r["retell_3g"] for r in rows) / max(1, len(rows)), 2),
               "mean_sibling_overlap": round(sum(sib) / max(1, len(sib)), 2),
               "mean_child_words": round(sum(r["child_words"] for r in rows) / max(1, len(rows)))}
    print(json.dumps(summary, indent=1))
    for r in rows:
        print(f"\n--- {r['parent']}  --[{r['choice']}]-->  {r['child']}{' (ending)' if r['ending'] else ''}  retell={r['retell_words']}/{r['retell_3g']}")
        print("PARENT ENDS: …" + r["parent_tail"][-220:])
        print("CHILD OPENS: " + r["child_head"][:220])
    if args.paths:
        out_of: dict[int, list[dict]] = {}
        for c in ch:
            out_of.setdefault(c["from_passage"], []).append(c)
        lines: list[str] = []

        def walk(pid: int, trail: list[str]) -> None:
            kids = [c for c in out_of.get(pid, []) if c["to_passage"] not in before and c["to_passage"] in ps]
            if not kids:
                if trail:
                    lines.append("\n\n".join(trail) + "\n\n" + "=" * 70 + "\n")
                return
            for c in kids:
                k = ps[c["to_passage"]]
                walk(k["id"], trail + [f"[reader chose: {c['text']}]\n## {k['title']}\n{k['body']}"])

        walk(start, [f"## {ps[start]['title']} (start)\n{ps[start]['body']}"])
        Path(args.paths).write_text("\n".join(lines))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    runs.manager.reset()


if __name__ == "__main__":
    main()
