# Copyright (C) 2026 Enrico Miot (EndrX)
# Licensed under GNU GPL v3 or later — see the LICENSE file or <https://www.gnu.org/licenses/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Chat-turn logic for the Math AI sidebar, kept free of any awt/UI
dependency so it can be exercised directly (e.g. over a UNO socket
connection) without simulating button clicks.

A LibreOffice Math document is one read/write string property,
doc.Formula, in StarMath notation. Here the AI only suggests formulas and
functions - nothing else. Every turn runs the model separately for:

  0. remove  - JSON {"remove": [row numbers]}: which of the formula's
               current rows (shown numbered) to remove - or to replace,
               when the user asks to change them.
  1. formula - write ONLY the new rows to add, in StarMath, one row per
               line (or NO_FORMULA); they take the place of the first
               removed row, else go at the end. Its output is cleaned up: models
               often answer in LaTeX ("\\frac{a}{b}", "\\( ... \\)") or
               Unicode ("x²", "π"), which to_starmath() converts. If a row
               still looks broken (unbalanced brackets, a leftover
               "\\command"), the model is asked ONCE to fix it.
  2. reply   - the chat reply, told exactly which rows were added and
               removed (or that nothing was), so it describes them instead
               of writing a formula into the chat itself.

Separately, explain (the panel's Explain button) reads the formula that
is in the document right now and asks the model to explain it - with no
chat history, so the explanation is about what is really on the sheet.

The suggestion goes into the document when the turn ends. The formula is
compared row by row (rows are separated by StarMath's "newline") in three
versions: as it was when the prompt was sent, as the AI suggests it, and
as it is now. If the user changed the formula meanwhile, or the AI removes
or changes rows that were already there, the panel asks which version to
keep (the same review dialog as Writer); pure additions are just applied.
"""

from __future__ import annotations

import difflib
import re
import traceback

from sk_office_ai.config import Settings
from sk_office_ai.json_chat import (
    DEFAULT_TIMEOUT, LANGUAGE_NAMES, MEMORY_NUM_CTX, chat_json, fit_history, has_memory,
)
from sk_office_ai.model_select import best_model
from sk_office_ai.ollama_client import Cancelled, OllamaError, chat

APP_NAME = "sk_office-math"

NO_FORMULA = "NO_FORMULA"

# ---- prompts --------------------------------------------------------------

REMOVE_SYSTEM_PROMPT = (
    "You manage the rows of a LibreOffice Math formula. You see the formula's "
    "CURRENT rows, numbered [1], [2], ... (the numbers are not part of the formula). "
    "Reply with ONLY one JSON object: {\"remove\": [row numbers]} - the rows the "
    "user asks to delete, or to change (a changed row is removed here and written "
    "again in another step). If no row must be removed or changed (e.g. the user "
    "asks to ADD a formula, or asks a question), reply {\"remove\": []}.\n\n"
    "Examples, for the rows [1] a^2 + b^2 = c^2, [2] e = mc^2, [3] F = m a:\n"
    'user: remove the last formula -> {"remove": [3]}\n'
    'user: delete Einstein\'s formula -> {"remove": [2]}\n'
    'user: change the first formula to use x, y, z -> {"remove": [1]}\n'
    'user: clear everything -> {"remove": [1, 2, 3]}\n'
    'user: add the area of a circle -> {"remove": []}'
)

REMOVE_INSTRUCTION = (
    "Current formula rows:\n{numbered}\n\nUser's request: {request}\n\n"
    'Reply with ONLY the JSON {{"remove": [...]}} for this request.'
)

FORMULA_SYSTEM_PROMPT = (
    "You suggest mathematical formulas and functions for a LibreOffice Math "
    "document. Whatever you reply is added to the document's formula, so you "
    "answer with ONLY the new formula rows, in StarMath notation (NOT LaTeX), one "
    "formula per line, with no explanations and no code fences. Never repeat rows "
    "that are already in the document: they stay as they are.\n"
    "StarMath examples: a over b, sqrt{x}, nroot{3}{x}, x^2, x_i, a cdot b, "
    "a times b, +-, <=, >=, <>, %pi, %alpha, infinity, "
    "sum from{i=1} to{n} i, int from{0} to{%pi} sin(x) dx, "
    "lim from{x toward 0} {sin(x) over x}, f(x) = x^2 + 1."
)

FORMULA_INSTRUCTION = (
    "Current formula in the document (one row per line):\n\"\"\"\n{formula}\n\"\"\"\n"
    "{removing}\n"
    "User's request: {request}\n\n"
    "Write ONLY the new formula rows to add for this request (including the new "
    "version of any row being changed), in StarMath, one row per line. If the "
    "request does not ask for a new formula or function (for example it is just a "
    f"question, or it only asks to remove rows), answer exactly {NO_FORMULA}"
)

REMOVING_LINE = "Rows being removed for this request:\n{rows}\n"

FIX_INSTRUCTION = (
    "This formula is not valid StarMath:\n\"\"\"\n{formula}\n\"\"\"\n"
    "Problems: {problems}\n"
    "Rewrite it correctly in StarMath (balanced brackets, no LaTeX commands), "
    "one row per line. Reply with ONLY the corrected formula."
)

REPLY_SYSTEM_PROMPT = (
    "You are the chat assistant of a LibreOffice Math side panel. You suggest "
    "formulas and functions: the plugin puts your suggested formula into the "
    "document for you, before this chat reply, and the user reviews it. "
    "Reply in plain text only: no JSON, no Markdown, no LaTeX."
)

REPLY_WROTE_INSTRUCTION = (
    "User's message: {request}\n\n{changes}\n"
    "Now tell the user, in ONE short sentence, what you changed in the formula. "
    "Do not repeat the formulas. {language}"
)

REPLY_ADDED = "You have just added these rows to the formula:\n\"\"\"\n{rows}\n\"\"\"\n"
REPLY_REMOVED = "You are removing these rows from the formula:\n\"\"\"\n{rows}\n\"\"\"\n"

EXPLAIN_SYSTEM_PROMPT = (
    "You explain mathematical formulas written in LibreOffice Math's StarMath "
    "notation, in plain words. Base your explanation ONLY on the formula you are "
    "given - it is what is in the document right now. Reply in plain text: no "
    "JSON, no Markdown, no LaTeX."
)

EXPLAIN_INSTRUCTION = (
    "This is the formula in the document right now, one row per line:\n"
    "\"\"\"\n{formula}\n\"\"\"\n\n"
    "Explain briefly what it is and what its parts mean. {language}"
)

REPLY_NONE_INSTRUCTION = (
    "Current formula in the document:\n\"\"\"\n{formula}\n\"\"\"\n\n"
    "User's message: {request}\n\n"
    "No formula was suggested for this message. Answer the user briefly. {language}"
)

# Never cleared by Clear chat (which forgets only the conversation): the
# model always "remembers" what it can do here.
CAPABILITY_MEMORY = [
    {"role": "user", "content": "What can you do in this document?"},
    {"role": "assistant", "content": (
        "I suggest formulas and functions: I add the formulas you ask for straight "
        "into your LibreOffice Math document and remove the ones you want gone, and "
        "you review the changes. I can also explain the formula that is there.")},
]


def _language_line(lang):
    return f"Write your reply in {LANGUAGE_NAMES.get(lang or 'en', 'English')}."


# ---- rows -----------------------------------------------------------------

_NEWLINE_RE = re.compile(r"\bnewline\b")


def formula_rows(formula):
    """The formula's visual rows (StarMath's "newline" separates them)."""
    return [r.strip() for r in _NEWLINE_RE.split(formula or "") if r.strip()]


def join_rows(rows):
    return " newline\n".join(rows)


# ---- cleaning the model's output (LaTeX / Unicode -> StarMath) ------------

_LATEX_SIMPLE = {
    "cdot": "cdot", "times": "times", "div": "div", "pm": "+-", "mp": "-+",
    "le": "<=", "leq": "<=", "ge": ">=", "geq": ">=", "ne": "<>", "neq": "<>",
    "approx": "approx", "equiv": "equiv", "sim": "sim", "propto": "prop",
    "infty": "infinity", "to": "toward", "rightarrow": "toward", "Rightarrow": "drarrow",
    "Leftarrow": "dlarrow", "Leftrightarrow": "dlrarrow", "leftarrow": "leftarrow",
    "partial": "partial", "nabla": "nabla", "in": "in", "notin": "notin",
    "subset": "subset", "subseteq": "subseteq", "cup": "union", "cap": "intersection",
    "forall": "forall", "exists": "exists", "cdots": "dotsaxis", "ldots": "dotslow",
    "dots": "dotslow", "circ": "circ", "neg": "neg", "land": "and", "lor": "or",
    "sin": "sin", "cos": "cos", "tan": "tan", "cot": "cot", "arcsin": "arcsin",
    "arccos": "arccos", "arctan": "arctan", "sinh": "sinh", "cosh": "cosh",
    "tanh": "tanh", "log": "log", "ln": "ln", "exp": "exp", "det": "det",
    "max": "max", "min": "min", "gcd": "gcd",
    "left": "left", "right": "right", "quad": "~", "qquad": "~~",
    ",": "`", ";": "~", ":": "~", "!": "", " ": "~",
    "{": "lbrace", "}": "rbrace", "|": "lline",
}
_GREEK = ("alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa "
          "lambda mu nu xi omicron pi varpi rho varrho sigma varsigma tau upsilon phi "
          "varphi chi psi omega").split()
_ONE_ARG = {"sqrt": "sqrt", "overline": "overline", "bar": "bar", "vec": "vec", "hat": "hat",
            "tilde": "tilde", "dot": "dot", "ddot": "ddot", "underline": "underline",
            "mathbf": "bold", "boldsymbol": "bold", "mathit": "ital", "abs": "abs"}
_PLAIN_ARG = ("mathrm", "operatorname", "mathbb", "mathcal", "displaystyle", "textstyle")
_BIG_OPS = {"sum": "sum", "prod": "prod", "int": "int", "iint": "iint", "iiint": "iiint",
            "oint": "lint", "lim": "lim", "bigcup": "union", "bigcap": "intersection"}
_UNICODE = {
    "±": " +- ", "∓": " -+ ", "×": " times ", "·": " cdot ", "÷": " div ", "≤": " <= ",
    "≥": " >= ", "≠": " <> ", "≈": " approx ", "≡": " equiv ", "∞": " infinity ",
    "→": " toward ", "⇒": " drarrow ", "∂": " partial ", "∇": " nabla ", "∈": " in ",
    "∑": " sum ", "∏": " prod ", "∫": " int ", "√": " sqrt ", "…": " dotslow ",
    "⋅": " cdot ", "−": "-",
    "²": "^2", "³": "^3", "¹": "^1", "⁰": "^0", "⁴": "^4", "⁵": "^5", "⁶": "^6",
    "⁷": "^7", "⁸": "^8", "⁹": "^9", "ⁿ": "^n", "₀": "_0", "₁": "_1", "₂": "_2",
    "₃": "_3", "ᵢ": "_i", "ₙ": "_n",
    "α": "%alpha ", "β": "%beta ", "γ": "%gamma ", "δ": "%delta ", "ε": "%epsilon ",
    "θ": "%theta ", "λ": "%lambda ", "μ": "%mu ", "π": "%pi ", "ρ": "%rho ",
    "σ": "%sigma ", "τ": "%tau ", "φ": "%phi ", "ω": "%omega ", "Δ": "%DELTA ",
    "Σ": "%SIGMA ", "Ω": "%OMEGA ", "Φ": "%PHI ", "Γ": "%GAMMA ", "Λ": "%LAMBDA ",
}


class _Latex:
    """Tiny recursive LaTeX -> StarMath converter for the subset models
    actually write. Anything unknown is left as it is (the problem check
    then reports it)."""

    def __init__(self, text):
        self.s, self.i = text, 0

    def group(self):
        """The next argument: a {...} group or one token, converted."""
        self.skip_space()
        if self.i < len(self.s) and self.s[self.i] == "{":
            depth, start = 0, self.i
            while self.i < len(self.s):
                c = self.s[self.i]
                if c == "\\":
                    self.i += 2
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        self.i += 1
                        return _Latex(self.s[start + 1:self.i - 1]).run()
                self.i += 1
            return _Latex(self.s[start + 1:]).run()
        if self.i < len(self.s) and self.s[self.i] == "\\":
            return self.command()
        if self.i < len(self.s):
            self.i += 1
            return self.s[self.i - 1]
        return ""

    def optional(self):
        self.skip_space()
        if self.i < len(self.s) and self.s[self.i] == "[":
            end = self.s.find("]", self.i)
            if end > 0:
                inner = self.s[self.i + 1:end]
                self.i = end + 1
                return _Latex(inner).run()
        return None

    def skip_space(self):
        while self.i < len(self.s) and self.s[self.i] == " ":
            self.i += 1

    def limits(self):
        """_{...} and ^{...} after a big operator -> from{..} to{..}."""
        out = ""
        for _ in range(2):
            self.skip_space()
            if self.i < len(self.s) and self.s[self.i] in "_^":
                kind = "from" if self.s[self.i] == "_" else "to"
                self.i += 1
                out += f" {kind}{{{self.group()}}}"
        return out

    def command(self):
        self.i += 1  # the backslash
        m = re.match(r"[A-Za-z]+", self.s[self.i:])
        if not m:
            c = self.s[self.i] if self.i < len(self.s) else ""
            self.i += 1
            if c in "()[]":
                return ""  # \( \) \[ \] math delimiters
            return f" {_LATEX_SIMPLE.get(c, c)} "
        name = m.group(0)
        self.i += len(name)
        if name in ("frac", "dfrac", "tfrac"):
            a, b = self.group(), self.group()
            return f" {{{a}}} over {{{b}}} "
        if name == "binom":
            a, b = self.group(), self.group()
            return f" binom{{{a}}}{{{b}}} "
        if name == "sqrt":
            n = self.optional()
            body = self.group()
            return f" nroot{{{n}}}{{{body}}} " if n else f" sqrt{{{body}}} "
        if name in _ONE_ARG:
            return f" {_ONE_ARG[name]}{{{self.group()}}} "
        if name in ("text", "textrm", "mbox"):
            return f' "{self.group()}" '
        if name in _PLAIN_ARG:
            return f" {self.group()} " if name not in ("displaystyle", "textstyle") else " "
        if name in _BIG_OPS:
            return f" {_BIG_OPS[name]}{self.limits()} "
        if name in _GREEK:
            return f" %{name.replace('var', '')} "
        if name.lower() in _GREEK and name[0].isupper():
            return f" %{name.upper()} "
        if name in _LATEX_SIMPLE:
            return f" {_LATEX_SIMPLE[name]} "
        return "\\" + name

    def run(self):
        out = []
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == "\\":
                out.append(self.command())
            else:
                out.append(c)
                self.i += 1
        return "".join(out)


def to_starmath(line):
    """One row of model output -> StarMath: LaTeX commands and Unicode math
    symbols converted, math delimiters ($, \\( \\), \\[ \\]) dropped."""
    text = line.strip()
    text = re.sub(r"^\$+|\$+$", "", text).strip()
    for k, v in _UNICODE.items():
        text = text.replace(k, v)
    if "\\" in text:
        text = _Latex(text).run()
    text = text.replace("**", "^").replace("*", " cdot ")
    # half-converted limits: "sum_{i=1}^{n}" or "sum from{i=1}^{n}" -> from{..} to{..}
    text = re.sub(r"\b(sum|prod|int|lim|iint|lint)\s*_\s*(\{[^{}]*\}|\S)",
                  lambda m: f"{m.group(1)} from{{{m.group(2).strip('{}')}}}", text)
    text = re.sub(r"(from\s*\{[^{}]*\})\s*\^\s*(\{[^{}]*\}|\S)",
                  lambda m: f"{m.group(1)} to{{{m.group(2).strip('{}')}}}", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    return re.sub(r"\s+\}", "}", re.sub(r"\{\s+", "{", text))


def formula_problems(row):
    """Reasons a row is probably not valid StarMath ([] if it looks fine)."""
    problems = []
    for open_, close in (("{", "}"), ("(", ")"), ("[", "]")):
        depth = 0
        for c in row:
            depth += (c == open_) - (c == close)
            if depth < 0:
                break
        if depth != 0:
            problems.append(f"unbalanced '{open_}{close}'")
    leftover = re.findall(r"\\[A-Za-z]+", row)
    if leftover:
        problems.append("LaTeX commands left: " + ", ".join(sorted(set(leftover))))
    return problems


def _is_no_formula(text):
    return any(line.strip().strip(".\"'`*_ ").upper() == NO_FORMULA for line in text.split("\n"))


def clean_formula(raw):
    """The formula run's output -> StarMath rows ([] for NO_FORMULA or
    nothing). Code fences and blank lines are dropped; an explicit
    "newline" inside a line also splits rows."""
    text = (raw or "").replace("\r", "")
    if not text.strip() or _is_no_formula(text):
        return []
    rows = []
    for line in text.split("\n"):
        if line.strip().startswith("```") or line.strip() in ("$$", "\\[", "\\]"):
            continue
        converted = to_starmath(line)
        rows += formula_rows(converted)
    return rows


# ---- removals and the AI's version (pure Python) ---------------------------

def numbered_rows(rows):
    return "\n".join(f"[{i}] {r}" for i, r in enumerate(rows, start=1)) or "(the formula is empty)"


def clean_removals(reply, base_rows):
    """The remove run's JSON -> sorted, valid 0-based row indices."""
    items = reply.get("remove") if isinstance(reply, dict) else None
    out = set()
    for item in items if isinstance(items, list) else []:
        try:
            n = int(str(item).strip().strip("[]"))
        except ValueError:
            continue
        if 1 <= n <= len(base_rows):
            out.add(n - 1)
    return sorted(out)


def ai_rows(base_rows, removed, added):
    """The formula as the AI leaves it: base minus the removed rows, with
    the new rows in place of the first removed one (a changed row stays
    where it was), else at the end. New rows that repeat a row that stays
    are dropped (the model was told never to repeat them)."""
    kept = [r for i, r in enumerate(base_rows) if i not in removed]
    added = [r for r in added if r not in kept]
    if removed:
        at = removed[0] - sum(1 for i in removed if i < removed[0])
        return kept[:at] + added + kept[at:]
    return kept + added


# ---- the merge (pure Python, no UNO) --------------------------------------
#
# Unlike Writer, where the AI only inserts, the AI's formula may also remove
# or change rows. So both sides are diffed against the formula as it was at
# send time (base), and every difference becomes an entry:
#   "same"   a base row nobody touched;
#   "add"    a row the AI ("ai") or the user ("user") added;
#   "remove" a base row the AI or the user removed (a changed row is a
#            remove plus an add).
# Keeping every change (the default) gives the AI's changes plus the user's.

def _changes(base, other):
    """-> (removed base indices, {base index: [rows inserted before it]})."""
    removed, inserted = set(), {}
    matcher = difflib.SequenceMatcher(a=base, b=other, autojunk=False)
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op == "equal":
            continue
        removed.update(range(a1, a2))
        if b2 > b1:
            inserted.setdefault(a1, []).extend(other[b1:b2])
    return removed, inserted


def merge_entries(base, ai, current):
    ai_removed, ai_inserted = _changes(base, ai)
    user_removed, user_inserted = _changes(base, current)
    entries = []
    for i in range(len(base) + 1):
        if i < len(base):
            for author, removed in (("ai", ai_removed), ("user", user_removed)):
                if i in removed:
                    entries.append({"text": base[i], "kind": "remove", "author": author, "row": i})
        for author, inserted in (("ai", ai_inserted), ("user", user_inserted)):
            for text in inserted.get(i, []):
                entries.append({"text": text, "kind": "add", "author": author})
        if i < len(base) and i not in ai_removed and i not in user_removed:
            entries.append({"text": base[i], "kind": "same", "author": None, "row": i})
    return entries


def merged_rows(entries, selection):
    """The formula rows for a choice (one bool per entry, True = keep the
    change): a base row stays unless one of its removals is kept."""
    dropped = {e["row"] for e, keep in zip(entries, selection) if e["kind"] == "remove" and keep}
    rows, seen = [], set()
    for e, keep in zip(entries, selection):
        if e["kind"] == "add":
            if keep:
                rows.append(e["text"])
        elif e["row"] not in dropped and e["row"] not in seen:
            seen.add(e["row"])
            rows.append(e["text"])
    return rows


def needs_review(entries):
    """Ask the user when they changed the formula meanwhile, or when the AI
    removes or changes rows that were already there."""
    return any(e["author"] == "user" or (e["author"] == "ai" and e["kind"] == "remove")
               for e in entries)


# ---- the turn ---------------------------------------------------------------

def _read_formula(doc) -> str:
    try:
        return doc.Formula or ""
    except Exception:
        return ""


def prepare_turn(doc, history: list[dict], user_message: str) -> dict:
    """Main thread: read the formula live (this also covers the user
    editing it by hand between turns) and remember it as the base."""
    formula = _read_formula(doc)
    return {
        "host": Settings.load(APP_NAME).ollama_host,
        "user_message": user_message,
        "history": list(history),
        "base_formula": formula,
    }


def build_calls(request, history, rows=None, fix=None, removed=(), added=None):
    """The runs' message lists (exposed for tests): "remove" and "formula"
    always ("formula" told which rows are being removed), "fix" when
    fix=(rows, problems), "reply" once added (the new rows, [] for none)
    is known."""
    base = formula_rows(request["base_formula"])
    shown = "\n".join(base) or "(empty)"
    message = request["user_message"]
    language = _language_line(request.get("language"))
    removing = REMOVING_LINE.format(rows="\n".join(base[i] for i in removed)) if removed else ""
    calls = {
        "remove": [{"role": "system", "content": REMOVE_SYSTEM_PROMPT}] + history + [
            {"role": "user", "content": REMOVE_INSTRUCTION.format(numbered=numbered_rows(base),
                                                                  request=message)}],
        "formula": [{"role": "system", "content": FORMULA_SYSTEM_PROMPT}] + history + [
            {"role": "user", "content": FORMULA_INSTRUCTION.format(formula=shown, removing=removing,
                                                                   request=message)}],
    }
    if fix is not None:
        bad_rows, problems = fix
        calls["fix"] = [{"role": "system", "content": FORMULA_SYSTEM_PROMPT}, {
            "role": "user", "content": FIX_INSTRUCTION.format(formula="\n".join(bad_rows),
                                                              problems="; ".join(problems))}]
    if added is not None:
        changes = ""
        if added:
            changes += REPLY_ADDED.format(rows="\n".join(added))
        if removed:
            changes += REPLY_REMOVED.format(rows="\n".join(base[i] for i in removed))
        if changes:
            instruction = REPLY_WROTE_INSTRUCTION.format(request=message, changes=changes,
                                                         language=language)
        else:
            instruction = REPLY_NONE_INSTRUCTION.format(formula=shown, request=message,
                                                        language=language)
        calls["reply"] = [{"role": "system", "content": REPLY_SYSTEM_PROMPT}] + CAPABILITY_MEMORY \
            + history + [{"role": "user", "content": instruction}]
    return calls


def _all_problems(rows):
    return [f"row {i + 1}: {p}" for i, r in enumerate(rows) for p in formula_problems(r)]


def _ask_turn(request: dict):
    """Background thread, no UNO: remove run, formula run (+ one fix run if
    needed), then the reply run. request["on_phase"](name) reports
    progress and request["on_reply"](reply) shows the reply at once.
    Returns (result, model_name); result has "message", "rows" (the whole
    formula as the AI leaves it), "added", "removed", "notes"."""
    host = request["host"]
    cancel = request.get("cancel")
    timeout = request.get("timeout", DEFAULT_TIMEOUT)
    phase = request.get("on_phase") or (lambda name: None)
    model, params = best_model(host)
    if cancel is not None:
        cancel.check()
    options = {"num_ctx": MEMORY_NUM_CTX} if has_memory(params) else None
    history = fit_history([{"role": "system", "content": ""}] + request["history"]
                          + [{"role": "user", "content": request["user_message"]}], params)[1:-1]
    base = formula_rows(request["base_formula"])

    phase("planning")
    removed = []
    if base:
        try:
            removed = clean_removals(chat_json(host, model, build_calls(request, history)["remove"],
                                               timeout, options, cancel), base)
        except Cancelled:
            raise
        except OllamaError:
            traceback.print_exc()  # no removals this turn

    phase("math_drafting")
    raw = chat(host, model, build_calls(request, history, removed=removed)["formula"], timeout,
               options=options, cancel=cancel, allow_empty=True)
    added = clean_formula(raw)
    notes = []
    problems = _all_problems(added)
    if problems:
        phase("math_fixing")
        fixed_raw = chat(host, model, build_calls(request, history, fix=(added, problems))["fix"],
                         timeout, options=options, cancel=cancel, allow_empty=True)
        fixed = clean_formula(fixed_raw)
        if fixed and len(_all_problems(fixed)) < len(problems):
            added, problems = fixed, _all_problems(fixed)
            notes.append("your first formula was not valid StarMath, so you corrected it")
        if problems:
            notes.append("the formula may still be invalid: " + "; ".join(problems))
    rows = ai_rows(base, removed, added)
    added = [r for r in added if r in rows]

    phase("replying")
    reply = chat(host, model, build_calls(request, history, removed=removed, added=added)["reply"],
                 timeout, options=options, cancel=cancel, allow_empty=True).strip()
    if request.get("on_reply") and reply:
        request["on_reply"](reply)
    return {"message": reply, "rows": rows if (added or removed) else [], "added": added,
            "removed": [base[i] for i in removed], "raw": raw, "notes": notes,
            "actions": [], "_model_params": params}, model


def apply_turn(doc, request: dict, result: dict, model: str, resolve_merge=None,
               confirm_changes=None):
    """Main thread: put the suggested formula into the document. If the
    user changed the formula meanwhile, or the AI removes/changes existing
    rows, resolve_merge(view) decides (view: "base", "ai", "current",
    "user_base", "entries", "keys"; it returns one bool per entry, or None
    to keep the formula as it is now). Pure additions are applied directly.
    With no resolver everything is kept.

    Returns (chat_text, wrote, model_name, confirmation)."""
    chat_text = result.get("message") or "(no reply)"
    ai = list(result.get("rows") or [])
    notes = list(result.get("notes") or [])
    if not result.get("added") and not result.get("removed") and not ai:
        return chat_text, False, model, None
    base = formula_rows(request["base_formula"])
    current_formula = _read_formula(doc)
    current = formula_rows(current_formula)
    entries = merge_entries(base, ai, current)
    selection = [True] * len(entries)
    if needs_review(entries) and resolve_merge is not None:
        chosen = resolve_merge({
            "base": base, "ai": ai, "current": current, "user_base": base, "entries": entries,
            "changes_only": True,
            "keys": {"merge_intro": "merge_intro_math", "head_ai_added": "head_ai_changed",
                     "merge_mixed": "merge_mixed_math", "merge_ai_only": "merge_ai_only_math"},
        })
        # closing the dialog changes nothing: the formula stays as it is now
        selection = list(chosen) if chosen is not None else [e["author"] != "ai" for e in entries]
        result["_merged"] = True
    rows = merged_rows(entries, selection)
    if rows != current:
        doc.Formula = join_rows(rows)
    kept_ai = sum(1 for e, s in zip(entries, selection) if e["author"] == "ai" and s)
    total_ai = sum(1 for e in entries if e["author"] == "ai")
    wrote = rows != current
    parts = []
    if result.get("added"):
        parts.append("you suggested adding: " + " | ".join(result["added"]))
    if result.get("removed"):
        parts.append("you suggested removing: " + " | ".join(result["removed"]))
    parts.append(f"the formula is now: {' newline '.join(rows) or '(empty)'}" if wrote
                 else "the formula was not changed")
    if result.get("_merged"):
        parts.append(f"the user reviewed it and kept {kept_ai} of your {total_ai} change(s)")
    if notes:
        parts.append("; ".join(notes))
    return chat_text, wrote, model, "Formula update: " + "; ".join(parts) + "."


def chat_turn(doc, history: list[dict], user_message: str):
    """Synchronous prepare -> ask -> apply, for tests over a UNO socket."""
    request = prepare_turn(doc, history, user_message)
    result, model = ask_turn(request)
    return apply_turn(doc, request, result, model)


def forget(doc):
    """Clear chat forgets the conversation only (the panel's history);
    CAPABILITY_MEMORY and the system prompts always stay."""


# ---- explain ----------------------------------------------------------------

def prepare_explain(doc) -> dict:
    """Main thread: read the formula that is in the document right now."""
    return {"host": Settings.load(APP_NAME).ollama_host,
            "rows": formula_rows(_read_formula(doc))}


def ask_explain(request: dict):
    """Background thread, no UNO: explain request["rows"] (no chat history:
    the explanation is about the sheet, not the conversation). Returns
    (explanation, model_name); ("", "") when the formula is empty."""
    if not request["rows"]:
        return "", ""
    model, _params = best_model(request["host"])
    reply = chat(request["host"], model, [
        {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
        {"role": "user", "content": EXPLAIN_INSTRUCTION.format(
            formula="\n".join(request["rows"]), language=_language_line(request.get("language")))},
    ], timeout=request.get("timeout", DEFAULT_TIMEOUT), cancel=request.get("cancel"))
    return reply.strip(), model


def ask_turn(request: dict):
    """_ask_turn plus SKAI_ Memory: what other SKAI apps know goes in, the exchange is recorded."""
    from sk_office_ai.skai_memory_bridge import around_turn
    return around_turn("sk_office_math", request, _ask_turn)
