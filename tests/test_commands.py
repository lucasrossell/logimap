from pathlib import Path

from logimap import commands
from logimap.profiles import WindowFocus


def _focus(caption: str) -> WindowFocus:
    return WindowFocus(wm_class="org.kde.dolphin", resource_name="dolphin", caption=caption)


def test_is_command() -> None:
    assert commands.is_command("run:konsole")
    assert commands.is_command("  RUN: konsole")
    assert not commands.is_command("ctrl+t")


def test_focus_dir_from_dolphin_full_path(tmp_path) -> None:
    d = tmp_path / "my project"
    d.mkdir()
    assert commands.focus_dir(_focus(f"{d} — Dolphin")) == str(d)


def test_focus_dir_falls_back_to_home() -> None:
    home = str(Path.home())
    assert commands.focus_dir(None) == home
    assert commands.focus_dir(_focus("Home — Dolphin")) == home
    assert commands.focus_dir(_focus("lucas : claude — Konsole")) == home


def test_build_argv_quotes_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(commands, "_in_container", lambda: False)
    d = tmp_path / "a b"
    d.mkdir()
    argv, folder = commands.build_argv("run:konsole --workdir {dir}", _focus(f"{d} — Dolphin"))
    assert folder == str(d)
    assert argv[:2] == ["systemd-run", "--user"]
    assert f"--working-directory={d}" in argv
    assert argv[-1] == f"konsole --workdir '{d}'"
