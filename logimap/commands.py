"""`run:` key actions: launch a shell command instead of injecting keys.

A binding whose shortcut starts with ``run:`` runs the rest as a ``bash -lc``
command on the host. ``{dir}`` is replaced with the (shell-quoted) folder of
the focused window, which is also exported as ``$LOGIMAP_DIR`` and used as the
working directory.

The folder comes from the window caption, so for Dolphin enable
Settings -> Configure Dolphin -> General -> "Show full path inside title bar".

Commands are started with ``systemd-run --user`` so they live in their own
transient unit and survive daemon restarts. When logimap itself runs inside a
distrobox/toolbox container, the call is routed to the host with
``distrobox-host-exec``.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path

from .profiles import WindowFocus

log = logging.getLogger(__name__)

RUN_PREFIX = "run:"

# "<path> — Dolphin" (em dash, as KDE apps format captions).
_CAPTION_SUFFIX = re.compile(r"\s+[—–-]\s+[^—–]+$")


def is_command(shortcut: str) -> bool:
    return shortcut.strip().lower().startswith(RUN_PREFIX)


def _in_container() -> bool:
    return os.path.exists("/run/.containerenv") or os.path.exists("/.dockerenv")


def focus_dir(focus: WindowFocus | None) -> str:
    """Best-effort folder for the focused window; falls back to $HOME."""
    home = str(Path.home())
    if focus is None or not focus.caption:
        return home
    caption = focus.caption.strip()
    candidates = [_CAPTION_SUFFIX.sub("", caption), caption]
    for cand in candidates:
        cand = cand.strip()
        if cand.startswith("~"):
            cand = os.path.expanduser(cand)
        if cand.startswith("/") and os.path.isdir(cand):
            return cand
    return home


def build_argv(shortcut: str, focus: WindowFocus | None) -> tuple[list[str], str]:
    command = shortcut.strip()[len(RUN_PREFIX):].strip()
    folder = focus_dir(focus)
    command = command.replace("{dir}", shlex.quote(folder))
    argv = [
        "systemd-run", "--user", "--collect", "--quiet",
        f"--working-directory={folder}",
        f"--setenv=LOGIMAP_DIR={folder}",
        "--", "bash", "-lc", command,
    ]
    if _in_container():
        host_exec = shutil.which("distrobox-host-exec") or shutil.which("host-spawn")
        if host_exec:
            argv = [host_exec, *argv]
        else:
            log.warning("running in a container without distrobox-host-exec; "
                        "command will run inside the container")
    return argv, folder


def run(shortcut: str, focus: WindowFocus | None) -> None:
    argv, folder = build_argv(shortcut, focus)
    log.info("running in %s: %s", folder, shlex.join(argv))
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    def _reap() -> None:
        _, err = proc.communicate()
        if proc.returncode:
            log.warning("command exited %s: %s", proc.returncode,
                        err.decode(errors="replace").strip())

    threading.Thread(target=_reap, daemon=True, name="run-reaper").start()
