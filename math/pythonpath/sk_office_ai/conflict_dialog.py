"""Modal "you and the AI changed the same spot" dialog.

Shown when the user edited part of the document while the model was still
thinking, and the model's reply wants to change that same part. Each
conflict is a section of (title, base_lines, mine_lines, ai_lines[,
mine_first_lines, ai_first_lines]), where base is that part as it was when
the prompt was sent and the last two are what "keep both" would leave in
each order (default: mine + ai / ai + mine). The dialog shows two
side-by-side line diffs against base - the AI's on the left, the user's on
the right - with added lines green, removed lines red, unchanged grey.

The user picks the AI's version, their own, or both; "both" swaps the
three buttons for a before/after question and swaps the two diffs for a
preview of what each order would leave, each above its own button, with a
back arrow at the top to return to the three choices.

UNO edit controls can't color individual lines, so every displayed line is
its own FixedText with its own TextColor.
"""

from __future__ import annotations

import difflib
import textwrap

import unohelper
from com.sun.star.awt import XActionListener, XMouseListener

from .i18n import Translator

# ask_conflict() results
USE_AI = "ai"
KEEP_MINE = "mine"
MINE_FIRST = "mine_first"
AI_FIRST = "ai_first"

GREEN = 0x2E7D32
RED = 0xC62828
GREY = 0x808080
TITLE = 0x000000
LEGEND = 0xA8A8A8

# Dialog geometry, in AppFont units (1 x-unit = 1/4 average char width).
_MARGIN = 6
_COL_W = 170
_GAP = 8
_ROW_H = 9
_WRAP_CHARS = _COL_W // 4 - 3
_MAX_ROWS = 32
_INTRO_KEYS = {"text": "intro_text", "cells": "intro_cells", "formula": "intro_formula"}
_RESULTS = {1: USE_AI, 2: KEEP_MINE, 3: MINE_FIRST, 4: AI_FIRST}


def diff_rows(base: list[str], new: list[str], show_removed: bool = True) -> list[tuple[str, int]]:
    """Line diff of base -> new as (prefixed_text, color) rows. Blank lines
    that were added or removed are skipped (a bare red "-" only confused);
    show_removed=False gives just the resulting text (for previews)."""
    rows = []
    matcher = difflib.SequenceMatcher(a=base, b=new, autojunk=False)
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op == "equal":
            rows += [(f"  {line}", GREY) for line in base[a1:a2]]
            continue
        if show_removed:
            rows += [(f"- {line}", RED) for line in base[a1:a2] if line.strip()]
        rows += [(f"+ {line}", GREEN) for line in new[b1:b2] if line.strip()]
    return rows


def _full(section):
    """Section padded to 6 fields: the keep-both results default to
    mine + ai and ai + mine."""
    title, base, mine, ai = section[:4]
    mine_first = section[4] if len(section) > 4 else mine + ai
    ai_first = section[5] if len(section) > 5 else ai + mine
    return title, base, mine, ai, mine_first, ai_first


def _wrap(rows):
    out = []
    for text, color in rows:
        pieces = textwrap.wrap(text, _WRAP_CHARS, subsequent_indent="  ",
                               drop_whitespace=False) or [text]
        out += [(piece, color) for piece in pieces]
    return out


def _column(sections, field, t, show_removed=True):
    rows = []
    for section in sections:
        section = _full(section)
        title, base = section[0], section[1]
        if title:
            rows.append((title, TITLE))
        rows += diff_rows(base, section[field], show_removed)
    rows = _wrap(rows)
    if len(rows) > _MAX_ROWS:
        hidden = len(rows) - (_MAX_ROWS - 1)
        rows = rows[:_MAX_ROWS - 1] + [(t("more_rows", count=hidden), GREY)]
    return rows


class _ButtonListener(unohelper.Base, XActionListener):
    def __init__(self, fn):
        self.fn = fn

    def actionPerformed(self, event):
        self.fn()

    def disposing(self, event):
        pass


def ask_conflict(ctx, parent_peer, kind: str, sections) -> str:
    """Show the conflict and block until the user chooses. kind is "text",
    "cells" or "formula" (picks the explanation line). Returns USE_AI,
    KEEP_MINE, MINE_FIRST or AI_FIRST; just closing the dialog = KEEP_MINE."""
    t = Translator(ctx)
    columns = {
        # page 1: what each side changed
        "ai": _column(sections, 3, t),
        "mine": _column(sections, 2, t),
        # page 2: what each keep-both order would leave
        "mf": _column(sections, 4, t, show_removed=False),
        "af": _column(sections, 5, t, show_removed=False),
    }
    n_rows = max(max(len(rows) for rows in columns.values()), 1)

    smgr = ctx.ServiceManager
    dlg_model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
    dlg_model.Title = t("conflict_title")
    width = 2 * _MARGIN + 2 * _COL_W + _GAP
    inner = width - 2 * _MARGIN
    right_x = _MARGIN + _COL_W + _GAP

    def add(name, service, **props):
        model = dlg_model.createInstance(service)
        for key, value in props.items():
            setattr(model, key, value)
        dlg_model.insertByName(name, model)
        return model

    intro = t(_INTRO_KEYS.get(kind, "intro_text"), count=len(sections))
    question = t("both_question")
    y = _MARGIN
    add("back", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=_MARGIN, PositionY=y, Width=16, Height=14,
        Label="\u2190", HelpText=t("back"))
    add("intro", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=_MARGIN, PositionY=y, Width=inner, Height=20, MultiLine=True, Label=intro)
    add("question", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=_MARGIN + 22, PositionY=y + 3, Width=inner - 22, Height=17,
        MultiLine=True, Label=question, FontWeight=150.0)
    y += 24
    add("head_ai", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=_MARGIN, PositionY=y, Width=_COL_W, Height=10,
        Label=t("head_ai"), FontWeight=150.0)
    add("head_mine", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=right_x, PositionY=y, Width=_COL_W, Height=10,
        Label=t("head_mine"), FontWeight=150.0)
    add("head_mf", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=_MARGIN, PositionY=y, Width=_COL_W, Height=10,
        Label=t("preview_mine_first"), FontWeight=150.0)
    add("head_af", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=right_x, PositionY=y, Width=_COL_W, Height=10,
        Label=t("preview_ai_first"), FontWeight=150.0)
    y += 12
    add("sep", "com.sun.star.awt.UnoControlFixedLineModel",
        PositionX=_MARGIN + _COL_W + _GAP // 2 - 1, PositionY=y, Width=2,
        Height=n_rows * _ROW_H, Orientation=1)
    for side, x in (("ai", _MARGIN), ("mine", right_x), ("mf", _MARGIN), ("af", right_x)):
        for i, (text, color) in enumerate(columns[side]):
            add(f"{side}_{i}", "com.sun.star.awt.UnoControlFixedTextModel",
                PositionX=x, PositionY=y + i * _ROW_H, Width=_COL_W, Height=_ROW_H,
                Label=text, TextColor=color, NoLabel=True)
    y += n_rows * _ROW_H + 8

    # page 1: three choices, AI under the AI column, mine under the user's
    third = (inner - 2 * _MARGIN) // 3
    add("use_ai", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=_MARGIN, PositionY=y, Width=third, Height=16, Label=t("use_ai"))
    add("keep_both", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=_MARGIN + third + _MARGIN, PositionY=y, Width=third, Height=16,
        Label=t("keep_both"))
    add("keep_mine", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=width - _MARGIN - third, PositionY=y, Width=third, Height=16,
        Label=t("keep_mine"))
    # page 2 ("keep both"): before/after, same row
    add("mine_first", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=_MARGIN, PositionY=y, Width=_COL_W, Height=16, Label=t("mine_first"))
    add("ai_first", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=right_x, PositionY=y, Width=_COL_W, Height=16, Label=t("ai_first"))
    y += 20
    add("legend", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=_MARGIN, PositionY=y, Width=inner, Height=10,
        Label=t("legend"), TextColor=LEGEND, Align=1)
    y += 10 + _MARGIN

    dlg_model.Width = width
    dlg_model.Height = y

    dialog = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
    dialog.setModel(dlg_model)
    toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    dialog.createPeer(toolkit, parent_peer)

    page1 = ["intro", "use_ai", "keep_both", "keep_mine", "head_ai", "head_mine"]
    page2 = ["back", "question", "mine_first", "ai_first", "head_mf", "head_af"]
    for side, page in (("ai", page1), ("mine", page1), ("mf", page2), ("af", page2)):
        page += [f"{side}_{i}" for i in range(len(columns[side]))]

    def show_page(both):
        for name in page1:
            dialog.getControl(name).setVisible(not both)
        for name in page2:
            dialog.getControl(name).setVisible(both)

    def finish(code):
        return lambda: dialog.endDialog(code)

    for name, fn in (("use_ai", finish(1)), ("keep_mine", finish(2)),
                     ("mine_first", finish(3)), ("ai_first", finish(4)),
                     ("keep_both", lambda: show_page(True)),
                     ("back", lambda: show_page(False))):
        dialog.getControl(name).addActionListener(_ButtonListener(fn))
    show_page(False)
    try:
        return _RESULTS.get(dialog.execute(), KEEP_MINE)
    finally:
        dialog.dispose()


def ask_confirm(ctx, parent_peer, sections) -> bool:
    """Ask before the AI removes or changes existing text. sections are
    (title, before_lines, after_lines); shown as one diff (removed red,
    added green, unchanged grey). True = apply, False = cancel (also when
    the dialog is just closed)."""
    t = Translator(ctx)
    rows = []
    for i, (title, before, after) in enumerate(sections):
        if i:
            rows.append(("", GREY))
        if title:
            rows.append((title, TITLE))
        rows += diff_rows(before, after)
    width_chars = 2 * _COL_W
    rows = [(piece, color) for text, color in rows
            for piece in (textwrap.wrap(text, width_chars // 4 - 3, subsequent_indent="  ",
                                        drop_whitespace=False) or [text])]
    if len(rows) > _MAX_ROWS:
        hidden = len(rows) - (_MAX_ROWS - 1)
        rows = rows[:_MAX_ROWS - 1] + [(t("more_rows", count=hidden), GREY)]

    smgr = ctx.ServiceManager
    dlg_model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
    dlg_model.Title = t("confirm_title")
    width = 2 * _MARGIN + 2 * _COL_W + _GAP
    inner = width - 2 * _MARGIN

    def add(name, service, **props):
        model = dlg_model.createInstance(service)
        for key, value in props.items():
            setattr(model, key, value)
        dlg_model.insertByName(name, model)
        return model

    y = _MARGIN
    add("intro", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=_MARGIN, PositionY=y, Width=inner, Height=20, MultiLine=True,
        Label=t("confirm_intro"))
    y += 24
    for i, (text, color) in enumerate(rows):
        add(f"row_{i}", "com.sun.star.awt.UnoControlFixedTextModel",
            PositionX=_MARGIN, PositionY=y + i * _ROW_H, Width=inner, Height=_ROW_H,
            Label=text, TextColor=color, NoLabel=True)
    y += max(len(rows), 1) * _ROW_H + 8
    half = (inner - _MARGIN) // 2
    add("apply", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=_MARGIN, PositionY=y, Width=half, Height=16,
        Label=t("apply_changes"), PushButtonType=1)
    add("cancel", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=width - _MARGIN - half, PositionY=y, Width=half, Height=16,
        Label=t("cancel_changes"), PushButtonType=2)
    y += 20
    add("legend", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=_MARGIN, PositionY=y, Width=inner, Height=10,
        Label=t("legend"), TextColor=LEGEND, Align=1)
    dlg_model.Width = width
    dlg_model.Height = y + 10 + _MARGIN

    dialog = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
    dialog.setModel(dlg_model)
    toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    dialog.createPeer(toolkit, parent_peer)
    try:
        return dialog.execute() == 1
    finally:
        dialog.dispose()


# ---- Writer: review what the AI wrote while you edited around it ----------

DROPPED = 0xA0A0A0
_ENTRY_LINES = 3      # a long line shows at most this many wrapped rows
_PAGE_ROWS = 24       # rows per page on the change-by-change page


class _ClickListener(unohelper.Base, XMouseListener):
    def __init__(self, fn):
        self.fn = fn

    def mousePressed(self, event):
        self.fn()

    def mouseReleased(self, event):
        pass

    def mouseEntered(self, event):
        pass

    def mouseExited(self, event):
        pass

    def disposing(self, event):
        pass


def _capped(rows, t):
    if len(rows) > _MAX_ROWS:
        hidden = len(rows) - (_MAX_ROWS - 1)
        rows = rows[:_MAX_ROWS - 1] + [(t("more_rows", count=hidden), GREY)]
    return rows


def _entry_label(entry, t, width_chars):
    """-> (label text, rows it takes) for one change-by-change row."""
    text = entry["text"].replace("\n", " ⏎ ")
    if entry["kind"] == "same":
        prefix = "   "
    else:
        sign = "+" if entry["kind"] == "add" else "−"
        prefix = f"{sign} [{t('tag_ai') if entry['author'] == 'ai' else t('tag_you')}] "
    pieces = textwrap.wrap(prefix + text, width_chars, subsequent_indent="     ") or [prefix]
    if len(pieces) > _ENTRY_LINES:
        pieces = pieces[:_ENTRY_LINES]
        pieces[-1] = pieces[-1][:max(0, width_chars - 1)] + "…"
    return "\n".join(pieces), len(pieces)


def _entry_color(entry, keep):
    if entry["kind"] == "same":
        return GREY
    if not keep:
        return DROPPED
    return GREEN if entry["kind"] == "add" else RED


def ask_merge(ctx, parent_peer, view):
    """The AI wrote into the document while the user edited around it.
    view: "base", "ai", "current" (lists of lines) and "entries" (Writer's
    merge entries: "text", "kind" same/add/remove, "author" ai/user).

    Page 1 shows what the AI added (base -> ai) and what the user changed
    (ai -> current) side by side, with three choices: the AI's text plus
    the user's changes (= the document as it is now), the AI's text without
    the user's changes, or change by change. Page 2 lists every change:
    added lines green, removed lines red, all kept by default; clicking one
    greys it out (dropped), clicking again keeps it.

    Returns one bool per entry (True = keep that change), or None when the
    dialog is just closed (= keep the document as it is)."""
    translate = Translator(ctx)
    overrides = view.get("keys") or {}

    def t(key, **fmt):
        return translate(overrides.get(key, key), **fmt)

    entries = view["entries"]
    keep = [True] * len(entries)
    # solo: the user changed nothing, only the AI's changes need an OK -
    # then there is no "what you changed" column at all (showing the
    # existing rows there made them look like the user's own changes)
    solo = not any(e["author"] == "user" for e in entries)

    width = 2 * _MARGIN + 2 * _COL_W + _GAP
    inner = width - 2 * _MARGIN
    right_x = _MARGIN + _COL_W + _GAP
    col_w = inner if solo else _COL_W

    ai_rows = _capped(_wrap(diff_rows(view["base"], view["ai"])), t)
    # what the user changed: against the AI's version (Writer: the AI's
    # text is already in the document) or against "user_base" (Math);
    # "changes_only" leaves out the unchanged rows there
    mine_rows = [] if solo else diff_rows(view.get("user_base", view["ai"]), view["current"])
    if view.get("changes_only"):
        mine_rows = [r for r in mine_rows if r[1] != GREY]
    mine_rows = _capped(_wrap(mine_rows), t)
    width_chars = inner // 4 - 3

    labels = [_entry_label(e, t, width_chars) for e in entries]
    pages, page, used = [], [], 0
    for i, (_label, rows) in enumerate(labels):
        if page and used + rows > _PAGE_ROWS:
            pages.append(page)
            page, used = [], 0
        page.append(i)
        used += rows
    pages.append(page)

    smgr = ctx.ServiceManager
    dlg_model = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
    dlg_model.Title = t("merge_title")

    def add(name, service, **props):
        model = dlg_model.createInstance(service)
        for key, value in props.items():
            setattr(model, key, value)
        dlg_model.insertByName(name, model)
        return model

    y = _MARGIN
    add("intro", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=_MARGIN, PositionY=y, Width=inner, Height=20, MultiLine=True,
        Label=t("merge_intro_solo" if solo else "merge_intro"))
    add("back", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=_MARGIN, PositionY=y, Width=16, Height=14,
        Label="←", HelpText=t("back"))
    add("select_intro", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=_MARGIN + 22, PositionY=y, Width=inner - 22, Height=20,
        MultiLine=True, Label=t("select_intro"))
    y += 24
    top = y
    # page 1
    add("head_ai", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=_MARGIN, PositionY=y, Width=col_w, Height=10,
        Label=t("head_ai_added"), FontWeight=150.0)
    n1 = max(len(ai_rows), len(mine_rows), 1)
    page1 = ["intro", "head_ai", "mixed", "ai_only", "select"]
    if not solo:
        add("head_mine", "com.sun.star.awt.UnoControlFixedTextModel",
            PositionX=right_x, PositionY=y, Width=_COL_W, Height=10,
            Label=t("head_you_changed"), FontWeight=150.0)
        add("sep", "com.sun.star.awt.UnoControlFixedLineModel",
            PositionX=_MARGIN + _COL_W + _GAP // 2 - 1, PositionY=y + 12, Width=2,
            Height=n1 * _ROW_H, Orientation=1)
        page1 += ["head_mine", "sep"]
    for side, x, rows in (("ai", _MARGIN, ai_rows), ("mine", right_x, mine_rows)):
        for i, (text, color) in enumerate(rows):
            add(f"{side}_{i}", "com.sun.star.awt.UnoControlFixedTextModel",
                PositionX=x, PositionY=y + 12 + i * _ROW_H, Width=col_w, Height=_ROW_H,
                Label=text, TextColor=color, NoLabel=True)
            page1.append(f"{side}_{i}")
    height1 = 12 + n1 * _ROW_H
    # page 2: every entry, positioned within its page
    page2 = ["back", "select_intro", "prev", "next", "page_label", "apply"]
    row_y = {}
    for page_entries in pages:
        offset = 0
        for i in page_entries:
            row_y[i] = y + offset * _ROW_H
            offset += labels[i][1]
    for i, entry in enumerate(entries):
        add(f"entry_{i}", "com.sun.star.awt.UnoControlFixedTextModel",
            PositionX=_MARGIN, PositionY=row_y[i], Width=inner, Height=labels[i][1] * _ROW_H,
            Label=labels[i][0], TextColor=_entry_color(entry, True), MultiLine=True, NoLabel=True,
            HelpText="" if entry["kind"] == "same" else t("select_toggle"))
    height2 = _PAGE_ROWS * _ROW_H if len(pages) > 1 else \
        max(1, sum(labels[i][1] for i in pages[0])) * _ROW_H
    y += max(height1, height2) + 6

    third = (inner - 2 * _MARGIN) // 3
    add("mixed", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=_MARGIN, PositionY=y, Width=third, Height=16,
        Label=t("merge_apply_ai" if solo else "merge_mixed"),
        HelpText="" if solo else t("merge_mixed_help"))
    add("ai_only", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=_MARGIN + third + _MARGIN, PositionY=y, Width=third, Height=16,
        Label=t("merge_keep_as_is" if solo else "merge_ai_only"),
        HelpText="" if solo else t("merge_ai_only_help"))
    add("select", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=width - _MARGIN - third, PositionY=y, Width=third, Height=16,
        Label=t("merge_select"))
    add("prev", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=_MARGIN, PositionY=y, Width=16, Height=16, Label="▲",
        HelpText=t("page_prev"))
    add("page_label", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=_MARGIN + 20, PositionY=y + 4, Width=60, Height=10, Label="")
    add("next", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=_MARGIN + 84, PositionY=y, Width=16, Height=16, Label="▼",
        HelpText=t("page_next"))
    add("apply", "com.sun.star.awt.UnoControlButtonModel",
        PositionX=width - _MARGIN - third, PositionY=y, Width=third, Height=16,
        Label=t("apply_selection"))
    y += 20
    add("legend", "com.sun.star.awt.UnoControlFixedTextModel",
        PositionX=_MARGIN, PositionY=y, Width=inner, Height=10,
        Label=t("legend"), TextColor=LEGEND, Align=1)
    y += 10 + _MARGIN
    dlg_model.Width = width
    dlg_model.Height = y

    dialog = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
    dialog.setModel(dlg_model)
    toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    dialog.createPeer(toolkit, parent_peer)

    state = {"page": 0, "selecting": False, "result": None}

    def show():
        selecting = state["selecting"]
        for name in page1:
            dialog.getControl(name).setVisible(not selecting)
        for name in page2:
            dialog.getControl(name).setVisible(selecting)
        many = len(pages) > 1
        for name in ("prev", "next", "page_label"):
            dialog.getControl(name).setVisible(selecting and many)
        current = set(pages[state["page"]]) if selecting else set()
        for i in range(len(entries)):
            dialog.getControl(f"entry_{i}").setVisible(i in current)
        dlg_model.getByName("page_label").Label = t(
            "page_of", page=state["page"] + 1, pages=len(pages))
        dlg_model.getByName("prev").Enabled = state["page"] > 0
        dlg_model.getByName("next").Enabled = state["page"] < len(pages) - 1

    def toggle(i):
        keep[i] = not keep[i]
        model = dlg_model.getByName(f"entry_{i}")
        model.TextColor = _entry_color(entries[i], keep[i])
        try:
            model.FontStrikeout = 0 if keep[i] else 1
        except Exception:
            pass

    def choose(selection):
        state["result"] = selection
        dialog.endDialog(1)

    def go(step):
        state["page"] = max(0, min(len(pages) - 1, state["page"] + step))
        show()

    def selecting(on):
        state["selecting"] = on
        show()

    handlers = {
        "mixed": lambda: choose([True] * len(entries)),
        # solo: the second button is "keep it as it is" (drop the AI's changes)
        "ai_only": lambda: choose([e["kind"] == "same" or (e["author"] == "ai") != solo
                                   for e in entries]),
        "select": lambda: selecting(True),
        "back": lambda: selecting(False),
        "prev": lambda: go(-1),
        "next": lambda: go(1),
        "apply": lambda: choose(list(keep)),
    }
    for name, fn in handlers.items():
        dialog.getControl(name).addActionListener(_ButtonListener(fn))
    for i, entry in enumerate(entries):
        if entry["kind"] != "same":
            dialog.getControl(f"entry_{i}").addMouseListener(
                _ClickListener(lambda i=i: toggle(i)))
    show()
    try:
        dialog.execute()
        return state["result"]
    finally:
        dialog.dispose()
