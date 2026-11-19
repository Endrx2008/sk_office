# Copyright (C) 2026 Enrico Miot (EndrX)
# Licensed under GNU GPL v3 or later — see the LICENSE file or <https://www.gnu.org/licenses/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Formula finder for the Calc AI sidebar (its own panel, separate from the
chat): the user describes what they want to compute; the model searches,
picks, and reads which cells to use - the plugin builds the formula.

  1. keywords - the model turns the request into a few search keywords
                (JSON {"keywords": [...]}).
  2. search   - the plugin searches LibreOffice's own catalogue of Calc
                functions (com.sun.star.sheet.FunctionDescriptions: name,
                category, description and arguments, in the UI language)
                plus a few basic operations between cells (* / - + % ^).
  3. choose   - the model gets the numbered results and replies with ONLY
                the number of the one that fits best (JSON {"choice": n},
                0 = none fits).
  4. args     - the model copies, from the request, the value of each
                argument of the chosen function (JSON {"args": [...]}, []
                when the request does not say which cells). The plugin then
                builds a complete formula, every reference absolute ($B$2)
                because the target cell is not known; without arguments the
                user fills them in LibreOffice's Function Wizard.

If the search finds nothing, the model is asked ONCE for other keywords.
The catalogue is read on the main thread (UNO); everything else is plain
Python and runs on a worker thread.
"""

from __future__ import annotations

import re
import unicodedata

from sk_office_ai.json_chat import DEFAULT_TIMEOUT, LANGUAGE_NAMES, chat_json
from sk_office_ai.model_select import best_model

CATEGORIES = {1: "Database", 2: "Date & Time", 3: "Financial", 4: "Information",
              5: "Logical", 6: "Mathematical", 7: "Array", 8: "Statistical",
              9: "Spreadsheet", 10: "Text", 11: "Add-in"}

MAX_RESULTS = 5  # measured: 5 named candidates got 5/8 right picks, 8 got 3/8

# Basic operations between cells, searched together with LibreOffice's
# functions. "words" are extra search words in the supported UI languages
# (not shown). When one is chosen, the model also names the cells it works
# on (read from the user's request) and the plugin builds the formula with
# every reference absolute ($B$2): the target cell is not known, so the
# formula stays right wherever it is applied.
BASIC_OPERATIONS = [
    {"key": "op_mul", "op": "*", "description": "Multiplies the values of two or more cells (product).",
     "words": "multiply multiplication product times moltiplicare moltiplica moltiplicazione prodotto per "
              "multiplicar producto multiplier produit multiplizieren produkt mal"},
    {"key": "op_div", "op": "/", "description": "Divides the value of one cell by another (quotient, ratio).",
     "words": "divide division divided quotient ratio dividere dividi divisione diviso quoziente rapporto "
              "dividir cociente diviser quotient dividieren quotient geteilt"},
    {"key": "op_sub", "op": "-", "description": "Subtracts one cell from another (difference).",
     "words": "subtract subtraction minus difference sottrarre sottrai sottrazione meno differenza "
              "restar resta diferencia soustraire soustraction moins subtrahieren differenz minus"},
    {"key": "op_add", "op": "+", "description": "Adds the values of two or more cells (sum, total).",
     "words": "add addition plus sum total sommare somma addizione piu totale sumar suma ajouter addition "
              "somme addieren summe plus"},
    {"key": "op_pct", "op": "%", "description": "Percentage: one cell as a percent of another (part divided by total, times 100).",
     "words": "percentage percent rate percentuale percento por ciento porcentaje pourcentage prozent anteil"},
    {"key": "op_pow", "op": "^", "description": "Raises the value of a cell to the power of another (exponent, square).",
     "words": "power exponent squared square potenza elevare elevato quadrato esponente potencia exponente "
              "puissance carre potenz quadrat hoch"},
]
_OP_LABELS = {"*": "Multiplication: cell * cell", "/": "Division: cell / cell",
              "-": "Subtraction: cell - cell", "+": "Addition: cell + cell",
              "%": "Percentage: cell / cell * 100", "^": "Power: cell ^ cell"}
for _op in BASIC_OPERATIONS:
    _op.update({"name": _op["key"], "category": "Basic operation", "args": [], "basic": True})

_CELL_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d{1,7})$")
_COL_RE = re.compile(r"^\$?([A-Za-z]{1,3})$")
_ROW_RE = re.compile(r"^\$?(\d{1,7})$")
_NUMBER_RE = re.compile(r"^-?\d+([.,]\d+)?%?$")


def absolute(ref):
    """'b2' -> '$B$2' (None if it is not a single cell reference)."""
    m = _CELL_RE.match(str(ref).strip())
    return f"${m.group(1).upper()}${m.group(2)}" if m else None


def absolute_ref(text):
    """A cell, range, whole-column or whole-row reference made absolute
    ('b2:b10' -> '$B$2:$B$10', 'c:c' -> '$C:$C'); None if it is not one."""
    parts = str(text).strip().split(":")
    if len(parts) == 1:
        return absolute(parts[0])
    if len(parts) != 2:
        return None
    a, b = parts
    if absolute(a) and absolute(b):
        return f"{absolute(a)}:{absolute(b)}"
    if _COL_RE.match(a) and _COL_RE.match(b):
        return f"${_COL_RE.match(a).group(1).upper()}:${_COL_RE.match(b).group(1).upper()}"
    if _ROW_RE.match(a) and _ROW_RE.match(b):
        return f"${_ROW_RE.match(a).group(1)}:${_ROW_RE.match(b).group(1)}"
    return None


def argument(value):
    """One argument for the formula: a reference (made absolute - the
    target cell is not known), a number or TRUE/FALSE as it is, anything
    else (a condition like >6, a text) as a quoted string."""
    text = str(value).strip()
    ref = absolute_ref(text)
    if ref:
        return ref
    if _NUMBER_RE.match(text) or text.upper() in ("TRUE", "FALSE"):
        return text
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return '"' + text.replace('"', '""') + '"'


def function_formula(fn, args, separator=";"):
    """=NAME(arg1;arg2;...) from the arguments the model read in the user's
    request; None without arguments (the user then fills them in the
    Function Wizard). Extra arguments beyond the function's are dropped
    unless its last argument repeats (Number 1, Number 2, ...)."""
    raw = [str(a).strip() for a in args or [] if str(a).strip()]
    # checks on the MODEL's output: a "value" that is a whole phrase is a
    # piece of the request copied by mistake, and a formula without any
    # cell reference is not what was asked for - then the user fills the
    # arguments in the Function Wizard instead of getting a wrong formula
    if (not raw or not any(absolute_ref(a) for a in raw)
            or any(not absolute_ref(a) and len(a.split()) > 3 for a in raw)):
        return None
    values = [argument(a) for a in raw]
    declared = len(fn["args"])
    repeats = declared and re.search(r"\d\s*$|\.\.\.", fn["args"][-1][0] + " " + fn["args"][-1][1])
    if declared and not repeats:
        values = values[:declared]
    return f"={fn['name']}({separator.join(values)})"


def basic_formula(op, cells):
    """The formula for a basic operation on these cells, every reference
    absolute; None without at least two valid cells."""
    refs = [r for r in (absolute(c) for c in cells or []) if r]
    if len(refs) < 2:
        return None
    if op["op"] == "%":
        return f"={refs[0]}/{refs[1]}*100"
    if op["op"] == "^":
        return f"={refs[0]}^{refs[1]}"
    return "=" + op["op"].join(refs)

KEYWORDS_SYSTEM_PROMPT = (
    "You help the user find the right LibreOffice Calc function. You do NOT "
    "write formulas. From what the user wants to do, give 1 to 3 short search "
    "keywords: the words the description of the right function would contain. "
    "Reply with ONLY one JSON object: {\"keywords\": [\"...\"]}.\n\n"
    "Examples:\n"
    'user: average of the grades, only the ones above 6 -> {"keywords": ["average", "criteria"]}\n'
    'user: find a value in the first column of a table -> {"keywords": ["vertical search", "lookup"]}\n'
    'user: monthly payment of a loan -> {"keywords": ["payment", "annuity"]}\n'
    'user: how many days between two dates -> {"keywords": ["days", "dates"]}'
)

KEYWORDS_INSTRUCTION = "What the user wants: {request}\n\n{language}Reply with ONLY the JSON."

KEYWORDS_RETRY = (
    "No function matched the keywords {tried}. Give 1 to 3 DIFFERENT, more "
    "general keywords. Reply with ONLY the JSON."
)

CHOOSE_SYSTEM_PROMPT = (
    "You pick the LibreOffice Calc function or basic operation that best fits what "
    "the user wants. You get a numbered list; reply with ONLY one JSON object: "
    "{\"choice\": N}, where N is the number of the best one in the list, or "
    "{\"choice\": 0} if none of them fits. Do not write anything else."
)

# A separate step reads the arguments (measured on 0.5B: asked together
# with the choice, it gave them in 1 of 6 requests; asked alone, 9 of 12
# were exactly right - once concrete example values, which it copied, were
# taken out of this prompt).
ARGS_SYSTEM_PROMPT = (
    "You read a LibreOffice Calc request and fill in the arguments of the function "
    "chosen for it. Reply with ONLY one JSON object {\"args\": [...]}: one value per "
    "argument of the function, in order, copied from the user's request: a cell or "
    "range reference exactly as the user wrote it, a number, or a condition or word "
    "exactly as the user wrote it. Never invent cells or values the user did not "
    "write. If the request does not say which cells to use, reply {\"args\": []}."
)

ARGS_INSTRUCTION = "Request: {request}\nFunction: {shape}\n\nReply with ONLY the JSON."

CHOOSE_INSTRUCTION = "What the user wants: {request}\n\nFunctions:\n{listing}\n\nReply with ONLY the JSON."


# ---- the catalogue (main thread) -------------------------------------------

_CATALOG: list[dict] = []


def load_catalog(ctx) -> list[dict]:
    """Every Calc function LibreOffice knows (cached per process):
    {"name", "category", "description", "args": [(name, description,
    optional)]}."""
    if not _CATALOG:
        descriptions = ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.sheet.FunctionDescriptions", ctx)
        for i in range(descriptions.getCount()):
            props = {p.Name: p.Value for p in descriptions.getByIndex(i)}
            _CATALOG.append({
                "name": props.get("Name", ""),
                "category": CATEGORIES.get(props.get("Category"), ""),
                "description": props.get("Description", ""),
                "args": [(a.Name.strip(), a.Description, bool(a.IsOptional))
                         for a in props.get("Arguments") or ()],
            })
    return list(_CATALOG) + BASIC_OPERATIONS


# ---- search (pure Python) ---------------------------------------------------

def _norm(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return text.lower()


def _words(text):
    return [w for w in re.split(r"[^a-z0-9]+", _norm(text)) if len(w) >= 2]


def _same_word(a, b):
    """Equal, or sharing a 5-letter stem (average/averages, payment/payments)."""
    if a == b:
        return True
    return len(a) >= 5 and len(b) >= 5 and a[:5] == b[:5]


def _score(fn, keyword):
    name = _norm(fn["name"])
    desc = _norm(fn["description"])
    desc_words = _words(fn["description"] + " " + fn.get("words", ""))
    arg_words = [w for _n, d, _o in fn["args"] for w in _words(d)]
    cat_words = _words(fn["category"])
    score = 0
    phrase = " ".join(_words(keyword))
    if phrase and len(phrase.split()) > 1 and phrase in desc:
        score += 6
    for w in _words(keyword):
        if w == name:
            score += 12
        elif len(w) >= 3 and name.startswith(w):
            score += 5
        if any(_same_word(w, d) for d in desc_words):
            score += 3
        if any(_same_word(w, a) for a in arg_words):
            score += 1
        if any(_same_word(w, c) for c in cat_words):
            score += 2
    if score and fn.get("basic"):
        score += 8  # a basic operation that matches is almost always what was meant
    return score


def search(catalog, keywords, limit=MAX_RESULTS):
    """The functions best matching any of the keywords, best first."""
    scored = []
    for fn in catalog:
        score = sum(_score(fn, k) for k in keywords)
        if score > 0:
            scored.append((score, fn["name"], fn))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [fn for _s, _n, fn in scored[:limit]]


def syntax(fn):
    """SUM(Number 1; ...) style signature; optional arguments in []. A
    basic operation shows its formula, or its shape without cells."""
    if fn.get("basic"):
        return fn.get("formula") or {"%": "=$A$1/$B$1*100", "^": "=$A$1^$B$1"}.get(
            fn["op"], f"=$A$1{fn['op']}$B$1")
    parts = [f"[{n}]" if optional else n for n, _d, optional in fn["args"]]
    return f"{fn['name']}({'; '.join(parts)})"


def _clean_keywords(reply):
    items = reply.get("keywords") if isinstance(reply, dict) else None
    if isinstance(items, str):
        items = [items]
    return [str(k).strip() for k in items or [] if str(k).strip()][:4]


def _cells(reply):
    """The arguments the model read in the request ("args"; "cells" too)."""
    items = None
    if isinstance(reply, dict):
        items = reply.get("args") if reply.get("args") is not None else reply.get("cells")
    if isinstance(items, str):
        items = re.split(r"[\s,;]+", items)
    return [str(c) for c in items or []]


def _choice(reply, count):
    try:
        n = int(str((reply or {}).get("choice", 0)).strip().strip("[]"))
    except (ValueError, AttributeError):
        return 0
    return n if 1 <= n <= count else 0


# ---- the finder (worker thread, no UNO) ------------------------------------

def find_formula(request: dict):
    """request: "host", "request" (what the user wants), "catalog",
    "language", optional "cancel", "timeout", "on_phase". Returns
    (result, model): result has "keywords", "candidates", "choice" (the
    chosen function dict or None)."""
    host = request["host"]
    cancel = request.get("cancel")
    timeout = request.get("timeout", DEFAULT_TIMEOUT)
    phase = request.get("on_phase") or (lambda name: None)
    lang = request.get("language") or "en"
    language = ("" if lang == "en" else
                f"The function descriptions are in {LANGUAGE_NAMES.get(lang, 'English')}: give the "
                "keywords in that language AND in English. ")
    model, _params = best_model(host)

    phase("finder_keywords")
    messages = [{"role": "system", "content": KEYWORDS_SYSTEM_PROMPT},
                {"role": "user", "content": KEYWORDS_INSTRUCTION.format(
                    request=request["request"], language=language)}]
    keywords = _clean_keywords(chat_json(host, model, messages, timeout, cancel=cancel))
    phase("finder_search")
    candidates = search(request["catalog"], keywords)
    if not candidates:
        phase("finder_keywords")
        retry = messages + [{"role": "assistant", "content": str({"keywords": keywords})},
                            {"role": "user", "content": KEYWORDS_RETRY.format(tried=keywords)}]
        more = _clean_keywords(chat_json(host, model, retry, timeout, cancel=cancel))
        keywords += more
        candidates = search(request["catalog"], more)
    choice = None
    if candidates:
        phase("finder_choose")
        listing = "\n".join(
            f"[{i}] {_OP_LABELS[fn['op']]} - {fn['description']}" if fn.get("basic") else
            f"[{i}] {syntax(fn)} - {fn['description']}"
            for i, fn in enumerate(candidates, start=1))
        reply = chat_json(host, model, [
            {"role": "system", "content": CHOOSE_SYSTEM_PROMPT},
            {"role": "user", "content": CHOOSE_INSTRUCTION.format(request=request["request"],
                                                                  listing=listing)},
        ], timeout, cancel=cancel)
        n = _choice(reply, len(candidates))
        choice = candidates[n - 1] if n else None
        if choice is not None:
            phase("finder_args")
            shape = (f"basic operation: {_OP_LABELS[choice['op']]}" if choice.get("basic")
                     else syntax(choice))
            args = _cells(chat_json(host, model, [
                {"role": "system", "content": ARGS_SYSTEM_PROMPT},
                {"role": "user", "content": ARGS_INSTRUCTION.format(request=request["request"],
                                                                    shape=shape)},
            ], timeout, cancel=cancel))
            if choice.get("basic") and len(args) == 1:
                # "B2*C2" as one value: split it into its cells
                args = [a for a in re.split(r"[*/+\-^]", args[0]) if a.strip()]
            # a copy, so the shared catalogue entry stays clean
            formula = (basic_formula(choice, args) if choice.get("basic")
                       else function_formula(choice, args))
            choice = dict(choice, cells=args, formula=formula)
            candidates[n - 1] = choice
    return {"keywords": keywords, "candidates": candidates, "choice": choice}, model
