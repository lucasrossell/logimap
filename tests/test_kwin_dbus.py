"""Tests for the KWin focus D-Bus message handling (no bus needed)."""

from __future__ import annotations

from jeepney import DBusAddress, MessageFlag, MessageType, new_method_call

from logimap.focus import kwin_dbus
from logimap.profiles import WindowFocus

US = DBusAddress(kwin_dbus.OBJECT_PATH, bus_name=kwin_dbus.SERVICE_NAME,
                 interface=kwin_dbus.INTERFACE)


def notify(rc="org.kde.dolphin", rn="dolphin", caption="Home — Dolphin", pid=42):
    return new_method_call(US, "Notify", "sssi", (rc, rn, caption, pid))


def test_notify_reaches_callback_and_is_answered():
    seen = []
    reply = kwin_dbus.handle_call(notify(), seen.append)
    assert seen == [WindowFocus(wm_class="org.kde.dolphin", resource_name="dolphin",
                                caption="Home — Dolphin", pid=42)]
    assert reply.header.message_type == MessageType.method_return


def test_nonpositive_pid_becomes_none():
    seen = []
    kwin_dbus.handle_call(notify(pid=0), seen.append)
    assert seen[0].pid is None


def test_unknown_method_gets_an_error_and_no_callback():
    seen = []
    msg = new_method_call(US, "Frobnicate", "s", ("x",))
    reply = kwin_dbus.handle_call(msg, seen.append)
    assert seen == []
    assert reply.header.message_type == MessageType.error


def test_wrong_signature_is_rejected():
    seen = []
    msg = new_method_call(US, "Notify", "ss", ("a", "b"))
    reply = kwin_dbus.handle_call(msg, seen.append)
    assert seen == []
    assert reply.header.message_type == MessageType.error


def test_no_reply_when_caller_expects_none():
    msg = notify()
    msg.header.flags |= MessageFlag.no_reply_expected
    seen = []
    assert kwin_dbus.handle_call(msg, seen.append) is None
    assert len(seen) == 1


def test_callback_error_still_answers():
    def boom(_):
        raise ValueError("boom")

    reply = kwin_dbus.handle_call(notify(), boom)
    assert reply.header.message_type == MessageType.method_return
