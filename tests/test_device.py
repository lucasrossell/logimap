"""KeypadHandle reconnect behaviour, with a fake SDK and fake keypads."""

import importlib.util
import os
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from logimap.profiles import KEY_NAMES


class FakeKeypad:
    """Records the keys painted on it; can fail chosen writes."""

    def __init__(self, fail_on=()):
        self.painted: list[int] = []
        self.fail_on = set(fail_on)
        self.info = "fake"

    def initialize(self):
        return True

    def has_lcd(self):
        return True

    def set_event_callback(self, cb):
        pass

    def start_monitoring(self):
        pass

    def stop_monitoring(self):
        pass

    def set_key_image(self, idx, jpeg):
        if idx in self.fail_on:
            self.fail_on.discard(idx)
            return False  # e.g. the keypad was unplugged mid-grid
        self.painted.append(idx)
        return True


@pytest.fixture
def device(monkeypatch, tmp_path):
    sdk = ModuleType("logilinux")
    sdk.MXKeypadButton = Enum("MXKeypadButton", {name: i for i, name in enumerate(KEY_NAMES)})
    sdk.ButtonEvent = object
    sdk.DeviceType = SimpleNamespace(MX_KEYPAD="keypad")
    sdk.Library = Mock()
    sdk.MXKeypadDevice = FakeKeypad  # so isinstance() checks pass
    monkeypatch.setitem(sys.modules, "logilinux", sdk)
    path = Path(__file__).parents[1] / "logimap" / "device.py"
    spec = importlib.util.spec_from_file_location("logimap.device", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.render, "make_blank", lambda *a, **k: b"blank")
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    # No keypad in this fake sysfs, so its age is unknown unless a test says so.
    monkeypatch.setattr(module, "_SYSFS_HIDRAW", str(tmp_path / "no-sysfs"))
    return module


def handle_with(device, *keypads):
    handle = device.KeypadHandle()
    handle._lib.find_device.side_effect = list(keypads)
    return handle


def images(device):
    return {btn: f"img{btn.value}".encode() for btn in device.GRID_BUTTONS}


def test_reconnect_mid_grid_repaints_every_key_on_the_new_keypad(device):
    # Keys painted before an unplug are gone with the old keypad, so the
    # whole grid must be repainted after reconnecting, not just the rest.
    old, new = FakeKeypad(fail_on={4}), FakeKeypad()
    handle = handle_with(device, old, new)
    handle.open()
    assert handle.paint_grid(images(device)) is True
    assert old.painted == [0, 1, 2, 3]
    assert sorted(set(new.painted)) == list(range(9))
    assert new.painted[-9:] == list(range(9))


def test_paint_grid_gives_up_when_keypad_keeps_reconnecting(device):
    keypads = [FakeKeypad(fail_on={0}) for _ in range(device.MAX_GRID_RESTARTS + 2)]
    handle = handle_with(device, *keypads)
    handle.open()
    assert handle.paint_grid(images(device)) is False


def test_open_waits_for_a_just_enumerated_keypad_to_settle(device, monkeypatch):
    monkeypatch.setattr(device, "keypad_age_seconds", lambda: 1.0)
    handle = handle_with(device, FakeKeypad())
    waits = []
    monkeypatch.setattr(handle._stop, "wait", lambda s: waits.append(s) or False)
    handle.open()
    assert waits == [pytest.approx(device.SETTLE_SECONDS - 1.0)]
    expected = device.STABLE_AGE_SECONDS - device.SETTLE_SECONDS
    assert handle.seconds_until_stable() == pytest.approx(expected, abs=0.5)


def test_open_does_not_wait_for_a_settled_keypad(device, monkeypatch):
    monkeypatch.setattr(device, "keypad_age_seconds", lambda: 60.0)
    handle = handle_with(device, FakeKeypad())
    monkeypatch.setattr(handle._stop, "wait", Mock(side_effect=AssertionError("waited")))
    handle.open()
    assert handle.seconds_until_stable() == 0.0


def test_open_aborts_settle_wait_on_shutdown(device, monkeypatch):
    monkeypatch.setattr(device, "keypad_age_seconds", lambda: 0.5)
    handle = handle_with(device, FakeKeypad())
    handle._stop.set()
    with pytest.raises(device.KeypadUnavailable):
        handle.open()
    handle._lib.find_device.assert_not_called()


def test_keypad_age_comes_from_its_hidraw_node(device, monkeypatch, tmp_path):
    sysfs, dev = tmp_path / "sys", tmp_path / "dev"
    for name, hid_id in (("hidraw3", "0003:0000046D:0000C077"), ("hidraw7", "0003:0000046D:0000C354")):
        (sysfs / name / "device").mkdir(parents=True)
        (sysfs / name / "device" / "uevent").write_text(f"DRIVER=hid-generic\nHID_ID={hid_id}\n")
    dev.mkdir()
    (dev / "hidraw7").touch()
    monkeypatch.setattr(device, "_SYSFS_HIDRAW", str(sysfs))
    monkeypatch.setattr(device, "_DEV", str(dev))
    ctime = os.stat(dev / "hidraw7").st_ctime
    monkeypatch.setattr(device.time, "time", lambda: ctime + 4.0)
    assert device.keypad_age_seconds() == pytest.approx(4.0)
    (dev / "hidraw7").unlink()
    assert device.keypad_age_seconds() is None
