"""Deterministic task classifier.

The classifier uses weighted keyword scoring rather than a cascading ``if``
chain so high-signal matches in earlier rules can no longer be silently
overwritten by later, weaker rules (e.g. ``"fix the architecture"`` used to
collapse to ``architecture`` even though the dominant signal was ``fix``).
"""

from __future__ import annotations

import re

from brains.router.schemas import Classification

CODE_HINTS = {"fix", "bug", "test", "refactor", "failing", "patch", "stacktrace"}
EXPLAIN_HINTS = {"explain", "why", "how", "what"}
ARCH_HINTS = {"architecture", "design", "system", "tradeoff", "scalability"}
DOC_HINTS = {"latest", "docs", "documentation", "api", "version", "current", "release"}
RESEARCH_HINTS = {"research", "compare", "survey", "benchmark"}
REPO_HINTS = {"repo", "codebase", "file", "function", "class", "module", "package"}

# Each task type lists (hint set, weight). Highest aggregate weighted score wins.
TASK_RULES: dict[str, list[tuple[set[str], int]]] = {
    "code_fix": [(CODE_HINTS, 2)],
    "code_explanation": [(EXPLAIN_HINTS, 1)],
    "architecture": [(ARCH_HINTS, 2)],
    "docs_lookup": [(DOC_HINTS, 2)],
    "research": [(RESEARCH_HINTS, 2)],
}


def _word_set(messages: list[dict]) -> tuple[str, set[str]]:
    text = " ".join(str(m.get("content", "")) for m in messages).lower()
    return text, set(re.findall(r"[a-z0-9_]+", text))


def _score_tasks(words: set[str]) -> dict[str, int]:
    scores: dict[str, int] = {}
    for task, rules in TASK_RULES.items():
        total = 0
        for hint_set, weight in rules:
            total += len(words & hint_set) * weight
        if total:
            scores[task] = total
    return scores


def _pick_task(scores: dict[str, int]) -> tuple[str, float]:
    if not scores:
        return "unknown", 0.4
    top_task, top_score = max(scores.items(), key=lambda item: item[1])
    runner_up = max((s for t, s in scores.items() if t != top_task), default=0)
    spread = top_score - runner_up
    # Confidence rises with both the absolute top score and the spread over the runner-up.
    confidence = min(0.95, 0.55 + 0.05 * top_score + 0.05 * spread)
    return top_task, round(confidence, 2)


def _pick_complexity(text: str) -> str:
    length = len(text)
    if length > 1200:
        return "hard"
    if length > 400:
        return "medium"
    if length < 80:
        return "trivial"
    return "easy"


def _pick_tier(task: str, complexity: str) -> str:
    if complexity == "hard":
        return "deep"
    if task in {"architecture", "research"}:
        return "strong"
    if task in {"code_fix", "code_explanation", "docs_lookup"}:
        return "default"
    return "cheap"


def classify(messages: list[dict], model: str = "") -> Classification:
    text, words = _word_set(messages)
    scores = _score_tasks(words)
    task, confidence = _pick_task(scores)

    needs_code = bool(words & REPO_HINTS) or task in {"code_fix", "code_explanation"}
    needs_docs = bool(words & DOC_HINTS) or task == "docs_lookup"
    complexity = _pick_complexity(text)
    tier = _pick_tier(task, complexity)

    return Classification(
        task_type=task,
        complexity=complexity,
        needs_codebase=needs_code,
        needs_external_docs=needs_docs,
        needs_memory=True,
        freshness_required=needs_docs,
        recommended_model_tier=tier,
        confidence=confidence,
    )
