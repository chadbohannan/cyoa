"""Replay one frontier passage draft against the live model and show how the reply ends.

Copies the DB to a scratch file, rebuilds the exact prompt `grow` sent for one choice out of
one passage, and calls the model with the same schema and token cap, but with the raw message
kept: finish_reason, token usage, the tool-call arguments. It answers "why did this draft
retry": a reply cut off by the token cap or context (finish_reason 'length'), a tool call
with no body, or just a bad answer.

    cd backend && CYOA_LLM=openai uv run python ../scripts/retry_probe.py --story 2 --passage 20 --choice 0 --level 1 --n 3
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="cyoa.db")
    ap.add_argument("--story", type=int, required=True)
    ap.add_argument("--passage", type=int, required=True, help="the parent passage")
    ap.add_argument("--choice", type=int, default=0, help="index of its outgoing choice to draft")
    ap.add_argument("--choice-text", help="draft this choice text instead (for a parent whose choices were never written)")
    ap.add_argument("--level", type=int, required=True, help="the level grow wrote the parent's children at")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--extra", help="JSON of sampler fields to add to the request body, e.g. '{\"dry_multiplier\": 0.8}'")
    ap.add_argument("--prose", action="store_true", help="ask as the app does for passages now: plain prose, parsed by schema.from_prose, no tool call")
    ap.add_argument("--dump", help="directory to write each reply's full tool-call arguments to")
    args = ap.parse_args()

    scratch = tempfile.mkdtemp()
    shutil.copy(args.db, f"{scratch}/cyoa.db")
    os.environ["CYOA_DB"] = f"{scratch}/cyoa.db"
    from cyoa import agents, db, llm, prompts

    db.init_db()
    plan = agents.DEFAULT_PLAN | {k: v for k, v in json.loads(
        db.connect().execute("SELECT plan FROM pacing WHERE story_id = ?", (args.story,)).fetchone()[0]).items()}
    ctx = agents.gather_context({"story_id": args.story, "passage_id": args.passage, "params": {}})["context"]
    ctx["arc"] = agents.arc_block(plan, args.level)
    roles = prompts.resolve()
    with db.transaction() as conn:
        rows = conn.execute("SELECT text FROM choices WHERE story_id = ? AND from_passage = ? ORDER BY sort_order", (args.story, args.passage)).fetchall()
    kept = [(r["text"], "", "") for r in rows]
    if args.choice_text:
        kept = [(args.choice_text, "", "")]
    choice = kept[0] if args.choice_text else kept[args.choice]
    stage = agents.stage_at(plan, args.level)
    role = "frontier_ending" if stage["ending"] else "frontier_passage"
    fields = {**agents.passage_fields(plan, args.level), **agents.flow_fields(ctx, choice, kept)}
    if stage["ending"]:
        fields["frame"] = agents.ending_frame(ctx, choice[0])
    prompt = agents.compose(ctx, roles, role, steer=plan.get("steer", ""), error=None, choice=choice[0], **fields)
    schema = agents.FrontierPassageDraft if not stage["ending"] else agents.FrontierDraft
    cap = args.max_tokens or (llm.ENDING_MAX_TOKENS if stage["ending"] else llm.MAX_TOKENS)
    print(f"role={role} choice={choice[0]!r} prompt={len(prompt)} chars, max_tokens={cap}, schema={schema.__name__}\n")

    model = llm.chat_model(max_tokens=cap, temperature=0.8)
    if not args.prose:
        model = model.bind_tools([schema], tool_choice=schema.__name__)
    if args.extra:
        base = model.bound if hasattr(model, "bound") else model
        base.extra_body = {**(base.extra_body or {}), **json.loads(args.extra)}
    for i in range(args.n):
        t0 = time.time()
        try:
            msg = model.invoke(prompt)
        except Exception as e:  # noqa: BLE001
            print(f"[{i}] {time.time() - t0:.0f}s EXCEPTION {agents.brief(e)}\n")
            continue
        meta = msg.response_metadata
        if args.prose:
            draft = schema.from_prose(msg.content or "")
            leak = bool(agents._CONTROL_TOKEN.search(msg.content or ""))
            print(f"[{i}] {time.time() - t0:.0f}s finish={meta.get('finish_reason')} tokens={(meta.get('token_usage') or {}).get('completion_tokens')} "
                  f"words={len(draft.body.split())} unfinished={agents._unfinished(draft.body)} leaked_markers={leak} ends={getattr(draft, 'ends_story', None)}")
            print(f"    tail: ...{draft.body[-100:]!r}\n")
            if args.dump:
                os.makedirs(args.dump, exist_ok=True)
                open(f"{args.dump}/reply-{i}-{meta.get('finish_reason')}.txt", "w").write(msg.content or "")
            continue
        calls = msg.tool_calls
        raw = (msg.additional_kwargs.get("tool_calls") or [{}])[0].get("function", {}).get("arguments", "")
        body = (calls[0]["args"].get("body") if calls else None) or ""
        print(f"[{i}] {time.time() - t0:.0f}s finish={meta.get('finish_reason')} usage={meta.get('token_usage')}")
        print(f"    tool_calls={len(calls)} invalid={len(msg.invalid_tool_calls)} content_chars={len(msg.content or '')} "
              f"args_chars={len(raw)} body_words={len(body.split())} keys={sorted(calls[0]['args']) if calls else None}")
        print(f"    body tail: ...{body[-120:]!r} unfinished={agents._unfinished(body) if body else None}")
        print(f"    args tail: ...{raw[-160:]!r}")
        if args.dump:
            os.makedirs(args.dump, exist_ok=True)
            full = raw or (str(msg.invalid_tool_calls[0].get("args")) if msg.invalid_tool_calls else json.dumps(calls[0]["args"]) if calls else "")
            open(f"{args.dump}/reply-{i}-{meta.get('finish_reason')}.json", "w").write(full)
        if msg.invalid_tool_calls:
            print(f"    invalid: {str(msg.invalid_tool_calls[0])[:300]}")
        print()


if __name__ == "__main__":
    main()
