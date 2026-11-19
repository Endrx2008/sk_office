# Copyright (C) 2026 Enrico Miot (EndrX)
# Licensed under GNU GPL v3 or later — see the LICENSE file or <https://www.gnu.org/licenses/>
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys
import traceback

import uno
import unohelper
from com.sun.star.awt import (
    XActionListener, XFocusListener, XKeyHandler, XKeyListener, XMouseListener, XWindowListener,
)
from com.sun.star.awt.Key import ESCAPE, RETURN
from com.sun.star.awt.KeyModifier import MOD1
from com.sun.star.datatransfer import XTransferable
from com.sun.star.awt.PosSize import POSSIZE
from com.sun.star.ui import XSidebarPanel, XToolPanel, XUIElement

sys.path.insert(0, os.path.dirname(__file__))
from calc_logic import (  # noqa: E402
    APP_NAME,
    apply_turn, ask_explain, ask_turn, forget, prepare_explain, prepare_turn,
)
import calc_busy  # noqa: E402
from sk_office_ai.async_call import main_thread_poster, run_in_background  # noqa: E402
from sk_office_ai.ollama_client import CancelToken  # noqa: E402
from sk_office_ai.conflict_dialog import ask_conflict, ask_confirm  # noqa: E402
from sk_office_ai.i18n import Translator, is_no_model  # noqa: E402
from sk_office_ai.config import Settings  # noqa: E402
from sk_office_ai.json_chat import DEFAULT_TIMEOUT, full_features  # noqa: E402
from sk_office_ai.model_select import best_model  # noqa: E402


def _is_no_reply_error(err):
    """A request that ran out of time or came back empty - worth a retry
    with more time (unlike e.g. Ollama not running at all)."""
    text = str(err).lower()
    return "timed out" in text or "empty reply" in text


class _SendListener(unohelper.Base, XActionListener):
    def __init__(self, panel):
        self.panel = panel

    def actionPerformed(self, event):
        self.panel._on_send()

    def disposing(self, event):
        pass


class _ExplainListener(unohelper.Base, XActionListener):
    def __init__(self, panel):
        self.panel = panel

    def actionPerformed(self, event):
        self.panel._on_explain()

    def disposing(self, event):
        pass


class _PromptKeyListener(unohelper.Base, XKeyListener):
    """Ctrl+Enter in the prompt box sends it. (Esc is handled by
    _EscapeHandler: the sidebar swallows Esc before the prompt sees it.)"""

    def __init__(self, panel):
        self.panel = panel

    def keyPressed(self, event):
        if event.KeyCode == RETURN and event.Modifiers & MOD1:
            self.panel._on_ctrl_enter()

    def keyReleased(self, event):
        pass

    def disposing(self, event):
        pass


class _ErrorLinkListener(unohelper.Base, XMouseListener):
    def __init__(self, panel):
        self.panel = panel

    def mousePressed(self, event):
        self.panel._copy_error()

    def mouseReleased(self, event):
        pass

    def mouseEntered(self, event):
        pass

    def mouseExited(self, event):
        pass

    def disposing(self, event):
        pass


class _TextTransferable(unohelper.Base, XTransferable):
    """Plain-text clipboard payload for SystemClipboard.setContents()."""

    def __init__(self, text):
        self.text = text
        flavor = uno.createUnoStruct("com.sun.star.datatransfer.DataFlavor")
        flavor.MimeType = "text/plain;charset=utf-16"
        flavor.HumanPresentableName = "Unicode-Text"
        flavor.DataType = uno.getTypeByName("string")
        self.flavor = flavor

    def getTransferData(self, flavor):
        return self.text

    def getTransferDataFlavors(self):
        return (self.flavor,)

    def isDataFlavorSupported(self, flavor):
        return flavor.MimeType.startswith("text/plain")


class _EscapeHandler(unohelper.Base, XKeyHandler):
    """Esc stops a running turn. The sidebar consumes the Esc key PRESS (it
    moves focus back to the document - verified: the prompt's key listener
    never receives it), but the RELEASE then arrives at the document, so
    this is registered on the document's controller and acts on release.
    It never consumes the key, so Esc keeps its normal meaning in the
    document, and it does nothing unless a turn is running."""

    def __init__(self, panel):
        self.panel = panel

    def keyPressed(self, event):
        return False

    def keyReleased(self, event):
        if event.KeyCode == ESCAPE:
            self.panel._on_escape()
        return False

    def disposing(self, event):
        pass


class _HistoryFocusListener(unohelper.Base, XFocusListener):
    """The chat view is read-only output: whenever it gets focus (a click),
    hand focus to the prompt box."""

    def __init__(self, panel):
        self.panel = panel

    def focusGained(self, event):
        self.panel.prompt_ctrl.setFocus()

    def focusLost(self, event):
        pass

    def disposing(self, event):
        pass


class _RetryListener(unohelper.Base, XActionListener):
    def __init__(self, panel):
        self.panel = panel

    def actionPerformed(self, event):
        self.panel._on_retry()

    def disposing(self, event):
        pass


class _CopyChatListener(unohelper.Base, XActionListener):
    def __init__(self, panel):
        self.panel = panel

    def actionPerformed(self, event):
        self.panel._copy_chat()

    def disposing(self, event):
        pass


class _ClearListener(unohelper.Base, XActionListener):
    def __init__(self, panel):
        self.panel = panel

    def actionPerformed(self, event):
        self.panel._on_clear()

    def disposing(self, event):
        pass


class _ResizeListener(unohelper.Base, XWindowListener):
    def __init__(self, panel):
        self.panel = panel

    def windowResized(self, event):
        self.panel._layout(event.Width, event.Height)

    def windowMoved(self, event):
        pass

    def windowShown(self, event):
        pass

    def windowHidden(self, event):
        pass

    def disposing(self, event):
        pass


class AiPanel(unohelper.Base, XUIElement, XToolPanel, XSidebarPanel):
    def __init__(self, ctx, resource_url, parent_window, frame):
        self.ctx = ctx
        self.resource_url = resource_url
        self.frame = frame
        self.smgr = ctx.ServiceManager
        self.history = []
        self.t = Translator(ctx)
        self._turn = None  # CancelToken of the running turn, if any
        self._live_entry = None  # chat entry index already showing this turn's reply
        self._notice_on = False
        self._retry_on = False
        self._retry_prompt = None
        self._retry_count = 0
        self._size = (300, 500)
        try:
            self.window = self._build_window(parent_window)
        except Exception:
            traceback.print_exc()
            raise

    def _create(self, service):
        return self.smgr.createInstanceWithContext(service, self.ctx)

    def _add_child(self, container, container_peer, toolkit, name, control_service, model_service, **props):
        model = self._create(model_service)
        for key, value in props.items():
            setattr(model, key, value)
        control = self._create(control_service)
        control.setModel(model)
        control.createPeer(toolkit, container_peer)
        container.addControl(name, control)
        return control

    def _build_window(self, parent_peer):
        toolkit = self.frame.getContainerWindow().getToolkit()

        self.container_model = self._create("com.sun.star.awt.UnoControlContainerModel")
        container = self._create("com.sun.star.awt.UnoControlContainer")
        container.setModel(self.container_model)

        container.setPosSize(0, 0, 300, 500, POSSIZE)
        container.createPeer(toolkit, parent_peer)
        container.setVisible(True)
        container_peer = container.getPeer()

        self.clear_ctrl = self._add_child(
            container, container_peer, toolkit, "clear",
            "com.sun.star.awt.UnoControlButton", "com.sun.star.awt.UnoControlButtonModel",
            Label=self.t("clear_chat"),
        )
        self.copy_chat_ctrl = self._add_child(
            container, container_peer, toolkit, "copy_chat",
            "com.sun.star.awt.UnoControlButton", "com.sun.star.awt.UnoControlButtonModel",
            Label="", ImageURL="private:graphicrepository/cmd/sc_copy.png",
            HelpText=self.t("copy_chat"), Tabstop=False,
        )
        self.notice_title_ctrl = self._add_child(
            container, container_peer, toolkit, "notice_title",
            "com.sun.star.awt.UnoControlFixedText", "com.sun.star.awt.UnoControlFixedTextModel",
            Label=self.t("limited_title"), TextColor=0x9A5B00, FontWeight=150.0,
        )
        self.notice_text_ctrl = self._add_child(
            container, container_peer, toolkit, "notice_text",
            "com.sun.star.awt.UnoControlFixedText", "com.sun.star.awt.UnoControlFixedTextModel",
            Label="", MultiLine=True, TextColor=0x6B4A00,
        )
        self.notice_title_ctrl.setVisible(False)
        self.notice_text_ctrl.setVisible(False)
        self.retry_ctrl = self._add_child(
            container, container_peer, toolkit, "retry",
            "com.sun.star.awt.UnoControlButton", "com.sun.star.awt.UnoControlButtonModel",
            Label="", ImageURL="private:graphicrepository/cmd/sc_reload.png",
            HelpText=self.t("retry"), Tabstop=False,
        )
        self.retry_label_ctrl = self._add_child(
            container, container_peer, toolkit, "retry_label",
            "com.sun.star.awt.UnoControlFixedText", "com.sun.star.awt.UnoControlFixedTextModel",
            Label=self.t("time_limit_removed"), TextColor=0x9A5B00,
        )
        self.retry_ctrl.setVisible(False)
        self.retry_label_ctrl.setVisible(False)
        # The chat view is a grid with selection turned off, not an edit
        # field: a read-only multi-line edit still shows a caret and lets
        # the mouse drag-select (VCL keeps tracking the drag; nothing
        # exposed through UNO stops it - tried focus redirection,
        # HideInactiveSelection, collapsing on drag, disabling mid-drag).
        # One grid row per wrapped display line; see _rebuild_chat_rows.
        columns = self._create("com.sun.star.awt.grid.DefaultGridColumnModel")
        column = columns.createColumn()
        column.Flexibility = 1
        columns.addColumn(column)
        self.history_ctrl = self._add_child(
            container, container_peer, toolkit, "history",
            "com.sun.star.awt.grid.UnoControlGrid", "com.sun.star.awt.grid.UnoControlGridModel",
            ColumnModel=columns,
            GridDataModel=self._create("com.sun.star.awt.grid.DefaultGridDataModel"),
            ShowRowHeader=False, ShowColumnHeader=False, HScroll=False, VScroll=True,
            UseGridLines=False, Tabstop=False, Border=1,
            SelectionModel=uno.Enum("com.sun.star.view.SelectionType", "NONE"),
            BackgroundColor=0xFFFFFF, TextColor=0x000000,
        )
        self.history_ctrl.getModel().RowBackgroundColors = uno.Any("[]long", (0xFFFFFF,))
        self._chat_entries = []
        self.prompt_ctrl = self._add_child(
            container, container_peer, toolkit, "prompt",
            "com.sun.star.awt.UnoControlEdit", "com.sun.star.awt.UnoControlEditModel",
            MultiLine=True, Text="",
        )
        self.send_ctrl = self._add_child(
            container, container_peer, toolkit, "send",
            "com.sun.star.awt.UnoControlButton", "com.sun.star.awt.UnoControlButtonModel",
            Label=self.t("send"),
        )
        self.explain_ctrl = self._add_child(
            container, container_peer, toolkit, "explain",
            "com.sun.star.awt.UnoControlButton", "com.sun.star.awt.UnoControlButtonModel",
            Label=self.t("explain_cells"),
        )
        self.status_ctrl = self._add_child(
            container, container_peer, toolkit, "status",
            "com.sun.star.awt.UnoControlFixedText", "com.sun.star.awt.UnoControlFixedTextModel",
            Label=self.t("hint_calc"), MultiLine=True,
        )

        self.error_ctrl = self._add_child(
            container, container_peer, toolkit, "error",
            "com.sun.star.awt.UnoControlFixedText", "com.sun.star.awt.UnoControlFixedTextModel",
            Label=self.t("error_link"), TextColor=0xCC0000,
        )
        self.error_ctrl.setVisible(False)
        self.error_ctrl.addMouseListener(_ErrorLinkListener(self))
        self.last_error = ""

        self.send_ctrl.addActionListener(_SendListener(self))
        self.explain_ctrl.addActionListener(_ExplainListener(self))
        self.clear_ctrl.addActionListener(_ClearListener(self))
        self.copy_chat_ctrl.addActionListener(_CopyChatListener(self))
        self.retry_ctrl.addActionListener(_RetryListener(self))
        self.prompt_ctrl.addKeyListener(_PromptKeyListener(self))
        self.history_ctrl.addFocusListener(_HistoryFocusListener(self))
        try:
            self.frame.getController().addKeyHandler(_EscapeHandler(self))
        except Exception:
            traceback.print_exc()
        container.addWindowListener(_ResizeListener(self))

        self._container = container
        self._layout(300, 500)
        self._detect_model()
        calc_busy.subscribe(self._on_busy_change)
        return container

    def _layout(self, width, height):
        margin = 8
        prompt_h = max(25, int(height * 0.1))
        button_h = 24
        y = margin

        self._size = (width, height)
        clear_w = min(100, width - 2 * margin)
        self.clear_ctrl.setPosSize(width - margin - clear_w, y, clear_w, 22, POSSIZE)
        copy_w = 26
        self.copy_chat_ctrl.setPosSize(width - margin - clear_w - 4 - copy_w, y, copy_w, 22, POSSIZE)
        self.notice_title_ctrl.setPosSize(margin, y + 4, max(10, width - 4 * margin - clear_w - copy_w), 18, POSSIZE)
        y += 22 + 4
        notice_h = 0
        if self._notice_on:
            notice_h = 64
            self.notice_text_ctrl.setPosSize(margin, y, width - 2 * margin, notice_h, POSSIZE)
            y += notice_h + 4
        retry_h = 0
        if self._retry_on:
            retry_h = 26
            self.retry_ctrl.setPosSize(margin, y, 28, 24, POSSIZE)
            self.retry_label_ctrl.setPosSize(margin + 34, y + 5, max(10, width - 2 * margin - 34), 18, POSSIZE)
            y += retry_h + 2
        history_h = max(80, int(height * 0.55) - notice_h - retry_h)

        self.history_ctrl.setPosSize(margin, y, width - 2 * margin, history_h, POSSIZE)
        self._rebuild_chat_rows()
        y += history_h + margin

        self.prompt_ctrl.setPosSize(margin, y, width - 2 * margin, prompt_h, POSSIZE)
        y += prompt_h + margin

        button_w = int((width - 2 * margin - margin) / 2)
        self.send_ctrl.setPosSize(margin, y, button_w, button_h, POSSIZE)
        self.explain_ctrl.setPosSize(margin + button_w + margin, y, button_w, button_h, POSSIZE)
        y += button_h + margin

        error_h = 20
        self.error_ctrl.setPosSize(margin, y, width - 2 * margin, error_h, POSSIZE)
        y += error_h + margin

        status_h = max(20, height - y - margin)
        self.status_ctrl.setPosSize(margin, y, width - 2 * margin, status_h, POSSIZE)

    def _append_history(self, text):
        self._chat_entries.append(text)
        self._rebuild_chat_rows()

    def _wrap_chat_line(self, line, font, max_px):
        """Greedy word wrap of one line to max_px, measured with the grid's
        own font (falls back to ~7px per character)."""
        def width(t):
            return font.getStringWidth(t) if font is not None else 7 * len(t)
        if width(line) <= max_px:
            return [line]
        out, current = [], ""
        for word in line.split(" "):
            candidate = f"{current} {word}" if current else word
            if width(candidate) <= max_px:
                current = candidate
                continue
            if current:
                out.append(current)
            while width(word) > max_px and len(word) > 1:
                cut = len(word)
                while cut > 1 and width(word[:cut]) > max_px:
                    cut -= 1
                out.append(word[:cut])
                word = word[cut:]
            current = word
        out.append(current)
        return out

    def _rebuild_chat_rows(self):
        """Re-wrap every chat entry to the current width into grid rows and
        scroll to the end."""
        data = self.history_ctrl.getModel().GridDataModel
        data.removeAllRows()
        if not self._chat_entries:
            return
        try:
            peer = self.history_ctrl.getPeer()
            font = peer.getFont(self.history_ctrl.getModel().FontDescriptor)
        except Exception:
            font = None
        max_px = max(40, self.history_ctrl.getPosSize().Width - 30)
        rows = []
        for i, entry in enumerate(self._chat_entries):
            if i:
                rows.append("")
            for line in entry.split("\n"):
                rows += self._wrap_chat_line(line, font, max_px)
        data.addRows(tuple("" for _ in rows), tuple((r,) for r in rows))
        try:
            self.history_ctrl.goToCell(0, len(rows) - 1)
        except Exception:
            traceback.print_exc()

    def _set_busy(self, busy):
        # While a turn runs, Send becomes Stop (like Claude's stop button).
        self.send_ctrl.getModel().Label = self.t("stop") if busy else self.t("send")
        if getattr(self, "explain_ctrl", None) is not None:
            self.explain_ctrl.getModel().Enabled = not busy
        if not busy:
            calc_busy.release("chat")

    def _on_busy_change(self, owner):
        """The formula finder is using the AI: grey out Send and Explain
        until it is done (one AI request at a time)."""
        if owner == "chat":
            return
        free = owner is None
        self.send_ctrl.getModel().Enabled = free
        self.explain_ctrl.getModel().Enabled = free and self._turn is None
        if owner == "finder":
            self.status_ctrl.getModel().Label = self.t("busy_finder")
        elif self._turn is None and self.status_ctrl.getModel().Label == self.t("busy_finder"):
            self.status_ctrl.getModel().Label = self.t("hint_calc")

    def _fail(self, exc, tb_text):
        if is_no_model(exc):
            self._show_no_model()
            self.status_ctrl.getModel().Label = self.t("no_model_title")
            return
        print(tb_text)
        self.status_ctrl.getModel().Label = self.t("error", error=exc)
        self._show_error(f"{exc}\n\n{tb_text}")

    def _show_no_model(self):
        """No model installed: the alert takes the place of the model name /
        limited-mode notice at the top until a model is found again."""
        title = self.notice_title_ctrl.getModel()
        title.Label, title.TextColor = self.t("no_model_title"), 0xC00000
        self.notice_text_ctrl.getModel().Label = self.t("no_model_text")
        self.notice_title_ctrl.setVisible(True)
        self.notice_text_ctrl.setVisible(True)
        self._notice_on = True
        self._layout(*self._size)

    def _resolve_merge(self, kind, sections):
        # Calc keeps its per-cell conflict dialog (mine / AI's / both)
        return ask_conflict(self.ctx, self.frame.getContainerWindow(), kind, sections)

    def _confirm_changes(self, sections):
        return ask_confirm(self.ctx, self.frame.getContainerWindow(), sections)

    def _start_send(self, prompt_text, prepare_turn, ask_turn, finish, timeout=DEFAULT_TIMEOUT):
        """Read the document now (main thread), ask the model on a worker
        thread, then finish(doc, request, prompt_text, result, model) back
        on the main thread - LibreOffice stays usable meanwhile. The turn
        owns a CancelToken: Stop/Esc cancels it, and a reply that arrives
        for a cancelled (or superseded) turn is ignored.

        While it runs, the status line shows the phase and the chat reply
        is shown as soon as it is ready; a stopped or failed turn changes
        nothing in the sheet."""
        self._hide_error()
        try:
            doc = self.frame.getController().getModel()
            request = prepare_turn(doc, self.history, prompt_text)
        except Exception as exc:
            self._fail(exc, traceback.format_exc())
            return
        if not calc_busy.try_acquire("chat"):
            self.status_ctrl.getModel().Label = self.t("busy_finder")
            return
        token = CancelToken()
        request["cancel"] = token
        request["language"] = self.t.lang
        request["timeout"] = timeout
        self._turn = token
        # the request shows in the chat at once; the reply fills it in later
        self._append_history(f"{self.t('you')}: {prompt_text}\n{self.t('ai')}: \u2026")
        self._live_entry = len(self._chat_entries) - 1
        self._set_busy(True)
        self.status_ctrl.getModel().Label = self.t("calc_working")
        post = main_thread_poster(self.ctx)

        def on_phase(name):
            post(lambda: self._show_phase(token, name))

        def on_reply(reply):
            post(lambda: self._show_live_reply(token, prompt_text, reply))

        request["on_reply"] = on_reply
        request["on_phase"] = on_phase

        def done(res, err, tb_text):
            if self._turn is not token:
                return
            try:
                if err is not None:
                    if self._live_entry is not None and self._chat_entries[self._live_entry].endswith("\u2026"):
                        self._set_live_entry(
                            f"{self.t('you')}: {prompt_text}\n{self.t('ai')}: {self.t('no_reply')}")
                    self._live_entry = None
                    self._fail(err, tb_text)
                    if _is_no_reply_error(err):
                        self._offer_retry(prompt_text)
                    return
                result, model = res
                finish(doc, request, prompt_text, result, model)
                if "_model_params" in result and not model.startswith("("):
                    self._update_notice(model, result["_model_params"])
                if self.prompt_ctrl.getModel().Text.strip() == prompt_text:
                    self.prompt_ctrl.getModel().Text = ""
            except Exception as exc:
                self._fail(exc, traceback.format_exc())
            finally:
                self._turn = None
                self._set_busy(False)

        run_in_background(self.ctx, lambda: ask_turn(request), done)

    def _set_live_entry(self, text):
        if self._live_entry is None:
            self._append_history(text)
        else:
            self._chat_entries[self._live_entry] = text
            self._live_entry = None
            self._rebuild_chat_rows()

    def _show_live_reply(self, token, prompt_text, reply):
        if self._turn is not token or self._live_entry is None:
            return
        self._chat_entries[self._live_entry] = f"{self.t('you')}: {prompt_text}\n{self.t('ai')}: {reply}"
        self._rebuild_chat_rows()

    def _show_phase(self, token, name):
        """Status line: drafting (the model is composing, nothing shown
        yet), writing (text arriving in the document), replying,
        formatting."""
        if self._turn is token:
            self.status_ctrl.getModel().Label = self.t(name)

    def _detect_model(self):
        """Find the model in the background and show the limited-mode notice
        if it is a small one."""
        def work():
            return best_model(Settings.load(APP_NAME).ollama_host)

        def done(res, err, tb_text):
            if err is None:
                self._update_notice(*res)
            elif is_no_model(err):
                self._show_no_model()

        run_in_background(self.ctx, work, done)

    def _update_notice(self, model, params):
        """Top-left: the model's name, or - for a small model - the orange
        limited-mode title, with the explanation (which names the model)
        below it."""
        on = not full_features(params)
        title = self.notice_title_ctrl.getModel()
        if on:
            self.notice_text_ctrl.getModel().Label = self.t("limited_text", model=model)
            title.Label, title.TextColor = self.t("limited_title"), 0x9A5B00
        else:
            title.Label, title.TextColor = model, 0x404040
        self.notice_title_ctrl.setVisible(True)
        if on != self._notice_on:
            self._notice_on = on
            self.notice_text_ctrl.setVisible(on)
            self._layout(*self._size)

    def _result_notes(self, result):
        """Localized notes about what the plugin itself held back."""
        notes = []
        if result.get("skipped_other_kinds"):
            notes.append(self.t("skipped_kinds"))
        if result.get("_declined"):
            notes.append(self.t("changes_cancelled"))
        return "".join(f"\n({n})" for n in notes)

    def _cancel(self):
        """Stop button / Esc: abort the running request (Ollama stops
        generating), drop the turn and empty the prompt box."""
        token, self._turn = self._turn, None
        if token is None:
            return
        token.cancel()
        if self._live_entry is not None:
            self._chat_entries[self._live_entry] += f"\n({self.t('cancelled')})"
            self._live_entry = None
            self._rebuild_chat_rows()
        self.prompt_ctrl.getModel().Text = ""
        self.status_ctrl.getModel().Label = self.t("cancelled")
        self._set_busy(False)

    def _on_clear(self):
        """Clear chat: cancel any running turn, empty the chat and forget
        the session memory (history, and anything the logic remembers)."""
        self._cancel()
        self.history.clear()
        self._chat_entries.clear()
        self._retry_count = 0
        self._retry_prompt = None
        self._hide_retry()
        self._rebuild_chat_rows()
        self._hide_error()
        try:
            forget(self.frame.getController().getModel())
        except Exception:
            traceback.print_exc()
        self.status_ctrl.getModel().Label = self.t("chat_cleared")

    def _on_send(self):
        if self._turn is not None:
            self._cancel()
            return
        prompt_text = self.prompt_ctrl.getModel().Text.strip()
        if prompt_text:
            self._retry_count = 0
            self._hide_retry()
            self._start_send(prompt_text, prepare_turn, ask_turn, self._finish_send)

    def _offer_retry(self, prompt_text):
        """Show the retry wheel at the top of the chat. After three retries
        the next one runs with no time limit, and the label says so."""
        self._retry_prompt = prompt_text
        self.retry_label_ctrl.setVisible(self._retry_count >= 3)
        if not self._retry_on:
            self._retry_on = True
            self.retry_ctrl.setVisible(True)
            self._layout(*self._size)

    def _hide_retry(self):
        if self._retry_on:
            self._retry_on = False
            self.retry_ctrl.setVisible(False)
            self.retry_label_ctrl.setVisible(False)
            self._layout(*self._size)

    def _on_retry(self):
        """Re-send the prompt that got no reply, with one more minute of
        time limit per retry (6, 7, 8 minutes), and none from the 4th on."""
        if self._turn is not None or not self._retry_prompt:
            return
        self._retry_count += 1
        timeout = None if self._retry_count > 3 else DEFAULT_TIMEOUT + 60 * self._retry_count
        prompt_text = self._retry_prompt
        self._hide_retry()
        self._start_send(prompt_text, prepare_turn, ask_turn, self._finish_send, timeout=timeout)

    def _on_ctrl_enter(self):
        if self._turn is None:
            self._on_send()

    def _on_escape(self):
        self._cancel()

    def _finish_send(self, doc, request, prompt_text, result, model):
        if not result.get("message") and not result.get("actions"):
            # No reply: shown, not remembered, and offered for a retry.
            self._set_live_entry(f"{self.t('you')}: {prompt_text}\n{self.t('ai')}: {self.t('no_reply')}")
            self.status_ctrl.getModel().Label = self.t("no_reply")
            self._offer_retry(prompt_text)
            return
        self._retry_count = 0
        self._hide_retry()
        chat_text, wrote, model_used, confirmation = apply_turn(
            doc, request, result, model, self._resolve_merge, self._confirm_changes
        )
        self.history.append({"role": "user", "content": prompt_text})
        assistant_content = chat_text
        if confirmation:
            # Fold the ACTUAL outcome of any actions into the assistant's
            # own history entry, so next turn's grounding reflects what
            # really landed in the document, not just what was asked for.
            assistant_content = f"{chat_text}\n\n[{confirmation}]"
        self.history.append({"role": "assistant", "content": assistant_content})
        shown_reply = result.get("message") or self.t("no_reply")
        display = f"{self.t('you')}: {prompt_text}\n{self.t('ai')}: {shown_reply}"
        notes = self._result_notes(result)
        if result.get("actions") and not wrote:
            # Text was generated but nothing landed: say so, rather than
            # leaving only the AI's own reply in the chat.
            notes += f"\n({self.t('nothing_applied')})"
        self._set_live_entry(display + notes)
        # no "done (model: ...)" line: the model's name is shown at the top
        self.status_ctrl.getModel().Label = self.t("hint_calc")

    def _on_explain(self):
        """Explain the selected cells (or the whole used range) as they are
        right now: read live,
        sent without the chat history (the answer is about the sheet, not
        the conversation). Shown in the chat and kept in the history so the
        user can ask follow-up questions."""
        if self._turn is not None:
            return
        self._hide_error()
        try:
            request = prepare_explain(self.frame.getController().getModel())
        except Exception as exc:
            self._fail(exc, traceback.format_exc())
            return
        if not request["cells"]:
            self._append_history(f"{self.t('you')}: {self.t('explain_cells_request')}\n"
                                 f"{self.t('ai')}: {self.t('cells_empty')}")
            return
        if not calc_busy.try_acquire("chat"):
            self.status_ctrl.getModel().Label = self.t("busy_finder")
            return
        token = CancelToken()
        request["cancel"] = token
        request["language"] = self.t.lang
        self._turn = token
        self._set_busy(True)
        self.status_ctrl.getModel().Label = self.t("explaining_cells")
        self._append_history(f"{self.t('you')}: {self.t('explain_cells_request')}\n{self.t('ai')}: \u2026")
        self._live_entry = len(self._chat_entries) - 1

        def done(res, err, tb_text):
            if self._turn is not token:
                return
            try:
                if err is not None:
                    self._set_live_entry(f"{self.t('you')}: {self.t('explain_cells_request')}\n"
                                         f"{self.t('ai')}: {self.t('no_reply')}")
                    self._fail(err, tb_text)
                    return
                explanation, _model = res
                cells = "; ".join(request["cells"])
                self.history.append({"role": "user", "content": f"(Explain these cells: {cells})"})
                self.history.append({"role": "assistant", "content": explanation})
                self._set_live_entry(f"{self.t('you')}: {self.t('explain_cells_request')}\n"
                                     f"{self.t('ai')}: {explanation or self.t('no_reply')}")
                self.status_ctrl.getModel().Label = self.t("hint_calc")
            except Exception as exc:
                self._fail(exc, traceback.format_exc())
            finally:
                self._turn = None
                self._set_busy(False)

        run_in_background(self.ctx, lambda: ask_explain(request), done)

    def _show_error(self, text):
        self.last_error = text
        self.error_ctrl.getModel().Label = self.t("error_link")
        self.error_ctrl.setVisible(True)

    def _hide_error(self):
        self.last_error = ""
        self.error_ctrl.setVisible(False)

    def _copy_chat(self):
        """Copy the whole chat (as shown, unwrapped) to the clipboard."""
        text = "\n\n".join(self._chat_entries)
        if not text:
            return
        try:
            clipboard = self._create("com.sun.star.datatransfer.clipboard.SystemClipboard")
            clipboard.setContents(_TextTransferable(text), None)
            self.status_ctrl.getModel().Label = self.t("chat_copied")
        except Exception as exc:
            traceback.print_exc()
            self.status_ctrl.getModel().Label = self.t("copy_failed", error=exc)

    def _copy_error(self):
        if not self.last_error:
            return
        try:
            clipboard = self._create("com.sun.star.datatransfer.clipboard.SystemClipboard")
            clipboard.setContents(_TextTransferable(self.last_error), None)
            self.error_ctrl.getModel().Label = self.t("error_copied")
        except Exception as exc:
            traceback.print_exc()
            self.error_ctrl.getModel().Label = self.t("copy_failed", error=exc)

    # XToolPanel
    def createAccessible(self, parent):
        return self.window.getAccessibleContext()

    def getWindow(self):
        return self.window

    # XSidebarPanel
    def getHeightForWidth(self, width):
        layout_size = uno.createUnoStruct("com.sun.star.ui.LayoutSize")
        layout_size.Minimum = 350
        layout_size.Preferred = 500
        layout_size.Maximum = -1
        return layout_size

    def getMinimalWidth(self):
        return 220

    # XUIElement
    def getFrame(self):
        return self.frame

    def getResourceURL(self):
        return self.resource_url

    def getType(self):
        return uno.getConstantByName("com.sun.star.ui.UIElementType.TOOLPANEL")

    def getRealInterface(self):
        return self
