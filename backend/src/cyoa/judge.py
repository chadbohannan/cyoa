"""Lint one passage for named craft defects with a System One decision model (concepts/story-quality-scoring.md).

A decision model answers typed questions with probabilities and no prose, so the reply is schema-valid by construction
(concepts/system-one-decision-models.md). Each defect here is one yes/no question taken from the literary-pass list in
runbooks/writing-a-cyoa-book.md. The model is shown the whole passage and the one before it; a question that fires on planted
defects and stays quiet on clean text is useful, a scalar "quality" or an A/B preference is not (it prefers a passage with a
moral appended). So `lint` gives a list of named defects, never a score for ranking. `METRICS` are the positive counterpart: yes/no
questions about a quality the author wants, averaged per passage and over subtrees by `quality_map`, to find where a book is
weakest rather than to rank passages against each other.

CYOA_JUDGE_URL (default Ollama's /v1/systemone) and CYOA_JUDGE_MODEL (default clef-flash-8k, the largest tag that loads
on a 12 GB card; its window is 8192 tokens).
"""

from __future__ import annotations

import os

import httpx

URL = os.environ.get("CYOA_JUDGE_URL", "http://localhost:11434/v1/systemone")
MODEL = os.environ.get("CYOA_JUDGE_MODEL", "clef-flash-8k")
PREV_CHARS = 1500  # of the previous passage, from its end; the passage itself is sent whole (8k window)
MAX_CHARS = 12000

DEFECTS = {
    "moral": "Does the passage end with a sentence that spells out a lesson or moral for the reader?",
    "clock": "Does the passage end by stating a clock time?",
    "summary": "Is the passage mostly a summary of events told from a distance, rather than a scene in a concrete moment?",
    "explains_clue": "Does the passage point out a clue and then explain to the reader what it proves?",
    "monologue": "Is most of the passage one character explaining at length?",
}


METRICS = {
    "vivid": "Does the passage use specific sensory detail rather than generic words?",
    "action": "Is something physically happening in the passage right now, rather than description or reflection?",
    "clear": "Is it clear where the reader is and what is happening to them right now?",
    "tight": "Is every sentence necessary, with no padding or repetition?",
    "tension": "Does the passage make the reader feel that something could go badly wrong in the next moment?",
}


def ask(body: str, previous: str, questions: dict[str, str], options: list[str] | tuple[str, ...] = ()) -> dict[str, float]:
    """P(yes) for each yes/no question about `body`, shown with the end of the passage before it and the choices that leave
    it. Raises ConnectionError if the model is down."""
    if len(body) > MAX_CHARS:
        raise ValueError(f"passage is {len(body)} characters; the 8k window takes about {MAX_CHARS}")
    state = (f"PREVIOUS PASSAGE (end):\n{previous[-PREV_CHARS:]}\n\n" if previous else "") + f"PASSAGE TO CHECK:\n{body}"
    if options:
        state += "\n\nOPTIONS FOR THE READER:\n" + "\n".join(f"- {o}" for o in options)
    qs = {k: {"type": "noul", "instructions": q} for k, q in questions.items()}
    try:
        r = httpx.post(URL, json={"model": MODEL, "state": state, "questions": qs}, timeout=60)
        r.raise_for_status()
        answers = r.json()["answers"]
    except httpx.HTTPError as e:
        raise ConnectionError(f"decision model {MODEL} unavailable ({URL}): {e}") from e
    return {k: round(answers[k]["noul"], 2) for k in questions}


def lint(body: str, previous: str = "") -> dict[str, float]:
    """P(yes) for each defect in DEFECTS."""
    return ask(body, previous, DEFECTS)
