"""Pick the "best" installed Ollama model: the one with the most parameters.

Ollama's GET /api/tags reports a "parameter_size" string per model (e.g.
"0.5B", "7B", "70B", "8x7B" for MoE models) — no extra /api/show calls
needed. We parse that instead of the model name, since names are free-form
and not reliably size-ordered.
"""

from __future__ import annotations

import re

from .ollama_client import DEFAULT_HOST, OllamaError, list_models

NO_MODEL_MESSAGE = (
    "Ai features are disabled.\n"
    "It looks like you turned off AI in the skai settings.\n"
    "Without a selected model, skai cannot function."
)


class NoModelError(OllamaError):
    """Ollama answers but has no model: the panels show NO_MODEL_MESSAGE
    (localized) as an alert instead of an error."""

_SIZE_RE = re.compile(r"(?:(\d+)x)?([\d.]+)\s*([KMBT])", re.IGNORECASE)
_MULT = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _param_count(parameter_size: str) -> float:
    """"8x7B" -> 8 * 7e9; "70B" -> 70e9; "" or unparseable -> 0.0."""
    match = _SIZE_RE.search((parameter_size or "").strip())
    if not match:
        return 0.0
    experts, size, unit = match.groups()
    count = float(size) * _MULT[unit.upper()]
    if experts:
        count *= int(experts)
    return count


def pick_best_model(host: str = DEFAULT_HOST) -> str:
    """Return the name of the installed model with the largest parameter count."""
    return best_model(host)[0]


def best_model(host: str = DEFAULT_HOST) -> tuple[str, float]:
    """(name, parameter count) of the installed model with the most parameters."""
    try:
        models = list_models(host)
    except OllamaError as exc:
        if "no models installed" in str(exc):
            raise NoModelError(NO_MODEL_MESSAGE) from exc
        raise
    def size(m):
        return _param_count((m.get("details") or {}).get("parameter_size", ""))

    best = max(models, key=size)
    return best["name"], size(best)
