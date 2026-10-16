"""decompdecomp in a window of its own, tied to the app.

A Chromium-family browser opens the app as an app window (--app: no tabs, no address bar) with a profile of its own
(--user-data-dir), which makes that window a browser process of its own: when the process ends, the window was
closed. The server watches it and quits then, unless helper agents or a job are still working (they carry on in the
background; `decomp` opens the window again). Every Windows has Edge (decompdecomp runs in WSL there and starts it
through interop); Linux and macOS use Chrome, Brave, Chromium, Edge or Vivaldi when one is installed. Without one,
or when BROWSER says how to open pages, the app opens in a normal browser tab as before.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from .. import hostenv

_LINUX = ("brave-browser", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
          "microsoft-edge-stable", "vivaldi-stable", "vivaldi")
_MAC = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Chromium.app/Contents/MacOS/Chromium")
_EDGE = ("Program Files (x86)/Microsoft/Edge/Application/msedge.exe", "Program Files/Microsoft/Edge/Application/msedge.exe")


def browser() -> str | None:
    """The Chromium-family browser that opens the app window here, or None (then: a normal tab)."""
    if os.environ.get("BROWSER"):  # the user (or a test: "true") says how to open pages
        return None
    rt = hostenv.runtime()
    if rt == "docker":
        return None
    if rt == "wsl":
        return next((str(root / rel) for root in sorted(Path("/mnt").glob("[a-z]")) for rel in _EDGE
                     if (root / rel).exists()), None)
    if sys.platform == "darwin":
        return next((p for p in _MAC if Path(p).exists()), None)
    if sys.platform.startswith("linux"):
        names = list(_LINUX)
        try:  # the user's default browser first, when it's one of them
            default = subprocess.run(["xdg-settings", "get", "default-web-browser"], capture_output=True, text=True,
                                     timeout=3).stdout.strip().removesuffix(".desktop")
            if default in names:
                names.insert(0, default)
        except (OSError, subprocess.SubprocessError):
            pass
        return next((found for n in names if (found := shutil.which(n))), None)
    return None


def profile_dir(internal: Path) -> str:
    """Where the window's own browser profile lives (a Windows path for Edge on Windows)."""
    if hostenv.runtime() == "wsl":
        cmd = hostenv._windows_tool("cmd.exe")
        if cmd:
            try:
                local = subprocess.run([cmd, "/c", "echo %LOCALAPPDATA%"], capture_output=True, text=True,
                                       timeout=10, cwd="/mnt/c" if Path("/mnt/c").exists() else None).stdout.strip()
                if local and "%" not in local:
                    return local + "\\decompdecomp\\window"
            except (OSError, subprocess.SubprocessError):
                pass
    return str(internal / "window")


class Window:
    """The one app window: open() starts it (or leaves the open one be), and on_close runs when it's closed."""

    def __init__(self, profile: str, on_close: Callable[[], None]):
        self.profile, self.on_close = profile, on_close
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def open(self, url: str) -> bool:
        """True when the app is in its window (just opened, or already open). False: open it as a tab instead."""
        with self._lock:
            if self.is_open():
                return True
            exe = browser()
            if not exe:
                return False
            argv = [exe, f"--app={url}", f"--user-data-dir={self.profile}", "--no-first-run",
                    "--no-default-browser-check", "--class=decompdecomp", "--window-size=1440,920"]
            try:
                self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                return False
            threading.Thread(target=self._watch, args=(self.proc,), daemon=True, name="window").start()
            return True

    def _watch(self, proc: subprocess.Popen) -> None:
        proc.wait()
        if proc is self.proc:
            self.on_close()


WINDOW: Window | None = None  # set by the server (server.build); hostenv.open_url opens the app through it


def open_app(url: str) -> bool:
    return WINDOW is not None and WINDOW.open(url)
