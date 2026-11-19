# Copyright (C) 2026 Enrico Miot (EndrX)
# Licensed under GNU GPL v3 or later — see the LICENSE file or <https://www.gnu.org/licenses/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Ask Ollama for a reply and parse it as a single JSON object.

Local models don't reliably emit *only* JSON — they sometimes wrap it in a
markdown code fence, or add a stray sentence before/after. Requests use
Ollama's constrained `format: "json"` mode, and parsing still tries a direct
parse first, then falls back to extracting the largest brace-matched {...}
substring, before giving up.
"""

from __future__ import annotations

import json

from .model_select import best_model
from .ollama_client import CancelToken, Cancelled, OllamaError, chat

# 300s rather than chat()'s 120s default: the call runs on a background
# thread now (see async_call.py), so a slow local model no longer freezes
# LibreOffice while it thinks, and modest hardware genuinely needs the time.
DEFAULT_TIMEOUT = 300.0

# No rules ever read the user's message: deciding what the user meant is
# the model's job (the user asked for exactly that - an earlier version
# pattern-matched commands like "delete the last line" and handled them
# without the model). The plugin only checks the SHAPE of the model's reply.
_RETRY_NUDGE = (
    'Your reply had an empty "actions" list. Read my last message again. If it '
    "asks you to do anything to the document (add, remove, change or format "
    'text), reply again with the same JSON shape and put the needed actions in '
    '"actions" - text only in "message" is never written to the document. If it '
    'does not, reply again with "actions": [].'
)


def _extract_json_object(text: str) -> str:
    start = text.find("{")
    if start == -1:
        raise OllamaError(f"no JSON object found in reply: {text!r}")

    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise OllamaError(f"unterminated JSON object in reply: {text!r}")


def chat_json(host: str, model: str, messages: list[dict], timeout: float = DEFAULT_TIMEOUT,
              options: dict | None = None, cancel: CancelToken | None = None) -> dict:
    """Like chat(), but parses the reply as a JSON object. Raises OllamaError
    if no valid JSON object can be extracted."""
    reply = chat(host, model, messages, timeout, format="json", options=options, cancel=cancel)
    try:
        parsed = json.loads(reply)
    except json.JSONDecodeError:
        candidate = _extract_json_object(reply)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise OllamaError(f"model reply was not valid JSON: {exc}: {reply!r}") from exc
    if not isinstance(parsed, dict):
        raise OllamaError(f"model reply was JSON but not an object: {reply!r}")
    return parsed


def _normalize_actions(result: dict) -> list[dict]:
    actions = result.get("actions")
    if isinstance(actions, dict):
        actions = [actions]
    if not isinstance(actions, list):
        return []
    return [a for a in actions if isinstance(a, dict)]


# Session memory (the conversation so far) only for models big enough to use
# it: measured on the 0.5B test model, the whole history made it copy its
# own earlier replies instead of doing what was asked. Big models get the
# full conversation (most recent first, within a character budget) and a
# larger context window so the system prompt is never pushed out.
# Models from this size up get the full feature set: session memory and
# several kinds of change in one reply. Smaller ones run in a limited mode
# (the panels say so): last exchange only, one kind of change per reply,
# and one follow-up when they answer without actions.
FULL_FEATURES_MIN_PARAMS = 7e9
MEMORY_MIN_PARAMS = FULL_FEATURES_MIN_PARAMS
MEMORY_CHAR_BUDGET = 16000
MEMORY_NUM_CTX = 8192


def has_memory(param_count: float) -> bool:
    return param_count >= MEMORY_MIN_PARAMS


def full_features(param_count: float) -> bool:
    return param_count >= FULL_FEATURES_MIN_PARAMS


LANGUAGE_NAMES = {"en": "English", "it": "Italian", "es": "Spanish", "fr": "French", "de": "German"}


def language_line(lang: str) -> str:
    """System-prompt line asking for the chat reply in the UI language."""
    return (f'Write the "message" text in {LANGUAGE_NAMES.get(lang, "English")}, '
            "whatever language the document is in.")


def fit_history(messages: list[dict], param_count: float) -> list[dict]:
    """messages = [system, *history, current user turn]. Small models keep
    only the last exchange of history; big ones keep as much recent
    history as fits MEMORY_CHAR_BUDGET, starting at a user message."""
    if len(messages) <= 2:
        return messages
    system, history, current = messages[0], messages[1:-1], messages[-1]
    if not has_memory(param_count):
        return [system] + recent_history(history) + [current]
    kept, used = [], 0
    for message in reversed(history):
        used += len(message.get("content") or "")
        if used > MEMORY_CHAR_BUDGET:
            break
        kept.insert(0, message)
    while kept and kept[0].get("role") != "user":
        kept.pop(0)
    return [system] + kept + [current]


def recent_history(history: list[dict]) -> list[dict]:
    """Only the last exchange (the last user message and whatever followed
    it). Measured on the 0.5B test model over a 6-turn conversation: with
    the whole history it increasingly copied its earlier replies (adding
    text when asked to delete, or answering in chat only); with just the
    last exchange it kept choosing the right action. The live document
    snapshot sent every turn already carries the real state, so older
    turns add little but drift."""
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user":
            return history[i:]
    return []


_FIX_NUDGE = (
    "Some of your actions cannot be carried out on the document as it is:\n{problems}\n"
    'Reply again with the same JSON shape and corrected "actions" (all of them, '
    "not only the fixed ones). Remember the document's paragraphs are numbered "
    "[1], [2], ... in my message; to remove a whole paragraph you can use its number."
)


def _ask_to_fix(host, model, messages, result, problems, timeout, options, cancel):
    """One follow-up asking the model to correct actions that can't be
    applied. Returns the corrected reply, or None."""
    if cancel is not None:
        cancel.check()
    fix_messages = messages + [
        {"role": "assistant", "content": json.dumps(
            {"message": result.get("message", ""), "actions": result["actions"]})},
        {"role": "user", "content": _FIX_NUDGE.format(
            problems="\n".join(f"- {p}" for p in problems))},
    ]
    try:
        fixed = chat_json(host, model, fix_messages, timeout, options, cancel)
    except Cancelled:
        raise
    except OllamaError:
        return None
    fixed["actions"] = _normalize_actions(fixed)
    return fixed if fixed["actions"] else None


def ask_for_actions(host: str, messages: list[dict], user_message: str,
                    timeout: float = DEFAULT_TIMEOUT,
                    retry_hint: str = "",
                    cancel: CancelToken | None = None,
                    system_prompt_for=None,
                    language: str | None = None,
                    validate=None) -> tuple[dict, str]:
    """One chat turn of the {"message","actions"} protocol: picks the best
    installed model, asks it, and normalizes result["actions"] to a list of
    dicts. Small (limited-mode) models that reply with no actions get ONE
    follow-up asking the MODEL to re-read the message and decide whether it
    needed actions (they often say "done!" in "message" while leaving
    "actions" empty); the follow-up's reply is used only if it carries
    actions. retry_hint (the app's action examples) is appended to it.
    Nothing here reads or interprets the user's message itself.

    `messages` carries the whole session history; fit_history() trims it
    to what the chosen model can use. system_prompt_for(param_count), if
    given, replaces the system prompt once the model's size is known (e.g.
    one for small models); language (a UI language code) asks
    for the chat reply in that language. cancel (a CancelToken) aborts the
    request(s) from another thread with ollama_client.Cancelled.
    result["_model_params"] reports the chosen model's size.
    validate(actions) -> [problem, ...], if given, checks the reply's
    actions can actually be carried out (e.g. quoted text exists); when it
    reports problems the MODEL is told what is wrong and asked once to
    correct its actions, so it doesn't believe it changed the document
    when nothing could be applied.

    Pure HTTP, no UNO - safe to run on a background thread.
    Returns (result, model_name)."""
    model, params = best_model(host)
    if cancel is not None:
        cancel.check()
    messages = fit_history(messages, params)
    system = messages[0]["content"]
    if system_prompt_for is not None:
        system = system_prompt_for(params)
    if language:
        system = f"{system}\n\n{language_line(language)}"
    messages = [{"role": "system", "content": system}] + messages[1:]
    if language and len(messages) > 1:
        # Repeated at the end of the user's turn: small models ignore a
        # system-prompt-only language instruction (measured on 0.5B).
        last = dict(messages[-1])
        last["content"] = f"{last['content']}\n\n({language_line(language)})"
        messages = messages[:-1] + [last]
    options = {"num_ctx": MEMORY_NUM_CTX} if has_memory(params) else None
    result = chat_json(host, model, messages, timeout, options, cancel)
    result["actions"] = _normalize_actions(result)
    result["_model_params"] = params
    if result["actions"] and validate is not None:
        problems = validate(result["actions"])
        if problems:
            fixed = _ask_to_fix(host, model, messages, result, problems, timeout, options, cancel)
            if fixed is not None:
                fixed["_model_params"] = params
                return fixed, model
            result["_invalid_actions"] = problems
        return result, model
    if result["actions"] or full_features(params):
        return result, model

    retry_messages = messages + [
        {"role": "assistant", "content": json.dumps(result)},
        {"role": "user", "content": f"{_RETRY_NUDGE} {retry_hint}".strip()},
    ]
    if cancel is not None:
        cancel.check()
    try:
        second = chat_json(host, model, retry_messages, timeout, options, cancel)
    except Cancelled:
        raise
    except OllamaError:
        return result, model
    second["actions"] = _normalize_actions(second)
    second["_model_params"] = params
    return (second if second["actions"] else result), model
