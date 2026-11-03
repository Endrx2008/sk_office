"""Run slow work (the Ollama HTTP call) off LibreOffice's main thread.

The work function runs on a plain Python thread and must NOT touch any UNO
document/UI object - it only does HTTP. Its outcome is handed back to LO's
main (VCL) thread through com.sun.star.awt.AsyncCallback, which is where
the done-callback reads/edits the document and updates the panel, so all
document mutation stays on the thread LibreOffice expects it on.
"""

from __future__ import annotations

import threading
import traceback

import unohelper
from com.sun.star.awt import XCallback


class _MainThreadCallback(unohelper.Base, XCallback):
    def __init__(self, fn):
        self.fn = fn

    def notify(self, data):
        try:
            self.fn()
        except Exception:
            traceback.print_exc()


def run_in_background(ctx, work, on_done):
    """Call work() on a worker thread, then on_done(result, error,
    traceback_text) on LO's main thread. Exactly one of result/error is
    meaningful; traceback_text is the formatted worker traceback (or "")."""
    async_callback = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.awt.AsyncCallback", ctx
    )

    def worker():
        result, error, tb_text = None, None, ""
        try:
            result = work()
        except Exception as exc:
            error, tb_text = exc, traceback.format_exc()
        async_callback.addCallback(
            _MainThreadCallback(lambda: on_done(result, error, tb_text)), None
        )

    threading.Thread(target=worker, name="sk_office-ai", daemon=True).start()


def main_thread_poster(ctx):
    """-> post(fn): call fn() later on LO's main thread. Safe to call from
    a worker thread; posts run in order (one VCL user-event queue, shared
    with run_in_background's done-callback)."""
    async_callback = ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.awt.AsyncCallback", ctx
    )

    def post(fn):
        async_callback.addCallback(_MainThreadCallback(fn), None)

    return post
