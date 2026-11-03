"""Chat-turn and summarize logic for the Writer AI sidebar, kept free of any
awt/UI dependency so it can be exercised directly (e.g. over a UNO socket
connection) without simulating button clicks.

Every turn calls the model THREE separate times, each with its own system
prompt, the chat history, and one user message holding the document's
current text, the request and the instruction:

  1. text   - write ONLY the new text to add to the document, directly in
              Markdown (or NO_TEXT). This call STREAMS: as the words arrive
              they are written into the document at the user's cursor in
              light grey (provisional), and the user can keep editing
              everything - including the grey text - while it is written.
  2. reply  - the chat reply, told exactly what step 1 wrote (or that
              nothing was written), so it describes it instead of trying to
              write the text into the chat itself.
  3. format - given the text just generated (paragraphs numbered), return
              ONLY a JSON object with per-paragraph formatting.

When the turn ends, the plugin compares, line by line, the area around the
cursor as it was when the prompt was sent, as the AI would have left it,
and as it is now. If the user changed anything there meanwhile, the panel
asks which version to keep (AI text + the user's changes, AI text only, or
change by change); then the grey goes away and the AI's paragraphs get
their real formatting (headings, lists, tables, bold/italic, the format
step's styles). The model only ever ADDS text; it cannot remove or change
what is already in the document.
"""

from __future__ import annotations

import difflib
import json
import re
import time
import traceback

import uno

from com.sun.star.text.ControlCharacter import PARAGRAPH_BREAK

from sk_libre_ai.config import Settings
from sk_libre_ai.json_chat import (
    DEFAULT_TIMEOUT, LANGUAGE_NAMES, MEMORY_NUM_CTX, chat_json, fit_history, has_memory,
)
from sk_libre_ai.model_select import best_model, pick_best_model
from sk_libre_ai.ollama_client import OllamaError, Cancelled, chat, chat_stream

APP_NAME = "sk_libre-writer"

DOCUMENT_SNAPSHOT_MAX_CHARS = 4000

NO_TEXT = "NO_TEXT"

# Colour of the AI's text while it is still being written (provisional).
PENDING_COLOR = 0xB4B4B4

# The streamed text is pushed to the document at most this often (seconds),
# and on every new line.
STREAM_INTERVAL = 0.15

# ---- the three runs' prompts ---------------------------------------------
#
# Measured on qwen2.5:0.5b (2026-10-14): with the chat reply asked FIRST,
# the model never understood that the text goes into the document in a
# later step - it wrote the poem/list into the chat, or said it couldn't
# write. Asking for the document text FIRST and then telling the reply
# step exactly what was written fixed that. Each run gets one user message
# holding the document, the request and the instruction (a separate
# numbered document message got copied into the output).

TEXT_SYSTEM_PROMPT = (
    "You write text that the plugin inserts directly into the user's LibreOffice "
    "Writer document, at their cursor. You answer with ONLY the new text, in "
    "Markdown: '# ' for a title, '- ' for list items, '| a | b |' for table rows, "
    "**bold**, *italic*; every line becomes its own paragraph. No explanations, "
    "no code fences, and never repeat text already in the document."
)

TEXT_INSTRUCTION = (
    "{document}\n\nUser's request: {request}\n\n"
    "Write ONLY the new text to add to the document for this request, in Markdown, "
    "in the language the user asks for (otherwise in the language of the request). "
    "Deleting or restyling text that is already in the document is done in another "
    "step, not here. "
    f"If the request does not ask for new text in the document (for example it is "
    f"just a question, or it only asks to delete or restyle existing text), answer "
    f"exactly {NO_TEXT}"
)

REPLY_SYSTEM_PROMPT = (
    "You are the chat assistant of a LibreOffice Writer side panel. You can write "
    "into the user's document: the plugin writes the text for each request into "
    "the document for you (in Markdown, turned into real formatting), before this "
    "chat reply. You can also delete lines or words and change the font of any "
    "text already in the document, whoever wrote it (the user confirms deletions). "
    "Reply in plain text only: no JSON, no Markdown."
)

REPLY_WROTE_INSTRUCTION = (
    "User's message: {request}\n\n"
    "You have just added this text to the document:\n\"\"\"\n{markdown}\n\"\"\"\n"
    "{edits}\n"
    "Now tell the user, in ONE short sentence, what you did in the document. "
    "Do not repeat the text. {language}"
)

REPLY_EDITS_INSTRUCTION = (
    "User's message: {request}\n\n{edits}\n"
    "Now tell the user, in ONE short sentence, what you are changing in the "
    "document. {language}"
)

REPLY_NONE_INSTRUCTION = (
    "{document}\n\nUser's message: {request}\n\n"
    "Nothing was added to or changed in the document for this message. Answer "
    "the user briefly. {language}"
)

REPLY_EDITS_LINE = "You are also making these changes to text already in the document:\n{items}"

# Run 0: changes to text that is ALREADY in the document - whoever wrote it.
# The document is shown numbered here (the numbers are how the model says
# which line), and the reply is JSON (Ollama's constrained JSON output).
EDIT_SYSTEM_PROMPT = (
    "You edit a LibreOffice Writer document for the user. You see its CURRENT "
    "text, one line per paragraph, numbered [1], [2], ... (the numbers are not "
    "part of the text). It does not matter who wrote a line, the user or you: "
    "you can delete it or change its font if the user asks.\n\n"
    "Reply with ONLY one JSON object: {\"edits\": [...]}, where each edit is one of:\n"
    '{"type": "delete_line", "line": 3}  - delete line [3] entirely\n'
    '{"type": "delete_words", "line": 3, "text": "..."}  - delete these words from '
    'line [3] ("text" copied exactly from that line)\n'
    '{"type": "set_font", "line": 3, "text": "...", "font": "Arial", "size": 14, '
    '"bold": true, "italic": false, "underline": false, "color": "#rrggbb"}  - change '
    'how text looks. Every field is optional: leave out "line" to change the whole '
    'document, leave out "text" to change the whole line, give ONLY the fields the '
    'user asked for\n'
    '{"type": "reset_font", "line": 3}  - remove manual formatting (leave out "line" '
    "for the whole document)\n\n"
    "Only delete or restyle what the user asks for. If the request does not ask to "
    'delete or restyle existing text (e.g. it asks for NEW text, or it is a question), '
    'reply {"edits": []}.\n\n'
    "Examples, for a document with [1] Shopping list, [2] Buy milk and bread, [3] Call Anna:\n"
    'user: delete the last line -> {"edits": [{"type": "delete_line", "line": 3}]}\n'
    'user: remove the word bread -> {"edits": [{"type": "delete_words", "line": 2, "text": "and bread"}]}\n'
    'user: make the title bold and 16 pt -> {"edits": [{"type": "set_font", "line": 1, "bold": true, "size": 16}]}\n'
    'user: make the whole document bold -> {"edits": [{"type": "set_font", "bold": true}]}\n'
    'user: put the whole document in Arial -> {"edits": [{"type": "set_font", "font": "Arial"}]}\n'
    'user: write a poem about the sea -> {"edits": []}'
)

EDIT_INSTRUCTION = (
    "Current document text:\n{numbered}\n\nUser's request: {request}\n\n"
    'Reply with ONLY the JSON {{"edits": [...]}} for this request.'
)

# A fixed opening exchange put before the chat history of the reply run
# (measured: in the text run it made the 0.5B model worse). It is NOT part of the conversation: Clear chat forgets the
# conversation but never this, so the model always "remembers" that it
# can write into the document.
CAPABILITY_MEMORY = [
    {"role": "user", "content": "Can you write into my document?"},
    {"role": "assistant", "content": (
        "Yes. I write the text you ask for directly into your document, at your "
        "cursor, in Markdown that becomes real formatting. It appears in light grey "
        "while I write it, and you review it at the end. I can also delete lines or "
        "words and change the font of anything already in the document, whether you "
        "or I wrote it - you confirm deletions first.")},
]

FORMAT_SYSTEM_PROMPT = (
    "You format text for a LibreOffice Writer document. The last message contains "
    "the NEW text that is about to be added, with its paragraphs numbered [1], [2], "
    "... Its structure (headings, lists, tables, bold, italic) is already set by "
    "its Markdown.\n\n"
    "Your ONLY task: decide extra formatting for the new paragraphs - what the "
    "user asked for (a font, a size, a colour, centred text, ...) or what clearly "
    "fits (e.g. a centred title). Reply with ONLY one JSON object:\n"
    '{"paragraphs": [{"number": 1, "font": "...", "size": 14, "bold": true, '
    '"italic": false, "underline": false, "color": "#rrggbb", "align": "center"}]}\n'
    '"align" is one of left, center, right, justify. List only the paragraphs '
    "that need formatting, with only the fields to set. If nothing needs extra "
    'formatting, reply {"paragraphs": []}.\n\n'
    "Examples (shape only):\n"
    "user asked 'write a title and a paragraph, title centred and blue': "
    '{"paragraphs": [{"number": 1, "align": "center", "color": "#1f4e9c"}]}\n'
    "user asked 'write a poem in Arial 14': "
    '{"paragraphs": [{"number": 1, "font": "Arial", "size": 14}, '
    '{"number": 2, "font": "Arial", "size": 14}, {"number": 3, "font": "Arial", "size": 14}]}\n'
    "user asked 'write a short paragraph about cats': "
    '{"paragraphs": []}'
)

FORMAT_INSTRUCTION = (
    "User's request: {request}\n\n"
    "This is the new text that is about to be added to the document, one "
    "paragraph per line:\n---\n{numbered}\n---\n"
    "Reply with ONLY the formatting JSON for these paragraphs."
)

SUMMARIZE_SYSTEM_PROMPT = (
    "You are an AI assistant embedded in LibreOffice Writer. Summarize the "
    "document text the user gives you, concisely, in the same language as "
    "the document. Reply with only the summary, no preamble."
)


def _reply_language_line(lang):
    return f"Write your reply in {LANGUAGE_NAMES.get(lang or 'en', 'English')}."


# ---- styles ---------------------------------------------------------------

# Programmatic style names: the same in every UI language (display names
# like "Default Paragraph Style" are localized and are NOT accepted here -
# setting one fails silently).
DEFAULT_STYLE = "Standard"
BULLET_LIST_STYLE = "List 1"        # the "Bullet •" list style
NUMBERED_LIST_STYLE = "Numbering 123"
QUOTE_STYLE = "Quotations"
CODE_FONT = "Liberation Mono"
_CHAR_PROPS = ("CharFontName", "CharHeight", "CharWeight", "CharPosture",
               "CharUnderline", "CharColor")
_ALIGN = {"left": "LEFT", "right": "RIGHT", "center": "CENTER", "centre": "CENTER",
          "justify": "BLOCK", "justified": "BLOCK"}


def _set_style(cursor, style_name, numbering=""):
    try:
        cursor.ParaStyleName = style_name
        cursor.NumberingStyleName = numbering
    except Exception:
        traceback.print_exc()


def _set_kind_style(cursor, kind, level=1):
    """Paragraph style for a Markdown line kind."""
    if kind == "heading":
        _set_style(cursor, f"Heading {min(level or 1, 6)}")
    elif kind == "bullet":
        _set_style(cursor, DEFAULT_STYLE, BULLET_LIST_STYLE)
    elif kind == "numbered":
        _set_style(cursor, DEFAULT_STYLE, NUMBERED_LIST_STYLE)
    elif kind == "quote":
        _set_style(cursor, QUOTE_STYLE)
    else:
        _set_style(cursor, DEFAULT_STYLE)


_FONT_CACHE: list[str] = []


def installed_fonts(doc) -> list[str]:
    """Font family names LibreOffice can use on this machine (cached)."""
    if not _FONT_CACHE:
        try:
            window = doc.getCurrentController().getFrame().getContainerWindow()
            device = window.getToolkit().createScreenCompatibleDevice(1, 1)
            _FONT_CACHE.extend(sorted({fd.Name for fd in device.getFontDescriptors() if fd.Name}))
        except Exception:
            traceback.print_exc()
    return list(_FONT_CACHE)


def _font_properties(style, fonts):
    """-> ([(property, value)], [notes]) for one paragraph's formatting."""
    props, notes = [], []
    font = style.get("font")
    if font:
        match = next((f for f in fonts if f.lower() == str(font).lower()), None)
        if fonts and match is None:
            notes.append(f"font '{font}' is not installed here, so LibreOffice shows a substitute")
        props.append(("CharFontName", match or str(font)))
    if style.get("size") is not None:
        try:
            size = float(style["size"])
            if 1 <= size <= 400:
                props.append(("CharHeight", size))
            else:
                notes.append(f"size {size} ignored (must be 1-400)")
        except (TypeError, ValueError):
            notes.append(f"size {style['size']!r} ignored (not a number)")
    if style.get("bold") is not None:
        props.append(("CharWeight", 150.0 if style["bold"] else 100.0))
    if style.get("italic") is not None:
        props.append(("CharPosture", uno.Enum("com.sun.star.awt.FontSlant",
                                              "ITALIC" if style["italic"] else "NONE")))
    if style.get("underline") is not None:
        props.append(("CharUnderline", 1 if style["underline"] else 0))
    color = str(style.get("color") or "").strip().lstrip("#")
    if color:
        if re.fullmatch(r"[0-9a-fA-F]{6}", color):
            props.append(("CharColor", int(color, 16)))
        else:
            notes.append(f"color {style['color']!r} ignored (use #rrggbb)")
    return props, notes


# ---- Markdown -> lines -> blocks (pure Python, no UNO) --------------------
#
# The text is parsed one line at a time, so the SAME parser serves the
# finished text and the text still streaming in: every complete Markdown
# line that produces a paragraph becomes one "line" (with the plain text
# that paragraph shows), and consecutive table rows are grouped into one
# table block at the end.

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_RULE_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_NUMBER_PREFIX_RE = re.compile(r"^\s*\[\d+\]\s+")
_FENCE_RE = re.compile(r"^\s*```\s*([\w-]*)\s*$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]*)\)")
_INLINE_RE = re.compile(
    r"\*\*\*(.+?)\*\*\*"            # 1 bold+italic
    r"|\*\*(.+?)\*\*"                # 2 bold
    r"|__(.+?)__"                    # 3 bold
    r"|\*(?!\s)(.+?)(?<!\s)\*"       # 4 italic
    r"|(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)"  # 5 italic
    r"|`([^`]+)`"                    # 6 code
)
# Partial-line prefixes: while the line written so far still matches one of
# these, it may yet become a heading/list/table/fence/rule, so nothing of
# it is shown.
_HOLD_RE = re.compile(r"^\s*(\[\d*\]?|(\[\d+\]\s+)?(#{1,6}|\d+[.)]?)|[-*_\s]+)$")
_PARTIAL_PREFIX_RES = (
    ("heading", re.compile(r"^(#{1,6})\s+")),
    ("bullet", re.compile(r"^\s*[-*+]\s+")),
    ("numbered", re.compile(r"^\s*\d+[.)]\s+")),
    ("quote", re.compile(r"^\s*>\s?")),
)


def parse_inline(text):
    """-> [(text, bold, italic, code)] runs for one line of Markdown."""
    text = _LINK_RE.sub(r"\1", text)
    runs, pos = [], 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            runs.append((text[pos:m.start()], False, False, False))
        if m.group(1):
            runs.append((m.group(1), True, True, False))
        elif m.group(2) or m.group(3):
            runs.append((m.group(2) or m.group(3), True, False, False))
        elif m.group(4) or m.group(5):
            runs.append((m.group(4) or m.group(5), False, True, False))
        else:
            runs.append((m.group(6), False, False, True))
        pos = m.end()
    if pos < len(text):
        runs.append((text[pos:], False, False, False))
    return [r for r in runs if r[0]]


def _plain(runs):
    return "".join(r[0] for r in runs)


def _table_cells(line):
    cells = line.strip()
    if cells.startswith("|"):
        cells = cells[1:]
    if cells.endswith("|"):
        cells = cells[:-1]
    return [_plain(parse_inline(c.strip())) for c in cells.split("|")]


def _is_no_text(text):
    """The model said there is nothing to add: its whole reply, or any line
    of it, is NO_TEXT (it sometimes writes a stray line before it)."""
    return any(line.strip().strip(".\"'`*_ ").upper() == NO_TEXT for line in text.split("\n"))


def clean_markdown(markdown):
    """The model's text step output, without a wrapping code fence; '' for
    NO_TEXT or nothing."""
    text = (markdown or "").strip()
    fence = re.match(r"^```[\w-]*\n(.*?)\n?```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text or _is_no_text(text):
        return ""
    return text


def _new_parse_state():
    return {"in_code": False, "wrapped": False, "seen": False, "group": 0}


def _parse_line(raw, state):
    """One complete Markdown line -> a line dict ({"kind", "text", "runs" |
    "cells", "level"}), or None if it makes no paragraph (blank line, rule,
    fence, table separator, a "```markdown" wrapper the model put around
    everything). Updates the parse state."""
    raw = raw.rstrip()
    fence = _FENCE_RE.match(raw)
    first = not state["seen"] and bool(raw.strip())
    if raw.strip():
        state["seen"] = True
    if fence:
        lang = fence.group(1).lower()
        if state["in_code"]:
            state["in_code"] = False
        elif first and lang in ("", "markdown", "md"):
            state["wrapped"] = True
        elif state["wrapped"] and not lang:
            state["wrapped"] = False
        else:
            state["in_code"] = True
        state["group"] += 1
        return None
    if state["in_code"]:
        return {"kind": "code", "text": raw, "runs": [(raw, False, False, True)] if raw else []}
    line = _NUMBER_PREFIX_RE.sub("", raw)
    if not line.strip() or _RULE_RE.match(line):
        state["group"] += 1
        return None
    if line.strip().startswith("|"):
        if _TABLE_SEP_RE.match(line):
            return None
        cells = _table_cells(line)
        return {"kind": "row", "cells": cells, "text": " | ".join(cells), "group": state["group"]}
    state["group"] += 1
    m = _HEADING_RE.match(line)
    if m:
        kind, level, runs = "heading", len(m.group(1)), parse_inline(m.group(2))
    else:
        level = 0
        for kind, regex in (("bullet", _BULLET_RE), ("numbered", _NUMBERED_RE), ("quote", _QUOTE_RE)):
            m = regex.match(line)
            if m:
                runs = parse_inline(m.group(1))
                break
        else:
            kind, runs = "paragraph", parse_inline(line.strip())
    if not runs:
        return None
    return {"kind": kind, "level": level, "runs": runs, "text": _plain(runs)}


def _partial_line(raw, state):
    """The line still being written -> {"kind", "level", "text"} with the
    text to show so far, or None to show nothing yet. The shown text only
    ever grows as the line grows ('*' and '`' markers are never shown; the
    line is set to its exact final text once it is complete)."""
    s = raw.lstrip()
    if state["in_code"]:
        return None if s.startswith("`") else {"kind": "code", "level": 0, "text": raw}
    if not s or s[0] in "|`" or _HOLD_RE.match(raw):
        return None
    line = _NUMBER_PREFIX_RE.sub("", raw)
    kind, level, content = "paragraph", 0, line.lstrip()
    for name, regex in _PARTIAL_PREFIX_RES:
        m = regex.match(line)
        if m:
            kind, content = name, line[m.end():].lstrip()
            if name == "heading":
                level = len(m.group(1))
            break
    return {"kind": kind, "level": level, "text": content.replace("*", "").replace("`", "")}


def markdown_lines(markdown, final=True, skip=frozenset()):
    """-> (lines, partial): the paragraph-making lines of the complete
    Markdown lines so far, and the line still being written (or None).
    final=True treats the last line as complete too. Lines whose text is in
    `skip` (the document's existing paragraphs: the small model often
    starts by copying them, against its instructions) are dropped, and a
    line still being written that could become one is not shown yet."""
    text = (markdown or "").replace("\r", "")
    stripped = text.strip()
    if not stripped or _is_no_text(text):
        return [], None
    last_line = text.split("\n")[-1].strip().upper()
    if not final and last_line and NO_TEXT.startswith(last_line):
        lines, _partial = markdown_lines(text[:len(text) - len(text.split("\n")[-1])], final=False,
                                         skip=skip)
        return lines, None  # the line being written might still turn out to be NO_TEXT
    raw_lines = text.split("\n")
    pending = None
    if not final:
        pending = raw_lines.pop()
    state = _new_parse_state()
    lines = [line for line in (_parse_line(raw, state) for raw in raw_lines)
             if line and line["text"].strip() not in skip]
    partial = _partial_line(pending, state) if pending is not None else None
    if partial is not None and any(d.startswith(partial["text"].strip()) for d in skip):
        partial = None
    return lines, partial


def blocks_from_lines(lines):
    """Lines -> blocks: one block per line, except that consecutive table
    rows become one "table" block (headers + rows). Every block lists the
    indices of its lines in "lines"."""
    blocks = []
    for i, line in enumerate(lines):
        if line["kind"] == "row":
            prev = blocks[-1] if blocks else None
            if (prev is not None and prev["kind"] == "table"
                    and lines[prev["lines"][-1]].get("group") == line.get("group")
                    and prev["lines"][-1] == i - 1):
                prev["rows"].append(line["cells"])
                prev["lines"].append(i)
            else:
                blocks.append({"kind": "table", "headers": line["cells"], "rows": [], "lines": [i]})
            continue
        block = {"kind": line["kind"], "runs": line.get("runs") or [], "lines": [i]}
        if line["kind"] == "heading":
            block["level"] = line["level"]
        blocks.append(block)
    return blocks


def parse_markdown(markdown, skip=frozenset()):
    """Markdown -> blocks: dicts with "kind" (heading, paragraph, bullet,
    numbered, quote, code, table) plus "level" (headings), "runs" (inline
    text runs) or "headers"/"rows" (tables). Every non-empty line is its own
    paragraph; a "[n] " prefix copied from the numbered snapshot is dropped."""
    return blocks_from_lines(markdown_lines(markdown, final=True, skip=skip)[0])


def block_line(block):
    """One-line plain rendering of a block (the numbered list for the format
    step)."""
    kind = block["kind"]
    if kind == "table":
        parts = [" | ".join(block["headers"])] + [" | ".join(r) for r in block["rows"]]
        return "table: " + " / ".join(parts)
    text = _plain(block.get("runs") or [])
    if kind == "bullet":
        return f"• {text}"
    return text


def parse_styles(reply, n_blocks):
    """The format step's JSON -> {block number: style dict}. Anything
    malformed is skipped (formatting is optional)."""
    items = reply.get("paragraphs") if isinstance(reply, dict) else None
    if items is None and isinstance(reply, dict):
        items = reply.get("styles")
    styles = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            number = int(str(item.get("number", "")).strip().strip("[]"))
        except ValueError:
            continue
        if 1 <= number <= n_blocks:
            styles[number] = {k: v for k, v in item.items() if k != "number"}
    return styles


# ---- edits to existing text (pure Python part) ----------------------------

EDIT_TYPES = ("delete_line", "delete_words", "set_font", "reset_font")
_STYLE_KEYS = ("font", "size", "bold", "italic", "underline", "color")


def numbered_document(texts, max_chars=DOCUMENT_SNAPSHOT_MAX_CHARS):
    """The document for the edit run: "[n] text" per body element (tables
    and empty lines labelled); a long one is cut in the middle."""
    lines = []
    for i, t in enumerate(texts, start=1):
        if t is None:
            lines.append(f"[{i}] (table)")
        elif not t.strip():
            lines.append(f"[{i}] (empty line)")
        else:
            lines.append(f"[{i}] {t}")
    if not lines or all(t is None or not t.strip() for t in texts):
        return "(The document is empty.)"
    body = "\n".join(lines)
    if len(body) > max_chars:
        half = max_chars // 2
        head = body[:half].rsplit("\n", 1)[0]
        tail = body[-half:].split("\n", 1)[-1]
        body = f"{head}\n[... some lines omitted ...]\n{tail}"
    return body


def _find_all(haystack, needle):
    """Offsets of every occurrence (exact case first, else ignoring case:
    small models re-case what they quote)."""
    for a, b in ((haystack, needle), (haystack.lower(), needle.lower())):
        found, start = [], 0
        while needle:
            k = a.find(b, start)
            if k < 0:
                break
            found.append(k)
            start = k + len(b)
        if found:
            return found
    return []


def _line_number(edit):
    try:
        return int(str(edit.get("line", "")).strip().strip("[]"))
    except ValueError:
        return None


def clean_edits(reply, base_body):
    """The edit run's JSON -> (valid edits, notes about dropped ones).
    Checked against the document as it was sent to the model."""
    items = reply.get("edits") if isinstance(reply, dict) else None
    edits, notes = [], []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("type") not in EDIT_TYPES:
            continue
        edit = {"type": item["type"]}
        if item.get("line") not in (None, ""):
            n = _line_number(item)
            if n is None or not 1 <= n <= len(base_body) or base_body[n - 1] is None:
                notes.append(f"{item['type']}: there is no text line [{item.get('line')}]")
                continue
            edit["line"] = n
        text = str(item.get("text") or "")
        text = _NUMBER_PREFIX_RE.sub("", text).strip()
        if text:
            edit["text"] = text
        if edit["type"] == "delete_line" and "line" not in edit:
            notes.append("delete_line without a line number")
            continue
        if edit["type"] == "delete_words":
            if not text:
                notes.append("delete_words without words")
                continue
            where = [base_body[edit["line"] - 1]] if "line" in edit else [t for t in base_body if t]
            if not any(_find_all(t, text) for t in where):
                notes.append(f"delete_words: '{text}' is not in the document")
                continue
        if edit["type"] == "set_font":
            for key in _STYLE_KEYS:
                if item.get(key) not in (None, ""):
                    edit[key] = item[key]
            if not any(k in edit for k in _STYLE_KEYS):
                notes.append("set_font with nothing to change")
                continue
        if "text" in edit and edit["type"] in ("set_font", "reset_font"):
            where = [base_body[edit["line"] - 1]] if "line" in edit else [t for t in base_body if t]
            if not any(_find_all(t, text) for t in where):
                edit.pop("text")  # restyle the whole line / document instead
        edits.append(edit)
    return edits, notes


def describe_edit(edit, base_body):
    """English description of an edit, for the model's reply step and the
    history."""
    line = edit.get("line")
    where = (f"line [{line}] ('{_snip(base_body[line - 1])}')" if line else "the whole document")
    kind = edit["type"]
    if kind == "delete_line":
        return f"delete {where}"
    if kind == "delete_words":
        return f"delete the words '{edit['text']}' from " + (where if line else "the document")
    if kind == "reset_font":
        return f"remove the manual formatting of {where}"
    fields = ", ".join(f"{k}={edit[k]}" for k in _STYLE_KEYS if k in edit)
    target = f"'{edit['text']}' in {where}" if edit.get("text") else where
    return f"set {fields} on {target}"


def _snip(text, n=40):
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[:n - 3] + "..."


# ---- the merge: who changed which line (pure Python, no UNO) --------------
#
# Three versions of the area around the cursor (the "window"):
#   base    - as it was when the prompt was sent (the paragraph holding the
#             cursor is shown split at the cursor, where the AI writes);
#   ai      - base with the AI's lines inserted at the cursor;
#   current - as it is now, after the user's edits while the AI wrote.
# merge_entries() turns ai -> current into one ordered list of entries:
#   "same"   an untouched line of the user's;
#   "add"    a line the AI added (author "ai") or the user added ("user");
#   "remove" a line the user removed (one of theirs or one of the AI's).
# Every add/remove is a change that can be kept (the default: the result
# is then exactly the current document) or dropped.

def ai_version(base_paragraphs, base_index, base_offset, ai_texts):
    """-> [{"text", "origin", ...}]: origin "base" (a window paragraph, with
    its "style"), "ai" (with "line", the AI line index), or
    "frag_before"/"frag_after" (the two halves of the cursor's paragraph
    when the AI wrote in the middle of it)."""
    base = list(base_paragraphs) or [("", DEFAULT_STYLE)]
    index = min(base_index, len(base) - 1)
    anchor_text, anchor_style = base[index]
    before, after = anchor_text[:base_offset], anchor_text[base_offset:]
    split = bool(before) and bool(after)
    out = [{"text": t, "origin": "base", "style": s} for t, s in base[:index]]
    if before:
        out.append({"text": before, "origin": "frag_before" if split else "base", "style": anchor_style})
    out += [{"text": t, "origin": "ai", "line": k} for k, t in enumerate(ai_texts)]
    if after:
        out.append({"text": after, "origin": "frag_after" if split else "base", "style": anchor_style})
    out += [{"text": t, "origin": "base", "style": s} for t, s in base[index + 1:]]
    return out


def base_split_texts(version):
    """The base lines as the merge compares them: ai_version() minus the
    AI's lines (so the cursor's paragraph shows split, like in the others)."""
    return [v["text"] for v in version if v["origin"] != "ai"]


def merge_entries(version, current_texts):
    entries = []
    matcher = difflib.SequenceMatcher(a=[v["text"] for v in version], b=current_texts,
                                      autojunk=False)
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op == "equal":
            for k in range(a2 - a1):
                src = version[a1 + k]
                is_ai = src["origin"] == "ai"
                entries.append({"text": src["text"], "kind": "add" if is_ai else "same",
                                "author": "ai" if is_ai else None, "src": src})
            continue
        for src in version[a1:a2]:
            entries.append({"text": src["text"], "kind": "remove", "author": "user", "src": src})
        for b in range(b1, b2):
            entries.append({"text": current_texts[b], "kind": "add", "author": "user", "src": None})
    return entries


def user_changed(entries):
    return any(e["author"] == "user" for e in entries)


def selection_for(entries, keep_ai=True, keep_user=True):
    """Which changes to keep: one bool per entry ("same" entries: True)."""
    return [True if e["kind"] == "same" else (keep_ai if e["author"] == "ai" else keep_user)
            for e in entries]


def merged_entries(entries, selection):
    """The entries that make up the result, in order."""
    return [e for e, keep in zip(entries, selection)
            if e["kind"] == "same" or (e["kind"] == "add") == bool(keep)]


def selection_ops(entries, selection):
    """Document edits turning the current window into the chosen result:
    [("del", index)] and [("ins", index, entry)] (insert before current
    paragraph `index`), in document order - run them in REVERSE."""
    ops, pos = [], 0
    for entry, keep in zip(entries, selection):
        if entry["kind"] == "remove":
            if not keep:
                ops.append(("ins", pos, entry))
            continue
        if entry["kind"] == "add" and not keep:
            ops.append(("del", pos))
        pos += 1
    return ops


# ---- document helpers -----------------------------------------------------

def _body_elements(doc):
    """Paragraphs (and tables) of the main text, in order."""
    elements = []
    enum = doc.getText().createEnumeration()
    while enum.hasMoreElements():
        elements.append(enum.nextElement())
    return elements


def _is_paragraph(element):
    return element.supportsService("com.sun.star.text.Paragraph")


def _body_texts(doc):
    """One entry per body element: the paragraph's text, or None for a
    table (keeps indices aligned with _body_elements)."""
    return [e.getString() if _is_paragraph(e) else None for e in _body_elements(doc)]


def _document_text(doc, max_chars=DOCUMENT_SNAPSHOT_MAX_CHARS):
    """The document's text, live, one paragraph per line (tables as
    "(table)"); a long document is cut in the MIDDLE so its end stays
    visible. Not numbered: the small model copied numbered listings into
    its own output."""
    texts = ["(table)" if t is None else t for t in _body_texts(doc)]
    if not any(t.strip() for t in texts):
        return "(The document is empty.)"
    body = "\n".join(texts)
    if len(body) > max_chars:
        half = max_chars // 2
        head = body[:half].rsplit("\n", 1)[0]
        tail = body[-half:].split("\n", 1)[-1]
        body = f"{head}\n[... part of the document omitted ...]\n{tail}"
    return body


# ---- the cursor window ------------------------------------------------------

# Paragraphs of context on each side of the insertion point that count as
# "the same spot": edits there while the AI writes are part of the merge.
_WINDOW = 1


def _offset_in_paragraph(anchor):
    cursor = anchor.getText().createTextCursorByRange(anchor)
    cursor.gotoStartOfParagraph(True)
    return len(cursor.getString())


def _window_bounds(anchor):
    """Bounds of the paragraph containing `anchor` plus up to _WINDOW
    paragraphs on each side, and the anchor paragraph's index in it.

    Taken once at prepare time as model cursors: LibreOffice keeps them in
    place across edits, so the window keeps covering the SAME text later -
    deleting a paragraph shrinks it rather than sliding it onto paragraphs
    the user never touched. Text typed exactly at a cursor's position ends
    up BEFORE that cursor, so the end bound sits at the end of the last
    paragraph (typing there stays inside), and the start bound sits just
    before the previous paragraph's break (typing at the start of the first
    window paragraph stays inside too). At the very start of the text the
    window simply starts at the text start."""
    text = anchor.getText()
    start = text.createTextCursorByRange(anchor)
    start.gotoStartOfParagraph(False)
    before = 0
    while before < _WINDOW and start.gotoPreviousParagraph(False):
        before += 1
    end = text.createTextCursorByRange(anchor)
    end.gotoEndOfParagraph(False)
    for _ in range(_WINDOW):
        if not end.gotoNextParagraph(False):
            break
        end.gotoEndOfParagraph(False)

    text_start = text.createTextCursor()
    text_start.gotoStart(False)
    at_text_start = text.compareRegionStarts(text_start, start) == 0
    pre = None
    if not at_text_start:
        pre = text.createTextCursorByRange(start)
        if not pre.goLeft(1, False):
            pre = None
    return {"win_start": start, "win_pre": pre, "win_end": end,
            "win_at_text_start": at_text_start, "base_index": before}


def _window_span(request):
    end = request["win_end"]
    text = end.getText()
    if request["win_at_text_start"]:
        span = text.createTextCursor()
        span.gotoStart(False)
    elif request["win_pre"] is not None:
        span = text.createTextCursorByRange(request["win_pre"])
        span.goRight(1, False)
    else:
        span = text.createTextCursorByRange(request["win_start"])
    span.gotoRange(end, True)
    return span


def _window_elements(span):
    """-> ([paragraph objects in span], contains_table)."""
    paragraphs, has_table = [], False
    enum = span.createEnumeration()
    while enum.hasMoreElements():
        element = enum.nextElement()
        if element.supportsService("com.sun.star.text.Paragraph"):
            paragraphs.append(element)
        else:
            has_table = True
    return paragraphs, has_table


def _window_paragraphs(span):
    """-> ([(text, style)] of the paragraphs in span, contains_table)."""
    elements, has_table = _window_elements(span)
    return [(e.getString(), e.ParaStyleName) for e in elements], has_table


def _paragraph_cursor_at(span, index, at_end):
    """Cursor at the start (or end) of the index-th paragraph of span."""
    cursor = span.getText().createTextCursorByRange(span.getStart())
    for _ in range(index):
        cursor.gotoNextParagraph(False)
    if at_end:
        cursor.gotoEndOfParagraph(False)
    else:
        cursor.gotoStartOfParagraph(False)
    return cursor


def _open_insertion_point(cursor):
    """Make the AI's text land in paragraphs of its own: mid/end of a
    paragraph -> split it first; at the start of a non-empty paragraph ->
    insert into a fresh empty paragraph before it, so the user's paragraph
    keeps its own style. Afterwards `cursor` sits in an empty paragraph."""
    text = cursor.getText()
    if _offset_in_paragraph(cursor) > 0:
        text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
    probe = text.createTextCursorByRange(cursor)
    probe.gotoEndOfParagraph(True)
    if probe.getString():
        text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
        cursor.goLeft(1, False)


def _delete_paragraph(span, index):
    """Remove the index-th paragraph of span with one adjacent break,
    joining backward so the neighbour keeps its own formatting."""
    start = _paragraph_cursor_at(span, index, False)
    end = _paragraph_cursor_at(span, index, True)
    cursor = start.getText().createTextCursorByRange(start)
    if cursor.goLeft(1, False):
        cursor.gotoRange(end, True)
    else:
        cursor.gotoRange(end, True)
        cursor.goRight(1, True)
    cursor.setString("")


def _insert_paragraph(span, index, count, content, style):
    """Insert a paragraph (plain text, paragraph style) before the index-th
    of the span's `count` paragraphs (after the last if index == count)."""
    text = span.getText()
    if index < count or count == 0:
        cursor = _paragraph_cursor_at(span, index, False) if count else \
            text.createTextCursorByRange(span.getStart())
        text.insertString(cursor, content, False)
        text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
        cursor.goLeft(1, False)
    else:
        cursor = _paragraph_cursor_at(span, count - 1, True)
        text.insertControlCharacter(cursor, PARAGRAPH_BREAK, False)
        text.insertString(cursor, content, False)
    paragraph = text.createTextCursorByRange(cursor)
    paragraph.gotoStartOfParagraph(True)
    paragraph.setPropertiesToDefault(_CHAR_PROPS)
    _set_style(paragraph, style or DEFAULT_STYLE)
    paragraph.setPropertyToDefault("ParaAdjust")


def _ungrey(span):
    """Remove the provisional grey from every text portion in span."""
    for paragraph in _window_elements(span)[0]:
        portions = paragraph.createEnumeration()
        while portions.hasMoreElements():
            portion = portions.nextElement()
            try:
                if portion.TextPortionType == "Text" and portion.CharColor == PENDING_COLOR:
                    portion.setPropertyToDefault("CharColor")
            except Exception:
                traceback.print_exc()


# ---- streaming the AI's text into the document (main thread) -------------

def _stream_state(request):
    return request.setdefault("stream", {
        "started": False,   # the insertion point has been opened
        "open": False,      # an AI paragraph for the current line exists
        "shown": "",        # text written for the current line so far
        "done": 0,          # complete lines written
        "lines": [],        # the complete lines (dicts), in order
    })


def _stream_paragraph(request, st):
    """Start the paragraph for the next AI line at the anchor."""
    anchor = request["anchor"]
    if not st["started"]:
        _open_insertion_point(anchor)
        st["started"] = True
    else:
        anchor.getText().insertControlCharacter(anchor, PARAGRAPH_BREAK, False)
    # a new paragraph inherits the previous one's style and alignment
    _set_style(anchor, DEFAULT_STYLE)
    anchor.setPropertyToDefault("ParaAdjust")
    st["open"] = True
    st["shown"] = ""


def _stream_set(request, st, new):
    """Make the current AI line show `new`. Growing text is appended at the
    anchor (wherever the user is editing). Text that must change (e.g. a
    '[link](url)' that turned out to be a link) is rewritten only if the
    user hasn't touched that line; otherwise it's left as it is."""
    old = st["shown"]
    if new == old:
        return
    anchor = request["anchor"]
    text = anchor.getText()
    common = 0
    while common < min(len(old), len(new)) and old[common] == new[common]:
        common += 1
    if len(old) > common:
        probe = text.createTextCursorByRange(anchor)
        probe.gotoStartOfParagraph(True)
        if probe.getString() != old:
            st["shown"] = new
            return
        probe = text.createTextCursorByRange(anchor)
        probe.goLeft(len(old) - common, True)
        probe.setString("")
    delta = new[common:]
    if delta:
        text.insertString(anchor, delta, False)
        run = text.createTextCursorByRange(anchor)
        run.goLeft(len(delta), True)
        run.setPropertiesToDefault(_CHAR_PROPS)
        run.CharColor = PENDING_COLOR
    st["shown"] = new


def stream_update(request, markdown, final=False):
    """Main thread: bring the document up to date with the Markdown the
    model has written so far (or all of it, final=True). Complete lines
    get their exact text and paragraph style; the line being written shows
    its words as they arrive. Everything is grey until finish."""
    st = _stream_state(request)
    lines, partial = markdown_lines(markdown, final, request.get("doc_lines") or frozenset())
    if len(lines) < st["done"]:
        # the model ended with NO_TEXT after some lines: nothing is kept
        # (apply_turn takes back what was streamed)
        st["no_text"] = True
        return
    anchor = request["anchor"]
    for line in lines[st["done"]:]:
        if not st["open"]:
            _stream_paragraph(request, st)
        _stream_set(request, st, line["text"])
        _set_kind_style(anchor, line["kind"], line.get("level"))
        st["done"] += 1
        st["open"] = False
        st["shown"] = ""
    st["lines"] = lines[:st["done"]]
    if partial is not None and partial["text"]:
        if not st["open"]:
            _stream_paragraph(request, st)
            _set_kind_style(anchor, partial["kind"], partial.get("level"))
        _stream_set(request, st, partial["text"])
    if final and st["open"]:
        # shown words whose line turned out to make no paragraph: take them
        # back, and the paragraph too if the user didn't write in it
        _stream_set(request, st, "")
        probe = anchor.getText().createTextCursorByRange(anchor)
        if probe.isStartOfParagraph() and probe.isEndOfParagraph():
            if not probe.goLeft(1, True):
                probe.goRight(1, True)
            probe.setString("")
            st["open"] = False


def _streamed_texts(st):
    """The AI's lines as they are in the document: complete lines, plus the
    one being written when the turn was stopped."""
    texts = [line["text"] for line in st["lines"]]
    if st["open"]:
        texts.append(st["shown"])
    return texts


# ---- turning the merge into the document ----------------------------------

def merge_view(request):
    """Main thread: (version, current_texts, entries, has_table) for the
    window now."""
    st = _stream_state(request)
    version = ai_version(request["base_paragraphs"], request["base_index"],
                         request["base_offset"], _streamed_texts(st) if st["started"] else [])
    current, has_table = _window_paragraphs(_window_span(request))
    current_texts = [t for t, _ in current]
    return version, current_texts, merge_entries(version, current_texts), has_table


def _apply_selection(request, entries, selection):
    """Edit the window into the chosen result; un-split the cursor's
    paragraph if nothing ended up between its two halves. Returns the
    result's entries, one per window paragraph."""
    ops = selection_ops(entries, selection)
    count = sum(1 for e in entries if e["kind"] != "remove")
    for op in reversed(ops):
        span = _window_span(request)
        if op[0] == "del":
            _delete_paragraph(span, op[1])
            count -= 1
        else:
            entry = op[2]
            src = entry["src"] or {}
            _insert_paragraph(span, op[1], count, entry["text"], src.get("style"))
            count += 1
    result = merged_entries(entries, selection)
    for i in range(len(result) - 1):
        a, b = result[i]["src"] or {}, result[i + 1]["src"] or {}
        if a.get("origin") == "frag_before" and b.get("origin") == "frag_after":
            cursor = _paragraph_cursor_at(_window_span(request), i, True)
            cursor.goRight(1, True)
            cursor.setString("")
            joined = {"text": a["text"] + b["text"], "kind": "same", "author": None,
                      "src": {"origin": "base", "style": a.get("style")}}
            result[i:i + 2] = [joined]
            break
    return result


def _format_ai_paragraph(paragraph, block, style, fonts):
    """Give an AI paragraph (its text already in place) its real formatting:
    paragraph style, Markdown bold/italic/code, the format step's style.
    -> notes."""
    text = paragraph.getText()
    whole = text.createTextCursorByRange(paragraph)
    _set_kind_style(whole, block["kind"], block.get("level"))
    whole.setPropertyToDefault("ParaAdjust")
    whole.setPropertiesToDefault(_CHAR_PROPS)
    offset = 0
    for run_text, bold, italic, code in block.get("runs") or []:
        if bold or italic or code:
            run = text.createTextCursorByRange(paragraph.getStart())
            run.goRight(offset, False)
            run.goRight(len(run_text), True)
            if bold:
                run.CharWeight = 150.0
            if italic:
                run.CharPosture = uno.Enum("com.sun.star.awt.FontSlant", "ITALIC")
            if code:
                run.CharFontName = CODE_FONT
        offset += len(run_text)
    props, notes = _font_properties(style, fonts)
    if props:
        _set_props(whole, props)
    align = _ALIGN.get(str(style.get("align", "")).strip().lower())
    if align:
        whole.ParaAdjust = uno.Enum("com.sun.star.style.ParagraphAdjust", align)
    elif style.get("align"):
        notes.append(f"align {style['align']!r} ignored")
    return notes


def _set_props(target, props):
    for name, value in props:
        target.setPropertyValue(name, value)


def _make_table(doc, span, first, last, block, props):
    """Replace window paragraphs first..last (the table's rows, shown as
    'a | b' lines while streaming) with a real table."""
    text = span.getText()
    cursor = _paragraph_cursor_at(span, first, False)
    cursor.gotoRange(_paragraph_cursor_at(span, last, True), True)
    cursor.setString("")
    headers, rows = block["headers"], block["rows"]
    n_cols = max([len(headers)] + [len(r) for r in rows] + [1])
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(1 + len(rows), n_cols)
    text.insertTextContent(cursor, table, False)
    for r, row in enumerate([headers] + rows):
        for c, value in enumerate(row[:n_cols]):
            cell = table.getCellByPosition(c, r)
            cell.setString(str(value))
            if props:
                cell_cursor = cell.createTextCursor()
                cell_cursor.gotoEnd(True)
                _set_props(cell_cursor, props)
    # the emptied paragraph is left after the table: drop it unless it is
    # the end of the document (Writer needs a paragraph after a table
    # there). Deleting from its start to the next paragraph's start keeps
    # the NEXT paragraph's own formatting.
    probe = text.createTextCursorByRange(cursor)
    probe.gotoEndOfParagraph(True)
    if not probe.getString() and probe.goRight(1, True):
        probe.setString("")


def _finish_window(doc, request, result_entries, blocks, styles, fonts):
    """After the merge: un-grey the window, and format every AI line that
    is in the result unchanged (tables whose rows are all there, in order,
    become real tables). -> [description of what was added]."""
    _ungrey(_window_span(request))
    paragraphs, has_table = _window_elements(_window_span(request))
    if has_table or len(paragraphs) != len(result_entries):
        return []
    line_block = {}
    for number, block in enumerate(blocks, start=1):
        for k in block["lines"]:
            line_block[k] = number
    ai_at = {}  # AI line index -> window paragraph index
    for i, entry in enumerate(result_entries):
        src = entry["src"] or {}
        if src.get("origin") == "ai" and entry["text"] == src["text"]:
            ai_at[src["line"]] = i
    descs, tables = [], []
    for number, block in enumerate(blocks, start=1):
        style = styles.get(number) or {}
        at = [ai_at.get(k) for k in block["lines"]]
        if block["kind"] == "table":
            if None not in at and at == list(range(at[0], at[0] + len(at))):
                tables.append((at[0], at[-1], block, _font_properties(style, fonts)[0]))
                descs.append(f"added a table ({len(block['rows'])} row(s))")
            continue
        if at[0] is None:
            continue
        try:
            notes = _format_ai_paragraph(paragraphs[at[0]], block, style, fonts)
            line = block_line(block)
            desc = f"added {block['kind']} '{line if len(line) <= 40 else line[:37] + '...'}'"
            if style:
                desc += " with formatting " + json.dumps(style, ensure_ascii=False)
            descs.append(desc + (f" ({'; '.join(notes)})" if notes else ""))
        except Exception as exc:
            traceback.print_exc()
            descs.append(f"formatting {block['kind']} failed ({exc})")
    for first, last, block, props in reversed(tables):
        try:
            _make_table(doc, _window_span(request), first, last, block, props)
        except Exception as exc:
            traceback.print_exc()
            descs.append(f"making the table failed, its rows stay as text ({exc})")
    return descs


def discard_turn(doc, request):
    """Main thread, when a turn is stopped or fails: remove the AI's
    provisional text that the user left untouched, keep everything the user
    did, un-grey. Never asks."""
    st = request.get("stream")
    if not st or not st["started"]:
        return
    try:
        _version, _current, entries, has_table = merge_view(request)
        if not has_table:
            _apply_selection(request, entries, selection_for(entries, keep_ai=False))
        _ungrey(_window_span(request))
    except Exception:
        traceback.print_exc()


# ---- edits to existing text (document part, main thread) ------------------

def capture_edit_targets(doc, request, edits):
    """Main thread, as soon as the edit run answers (before the new text
    streams in): pin each edited line with a model cursor at its start, so
    it is still found later even if lines are added or removed around it.
    The line is located by its text as the model saw it (nearest to its old
    position); if the user already changed it, it gets no target."""
    base = request["base_body"]
    elements = _body_elements(doc)
    current = [e.getString() if _is_paragraph(e) else None for e in elements]
    targets = []
    for edit in edits:
        target = {"edit": edit, "cursor": None, "base": None}
        line = edit.get("line")
        if line:
            want = base[line - 1]
            matches = [k for k, t in enumerate(current) if t == want]
            if matches:
                k = min(matches, key=lambda k: abs(k - (line - 1)))
                target["cursor"] = doc.getText().createTextCursorByRange(elements[k].getStart())
                target["base"] = want
        targets.append(target)
    request["edit_targets"] = targets


def _paragraph_at(cursor):
    """(cursor selecting the whole paragraph that contains `cursor`, its text)."""
    text = cursor.getText()
    para = text.createTextCursorByRange(cursor)
    para.gotoStartOfParagraph(False)
    para.gotoEndOfParagraph(True)
    return para, para.getString()


def _range_in(paragraph, offset, length):
    text = paragraph.getText()
    r = text.createTextCursorByRange(paragraph.getStart())
    r.goRight(offset, False)
    r.goRight(length, True)
    return r


def _delete_paragraph_range(para):
    """Remove a whole paragraph with one adjacent break, keeping the
    neighbour's formatting (joins backward when there is a previous one)."""
    text = para.getText()
    start = text.createTextCursorByRange(para.getStart())
    end = text.createTextCursorByRange(para.getEnd())
    if start.goLeft(1, False):
        start.gotoRange(end, True)
        start.setString("")
    elif end.goRight(1, False):
        cursor = text.createTextCursorByRange(para.getStart())
        cursor.gotoRange(end, True)
        cursor.setString("")
    else:
        para.setString("")


def _word_deletions(text, words):
    """[(offset, length)] to delete every occurrence of words in text,
    each widened by one neighbouring space so no double space is left."""
    spans = []
    for k in _find_all(text, words):
        start, end = k, k + len(words)
        if start > 0 and text[start - 1] == " " and (end == len(text) or text[end] in " .,;:!?"):
            start -= 1
        elif end < len(text) and text[end] == " " and start == 0:
            end += 1
        spans.append((start, end - start))
    return spans


def _plan_deletions(doc, request):
    """-> (plans, notes). A plan: {"kind": "line"|"words", "para", "before",
    "after", "spans", "desc"}."""
    plans, notes = [], []
    for target in request.get("edit_targets") or []:
        edit = target["edit"]
        if edit["type"] not in ("delete_line", "delete_words"):
            continue
        desc = describe_edit(edit, request["base_body"])
        paragraphs = []
        if edit.get("line"):
            if target["cursor"] is None:
                notes.append(f"did not {desc}: that line was changed meanwhile")
                continue
            para, current = _paragraph_at(target["cursor"])
            if edit["type"] == "delete_line" and current != target["base"]:
                notes.append(f"did not {desc}: the user changed that line meanwhile")
                continue
            paragraphs.append((para, current))
        else:
            for element in _body_elements(doc):
                if _is_paragraph(element) and _find_all(element.getString(), edit["text"]):
                    para = element.getText().createTextCursorByRange(element)
                    paragraphs.append((para, element.getString()))
        for para, current in paragraphs:
            if edit["type"] == "delete_line":
                plans.append({"kind": "line", "para": para, "before": [current], "after": [],
                              "desc": desc})
                continue
            spans = _word_deletions(current, edit["text"])
            if not spans:
                notes.append(f"did not {desc}: the words are no longer there")
                continue
            after = current
            for offset, length in sorted(spans, reverse=True):
                after = after[:offset] + after[offset + length:]
            plans.append({"kind": "words", "para": para, "before": [current], "after": [after],
                          "spans": spans, "desc": desc})
    return plans, notes


def _run_deletions(plans):
    descs = []
    for plan in [p for p in plans if p["kind"] == "words"]:
        for offset, length in sorted(plan["spans"], reverse=True):
            _range_in(plan["para"], offset, length).setString("")
        descs.append(plan["desc"])
    for plan in [p for p in plans if p["kind"] == "line"]:
        para, _current = _paragraph_at(plan["para"].getStart())
        _delete_paragraph_range(para)
        descs.append(plan["desc"])
    return list(dict.fromkeys(descs))


def _whole_document_ranges(doc):
    body = doc.getText().createTextCursor()
    body.gotoStart(False)
    body.gotoEnd(True)
    ranges = [body]
    tables = doc.getTextTables()
    for i in range(tables.getCount()):
        table = tables.getByIndex(i)
        for name in table.getCellNames():
            cell = table.getCellByName(name).createTextCursor()
            cell.gotoStart(False)
            cell.gotoEnd(True)
            ranges.append(cell)
    return ranges


def _run_font_edits(doc, request):
    """set_font / reset_font, on a line, words in a line, words anywhere,
    or the whole document. Never asks. -> (descs, notes)."""
    descs, notes = [], []
    fonts = request.get("fonts") or []
    for target in request.get("edit_targets") or []:
        edit = target["edit"]
        if edit["type"] not in ("set_font", "reset_font"):
            continue
        desc = describe_edit(edit, request["base_body"])
        words = edit.get("text")
        try:
            if edit.get("line"):
                if target["cursor"] is None:
                    notes.append(f"did not {desc}: that line was changed meanwhile")
                    continue
                para, current = _paragraph_at(target["cursor"])
                ranges = ([_range_in(para, k, len(words)) for k in _find_all(current, words)]
                          if words else [para])
            elif words:
                ranges = []
                for element in _body_elements(doc):
                    if _is_paragraph(element):
                        current = element.getString()
                        whole = element.getText().createTextCursorByRange(element)
                        ranges += [_range_in(whole, k, len(words)) for k in _find_all(current, words)]
            else:
                ranges = _whole_document_ranges(doc)
            if not ranges:
                notes.append(f"did not {desc}: the text is no longer there")
                continue
            if edit["type"] == "reset_font":
                for r in ranges:
                    r.setPropertiesToDefault(_CHAR_PROPS)
                descs.append(desc)
                continue
            props, font_notes = _font_properties(edit, fonts)
            for r in ranges:
                _set_props(r, props)
            descs.append(desc + (f" ({'; '.join(font_notes)})" if font_notes else ""))
        except Exception as exc:
            traceback.print_exc()
            notes.append(f"{desc} failed ({exc})")
    return descs, notes


def _apply_edits(doc, request, confirm_changes):
    """Deletions (after the user's OK, if a confirm callback is given),
    then font changes. -> (descs, notes, declined)."""
    if "edit_targets" not in request:
        capture_edit_targets(doc, request, request.get("edits") or [])
    plans, notes = _plan_deletions(doc, request)
    descs, declined = [], False
    if plans:
        if confirm_changes is None or confirm_changes([("", p["before"], p["after"]) for p in plans]):
            descs += _run_deletions(plans)
        else:
            declined = True
            notes.append("the user cancelled the deletions")
    font_descs, font_notes = _run_font_edits(doc, request)
    return descs + font_descs, notes + font_notes, declined


# ---- the turn ---------------------------------------------------------------

def prepare_turn(doc, history: list[dict], user_message: str) -> dict:
    """Main thread, before the model is asked: read the live document
    (numbered snapshot + selection), and remember WHERE the new text will
    be inserted plus what the paragraphs around it looked like right now,
    so the merge can tell what the user changed meanwhile.

    The anchor is a model text cursor, not the view cursor: LibreOffice
    keeps model cursors positioned correctly while the document is edited,
    and it stays put while the user moves their own cursor elsewhere."""
    settings = Settings.load(APP_NAME)
    view_cursor = doc.getCurrentController().getViewCursor()
    selected_text = view_cursor.getString()

    document_context = (
        "The document already contains this text (do NOT repeat it):\n\"\"\"\n"
        f"{_document_text(doc)}\n\"\"\""
    )
    if selected_text:
        document_context += f"\nText currently selected by the user:\n\"\"\"\n{selected_text}\n\"\"\""

    end = view_cursor.getEnd()
    anchor = end.getText().createTextCursorByRange(end)
    window = _window_bounds(anchor)
    base_paragraphs, _has_table = _window_paragraphs(_window_span(window))
    return {
        **window,
        "host": settings.ollama_host,
        "user_message": user_message,
        "history": list(history),
        "document_context": document_context,
        "anchor": anchor,
        "base_paragraphs": base_paragraphs,
        "base_offset": _offset_in_paragraph(anchor),
        "fonts": installed_fonts(doc),
        "doc_lines": frozenset(t.strip() for t in _body_texts(doc) if t and t.strip()),
        "base_body": _body_texts(doc),
    }


def build_calls(request, history, markdown=None, markdown_blocks=None, edits_text=""):
    """The runs' message lists: its system prompt, the chat history, then
    ONE user message with the document, the request and the instruction.
    "text" always; "reply" once the text step is done (markdown = what was
    written, "" for nothing); "format" with the parsed blocks. Exposed for
    tests."""
    document = request["document_context"]
    message = request["user_message"]
    language = _reply_language_line(request.get("language"))
    calls = {"edits": [{"role": "system", "content": EDIT_SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": EDIT_INSTRUCTION.format(
            numbered=numbered_document(request["base_body"]), request=message)}]}
    calls["text"] = [{"role": "system", "content": TEXT_SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": TEXT_INSTRUCTION.format(document=document, request=message)}]
    if markdown is not None:
        if markdown:
            instruction = REPLY_WROTE_INSTRUCTION.format(request=message, markdown=markdown,
                                                         edits=edits_text, language=language)
        elif edits_text:
            instruction = REPLY_EDITS_INSTRUCTION.format(request=message, edits=edits_text,
                                                         language=language)
        else:
            instruction = REPLY_NONE_INSTRUCTION.format(document=document, request=message,
                                                        language=language)
        calls["reply"] = [{"role": "system", "content": REPLY_SYSTEM_PROMPT}] + CAPABILITY_MEMORY + history + [
            {"role": "user", "content": instruction}]
    if markdown_blocks is not None:
        numbered = "\n".join(f"[{i}] {block_line(b)}" for i, b in enumerate(markdown_blocks, start=1))
        calls["format"] = [{"role": "system", "content": FORMAT_SYSTEM_PROMPT}] + history + [
            {"role": "user", "content": FORMAT_INSTRUCTION.format(request=message, numbered=numbered)}]
    return calls


def _ask_turn(request: dict):
    """Background thread: the model runs. No UNO access here.

    0. edits to text already in the document (JSON: delete lines/words,
    change fonts) - request["on_edits"](edits) is called at once so the
    panel can pin the edited lines; 1. the Markdown text to add, streamed: request["on_text"](so_far) is
    called every STREAM_INTERVAL seconds and at every new line (the panel
    hands it to stream_update() on the main thread); 2. the chat reply,
    told exactly what was written (or that nothing was) - shown at once
    through request["on_reply"](reply); 3. the formatting JSON for the text
    (skipped when there is none). The session history is trimmed to what
    the model can use (last exchange below 7B). Returns (result,
    model_name); result has "message", "markdown", "blocks", "styles",
    "_model_params"."""
    host = request["host"]
    cancel = request.get("cancel")
    timeout = request.get("timeout", DEFAULT_TIMEOUT)
    model, params = best_model(host)
    if cancel is not None:
        cancel.check()
    options = {"num_ctx": MEMORY_NUM_CTX} if has_memory(params) else None
    history = fit_history([{"role": "system", "content": ""}] + request["history"]
                          + [{"role": "user", "content": request["user_message"]}], params)[1:-1]

    on_text = request.get("on_text")
    last = {"at": 0.0, "lines": 0}

    def on_delta(text_so_far):
        if on_text is None:
            return
        lines = text_so_far.count("\n")
        now = time.monotonic()
        if lines != last["lines"] or now - last["at"] >= STREAM_INTERVAL:
            last["at"], last["lines"] = now, lines
            on_text(text_so_far)

    phase = request.get("on_phase") or (lambda name: None)
    phase("planning")
    calls = build_calls(request, history)
    edits, edit_notes = [], []
    try:
        edits, edit_notes = clean_edits(chat_json(host, model, calls["edits"], timeout, options, cancel),
                                        request["base_body"])
    except Cancelled:
        raise
    except OllamaError:
        traceback.print_exc()  # no edits this turn; the text step still runs
    if request.get("on_edits"):
        request["on_edits"](edits)
    edits_text = (REPLY_EDITS_LINE.format(
        items="\n".join(f"- {describe_edit(e, request['base_body'])}" for e in edits)) if edits else "")

    phase("drafting")
    raw = chat_stream(host, model, calls["text"], on_delta, timeout, options=options, cancel=cancel)
    if on_text is not None:
        on_text(raw)
    skip = request.get("doc_lines") or frozenset()
    markdown = clean_markdown(raw)
    blocks = parse_markdown(raw, skip)
    written = "\n".join(line["text"] for line in markdown_lines(raw, skip=skip)[0])

    calls = build_calls(request, history, written, edits_text=edits_text)
    phase("replying")
    reply = chat(host, model, calls["reply"], timeout, options=options, cancel=cancel,
                 allow_empty=True).strip()
    if request.get("on_reply") and reply:
        request["on_reply"](reply)

    styles = {}
    if blocks:
        calls = build_calls(request, history, written, blocks, edits_text)
        phase("formatting")
        try:
            styles = parse_styles(chat_json(host, model, calls["format"], timeout, options, cancel),
                                  len(blocks))
        except Cancelled:
            raise
        except OllamaError:
            traceback.print_exc()  # formatting is optional: insert the text unformatted
    return {"message": reply, "markdown": markdown, "raw_markdown": raw, "blocks": blocks,
            "styles": styles, "edits": edits, "edit_notes": edit_notes, "actions": [],
            "_model_params": params}, model


def apply_turn(doc, request: dict, result: dict, model: str, resolve_merge=None,
               confirm_changes=None):
    """Main thread, after the model runs: settle the new text (see
    _settle_new_text), then apply the edits to existing text: deletions
    after confirm_changes(sections) says yes (sections are (title,
    before_lines, after_lines); with no callback they just run), then font
    changes, which never ask.

    Returns (chat_text, wrote_to_document, model_name, confirmation)."""
    chat_text = result.get("message") or "(no reply)"
    wrote, parts = _settle_new_text(doc, request, result, resolve_merge)
    request.setdefault("edits", result.get("edits") or [])
    descs, notes, declined = _apply_edits(doc, request, confirm_changes)
    notes = list(result.get("edit_notes") or []) + notes
    if declined:
        result["_declined"] = True
    if descs:
        parts.append("changed existing text: " + "; ".join(descs))
    if notes:
        parts.append("not done: " + "; ".join(notes))
    confirmation = ("Document update: " + "; ".join(parts) + ".") if parts else None
    return chat_text, wrote or bool(descs), model, confirmation


def _settle_new_text(doc, request, result, resolve_merge):
    """Write the rest of the streamed text, then settle the cursor window:
    if the user changed anything there while the AI wrote, resolve_merge
    (view) decides what to keep. view has "base", "ai" and "current" (the
    window's lines in the three versions) and "entries" (see
    merge_entries); it returns one bool per entry (True = keep that
    change), or None to keep the document as it is. With no resolver
    everything is kept. Then the grey is removed and the AI's lines get
    their formatting. -> (wrote, [history notes])."""
    stream_update(request, result.get("raw_markdown", result.get("markdown")) or "", final=True)
    st = _stream_state(request)
    lines = st["lines"]
    if not lines or st.get("no_text"):
        discard_turn(doc, request)
        return False, []

    version, current_texts, entries, has_table = merge_view(request)
    selection = selection_for(entries)
    if has_table:
        _ungrey(_window_span(request))
        return True, ["your text was added (the user put a table next to it while you wrote, "
                      "so nothing was rearranged)"]
    if user_changed(entries) and resolve_merge is not None:
        chosen = resolve_merge({
            "base": base_split_texts(version), "ai": [v["text"] for v in version],
            "current": current_texts, "entries": entries,
        })
        if chosen is not None:
            selection = list(chosen)
        result["_merged"] = True
    final = _apply_selection(request, entries, selection)
    blocks = blocks_from_lines(lines)
    descs = _finish_window(doc, request, final, blocks, result.get("styles") or {},
                           request.get("fonts") or [])
    kept_ai = sum(1 for e, s in zip(entries, selection) if e["author"] == "ai" and s)
    total_ai = sum(1 for e in entries if e["author"] == "ai")
    note = ""
    if user_changed(entries):
        dropped_user = sum(1 for e, s in zip(entries, selection) if e["author"] == "user" and not s)
        note = (f" (the user edited the text around the cursor while you wrote; kept {kept_ai} of "
                f"your {total_ai} line(s) unchanged"
                + (f", undid {dropped_user} of their own change(s)" if dropped_user else "") + ")")
    wrote = any((e["src"] or {}).get("origin") == "ai" for e in final)
    if not descs and not kept_ai:
        return wrote, ["none of your new text was kept" + note]
    return wrote, [("; ".join(descs) or "your text was added") + note]


def chat_turn(doc, history: list[dict], user_message: str):
    """Synchronous prepare -> ask -> apply, for tests over a UNO socket.
    The panel runs the same steps with ask_turn() on a background thread.
    Returns (chat_text, wrote_to_document, model_name, confirmation)."""
    request = prepare_turn(doc, history, user_message)
    result, model = ask_turn(request)
    return apply_turn(doc, request, result, model)


def forget(doc):
    """Called when the user clears the chat. The session memory is the
    panel's chat history; nothing else is kept per document. What the model
    knows about writing into the document (the system prompts and
    CAPABILITY_MEMORY) is never forgotten."""


# ---- summarize --------------------------------------------------------------

def prepare_summary(doc) -> dict:
    """Main thread: read the whole document's text for summarize."""
    return {"host": Settings.load(APP_NAME).ollama_host, "text": doc.getText().getString()}


def ask_summary(request: dict):
    """Background thread, no UNO. Returns (summary, model_name)."""
    model = pick_best_model(request["host"])
    reply = chat(
        request["host"],
        model,
        [
            {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
            {"role": "user", "content": f"Summarize this document:\n---\n{request['text']}\n---"},
        ],
        timeout=DEFAULT_TIMEOUT,
        cancel=request.get("cancel"),
    )
    return reply.strip(), model


def summarize(doc):
    """Summarize the whole document's text. Returns (summary, model_name)."""
    return ask_summary(prepare_summary(doc))


def ask_turn(request: dict):
    """_ask_turn plus SKAI_ Memory: what other SKAI apps know goes in, the exchange is recorded."""
    from sk_libre_ai.skai_memory_bridge import around_turn
    return around_turn("sk_libre_writer", request, _ask_turn)
