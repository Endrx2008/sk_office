"""The "Find a formula" sidebar panel for Calc: a separate bar from the
chat, only for this. The user describes what they want to compute; the
model searches LibreOffice's catalogue of functions (keywords), then picks
one of the results by number. The panel shows the chosen function's
syntax, description and arguments, and the other functions found (click
one to see it)."""

import os
import sys
import traceback

import uno
import unohelper
from com.sun.star.awt import XActionListener, XKeyListener, XMouseListener, XWindowListener
from com.sun.star.awt.Key import RETURN
from com.sun.star.awt.KeyModifier import MOD1
from com.sun.star.awt.PosSize import POSSIZE
from com.sun.star.ui import XSidebarPanel, XToolPanel, XUIElement

sys.path.insert(0, os.path.dirname(__file__))
from calc_formulas import absolute, find_formula, function_formula, load_catalog, syntax  # noqa: E402
from calc_logic import APP_NAME  # noqa: E402
import calc_busy  # noqa: E402
from sk_libre_ai.async_call import main_thread_poster, run_in_background  # noqa: E402
from sk_libre_ai.config import Settings  # noqa: E402
from sk_libre_ai.i18n import Translator  # noqa: E402
from sk_libre_ai.ollama_client import CancelToken  # noqa: E402

MAX_OTHERS = 5


def _cell_ref(col, row):
    name, col = "", col + 1
    while col > 0:
        col, rem = divmod(col - 1, 26)
        name = chr(ord("A") + rem) + name
    return f"{name}{row + 1}"


class _Action(unohelper.Base, XActionListener):
    def __init__(self, fn):
        self.fn = fn

    def actionPerformed(self, event):
        self.fn()

    def disposing(self, event):
        pass


class _Click(unohelper.Base, XMouseListener):
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


class _CtrlEnter(unohelper.Base, XKeyListener):
    def __init__(self, fn):
        self.fn = fn

    def keyPressed(self, event):
        if event.KeyCode == RETURN and event.Modifiers & MOD1:
            self.fn()

    def keyReleased(self, event):
        pass

    def disposing(self, event):
        pass


class _Resize(unohelper.Base, XWindowListener):
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


class FormulaPanel(unohelper.Base, XUIElement, XToolPanel, XSidebarPanel):
    def __init__(self, ctx, resource_url, parent_window, frame):
        self.ctx = ctx
        self.resource_url = resource_url
        self.frame = frame
        self.smgr = ctx.ServiceManager
        self.t = Translator(ctx)
        self._turn = None
        self._others = []
        self._recommended = None
        self._shown = None  # the function shown now (what Apply inserts)
        self._size = (300, 420)
        try:
            self.window = self._build_window(parent_window)
        except Exception:
            traceback.print_exc()
            raise

    def _create(self, service):
        return self.smgr.createInstanceWithContext(service, self.ctx)

    def _add(self, name, kind, **props):
        model = self._create(f"com.sun.star.awt.UnoControl{kind}Model")
        for key, value in props.items():
            setattr(model, key, value)
        control = self._create(f"com.sun.star.awt.UnoControl{kind}")
        control.setModel(model)
        control.createPeer(self._toolkit, self._container.getPeer())
        self._container.addControl(name, control)
        return control

    def _build_window(self, parent_peer):
        self._toolkit = self.frame.getContainerWindow().getToolkit()
        container = self._create("com.sun.star.awt.UnoControlContainer")
        container.setModel(self._create("com.sun.star.awt.UnoControlContainerModel"))
        container.setPosSize(0, 0, 300, 420, POSSIZE)
        container.createPeer(self._toolkit, parent_peer)
        container.setVisible(True)
        self._container = container

        self.intro = self._add("intro", "FixedText", Label=self.t("finder_intro"), MultiLine=True)
        self.prompt = self._add("prompt", "Edit", MultiLine=True, Text="")
        self.search_btn = self._add("search", "Button", Label=self.t("finder_button"))
        self.status = self._add("status", "FixedText", Label="", MultiLine=True)
        self.name = self._add("name", "FixedText", Label="", FontWeight=150.0, MultiLine=True)
        self.desc = self._add("desc", "FixedText", Label="", MultiLine=True)
        self.args = self._add("args", "FixedText", Label="", MultiLine=True, TextColor=0x404040)
        self.apply_btn = self._add("apply", "Button", Label=self.t("finder_apply"),
                                   HelpText=self.t("finder_apply_help"))
        self.apply_btn.setVisible(False)
        self.others_title = self._add("others_title", "FixedText", Label=self.t("finder_others"),
                                      FontWeight=150.0)
        self.others_title.setVisible(False)
        self.other_ctrls = []
        for i in range(MAX_OTHERS):
            ctrl = self._add(f"other_{i}", "FixedText", Label="", TextColor=0x1F4E9C,
                             HelpText=self.t("finder_show"))
            ctrl.addMouseListener(_Click(lambda i=i: self._show_other(i)))
            ctrl.setVisible(False)
            self.other_ctrls.append(ctrl)

        self.search_btn.addActionListener(_Action(self._on_search))
        self.apply_btn.addActionListener(_Action(self._on_apply))
        self.prompt.addKeyListener(_CtrlEnter(self._on_ctrl_enter))
        container.addWindowListener(_Resize(self))
        self._layout(300, 420)
        calc_busy.subscribe(self._on_busy_change)
        return container

    def _lines(self, text, width):
        """Rough number of wrapped lines text needs at this width (~6.5 px
        per character), so each block takes only the height it needs."""
        per_line = max(10, int(width / 6.5))
        return sum(max(1, -(-len(part) // per_line)) for part in (text or "").split("\n")) if text else 0

    def _layout(self, width, height):
        m, line = 8, 15
        w = max(40, width - 2 * m)
        self._size = (width, height)
        y = m

        def place(ctrl, text, min_lines=1, gap=4):
            nonlocal y
            n = max(min_lines, self._lines(text, w))
            ctrl.setPosSize(m, y, w, n * line + 2, POSSIZE)
            y += n * line + 2 + gap

        place(self.intro, self.intro.getModel().Label, 2)
        self.prompt.setPosSize(m, y, w, 40, POSSIZE)
        y += 44
        self.search_btn.setPosSize(m, y, w, 24, POSSIZE)
        y += 28
        place(self.status, self.status.getModel().Label, 1, 2)
        place(self.name, self.name.getModel().Label, 0, 2)
        place(self.desc, self.desc.getModel().Label, 0, 2)
        place(self.args, self.args.getModel().Label, 0, 4)
        if self._shown is not None:
            self.apply_btn.setPosSize(m, y, w, 24, POSSIZE)
            y += 30
        self.others_title.setPosSize(m, y, w, 17, POSSIZE)
        y += 19
        for ctrl in self.other_ctrls:
            ctrl.setPosSize(m + 6, y, w - 6, 16, POSSIZE)
            y += 17

    # ---- searching -------------------------------------------------------

    def _set_busy(self, busy):
        self.search_btn.getModel().Label = self.t("stop") if busy else self.t("finder_button")
        if not busy:
            calc_busy.release("finder")

    def _on_busy_change(self, owner):
        """The chat is using the AI: grey out the search until it is done
        (one AI request at a time)."""
        if owner == "finder":
            return
        self.search_btn.getModel().Enabled = owner is None
        if owner == "chat":
            self.status.getModel().Label = self.t("busy_chat")
        elif self.status.getModel().Label == self.t("busy_chat"):
            self.status.getModel().Label = ""
            self._layout(*self._size)

    def _on_ctrl_enter(self):
        if self._turn is None:
            self._on_search()

    def _on_search(self):
        if self._turn is not None:  # the button is Stop while searching
            token, self._turn = self._turn, None
            token.cancel()
            self.status.getModel().Label = self.t("cancelled")
            self._set_busy(False)
            return
        text = self.prompt.getModel().Text.strip()
        if not text:
            return
        try:
            catalog = load_catalog(self.ctx)
        except Exception as exc:
            traceback.print_exc()
            self.status.getModel().Label = self.t.error_text(exc)
            return
        if not calc_busy.try_acquire("finder"):
            self.status.getModel().Label = self.t("busy_chat")
            return
        token = CancelToken()
        self._turn = token
        self._set_busy(True)
        self._show(None)
        post = main_thread_poster(self.ctx)

        def on_phase(name):
            post(lambda: self._turn is token and setattr(self.status.getModel(), "Label", self.t(name)))

        request = {"host": Settings.load(APP_NAME).ollama_host, "request": text,
                   "catalog": catalog, "language": self.t.lang, "cancel": token,
                   "on_phase": on_phase}

        def done(res, err, tb_text):
            if self._turn is not token:
                return
            try:
                if err is not None:
                    print(tb_text)
                    self.status.getModel().Label = self.t("error", error=err)
                    return
                result, _model = res
                self._recommended = result["choice"]
                self._others = [fn for fn in result["candidates"] if fn is not result["choice"]]
                if result["choice"] is None:
                    self.status.getModel().Label = (self.t("finder_none_fits")
                                                    if result["candidates"] else
                                                    self.t("finder_nothing_found"))
                else:
                    self.status.getModel().Label = self.t("finder_recommended")
                self._show(result["choice"])
            except Exception as exc:
                traceback.print_exc()
                self.status.getModel().Label = self.t.error_text(exc)
            finally:
                self._turn = None
                self._set_busy(False)

        run_in_background(self.ctx, lambda: find_formula(request), done)

    def _show(self, fn):
        """Show one function (or clear the result), and the others found."""
        self._shown = fn
        self.apply_btn.setVisible(fn is not None)
        if fn is None:
            self.name.getModel().Label = ""
            self.desc.getModel().Label = ""
            self.args.getModel().Label = ""
        elif fn.get("basic"):
            self.name.getModel().Label = f"{self.t(fn['key'])}: {syntax(fn)}"
            self.desc.getModel().Label = self.t(fn["key"] + "_desc")
            refs = [r for r in (absolute(c) for c in fn.get("cells") or []) if r]
            self.args.getModel().Label = (self.t("op_cells_fixed", cells=", ".join(refs))
                                          if fn.get("formula") else self.t("op_no_cells"))
        else:
            # with the arguments read from the request: the complete formula
            self.name.getModel().Label = (f"{fn['name']}: {fn['formula']}" if fn.get("formula")
                                          else syntax(fn))
            self.desc.getModel().Label = f"{fn['description']} ({fn['category']})"
            self.args.getModel().Label = "\n".join(
                [f"• {n}: {d}" for n, d, _optional in fn["args"]]
                + ([self.t("fn_args_from_request")] if fn.get("formula") else []))
        self._layout(*self._size)
        self.others_title.setVisible(bool(self._others))
        for i, ctrl in enumerate(self.other_ctrls):
            if i < len(self._others):
                fn = self._others[i]
                label = self.t(fn["key"]) if fn.get("basic") else fn["name"]
                ctrl.getModel().Label = label + ("  \u2605" if fn is self._recommended else "")
                ctrl.setVisible(True)
            else:
                ctrl.setVisible(False)

    def _show_other(self, i):
        """Clicking another function found shows it; the one shown before
        goes back into the list - the recommended one (marked with a star)
        always at the top."""
        if i >= len(self._others):
            return
        fn = self._others[i]
        previous = self._shown
        rest = [f for f in self._others if f is not fn]
        if previous is not None:
            rest = [previous] + rest if previous is self._recommended else rest + [previous]
        if self._recommended is not None and self._recommended in rest:
            rest.remove(self._recommended)
            rest.insert(0, self._recommended)
        self._others = rest
        self.status.getModel().Label = self.t(
            "finder_recommended" if fn is self._recommended else "finder_other")
        self._show(fn)

    def _on_apply(self):
        """Put the shown function into the user's selected cell (the top-left
        one of a selected range) as =NAME(), then open LibreOffice's Function
        Wizard on it, where the user fills in the arguments (it shows each
        one with its description and lets them pick ranges with the mouse).
        FormulaLocal takes the function name in the UI language, which is
        the language the catalogue's names are in."""
        fn = self._shown
        if fn is None:
            return
        try:
            controller = self.frame.getController()
            selection = controller.getSelection()
            addr = selection.queryInterface(
                uno.getTypeByName("com.sun.star.sheet.XCellRangeAddressable")).getRangeAddress()
            sheet = controller.getActiveSheet()
            cell = sheet.getCellByPosition(addr.StartColumn, addr.StartRow)
            if fn.get("basic"):
                # a basic operation is a whole formula already (absolute refs)
                if not fn.get("formula"):
                    self.status.getModel().Label = self.t("op_no_cells")
                    return
                cell.setFormula(fn["formula"])
                controller.select(cell)
                self.status.getModel().Label = self.t(
                    "op_applied", formula=fn["formula"], cell=_cell_ref(addr.StartColumn, addr.StartRow))
                return
            if fn.get("formula"):
                # complete formula built from the request's cells: insert it
                # as it is. The argument separator follows the locale (";"
                # or ","): try ";" and fall back to "," on a syntax error.
                cell.setPropertyValue("FormulaLocal", fn["formula"])
                if cell.getError() in (501, 508, 509, 510):
                    cell.setPropertyValue("FormulaLocal", function_formula(fn, fn["cells"], ","))
                controller.select(cell)
                self.status.getModel().Label = self.t(
                    "op_applied", formula=cell.getPropertyValue("FormulaLocal"),
                    cell=_cell_ref(addr.StartColumn, addr.StartRow))
                return
            cell.setPropertyValue("FormulaLocal", f"={fn['name']}()")
            controller.select(cell)
            name = cell.queryInterface(
                uno.getTypeByName("com.sun.star.sheet.XCellAddressable")).getCellAddress()
            ref = _cell_ref(name.Column, name.Row)
            self.status.getModel().Label = self.t("finder_applied", name=fn["name"], cell=ref)
            if fn["args"]:
                dispatcher = self._create("com.sun.star.frame.DispatchHelper")
                dispatcher.executeDispatch(self.frame, ".uno:FunctionDialog", "", 0, ())
        except Exception as exc:
            traceback.print_exc()
            self.status.getModel().Label = self.t.error_text(exc)

    # XToolPanel
    def createAccessible(self, parent):
        return self.window.getAccessibleContext()

    def getWindow(self):
        return self.window

    # XSidebarPanel
    def getHeightForWidth(self, width):
        size = uno.createUnoStruct("com.sun.star.ui.LayoutSize")
        # tall enough for the result and the other functions found; the
        # chat panel above takes whatever is left
        size.Minimum = 440
        size.Preferred = 520
        size.Maximum = 640
        return size

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
