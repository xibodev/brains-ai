"""Static price catalog for the savings ledger.

Prices are quoted as **USD per 1,000,000 tokens** (the unit every major
upstream uses on their pricing page) for the named model. Both the
*input* (prompt) and *output* (completion) sides are tracked separately
because they are billed at different rates.

This is a **fallback catalog** that exists so brains can show savings
out-of-the-box without any operator configuration. Anything we don't
recognise simply gets ``None`` from :func:`lookup_price` and the
savings ledger records the request with ``cost_actual_usd=NULL`` so
the dashboard can tell the difference between "free" and "unknown".

Operators can override or extend the catalog via the runtime overlay
under ``savings.price_catalog`` (a mapping of ``model_id -> {input,
output}``). The overlay wins, then the static catalog, then ``None``.

Numbers below were the publicly-quoted list price at the time the
file was authored (2026-06). If you spot a stale entry, send a PR;
nothing here is load-bearing — it only affects how savings are
estimated for *display*.
"""

from __future__ import annotations

from collections.abc import Mapping

# Prices are USD / 1,000,000 tokens.
# Keys are matched case-insensitively against the requested / routed model
# string. The longest matching prefix wins so callers can pass a fully
# qualified model id (e.g. ``"gpt-4o-2024-08-06"``) and still hit the
# generic ``"gpt-4o"`` entry.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    # --- OpenAI ---
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "gpt-4": (30.00, 60.00),
    "gpt-3.5-turbo": (0.50, 1.50),
    "o1": (15.00, 60.00),
    "o1-mini": (3.00, 12.00),
    "o1-preview": (15.00, 60.00),
    "o3-mini": (1.10, 4.40),
    # --- Anthropic ---
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku-4": (0.80, 4.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-3-opus": (15.00, 75.00),
    "claude-3-sonnet": (3.00, 15.00),
    "claude-3-haiku": (0.25, 1.25),
    # --- Google ---
    "gemini-2.5-pro": (1.25, 5.00),
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-1.5-flash": (0.075, 0.30),
    # --- Local / open weights — billed at $0 because the operator
    # already pays the GPU bill flat. Showing "saved 100%" against
    # any hosted model is the whole point of this row.
    "llama": (0.0, 0.0),
    "mistral": (0.0, 0.0),
    "qwen": (0.0, 0.0),
    "deepseek": (0.0, 0.0),
    "ollama": (0.0, 0.0),
    # --- Echo (built-in STUB provider, not a real upstream) —
    # priced at a tiny non-zero value so smoke tests can assert
    # non-zero costs while still exercising the math. Ledger rows
    # written for echo are flagged ``is_stub=True`` and excluded from
    # the dashboard headline by default; this entry is what they cost
    # only when the operator opts in via ``include_stubs=1``.
    "echo": (0.001, 0.001),
}


def _normalise_overrides(
    overrides: Mapping[str, Mapping[str, float]] | None,
) -> dict[str, tuple[float, float]]:
    """Coerce the overlay shape ``{model: {input, output}}`` into the
    catalog's ``(input, output)`` tuple form."""
    if not overrides:
        return {}
    out: dict[str, tuple[float, float]] = {}
    for model, spec in overrides.items():
        if not isinstance(spec, Mapping):
            continue
        try:
            ip = float(spec.get("input", 0.0) or 0.0)
            op = float(spec.get("output", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        out[str(model).lower()] = (ip, op)
    return out


def lookup_price(
    model: str | None,
    *,
    overrides: Mapping[str, Mapping[str, float]] | None = None,
) -> tuple[float, float] | None:
    """Return ``(input_per_million, output_per_million)`` for *model*.

    Match order, first hit wins:
      1. Exact match in the overlay overrides.
      2. Longest prefix match in the overlay overrides.
      3. Exact match in :data:`DEFAULT_PRICES`.
      4. Longest prefix match in :data:`DEFAULT_PRICES`.
      5. ``None`` — unknown, caller should record cost as ``NULL``.
    """
    if not model:
        return None
    key = model.lower()
    overlay = _normalise_overrides(overrides)
    if key in overlay:
        return overlay[key]
    if key in DEFAULT_PRICES:
        return DEFAULT_PRICES[key]
    # Longest prefix match: try the overlay first, then defaults.
    best: tuple[str, tuple[float, float]] | None = None
    for src in (overlay, DEFAULT_PRICES):
        for catalog_key, price in src.items():
            if key.startswith(catalog_key) and (best is None or len(catalog_key) > len(best[0])):
                best = (catalog_key, price)
        if best:
            return best[1]
    return None


def compute_cost(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    *,
    overrides: Mapping[str, Mapping[str, float]] | None = None,
) -> float | None:
    """Compute the total USD cost of a single call, or ``None`` if the
    model is not in the catalog (and no override matched)."""
    price = lookup_price(model, overrides=overrides)
    if price is None:
        return None
    ip, op = price
    return (input_tokens / 1_000_000.0) * ip + (output_tokens / 1_000_000.0) * op
