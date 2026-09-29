"""Prompt roles: the TASK instruction for each kind of model call, as global data.

A role is one complete instruction; `agents.compose` puts it after the context
frame and wraps it with the retry and steer lines, which stay code. There is
exactly one text per role for every story: an agent is worth running because it
applies one instruction across many passages, so a prompt is judged across all
of them, and a single passage that must be right is written by hand.

Defaults live here. Overrides are a sparse JSON object {key: text} in
backend/prompts.json (CYOA_PROMPTS elsewhere), a file rather than a table so
tuned prompts survive deleting cyoa.db and can be diffed and committed. Nothing
outside this module knows it is a file: callers see resolve, save, reset and
validate. Runs snapshot `resolve()` into their params (runs.py), so a paused run
keeps asking the way it started.
"""

from __future__ import annotations

import json
import os
import string
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "prompts.json"
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Prompt:
    label: str
    doc: str
    default: str
    placeholders: tuple[str, ...] = ()


_SUGGEST_CHOICES = (
    "Suggest new choices for PASSAGE that are distinct from EXISTING CHOICES. A target_title may be an existing title "
    "from ALL PASSAGE TITLES when the choice should lead there, otherwise a new title."
)
_DRAFT_TARGET = (
    "The reader took the choice '{choice}' from PASSAGE and it has no destination yet. Write the destination passage: "
    "a title and body that follow directly from that choice."
)

PROMPTS: dict[str, Prompt] = {
    "draft_body": Prompt(
        "Draft body", "Verb: write the body text of the selected passage.",
        "Write the body text for PASSAGE. Stay consistent with WORLD and TIMELINE and with the passages it is reached "
        "from. Do not list choices in the body.",
    ),
    "suggest_choices": Prompt(
        "Suggest choices", "Verb: propose new choices out of the selected passage.", _SUGGEST_CHOICES,
    ),
    "draft_target": Prompt(
        "Draft target", "Verb: write the destination of a dangling choice.", _DRAFT_TARGET, ("choice",),
    ),
    "rewrite": Prompt(
        "Rewrite", "Verb: rewrite the passage's prose in a tone, keeping every fact.",
        "Rewrite the body of PASSAGE in a {tone} tone. Keep every fact, character and place the same; change only the prose.",
        ("tone",),
    ),
    "suggest_next": Prompt(
        "Suggest next", "Verb: decide whether the passage branches, continues or ends.",
        "Decide what should happen after PASSAGE, weighing WORLD, TIMELINE and EXISTING CHOICES: does it call for a real "
        "decision, a single next passage, or is it a good ending? A branch's target_title may be an existing title from "
        "ALL PASSAGE TITLES, otherwise a new one.",
    ),
    "canon_check": Prompt(
        "Canon check", "canon_check: report contradictions of lore and timeline, with a minimal fix.",
        "Compare PASSAGE to WORLD and TIMELINE. Report only genuine contradictions of a stated fact — a character "
        "who cannot be present, a place described against its entry, an event referred to before it happens or "
        "as if it had not happened. Tone, style and omissions are not issues. If there are issues, give fixed_body: "
        "the passage body text only (no title, no heading), rewritten with the smallest edits that repair every issue "
        "listed — none of the contradicted claims may survive in it.",
    ),
    "frontier_choices": Prompt(
        "Frontier choices", "expand_frontier: choices out of a frontier passage, each leading somewhere new. "
        "{branching} is the most choices the run will keep; {remaining} counts the passages this storyline has "
        "left, including the one each choice leads to (1 means that passage is an ending).",
        _SUGGEST_CHOICES + " Every target_title must be NEW — not in ALL PASSAGE TITLES — because the story is growing "
        "outward here; do not lead back to an existing passage. Offer at most {branching} choices, and fewer when the "
        "moment holds fewer real options. This storyline has at most {remaining} passages left, counting the one each "
        "choice leads to: as that number shrinks, prefer choices that move toward a resolution over ones that open new threads.",
        ("branching", "remaining"),
    ),
    "frontier_passage": Prompt(
        "Frontier passage", "expand_frontier: the new passage a frontier choice leads to, before the last level. "
        "{remaining} counts the passages this storyline has left, including this one.",
        _DRAFT_TARGET + " This storyline has at most {remaining} passages left, counting this one: pace it toward a "
        "resolution as that number shrinks. Set ends_story only if this passage is a natural, satisfying conclusion.",
        ("choice", "remaining"),
    ),
    "frontier_ending": Prompt(
        "Frontier ending", "expand_frontier: the passage a choice leads to at the run's last level, written as an ending.",
        "The reader took the choice '{choice}' from PASSAGE, and the passage it leads to is the last in this storyline. "
        "Write it: a title and body that follow directly from that choice and bring the story to a conclusion — success, "
        "failure, or a decisive turn — consistent with everything the reader has seen. Resolve or clearly close the "
        "central question; do not end on a cliffhanger or offer further choices.",
        ("choice",),
    ),
    "audit_continuity": Prompt(
        "Audit continuity", "refine: check a drafted passage against the story so far, not today's WORLD/TIMELINE rubric "
        "(that is audit_canon).",
        "Check DRAFT below against STORY SO FAR, WORLD and TIMELINE. Report only genuine issues: it contradicts something "
        "already established; it ignores or makes no sense of the choice that led to it; it introduces a person, object "
        "or piece of knowledge the reader has not yet earned; it shifts out of second person present tense. For each "
        "issue, quote must be a verbatim span of DRAFT and evidence must be a verbatim span of STORY SO FAR, WORLD or "
        "TIMELINE that the draft contradicts — leave evidence empty only for a tense/person issue with no such span. "
        "verdict is 'revise' if any issue is found, otherwise 'pass'.\n\n"
        "DRAFT TITLE: {draft_title}\nDRAFT BODY: {draft_body}",
        ("draft_title", "draft_body"),
    ),
    "audit_canon": Prompt(
        "Audit canon", "refine: check a drafted passage against WORLD and TIMELINE, the canon_check rubric applied "
        "before the passage is written rather than after.",
        "Compare DRAFT below to WORLD and TIMELINE. Report only genuine contradictions of a stated fact — a character "
        "who cannot be present, a place described against its entry, an event referred to before it happens or as if "
        "it had not happened. Tone, style and omissions are not issues. For each issue, quote must be a verbatim span "
        "of DRAFT and evidence must be a verbatim span of the WORLD or TIMELINE entry it contradicts. verdict is "
        "'revise' if any issue is found, otherwise 'pass'.\n\n"
        "DRAFT TITLE: {draft_title}\nDRAFT BODY: {draft_body}",
        ("draft_title", "draft_body"),
    ),
    "revise_passage": Prompt(
        "Revise passage", "refine: fix a drafted passage's verified issues and change nothing else.",
        "DRAFT below was audited and these issues were verified against it:\n{issues}\n\nRewrite DRAFT to fix every "
        "listed issue. Change only what each issue requires; keep everything else — wording, facts, tone — the same. "
        "Return the full title and body.\n\n"
        "DRAFT TITLE: {draft_title}\nDRAFT BODY: {draft_body}",
        ("draft_title", "draft_body", "issues"),
    ),
}


class PromptError(ValueError):
    """Text that cannot be a role's prompt; the message names what is wrong."""


def _path() -> Path:
    return Path(os.environ.get("CYOA_PROMPTS") or DEFAULT_PATH)


def _read() -> dict[str, str]:
    path = _path()
    if not path.exists():
        return {}
    return {k: v for k, v in json.loads(path.read_text() or "{}").items() if k in PROMPTS}


def _write(overrides: dict[str, str]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".prompts-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(overrides, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def overrides() -> dict[str, str]:
    """The keys the author has changed, and their text."""
    with _LOCK:
        return _read()


def resolve() -> dict[str, str]:
    """Every role's current text: the defaults overlaid with the overrides."""
    return {**{k: p.default for k, p in PROMPTS.items()}, **overrides()}


def validate(key: str, text: str) -> None:
    """Raise KeyError for an unknown role, PromptError for text `str.format` would
    reject or that drops one of the role's placeholders."""
    prompt = PROMPTS[key]
    allowed = ", ".join(f"{{{p}}}" for p in prompt.placeholders) or "none"
    if not text.strip():
        raise PromptError(f"{key}: the prompt is empty")
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(text) if f is not None]
    except ValueError as e:
        raise PromptError(f"{key}: {e} (write a literal brace as {{{{ or }}}}; placeholders allowed: {allowed})") from None
    for f in fields:
        if f not in prompt.placeholders:
            raise PromptError(f"{key}: unknown placeholder {{{f}}}; placeholders allowed: {allowed}")
    for p in prompt.placeholders:
        if p not in fields:
            raise PromptError(f"{key}: missing placeholder {{{p}}}; every one of {allowed} is required")


def save(key: str, text: str) -> None:
    validate(key, text)
    with _LOCK:
        data = _read()
        data[key] = text
        _write(data)


def reset(key: str) -> None:
    PROMPTS[key]  # KeyError for an unknown role
    with _LOCK:
        data = _read()
        if data.pop(key, None) is not None:
            _write(data)
