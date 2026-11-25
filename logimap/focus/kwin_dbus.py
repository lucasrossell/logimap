"""KWin-based focus source: load a KWin JS script, receive D-Bus pings.

Uses jeepney's blocking I/O on one background thread rather than an asyncio
D-Bus library: the daemon only needs to own one name, answer one method and
make a few calls at startup, and this keeps asyncio out of the process.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from pathlib import Path
from typing import Callable

from jeepney import (
    DBusAddress,
    HeaderFields,
    MatchRule,
    MessageFlag,
    new_error,
    new_method_call,
    new_method_return,
)
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import DBusConnection, open_dbus_connection
from jeepney.low_level import Message
from jeepney.wrappers import unwrap_msg

from ..profiles import WindowFocus

log = logging.getLogger(__name__)

SERVICE_NAME = "org.logimap.Focus1"
OBJECT_PATH = "/org/logimap/Focus1"
INTERFACE = "org.logimap.Focus1"

KWIN_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "kwin_script" / "logimap-focus.js"
KWIN_SCRIPT_NAME = "logimap-focus"

CALL_TIMEOUT_SECONDS = 5.0
# How often the receive loop checks whether stop() was called.
_STOP_POLL_SECONDS = 1.0
_PRIMARY_OWNER = 1  # RequestName reply code
_NOTIFY_SIGNATURE = "sssi"

_KWIN_SCRIPTING = DBusAddress(
    "/Scripting", bus_name="org.kde.KWin", interface="org.kde.kwin.Scripting"
)


def focus_from_notify(msg: Message) -> WindowFocus | None:
    """The WindowFocus carried by a Notify call to our object, else None."""
    fields = msg.header.fields
    if (
        fields.get(HeaderFields.path) != OBJECT_PATH
        or fields.get(HeaderFields.member) != "Notify"
        or fields.get(HeaderFields.interface, INTERFACE) != INTERFACE
        or fields.get(HeaderFields.signature) != _NOTIFY_SIGNATURE
    ):
        return None
    resource_class, resource_name, caption, pid = msg.body
    return WindowFocus(
        wm_class=resource_class,
        resource_name=resource_name,
        caption=caption,
        pid=pid if pid > 0 else None,
    )


def handle_call(msg: Message, callback: Callable[[WindowFocus], None]) -> Message | None:
    """Handle a method call sent to us; return the reply to send, if any."""
    focus = focus_from_notify(msg)
    if focus is None:
        member = msg.header.fields.get(HeaderFields.member)
        reply = new_error(
            msg, "org.freedesktop.DBus.Error.UnknownMethod", "s",
            (f"logimap does not implement {member!r}",),
        )
    else:
        try:
            callback(focus)
        except Exception:
            log.exception("focus callback raised")
        reply = new_method_return(msg)
    if msg.header.flags & MessageFlag.no_reply_expected:
        return None
    return reply


class KWinFocusSource:
    """Owns the org.logimap.Focus1 service; re-emits WindowFocus to a callback.

    Runs a blocking D-Bus receive loop on a background thread so callers can
    remain synchronous. The callback runs on that thread.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._callback: Callable[[WindowFocus], None] | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error: BaseException | None = None

    def subscribe(self, callback: Callable[[WindowFocus], None]) -> None:
        if self._thread is not None:
            raise RuntimeError("already subscribed")
        self._callback = callback
        self._thread = threading.Thread(target=self._run, daemon=True, name="kwin-focus")
        self._thread.start()
        if not self._ready.wait(timeout=CALL_TIMEOUT_SECONDS * 3):
            raise RuntimeError("KWinFocusSource failed to start in time")
        if self._error is not None:
            raise RuntimeError(f"KWinFocusSource failed to start: {self._error}")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=_STOP_POLL_SECONDS * 2)

    def _run(self) -> None:
        try:
            conn = open_dbus_connection(bus="SESSION")
        except Exception as e:
            log.exception("could not connect to the session bus")
            self._error = e
            self._ready.set()
            return
        try:
            self._serve(conn)
        except Exception as e:
            log.exception("KWin focus loop failed")
            self._error = e
        finally:
            self._ready.set()
            conn.close()

    def _serve(self, conn: DBusConnection) -> None:
        assert self._callback is not None
        # Install the filter before taking the name, so Notify calls that
        # arrive while we wait on replies below (KWin emits the active window
        # as soon as the script runs) are queued rather than dropped.
        calls: deque[Message] = deque(maxlen=64)
        with conn.filter(MatchRule(type="method_call"), queue=calls):
            (code,) = unwrap_msg(conn.send_and_get_reply(
                message_bus.RequestName(SERVICE_NAME), timeout=CALL_TIMEOUT_SECONDS
            ))
            if code != _PRIMARY_OWNER:
                log.warning("could not become primary owner of %s (reply=%s)", SERVICE_NAME, code)
            else:
                log.info("owning %s — waiting for KWin focus events", SERVICE_NAME)

            try:
                _ensure_script_loaded(conn)
            except Exception:
                log.exception("failed to load KWin focus script — is Plasma running?")

            self._ready.set()
            while not self._stop.is_set():
                try:
                    msg = conn.recv_until_filtered(calls, timeout=_STOP_POLL_SECONDS)
                except TimeoutError:
                    continue
                reply = handle_call(msg, self._callback)
                if reply is not None:
                    conn.send(reply)


def _call(conn: DBusConnection, msg: Message) -> tuple:
    return unwrap_msg(conn.send_and_get_reply(msg, timeout=CALL_TIMEOUT_SECONDS))


def _ensure_script_loaded(conn: DBusConnection) -> None:
    """Load logimap-focus.js via org.kde.kwin.Scripting if not already loaded.

    The API on Plasma 6:
      - org.kde.KWin / Scripting / loadScript(path, pluginName) -> int scriptId
      - /Scripting/Script<scriptId> .run()
      - isScriptLoaded(pluginName) -> bool
    """
    if not KWIN_SCRIPT_PATH.exists():
        raise FileNotFoundError(f"KWin script missing: {KWIN_SCRIPT_PATH}")

    (already,) = _call(conn, new_method_call(
        _KWIN_SCRIPTING, "isScriptLoaded", "s", (KWIN_SCRIPT_NAME,)
    ))
    if already:
        log.info("KWin script '%s' already loaded", KWIN_SCRIPT_NAME)
        return

    (script_id,) = _call(conn, new_method_call(
        _KWIN_SCRIPTING, "loadScript", "ss", (str(KWIN_SCRIPT_PATH), KWIN_SCRIPT_NAME)
    ))
    log.info("loaded KWin script '%s' -> id=%d", KWIN_SCRIPT_NAME, script_id)

    script = DBusAddress(
        f"/Scripting/Script{script_id}", bus_name="org.kde.KWin", interface="org.kde.kwin.Script"
    )
    _call(conn, new_method_call(script, "run"))
    log.info("KWin script '%s' running", KWIN_SCRIPT_NAME)


def install_kwin_script() -> int:
    """`logimap install-kwin` entry point — load script and exit."""
    try:
        conn = open_dbus_connection(bus="SESSION")
        try:
            _ensure_script_loaded(conn)
        finally:
            conn.close()
        return 0
    except Exception as e:
        log.error("install-kwin failed: %s", e)
        return 1
