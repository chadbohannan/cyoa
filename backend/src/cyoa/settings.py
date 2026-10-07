"""Machine settings the author changes from the Settings screen.

Two things live here because they belong to this machine's model server, not to a
story or a prompt: which model the agents ask by default (`model`), and how many
model calls may be in flight at once (`concurrency`, the agents.py gate). They are
kept in settings.json next to prompts.json (CYOA_SETTINGS overrides the path; the
file is not committed), and only keys the author set are stored: an absent key
means "use the environment's value" (OLLAMA_MODEL / CYOA_OPENAI_MODEL, and
CYOA_AGENT_CONCURRENCY), so deleting the file restores the launch configuration.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "settings.json"
KEYS = ("model", "concurrency")
MAX_CONCURRENCY = 64
_LOCK = threading.Lock()


def _path() -> Path:
    return Path(os.environ.get("CYOA_SETTINGS") or DEFAULT_PATH)


def read() -> dict[str, Any]:
    """The keys the author has set."""
    path = _path()
    try:
        data = json.loads(path.read_text() or "{}")
    except (FileNotFoundError, ValueError):
        return {}
    return {k: v for k, v in data.items() if k in KEYS and v is not None}


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into the stored settings; a None value clears that key. Returns what is stored."""
    with _LOCK:
        data = read()
        for k, v in patch.items():
            if k not in KEYS:
                continue
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
        return data


def model() -> str | None:
    return read().get("model")


def concurrency() -> int | None:
    return read().get("concurrency")
