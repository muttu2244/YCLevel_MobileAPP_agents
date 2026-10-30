"""Loads the agent prompts from `prompts/*.md`.

Each prompt file is written for a human first (rules, worked examples, notes on
what to do when it goes wrong) and keeps the machine copy in the first fenced
block after "## System prompt". That way there is exactly one source of truth
for the wording an agent actually sends to a model, and it is reviewable in a
diff.
"""

from __future__ import annotations

import re

from .config import PROMPTS_DIR

_BLOCK = re.compile(r"## System prompt[^\n]*\n+```[a-zA-Z]*\s*\n(.*?)```", re.DOTALL)


def system_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"missing prompt file: {path}")
    match = _BLOCK.search(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"{path} has no fenced block under '## System prompt'")
    return match.group(1).strip()


def available() -> list[str]:
    return sorted(p.name for p in PROMPTS_DIR.glob("*.md"))
