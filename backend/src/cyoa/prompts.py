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
        "Write the body text for PASSAGE. Stay consistent with WORLD and with the passages it is reached "
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
        "Decide what should happen after PASSAGE, weighing WORLD and EXISTING CHOICES: does it call for a real "
        "decision, a single next passage, or is it a good ending? A branch's target_title may be an existing title from "
        "ALL PASSAGE TITLES, otherwise a new one.",
    ),
    "canon_check": Prompt(
        "Canon check", "canon_check: report contradictions of lore, with a minimal fix.",
        "Compare PASSAGE to WORLD. Report only genuine contradictions of a stated fact — a character who cannot be "
        "present, a place described against its entry, a rule broken. Tone, style and omissions are not issues. If "
        "there are issues, give fixed_body: "
        "the passage body text only (no title, no heading), rewritten with the smallest edits that repair every issue "
        "listed — none of the contradicted claims may survive in it.",
    ),
    # The frontier roles are one set for every act of the pacing plan (concepts/act-pacing.md);
    # the act's fields reach them through {act_guidance}, {length} and {act_remaining}.
    "frontier_choices": Prompt(
        "Frontier choices", "expand_frontier: choices out of a frontier passage, each leading somewhere new. Where the "
        "story is in its arc comes from the STORY ARC block of the frame (agents.arc_block), not from slots. {branching} "
        "is the act's most choices; {remaining} counts the passages this storyline has left, including the one each "
        "choice leads to (1 means that passage is an ending).",
        _SUGGEST_CHOICES + " Every target_title must be NEW — not in ALL PASSAGE TITLES — because the story is growing "
        "outward here; do not lead back to an existing passage. Offer at most {branching} choices, and fewer when the "
        "moment holds fewer real options. Read STORY ARC: the choices carry the story from where PASSAGE sits in it "
        "toward the stage the next passages open, so when that is a later act, set up what that act is for. This "
        "storyline has at most {remaining} passages left, counting the one each choice leads to (1 means that "
        "passage is an ending).",
        ("branching", "remaining"),
    ),
    "frontier_passage": Prompt(
        "Frontier passage", "expand_frontier: the new passage a frontier choice leads to, in one of the plan's acts, asked "
        "for as plain prose (no tool call). {remaining} counts the passages this storyline has left, including this one; "
        "{act_remaining} the passages its act has left, including this one; {length} is the act's. The act's guidance "
        "reaches the writer through the story arc in the frame. {intent_line} and {siblings_line} are whole sentences, "
        "empty when the choices step gave no intent; {ending_rule} is the instruction to close with 'The End', empty "
        "where the act forbids early endings.",
        "The reader took the choice '{choice}' from PASSAGE. Write the passage it leads to, as the next beat of one "
        "continuous story: begin at the very moment after the choice, continuing from how PASSAGE ended "
        "({parent_ending}), in the same place, time, tone and momentum unless the choice itself changes them. Show "
        "the consequence of the choice rather than restating it. Do not recap or re-introduce what the reader has "
        "already read; carry forward its open threads, who is present, what the reader holds and knows, and its voice."
        "{intent_line}{siblings_line}\n"
        "Length: about {length}. Pacing: this storyline has at most {remaining} passages left and this act has "
        "{act_remaining}, counting this one; head for the act's next turn as they shrink, without rushing the moment "
        "itself.\n"
        "Format: second person, present tense. Reply with the passage text only: no title, heading or notes."
        "{ending_rule}",
        ("choice", "parent_ending", "intent_line", "siblings_line", "remaining", "act_remaining", "length", "ending_rule"),
    ),
    "frontier_ending": Prompt(
        "Frontier ending", "expand_frontier: the last passage of a storyline, at the plan's ending level. Not written as a "
        "gamebook passage: its frame (agents.ending_frame) is the story so far as plain prose, then the reader's last "
        "action, and the task is to conclude it, unhurried (about one to two thousand words, a prompt sentence not a plan field). {intent} is what this ending should bring about, "
        "{siblings} the other endings from the same moment, {act_guidance} the plan's ending guidance.",
        "Write the ending of this story. Above is the story as the reader has lived it; the last thing the reader did "
        "was: '{choice}'. Compose the conclusion the whole journey has been heading toward: begin at the moment right "
        "after that action, resolve the central question the story raised, let the consequences of everything the "
        "reader did land, for better or worse, and close on a final image that feels earned. Write it as the story's "
        "last scene, in the same voice, person and tense as the story so far. Nothing is left to decide: do not offer, "
        "weigh or settle any further choice for the reader. Keep to the people, places and facts already established, "
        "and introduce no new named character the story has not pointed to. Do not retell what the reader has already "
        "read. Give it room: a full, unhurried conclusion, typically one to two thousand words, long enough to play "
        "out the consequences and land the final image. Finish on a complete sentence. What this ending should bring about: {intent} The other endings the reader could reach from the "
        "same moment are {siblings}: this one is its own. {act_guidance}",
        ("choice", "intent", "siblings", "act_guidance"),
    ),
    "audit_continuity": Prompt(
        "Audit continuity", "refine: check a drafted passage against the story so far, not today's WORLD rubric "
        "(that is audit_canon).",
        "Check DRAFT below against STORY SO FAR and WORLD. Report only genuine issues: it contradicts something "
        "already established; it ignores or makes no sense of the choice that led to it; it does not pick up where the "
        "preceding passage left off, or re-tells what the reader has just read (evidence is the repeated or "
        "contradicted span of STORY SO FAR or PASSAGE); it shifts out of second "
        "person present tense (addressing the reader as 'you', even by name, is correct). A writer adds new places, "
        "people, objects and details, so a detail that is merely new is never an issue: report it only when it "
        "contradicts a specific line of STORY SO FAR or WORLD. For each issue, quote must be a verbatim span of DRAFT "
        "and evidence must be a verbatim span of STORY SO FAR or WORLD that the draft contradicts — an issue without "
        "such a span is discarded, except a tense/person slip, which needs none. "
        "verdict is 'revise' if any issue is found, otherwise 'pass'.\n\n"
        "DRAFT TITLE: {draft_title}\nDRAFT BODY: {draft_body}",
        ("draft_title", "draft_body"),
    ),
    "audit_ending": Prompt(
        "Audit ending", "refine, at the ending level only: check a drafted ending as a conclusion. audit_continuity "
        "and audit_canon still run on it; this one asks what they do not: is it an ending.",
        "Check DRAFT below as the ENDING of this story, against the story above. Report only genuine issues: it does "
        "not conclude (it resolves nothing the story raised and reads as the next scene, or it leaves the central "
        "question open); it offers, weighs or settles a further decision for the reader that the reader did not make "
        "(stating the reader's own last action is fine); it introduces a named character who is neither in WORLD nor "
        "in the story so far; it re-tells what the story so far has already told instead of moving past it; it stops "
        "before a closing moment, or mid-sentence. Length is not an issue, and neither is a bleak or unhappy ending. "
        "For each issue, quote must be a verbatim span of DRAFT; evidence is a verbatim span of the story or WORLD "
        "where the issue has one (a re-told passage, an established name) and empty where it has none (an ending "
        "that does not conclude). verdict is 'revise' if any issue is found, otherwise 'pass'.\n\n"
        "DRAFT TITLE: {draft_title}\nDRAFT BODY: {draft_body}",
        ("draft_title", "draft_body"),
    ),
    "audit_canon": Prompt(
        "Audit canon", "refine: check a drafted passage against WORLD, the canon_check rubric applied "
        "before the passage is written rather than after.",
        "Compare DRAFT below to WORLD. Report only genuine contradictions of a stated fact — a character who cannot "
        "be present, a place described against its entry, a rule broken. Tone, style and omissions are not issues. "
        "For each issue, quote must be a verbatim span of DRAFT and evidence must be a verbatim span of the WORLD "
        "entry it contradicts. verdict is "
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
    "revise_ending": Prompt(
        "Revise ending", "refine, at the ending level only: fix an ending's verified issues without shrinking it.",
        "DRAFT below was audited as the ending of the story above, and these issues were verified against it:\n{issues}\n\n"
        "Rewrite DRAFT to fix every listed issue. Change only what each issue requires. It is a full ending: keep its "
        "length, its voice and its closing moment, and do not summarise or cut to make a fix. Where an issue is that "
        "it does not conclude, write the conclusion in full. Return the full title and body.\n\n"
        "DRAFT TITLE: {draft_title}\nDRAFT BODY: {draft_body}",
        ("draft_title", "draft_body", "issues"),
    ),
    # Interview Me (concepts/interview-me.md): the one role whose frame is the conversation,
    # not a passage (agents.render_interview).
    "interview": Prompt(
        "Interview", "Interview Me: one turn of the agent-led interview that builds the premise and the characters.",
        "You are interviewing the author to build a BRIEF for a choose-your-own-adventure generator: this story's "
        "PREMISE and its cast of CHARACTERS. Every writing agent reads the premise first and then branches from it, so "
        "the premise describes a situation and its tensions, never a route through the story. It holds who the reader "
        "is and where they stand at the start, what they want, what opposes them, what is at stake, the tone, the rules "
        "of the world that must stay true, and open questions that different branches may answer differently. It leaves "
        "out events in sequence, what anyone finds or does next, outcomes and endings: a sentence that is true on only "
        "one path through the story does not belong. A character holds what they want, what they know or hide and how "
        "they stand toward the reader, not scripted actions. You lead the conversation. First record what the author's "
        "latest message in CONVERSATION establishes. Return premise — the whole revised description, not just the "
        "change — when it settles or changes any of the above; otherwise leave premise null. Create only characters "
        "CHARACTERS does not already list, and edit (by #id) the ones it adds to, always writing a character's complete "
        "body; do not repeat a character that is already there. Merge two entries that turn out to be the same person. "
        "If the author asked you to decide, invent something that fits and record it. If the author tells you something "
        "that happens in the story, ask once whether it holds on every path or is only one option, then record it as "
        "background or as an open possibility accordingly. When a statement cannot be true alongside PREMISE, "
        "CHARACTERS or OTHER LORE, do not pick a side: report it as a conflict. List in resolved the OPEN CONFLICTS the "
        "latest message settles, and record what it decided. Never change a [reviewed] character; report the change you "
        "wanted as a conflict instead. If the conversation is empty, record nothing and just open. Then write reply: "
        "exactly one short question, about a situation, a want, a fear, a rule of the world or a possibility, never "
        "about what happens next, what someone finds, or how something ends. Ask about an open conflict first, unless "
        "the author just skipped it; otherwise, if the latest answer was vague, ask the author to clarify it; otherwise "
        "ask about the biggest gap in the brief — who the reader is, what they want, what opposes them, what is at "
        "stake, the tone and the world's rules, their allies and other significant figures, and for each character "
        "what they want, how they stand toward the reader and what they know or hide. Never ask what CONVERSATION "
        "already asked, in any wording: a skipped question is dropped, so move to a different gap.",
    ),
    "audit_interview": Prompt(
        "Audit interview", "Interview Me: reviews the premise and character changes a turn wants to write, before "
        "anything is saved; the gate on the author's own text.",
        "Review the PROPOSED QUESTION [0] and the PROPOSED CHANGES another agent wants to write into the author's "
        "premise and characters, against the current text above, the CONVERSATION and THE AUTHOR'S LATEST MESSAGE. "
        "Report [0] when the question asks what happens next in the story, what someone finds, or how something ends "
        "(a brief asks about situations, wants, fears, rules and possibilities), repeats a question already in "
        "CONVERSATION in any wording, asks about something the author skipped, or asks more than one question. Report a "
        "change, by its [number], only when it should not be "
        "written: it does not follow from what the author said in their latest message (or from their asking the agent "
        "to decide); it throws away or garbles facts in BEFORE that the author did not change; its AFTER is empty, a "
        "placeholder, a stand-in word such as 'null' or 'none', or is not the kind of text the field holds; it "
        "contradicts the PREMISE, CHARACTERS or OTHER LORE without that being reported as a conflict; or it invents a "
        "character or a fact the conversation never mentioned when the author did not ask the agent to decide; or it "
        "narrates events in order or fixes an outcome (what someone finds, does next, or how something ends) that a "
        "choose-your-own-adventure branch could resolve differently, since the premise and characters describe the "
        "situation, not a route through it. A "
        "rewrite that keeps the earlier facts and adds what the author said is fine, and so is a change of wording. "
        "For each change you report, give the problem in one sentence. verdict is 'revise' if you reported any change, "
        "otherwise 'pass'.",
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
