# Copyright (C) 2026 Enrico Miot (EndrX)
# Licensed under GNU GPL v3 or later — see the LICENSE file or <https://www.gnu.org/licenses/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Chat-turn logic for the Calc AI sidebar, kept free of any awt/UI
dependency so it can be exercised directly (e.g. over a UNO socket
connection) without simulating button clicks.

Every chat turn runs the model twice: first ONLY the spreadsheet edits,
as JSON {"actions": [...]} built from the live sheet (every value shown
with its cell address), then the chat reply, told exactly what was done.
The plugin - never the model - executes the actions through the cell API
(writes, then formatting), so a malformed action can't corrupt the sheet
beyond the actions that did parse. The Explain button (prepare_explain /
ask_explain) reads the selected cells live and has them explained without
the chat history.
"""

from __future__ import annotations

import traceback

import uno

from sk_office_ai.config import Settings
from sk_office_ai.conflict_dialog import KEEP_MINE, MINE_FIRST, USE_AI
from sk_office_ai.json_chat import (
    DEFAULT_TIMEOUT, LANGUAGE_NAMES, MEMORY_NUM_CTX, chat_json, fit_history, has_memory,
)
from sk_office_ai.model_select import best_model
from sk_office_ai.ollama_client import chat

APP_NAME = "sk_office-calc"

# Every chat turn runs the model twice (measured on qwen2.5:0.5b: asked
# for {"message", "actions"} in ONE reply, it filled "message" and left
# "actions" empty in 6-8 of 8 edit requests; asked for the actions ALONE,
# 7 of 8 got actions):
#   1. actions - JSON {"actions": [...]} only, from the sheet shown with
#                cell addresses;
#   2. reply   - plain text, told exactly what was done.

ACTIONS_SYSTEM_PROMPT = (
    "You edit a LibreOffice Calc spreadsheet for the user. You see the sheet's "
    "current cells. Reply with ONLY one JSON object {\"actions\": [...]} with the "
    "edits the user asks for. Each action is one of:\n"
    '{"type": "set_cell", "cell": "C3", "value": "..."}  - write one value\n'
    '{"type": "table", "start_cell": "D1", "headers": ["...", "..."], "rows": [["...", "..."]]}  '
    "- write a table (headers may be []; a text with several lines goes one line per row)\n"
    '{"type": "clear_range", "range": "B2:B5"}  - empty cells\n'
    '{"type": "format", "range": "A1:B1", "bold": true}  - change how cells look. Fields: '
    'bold, italic, underline (true/false), color and background ("#rrggbb"), font, size, '
    'align (left, center, right), number_format (a code like 0.00 or 0%, or number, '
    "percent, currency, date, time); give ONLY the fields the user asked for\n"
    '{"type": "fit_columns", "range": "A:B"}  - fit column widths to their content\n'
    '{"type": "table_style", "range": "A1:C5", "color": "blue"}  - style a whole table: '
    "header row coloured and bold, alternating row colours, borders (color: blue, green, "
    "red, orange, grey, purple, yellow, teal or #rrggbb)\n"
    '{"type": "borders", "range": "A1:C5", "style": "all"}  - borders: all, outline or none\n'
    '{"type": "merge", "range": "A1:C1"}  - merge cells into one\n'
    '{"type": "column_width", "range": "A:C", "width": 4}  - column width in cm\n'
    '{"type": "row_height", "range": "1:1", "height": 1}  - row height in cm\n'
    '{"type": "freeze", "rows": 1, "columns": 0}  - keep the first rows/columns visible when scrolling\n'
    '{"type": "add_sheet", "name": "..."}, {"type": "rename_sheet", "name": "...", "new_name": "..."}, '
    '{"type": "tab_color", "sheet": "...", "color": "green"}, {"type": "delete_sheet", "name": "..."}  '
    "- manage the sheets (without \"name\"/\"sheet\": the current sheet)\n"
    "You can change or format any cell, also the ones the user wrote. If the user "
    "only asks a question, reply {\"actions\": []}.\n\n"
    "Examples, for a sheet with A1=Name, B1=Score, A2=Anna, B2=7:\n"
    'user: write Hello in C1 -> {"actions": [{"type": "set_cell", "cell": "C1", "value": "Hello"}]}\n'
    'user: make a table of 2 cities and their country -> {"actions": [{"type": "table", '
    '"start_cell": "D1", "headers": ["City", "Country"], "rows": [["Rome", "Italy"], ["Paris", "France"]]}]}\n'
    'user: make the first row bold -> {"actions": [{"type": "format", "range": "A1:B1", "bold": true}]}\n'
    'user: give the table a green style -> {"actions": [{"type": "table_style", "range": "A1:B2", "color": "green"}]}\n'
    'user: call this sheet Grades -> {"actions": [{"type": "rename_sheet", "new_name": "Grades"}]}\n'
    'user: clear column B -> {"actions": [{"type": "clear_range", "range": "B1:B2"}]}\n'
    'user: what is in A2? -> {"actions": []}'
)

ACTIONS_INSTRUCTION = "{sheet}\n\nUser's request: {request}\n\nReply with ONLY the JSON."

REPLY_SYSTEM_PROMPT = (
    "You are the chat assistant of a LibreOffice Calc side panel. You can change "
    "the user's spreadsheet: the plugin makes the changes for each request for you "
    "(writing values and tables, clearing and formatting cells), before this chat "
    "reply. Reply in plain text only: no JSON, no Markdown."
)

REPLY_DONE_INSTRUCTION = (
    "User's message: {request}\n\nYou have just made these changes to the sheet:\n"
    "{done}\n\nNow tell the user, in ONE short sentence, what you changed. {language}"
)

REPLY_NONE_INSTRUCTION = (
    "{sheet}\n\nUser's message: {request}\n\nNothing was changed in the sheet for "
    "this message. Answer the user briefly. {language}"
)

# Never cleared by Clear chat (which forgets only the conversation); put
# before the reply run's history. (In the actions run a remembered reply
# with no actions made the small model answer with no actions too.)
CAPABILITY_MEMORY = [
    {"role": "user", "content": "What can you do in this spreadsheet?"},
    {"role": "assistant", "content": (
        "I write values and tables into your sheet, clear cells, format cells (bold, "
        "colours, number formats, alignment) and fit column widths - on any cell, also "
        "the ones you wrote.")},
]

EXPLAIN_SYSTEM_PROMPT = (
    "You explain spreadsheet cells to the user in plain words. You get the cells "
    "the user selected, as they are right now (formulas with their result). Base "
    "your answer ONLY on those cells. Reply in plain text, no JSON, no Markdown."
)

EXPLAIN_INSTRUCTION = (
    "These are the cells I selected (formulas shown with their result):\n"
    "---\n{cells}\n---\n\nWhat do they do? Answer with a list, one line per cell, "
    "each line starting with the cell address: what the cell contains, or what its "
    "formula calculates. {language}"
)


def _parse_cell_ref(sheet, ref):
    addr = sheet.getCellRangeByName(ref).RangeAddress
    return addr.StartColumn, addr.StartRow


def _plan_table(sheet, action):
    """-> (writes, note): writes is a list of (col, row, value) the action
    wants, computed without touching the sheet so conflicts can be checked
    before anything is written."""
    headers = action.get("headers") or []
    rows = action.get("rows") or []
    start_cell = action.get("start_cell") or "A1"
    try:
        start_col, start_row = _parse_cell_ref(sheet, start_cell)
    except Exception:
        start_col, start_row = 0, 0
        start_cell = f"{start_cell!r} (unparseable, used A1 instead)"

    writes = []
    r = start_row
    for row in ([headers] if headers else []) + list(rows):
        if not isinstance(row, list):
            row = [row]
        for c, value in enumerate(row):
            writes.append((start_col + c, r, str(value)))
        r += 1

    num_cols = max([len(headers)] + [len(row) if isinstance(row, list) else 1 for row in rows] + [0])
    note = (
        f"Wrote a table at {start_cell}: "
        f"{'1 header row + ' if headers else ''}{len(rows)} data row(s), "
        f"{num_cols} column(s) wide."
    )
    return writes, note


def _plan_set_cell(sheet, action):
    cell_ref = action.get("cell")
    if not cell_ref:
        return [], "set_cell action failed: no cell reference given."
    try:
        col, row = _parse_cell_ref(sheet, cell_ref)
    except Exception:
        return [], f"set_cell action failed: bad cell reference {cell_ref!r}."
    value = action.get("value", "")
    return [(col, row, str(value))], f"Set {cell_ref} to {value!r}."


_MAX_CLEAR_CELLS = 10000


def _plan_clear_range(sheet, action):
    ref = action.get("range") or action.get("cell")
    if not ref:
        return [], "clear_range action failed: no range given."
    try:
        addr = sheet.getCellRangeByName(ref).RangeAddress
    except Exception:
        return [], f"clear_range action failed: bad range {ref!r}."
    cells = (addr.EndColumn - addr.StartColumn + 1) * (addr.EndRow - addr.StartRow + 1)
    if cells > _MAX_CLEAR_CELLS:
        return [], f"clear_range action refused: {ref} is {cells} cells, over {_MAX_CLEAR_CELLS}."
    writes = [(col, row, "")
              for row in range(addr.StartRow, addr.EndRow + 1)
              for col in range(addr.StartColumn, addr.EndColumn + 1)]
    return writes, f"Cleared {ref}."


_FORMAT_TYPES = ("format", "fit_columns", "table_style", "borders", "merge", "column_width",
                 "row_height", "freeze", "add_sheet", "rename_sheet", "tab_color", "delete_sheet")
_COLOR_NAMES = {"blue": 0x4472C4, "green": 0x70AD47, "red": 0xC0504D, "orange": 0xED7D31,
                "grey": 0x7F7F7F, "gray": 0x7F7F7F, "purple": 0x7030A0, "yellow": 0xFFC000,
                "teal": 0x4BACC6, "black": 0x000000, "white": 0xFFFFFF}
_NUMBER_FORMAT_TYPES = {"date": 2, "time": 4, "currency": 8, "number": 16, "percent": 128}
_ALIGN = {"left": "LEFT", "center": "CENTER", "centre": "CENTER", "right": "RIGHT"}


def _color(value):
    named = _COLOR_NAMES.get(str(value or "").strip().lower())
    if named is not None:
        return named
    text = str(value or "").strip().lstrip("#")
    return int(text, 16) if len(text) == 6 and all(c in "0123456789abcdefABCDEF" for c in text) else None


def _number_format_key(doc, cell_range, code):
    formats = doc.getNumberFormats()
    locale = cell_range.CharLocale
    kind = _NUMBER_FORMAT_TYPES.get(str(code).strip().lower())
    if kind is not None:
        return formats.getStandardFormat(kind, locale)
    key = formats.queryKey(str(code), locale, False)
    return key if key != -1 else formats.addNew(str(code), locale)


def _tint(color, amount=0.8):
    """color mixed with white (amount = how much white)."""
    r, g, b = (color >> 16) & 255, (color >> 8) & 255, color & 255
    mix = [int(c + (255 - c) * amount) for c in (r, g, b)]
    return (mix[0] << 16) | (mix[1] << 8) | mix[2]


def _border_line(color=0x000000):
    line = uno.createUnoStruct("com.sun.star.table.BorderLine2")
    line.Color = color
    line.LineWidth = 26  # 1/100 mm, about 0.75 pt
    line.LineStyle = 0  # solid
    return line


def _set_borders(cells, style, color=0x000000):
    border = cells.TableBorder2
    full = _border_line(color) if style in ("all", "outline") else uno.createUnoStruct(
        "com.sun.star.table.BorderLine2")
    inner = _border_line(color) if style == "all" else uno.createUnoStruct(
        "com.sun.star.table.BorderLine2")
    for side in ("TopLine", "BottomLine", "LeftLine", "RightLine"):
        setattr(border, side, full)
        setattr(border, f"Is{side}Valid", True)
    for side in ("HorizontalLine", "VerticalLine"):
        setattr(border, side, inner)
        setattr(border, f"Is{side}Valid", True)
    cells.TableBorder2 = border


def _sheet_named(doc, name, default):
    if not name:
        return default
    sheets = doc.getSheets()
    return sheets.getByName(str(name)) if sheets.hasByName(str(name)) else None


def _run_sheet_action(doc, sheet, action):
    """add_sheet / rename_sheet / tab_color / delete_sheet / freeze -> note."""
    kind = action["type"]
    sheets = doc.getSheets()
    if kind == "freeze":
        try:
            rows, cols = int(action.get("rows") or 0), int(action.get("columns") or 0)
        except (TypeError, ValueError):
            return "freeze action failed: rows/columns must be numbers."
        doc.getCurrentController().freezeAtPosition(cols, rows)
        return f"Froze {rows} row(s) and {cols} column(s)."
    if kind == "add_sheet":
        name = str(action.get("name") or "").strip()
        if not name or sheets.hasByName(name):
            return f"add_sheet action failed: {'a sheet needs a name' if not name else repr(name) + ' already exists'}."
        sheets.insertNewByName(name, sheets.getCount())
        return f"Added the sheet {name!r}."
    target = _sheet_named(doc, action.get("sheet") or (action.get("name") if kind != "add_sheet" else None), sheet)
    if target is None:
        return f"{kind} action failed: there is no sheet named {action.get('sheet') or action.get('name')!r}."
    if kind == "rename_sheet":
        new = str(action.get("new_name") or "").strip()
        if not new or sheets.hasByName(new):
            return f"rename_sheet action failed: {'no new name given' if not new else repr(new) + ' already exists'}."
        old = target.getName()
        target.setName(new)
        return f"Renamed the sheet {old!r} to {new!r}."
    if kind == "tab_color":
        color = _color(action.get("color"))
        if color is None:
            return f"tab_color action failed: unknown colour {action.get('color')!r}."
        target.TabColor = color
        return f"Coloured the tab of {target.getName()!r} #{color:06x}."
    return f"Skipped {kind}."


def _run_format(doc, sheet, action):
    """One format/style/sheet action -> a note on what was done."""
    if action["type"] in ("freeze", "add_sheet", "rename_sheet", "tab_color"):
        return _run_sheet_action(doc, sheet, action)
    ref = action.get("range") or action.get("cell")
    if not ref:
        return f"{action['type']} action failed: no range given."
    try:
        cells = sheet.getCellRangeByName(ref)
    except Exception:
        return f"{action['type']} action failed: bad range {ref!r}."
    if action["type"] == "fit_columns":
        columns = cells.getColumns()
        for i in range(columns.getCount()):
            columns.getByIndex(i).OptimalWidth = True
        return f"Fitted the width of columns {ref}."
    if action["type"] in ("column_width", "row_height"):
        key = "width" if action["type"] == "column_width" else "height"
        try:
            size = int(float(action.get(key)) * 1000)  # cm -> 1/100 mm
        except (TypeError, ValueError):
            return f"{action['type']} action failed: {key} must be a number of cm."
        lines = cells.getColumns() if key == "width" else cells.getRows()
        for i in range(lines.getCount()):
            setattr(lines.getByIndex(i), "Width" if key == "width" else "Height", size)
        return f"Set the {key} of {ref} to {action.get(key)} cm."
    if action["type"] == "merge":
        cells.merge(True)
        return f"Merged {ref}."
    if action["type"] == "borders":
        style = str(action.get("style") or "all").lower()
        if style not in ("all", "outline", "none"):
            return f"borders action failed: style must be all, outline or none, not {style!r}."
        _set_borders(cells, style, _color(action.get("color")) or 0x000000)
        return f"Set {style} borders on {ref}."
    if action["type"] == "table_style":
        color = _color(action.get("color") or "blue")
        if color is None:
            return f"table_style action failed: unknown colour {action.get('color')!r}."
        addr = cells.RangeAddress
        header = sheet.getCellRangeByPosition(addr.StartColumn, addr.StartRow, addr.EndColumn, addr.StartRow)
        header.CellBackColor = color
        header.CharColor = 0xFFFFFF
        header.CharWeight = 150.0
        for i, row in enumerate(range(addr.StartRow + 1, addr.EndRow + 1)):
            line = sheet.getCellRangeByPosition(addr.StartColumn, row, addr.EndColumn, row)
            line.CellBackColor = _tint(color, 0.8) if i % 2 == 0 else 0xFFFFFF
        _set_borders(cells, "all", _tint(color, 0.3))
        return f"Styled the table {ref}: header #{color:06x}, banded rows, borders."
    done, skipped = [], []
    if action.get("bold") is not None:
        cells.CharWeight = 150.0 if action["bold"] else 100.0
        done.append("bold" if action["bold"] else "not bold")
    if action.get("italic") is not None:
        cells.CharPosture = uno.Enum("com.sun.star.awt.FontSlant",
                                     "ITALIC" if action["italic"] else "NONE")
        done.append("italic" if action["italic"] else "not italic")
    if action.get("underline") is not None:
        cells.CharUnderline = 1 if action["underline"] else 0
        done.append("underline" if action["underline"] else "no underline")
    if action.get("font"):
        cells.CharFontName = str(action["font"])
        done.append(f"font {action['font']}")
    if action.get("size") is not None:
        try:
            cells.CharHeight = float(action["size"])
            done.append(f"size {action['size']}")
        except (TypeError, ValueError):
            skipped.append(f"size {action['size']!r}")
    for key, prop in (("color", "CharColor"), ("background", "CellBackColor")):
        if action.get(key):
            value = _color(action[key])
            if value is None:
                skipped.append(f"{key} {action[key]!r} (use #rrggbb)")
            else:
                setattr(cells, prop, value)
                done.append(f"{key} #{value:06x}")
    if action.get("align"):
        align = _ALIGN.get(str(action["align"]).strip().lower())
        if align:
            cells.HoriJustify = uno.Enum("com.sun.star.table.CellHoriJustify", align)
            done.append(f"aligned {action['align']}")
        else:
            skipped.append(f"align {action['align']!r}")
    if action.get("number_format"):
        try:
            cells.NumberFormat = _number_format_key(doc, cells, action["number_format"])
            done.append(f"number format {action['number_format']}")
        except Exception:
            skipped.append(f"number format {action['number_format']!r}")
    if not done:
        return f"format action on {ref} changed nothing" + (
            f" (ignored: {', '.join(skipped)})." if skipped else ".")
    return f"Formatted {ref}: {', '.join(done)}" + (
        f" (ignored: {', '.join(skipped)})." if skipped else ".")


_ACTION_PLANNERS = {
    "table": _plan_table,
    "set_cell": _plan_set_cell,
    "clear_range": _plan_clear_range,
}


def _plan_actions(sheet, actions):
    """-> (writes, notes). One malformed action (e.g. a bad cell reference)
    can't abort the rest of the turn's actions."""
    writes, notes = [], []
    for action in actions:
        action_type = action.get("type")
        planner = _ACTION_PLANNERS.get(action_type)
        if action_type in _FORMAT_TYPES:
            continue  # run after the cell writes, see apply_turn
        if planner is None:
            notes.append(f"Skipped unknown action type {action_type!r}.")
            continue
        try:
            action_writes, note = planner(sheet, action)
        except Exception as exc:
            traceback.print_exc()
            action_writes, note = [], f"{action_type} action failed: {exc}"
        writes.extend(action_writes)
        notes.append(note or f"{action_type} action completed.")
    return writes, notes


def _snapshot_cells(sheet):
    """{(col, row): string} for the sheet's used range at this moment, plus
    the (first, last) row span that was too long to record (None if all of
    it was). Cells outside the used range were empty."""
    cursor = sheet.createCursor()
    cursor.gotoEndOfUsedArea(True)
    addr = cursor.RangeAddress
    last_row = min(addr.EndRow, addr.StartRow + _MAX_USED_RANGE_ROWS - 1)
    values = {}
    for row in range(addr.StartRow, last_row + 1):
        for col in range(addr.StartColumn, addr.EndColumn + 1):
            values[(col, row)] = sheet.getCellByPosition(col, row).getString()
    untracked = (last_row + 1, addr.EndRow) if last_row < addr.EndRow else None
    return values, untracked


def _col_name(col: int) -> str:
    """0 -> 'A', 25 -> 'Z', 26 -> 'AA', ... (spreadsheet column naming)."""
    name = ""
    col += 1
    while col > 0:
        col, rem = divmod(col - 1, 26)
        name = chr(ord("A") + rem) + name
    return name


def _cell_name(col: int, row: int) -> str:
    return f"{_col_name(col)}{row + 1}"


_MAX_USED_RANGE_ROWS = 300


def _read_used_range_context(doc) -> str:
    """Read the ACTUAL current used range of the active sheet, live, every
    call (never cached) - this is ground truth for the model, independent of
    whatever happens to be selected or whatever the model's own past turns
    claimed to have written. Confirmed live against a real headless Calc
    instance: sheet.createCursor() + XSheetCellCursor.gotoEndOfUsedArea(True)
    gives the real used-area RangeAddress (sheet.createCursorByRange(...)
    anchored at a single cell gives the identical result; createCursor() is
    simpler since it needs no anchor cell). On a genuinely empty sheet this
    comes back as a degenerate single-cell range (A1:A1) - checked for and
    reported as "(sheet is empty)" rather than a misleading one-line dump.
    """
    try:
        sheet = doc.getCurrentController().getActiveSheet()
        cursor = sheet.createCursor()
        cursor.gotoEndOfUsedArea(True)
        addr = cursor.RangeAddress

        if (
            addr.StartColumn == addr.EndColumn == 0
            and addr.StartRow == addr.EndRow == 0
            and sheet.getCellByPosition(0, 0).getString() == ""
        ):
            return "(sheet is empty)"

        total_rows = addr.EndRow - addr.StartRow + 1
        end_row = addr.EndRow
        truncated = total_rows > _MAX_USED_RANGE_ROWS
        if truncated:
            end_row = addr.StartRow + _MAX_USED_RANGE_ROWS - 1

        # every value with its cell address ("Row 2: A2=Anna | B2=7"): with
        # bare tab-separated values the model had to guess the addresses
        lines = []
        for row in range(addr.StartRow, end_row + 1):
            values = [
                f"{_cell_name(col, row)}={sheet.getCellByPosition(col, row).getString()}"
                for col in range(addr.StartColumn, addr.EndColumn + 1)
                if sheet.getCellByPosition(col, row).getString()
            ]
            lines.append(f"Row {row + 1}: " + (" | ".join(values) if values else "(empty)"))

        range_label = (
            f"{_cell_name(addr.StartColumn, addr.StartRow)}:"
            f"{_cell_name(addr.EndColumn, addr.EndRow)}"
        )
        header = f"Current cells (used range {range_label}):"
        dump = header + "\n" + "\n".join(lines)
        if truncated:
            dump += (
                f"\n... (truncated: showing first {_MAX_USED_RANGE_ROWS} of "
                f"{total_rows} used rows)"
            )
        return dump
    except Exception:
        traceback.print_exc()
        return "(could not read sheet state)"


def _selection_name(controller):
    try:
        addr = controller.getSelection().queryInterface(
            uno.getTypeByName("com.sun.star.sheet.XCellRangeAddressable")).getRangeAddress()
        first = _cell_name(addr.StartColumn, addr.StartRow)
        last = _cell_name(addr.EndColumn, addr.EndRow)
        return first if first == last else f"{first}:{last}"
    except Exception:
        return ""


def prepare_turn(doc, history: list[dict], user_message: str) -> dict:
    """Main thread, before the model is asked: read the live sheet (every
    value with its cell address) and the selection, and record the sheet
    and its cell values right now so apply_turn() can tell which cells the
    user changed while the model was thinking. Actions always land on THIS
    sheet, even if the user switches sheets meanwhile."""
    settings = Settings.load(APP_NAME)
    controller = doc.getCurrentController()
    # Rebuilt fresh on every call - never cached - so the model is always
    # grounded in the sheet's real current state.
    sheet_context = _read_used_range_context(doc)
    selected = _selection_name(controller)
    if selected:
        sheet_context += f"\nThe user's selected cell(s): {selected}"
    sheet = controller.getActiveSheet()
    base_values, untracked = _snapshot_cells(sheet)
    return {
        "host": settings.ollama_host,
        "user_message": user_message,
        "history": list(history),
        "sheet_context": sheet_context,
        "doc": doc,
        "sheet": sheet,
        "base_values": base_values,
        "untracked": untracked,
    }


def describe_action(action):
    """English description of an action, for the reply run."""
    kind = action.get("type")
    if kind == "set_cell":
        return f"wrote {action.get('value', '')!r} in {action.get('cell')}"
    if kind == "table":
        rows = action.get("rows") or []
        return f"wrote a table at {action.get('start_cell', 'A1')} ({len(rows)} row(s))"
    if kind == "clear_range":
        return f"cleared {action.get('range') or action.get('cell')}"
    if kind == "fit_columns":
        return f"fitted the width of columns {action.get('range')}"
    if kind == "add_sheet":
        return f"added a sheet named {action.get('name')!r}"
    if kind == "rename_sheet":
        return f"renamed the sheet {action.get('name') or '(current)'} to {action.get('new_name')!r}"
    if kind == "delete_sheet":
        return f"asked to delete the sheet {action.get('name') or '(current)'} (the user confirms it)"
    if kind == "freeze":
        return f"froze {action.get('rows') or 0} row(s) and {action.get('columns') or 0} column(s)"
    fields = ", ".join(f"{k}={v}" for k, v in action.items() if k not in ("type", "range", "cell"))
    target = action.get("range") or action.get("cell") or action.get("sheet") or ""
    verb = {"format": "formatted", "table_style": "styled the table", "borders": "set borders on",
            "merge": "merged", "column_width": "set the column width of",
            "row_height": "set the row height of", "tab_color": "coloured the tab of"}.get(kind, kind)
    return f"{verb} {target}" + (f" ({fields})" if fields else "")


def build_calls(request, history, actions=None):
    """The runs' message lists (exposed for tests): "actions" always,
    "reply" once the actions are known ([] for none)."""
    message = request["user_message"]
    language = f"Write your reply in {LANGUAGE_NAMES.get(request.get('language') or 'en', 'English')}."
    calls = {"actions": [{"role": "system", "content": ACTIONS_SYSTEM_PROMPT}] + history + [
        {"role": "user", "content": ACTIONS_INSTRUCTION.format(sheet=request["sheet_context"],
                                                               request=message)}]}
    if actions is not None:
        if actions:
            instruction = REPLY_DONE_INSTRUCTION.format(
                request=message, done="\n".join(f"- {describe_action(a)}" for a in actions),
                language=language)
        else:
            instruction = REPLY_NONE_INSTRUCTION.format(sheet=request["sheet_context"],
                                                        request=message, language=language)
        calls["reply"] = [{"role": "system", "content": REPLY_SYSTEM_PROMPT}] + CAPABILITY_MEMORY \
            + history + [{"role": "user", "content": instruction}]
    return calls


def _ask_turn(request: dict):
    """Background thread, no UNO: the actions run, then the reply run (told
    what was done). request["on_phase"](name) reports progress and
    request["on_reply"](reply) shows the reply at once. The session history
    is trimmed to what the model can use (last exchange below 7B).
    Returns (result, model_name); result has "message", "actions",
    "_model_params"."""
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
    phase("calc_working")
    reply = chat_json(host, model, build_calls(request, history)["actions"], timeout, options, cancel)
    actions = reply.get("actions") if isinstance(reply, dict) else None
    if isinstance(actions, dict):
        actions = [actions]
    actions = [a for a in actions if isinstance(a, dict)] if isinstance(actions, list) else []
    phase("replying")
    message = chat(host, model, build_calls(request, history, actions)["reply"], timeout,
                   options=options, cancel=cancel, allow_empty=True).strip()
    if request.get("on_reply") and message:
        request["on_reply"](message)
    return {"message": message, "actions": actions, "_model_params": params}, model


def _base_value(request, col, row):
    untracked = request["untracked"]
    if untracked and untracked[0] <= row <= untracked[1]:
        return None
    return request["base_values"].get((col, row), "")


def apply_turn(doc, request: dict, result: dict, model: str, resolve_conflict=None,
               confirm_changes=None):
    """Main thread, after the model replied: write its cells. Cells the
    user changed since prepare_turn() AND the AI wants to set to something
    else are conflicts: resolve_conflict("cells", sections) picks USE_AI,
    KEEP_MINE, or MINE_FIRST / AI_FIRST (both values in the cell, one per
    line). Non-conflicting AI cells are written either way. With no
    resolver the AI values win.

    Writes that would overwrite or clear a cell with content first need
    confirm_changes(sections) (sections: (cell, [old], [new])); declined
    ones are skipped. Then the format / fit_columns actions run (they never
    ask).
    Returns (chat_text, wrote_bool, model_name, confirmation); the panel
    folds "confirmation" into the history so the next turn knows what
    really happened."""
    chat_text = result.get("message") or "(no reply)"
    actions = result.get("actions") or []
    sheet = request["sheet"]
    writes, action_notes = _plan_actions(sheet, actions) if actions else ([], [])

    conflicts = []
    for col, row, value in writes:
        current = sheet.getCellByPosition(col, row).getString()
        base = _base_value(request, col, row)
        if base is not None and current != base and current != value:
            conflicts.append((col, row, base, current, value))

    if conflicts and resolve_conflict is not None:
        def as_lines(value):
            return [value] if value else []

        choice = resolve_conflict("cells", [
            (_cell_name(col, row), as_lines(base), as_lines(mine), as_lines(ai))
            for col, row, base, mine, ai in conflicts
        ])
        if choice != USE_AI:
            by_cell = {(col, row): (mine, ai) for col, row, _, mine, ai in conflicts}
            kept = []
            for col, row, value in writes:
                if (col, row) not in by_cell:
                    kept.append((col, row, value))
                elif choice != KEEP_MINE:
                    # keep both, one per line inside the cell, in the chosen order
                    mine, ai = by_cell[(col, row)]
                    parts = [mine, ai] if choice == MINE_FIRST else [ai, mine]
                    kept.append((col, row, "\n".join(p for p in parts if p)))
            writes = kept
            if choice == KEEP_MINE:
                action_notes.append(
                    f"The user edited {len(by_cell)} of those cell(s) while you were "
                    "replying and kept their own values there."
                )
            else:
                action_notes.append(
                    f"The user edited {len(by_cell)} of those cell(s) while you were "
                    "replying and kept both values in each, "
                    + ("theirs first." if choice == MINE_FIRST else "yours first.")
                )

    # Overwriting or clearing a cell that already has content is the
    # spreadsheet's "deletion": like deletions in Writer and Math it needs
    # the user's OK (confirm_changes; closing = cancel). Writing into empty
    # cells and formatting never ask.
    overwrites = []
    for col, row, value in writes:
        current = sheet.getCellByPosition(col, row).getString()
        if current and current != value:
            overwrites.append((col, row, current, value))
    if overwrites and confirm_changes is not None:
        sections = [(_cell_name(col, row), [current], [value] if value else [])
                    for col, row, current, value in overwrites]
        if not confirm_changes(sections):
            skip = {(col, row) for col, row, _c, _v in overwrites}
            writes = [w for w in writes if (w[0], w[1]) not in skip]
            result["_declined"] = True
            action_notes.append(f"The user did not let you overwrite or clear {len(skip)} "
                                "cell(s) that already had content.")

    for col, row, value in writes:
        sheet.getCellByPosition(col, row).setString(value)

    formatted = False
    # deleting a sheet is a deletion too: it needs the user's OK
    for action in [a for a in actions if a.get("type") == "delete_sheet"]:
        sheets = doc.getSheets()
        target = _sheet_named(doc, action.get("name") or action.get("sheet"), sheet)
        if target is None:
            action_notes.append(f"delete_sheet action failed: no sheet named {action.get('name')!r}.")
            continue
        name = target.getName()
        if sheets.getCount() < 2:
            action_notes.append(f"delete_sheet refused: {name!r} is the only sheet.")
            continue
        cursor = target.createCursor()
        cursor.gotoEndOfUsedArea(True)
        used = cursor.RangeAddress
        filled = target.getCellByPosition(0, 0).getString() or used.EndRow or used.EndColumn
        what = [f"sheet {name!r}" + (" (with data)" if filled else " (empty)")]
        if confirm_changes is not None and not confirm_changes([(name, what, [])]):
            result["_declined"] = True
            action_notes.append(f"The user did not let you delete the sheet {name!r}.")
            continue
        sheets.removeByName(name)
        formatted = True
        action_notes.append(f"Deleted the sheet {name!r}.")
    for action in actions:
        if action.get("type") == "delete_sheet":
            continue
        if action.get("type") in _FORMAT_TYPES:
            try:
                note = _run_format(doc, sheet, action)
                formatted = formatted or not note.startswith(action["type"])
            except Exception as exc:
                traceback.print_exc()
                note = f"{action['type']} action failed: {exc}"
            action_notes.append(note)

    confirmation = ("Sheet update: " + " ".join(action_notes)) if action_notes else None
    return chat_text, bool(writes) or formatted, model, confirmation


def chat_turn(doc, history: list[dict], user_message: str):
    """Synchronous prepare -> ask -> apply, for tests over a UNO socket.
    Returns (chat_text, wrote_bool, model_name, confirmation)."""
    request = prepare_turn(doc, history, user_message)
    result, model = ask_turn(request)
    return apply_turn(doc, request, result, model)


def forget(doc):
    """Clear chat forgets the conversation only (the panel's history);
    the system prompt and CAPABILITY_MEMORY always stay."""


# ---- explain ----------------------------------------------------------------

_MAX_EXPLAIN_CELLS = 400


def prepare_explain(doc) -> dict:
    """Main thread: read ONLY the cells the user selected, live, with
    formulas ([] when the selection has nothing in it - the panel then asks
    the user to select some cells first)."""
    controller = doc.getCurrentController()
    sheet = controller.getActiveSheet()
    lines = []
    try:
        addr = controller.getSelection().queryInterface(
            uno.getTypeByName("com.sun.star.sheet.XCellRangeAddressable")).getRangeAddress()
    except Exception:
        traceback.print_exc()
        addr = None
    if addr is not None:
        for row in range(addr.StartRow, addr.EndRow + 1):
            for col in range(addr.StartColumn, addr.EndColumn + 1):
                if len(lines) >= _MAX_EXPLAIN_CELLS:
                    lines.append("... (more cells not shown)")
                    return {"host": Settings.load(APP_NAME).ollama_host, "cells": lines}
                cell = sheet.getCellByPosition(col, row)
                formula, shown = cell.getFormula(), cell.getString()
                if not formula and not shown:
                    continue
                if formula.startswith("="):
                    lines.append(f"{_cell_name(col, row)}: {formula}  ->  {shown}")
                else:
                    lines.append(f"{_cell_name(col, row)}: {shown}")
    return {"host": Settings.load(APP_NAME).ollama_host, "cells": lines}


def ask_explain(request: dict):
    """Background thread, no UNO, no chat history: explain request["cells"].
    Returns (explanation, model); ("", "") when there is nothing to explain."""
    if not request["cells"]:
        return "", ""
    model, _params = best_model(request["host"])
    language = f"Write your reply in {LANGUAGE_NAMES.get(request.get('language') or 'en', 'English')}."
    reply = chat(request["host"], model, [
        {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
        {"role": "user", "content": EXPLAIN_INSTRUCTION.format(
            cells="\n".join(request["cells"]), language=language)},
    ], timeout=request.get("timeout", DEFAULT_TIMEOUT), cancel=request.get("cancel"))
    return reply.strip(), model


def ask_turn(request: dict):
    """_ask_turn plus SKAI_ Memory: what other SKAI apps know goes in, the exchange is recorded."""
    from sk_office_ai.skai_memory_bridge import around_turn
    return around_turn("sk_office_calc", request, _ask_turn)
