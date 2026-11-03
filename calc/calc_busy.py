"""One AI request at a time across the Calc panels (the chat and the
formula finder), so the local model is never asked two things at once.
Process-wide: it also covers several Calc windows. Everything here runs
on LibreOffice's main thread (button handlers and done-callbacks)."""

_owner = [None]
_listeners = []


def owner():
    """Who is using the AI right now: None, "chat" or "finder"."""
    return _owner[0]


def try_acquire(who) -> bool:
    if _owner[0] not in (None, who):
        return False
    _owner[0] = who
    _notify()
    return True


def release(who):
    if _owner[0] == who:
        _owner[0] = None
        _notify()


def subscribe(fn):
    """fn(owner) is called whenever the owner changes (to grey out the other
    panel's buttons). Listeners of panels that are gone are dropped."""
    _listeners.append(fn)
    fn(_owner[0])


def _notify():
    for fn in list(_listeners):
        try:
            fn(_owner[0])
        except Exception:
            _listeners.remove(fn)
