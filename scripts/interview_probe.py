"""Send one Interview Me turn's prompt to the model and print what comes back, raw.

Read-only: it renders the prompt an opening turn would send for a story (the
same frame and role text the backend uses) and calls the configured model with
a token cap and a timeout, then prints the finish reason, token usage, the raw
text/tool call, and whether it parsed as InterviewReply. Nothing is written to
the database. Use it to see why a turn is slow or failing on a given model.

    cd backend && CYOA_LLM=openai uv run python ../scripts/interview_probe.py --story 1 [--max-tokens 1500]
"""

from __future__ import annotations

import argparse
import json
import time

from cyoa import agents, llm, prompts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--story", type=int, required=True)
    ap.add_argument("--max-tokens", type=int, default=1500)
    ap.add_argument("--show-prompt", action="store_true")
    ap.add_argument("--raw", action="store_true", help="openai backend: send the same forced tool call with the plain "
                    "OpenAI client and print the server's message verbatim (langchain drops what it cannot parse)")
    ap.add_argument("--extra", default="{}", help="--raw only: JSON merged into the request body, e.g. "
                    "'{\"chat_template_kwargs\": {\"enable_thinking\": false}}'")
    args = ap.parse_args()

    prompt = agents.interview_preview(args.story, prompts.resolve())
    print(f"backend={llm.BACKEND} model={llm.DEFAULT_MODEL} method={llm.structured_method()} prompt_chars={len(prompt)}")
    if args.show_prompt:
        print(prompt, "\n" + "-" * 60)

    if args.raw:
        return raw_call(prompt, args.max_tokens, json.loads(args.extra))
    model = llm.chat_model(temperature=0.7, max_tokens=args.max_tokens)
    structured = model.with_structured_output(agents.InterviewReply, method=llm.structured_method(), include_raw=True)
    t = time.time()
    out = structured.invoke(prompt)
    raw = out["raw"]
    meta = getattr(raw, "response_metadata", {}) or {}
    print(f"seconds={time.time() - t:.1f} finish_reason={meta.get('finish_reason')} usage={meta.get('token_usage') or meta.get('usage')}")
    print("tool_calls:", json.dumps(getattr(raw, "tool_calls", None), indent=1)[:3000])
    content = raw.content if isinstance(raw.content, str) else json.dumps(raw.content)
    print(f"content ({len(content)} chars), head:\n{content[:1500]}")
    if len(content) > 1500:
        print(f"... tail:\n{content[-800:]}")
    extra = getattr(raw, "additional_kwargs", {}) or {}
    for k, v in extra.items():
        v = v if isinstance(v, str) else json.dumps(v)
        print(f"additional_kwargs[{k}] ({len(v)} chars), head:\n{v[:1200]}" + (f"\n... tail:\n{v[-600:]}" if len(v) > 1200 else ""))
    print("parsed:", out["parsed"].model_dump() if out["parsed"] else None)
    print("parsing_error:", out["parsing_error"])


def raw_call(prompt: str, max_tokens: int, extra: dict) -> None:
    import openai
    from langchain_core.utils.function_calling import convert_to_openai_tool

    tool = convert_to_openai_tool(agents.InterviewReply)
    client = openai.OpenAI(base_url=llm.OPENAI_URL, api_key="none")
    t = time.time()
    r = client.chat.completions.create(
        model=llm.DEFAULT_MODEL, messages=[{"role": "user", "content": prompt}], temperature=0.7, tools=[tool],
        tool_choice={"type": "function", "function": {"name": tool["function"]["name"]}}, extra_body={"max_tokens": max_tokens, **extra})
    msg = r.choices[0].message.model_dump()
    print(f"seconds={time.time() - t:.1f} finish_reason={r.choices[0].finish_reason} usage={r.usage}")
    for k, v in msg.items():
        v = v if isinstance(v, str) else json.dumps(v)
        if v in ("null", "[]", None):
            continue
        print(f"message[{k}] ({len(v)} chars), head:\n{v[:1500]!r}" + (f"\n... tail:\n{v[-600:]!r}" if len(v) > 1500 else ""))


if __name__ == "__main__":
    main()
