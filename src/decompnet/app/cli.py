"""`decomp`: start decomp.wtf (or bring the running one to the front) and open it in the browser."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_PORT = 6464


def home_dir() -> Path:
    return Path(os.environ.get("DECOMP_HOME", Path.home() / "Decomp")).expanduser()


def _alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == "decomp-wtf"
    except (OSError, ValueError):
        return False


def _free_port(start: int) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("no free port for decomp.wtf")


def _open_when_ready(url: str, port: int) -> None:
    def wait():
        for _ in range(100):
            if _alive(port):
                webbrowser.open(url)
                return
            time.sleep(0.2)
    threading.Thread(target=wait, daemon=True).start()


def run(args) -> int:
    import uvicorn

    from .engine import RomNetwork
    from .server import build

    home = home_dir()
    internal = home / ".decomp"
    internal.mkdir(parents=True, exist_ok=True)
    state_file = internal / "server.json"
    try:
        prev = json.loads(state_file.read_text())
    except (OSError, ValueError):
        prev = {}
    if prev.get("port") and _alive(prev["port"]):
        url = f"http://127.0.0.1:{prev['port']}/?k={prev['secret']}"
        print(f"decomp.wtf is already running: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    port = _free_port(args.port)
    secret = secrets.token_urlsafe(24)
    state_file.write_text(json.dumps({"port": port, "secret": secret, "pid": os.getpid()}))
    try:
        os.chmod(state_file, 0o600)
    except OSError:
        pass
    net = RomNetwork(internal / "decomp.db", home)
    mcp, manager, security = build(net, internal, port, secret)
    app = mcp.streamable_http_app(transport_security=security, max_request_body_size=80 * 1024 * 1024, host="127.0.0.1")
    url = f"http://127.0.0.1:{port}/?k={secret}"
    print(f"decomp.wtf {__import__('decompnet').__version__} on {url}\nyour projects: {home}\nstop with Ctrl-C")
    if not args.no_browser:
        _open_when_ready(url, port)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        manager.stop()
        manager.on_stop()
    return 0


def setup(args) -> int:
    """Install what decomp.wtf needs on this computer: the free compilers and the draft decompiler."""
    from .. import toolchains
    from . import agents

    plat = toolchains.current_platform()
    print(f"Setting up decomp.wtf tools for {plat}")
    for tid in ("gcc-2.7.2-kmc", "m2c", "objdiff-cli"):
        tc = toolchains.get(tid)
        if not tc.supported:
            print(f"  - {tc.name}: no build for {plat} (projects that need it can't be matched here yet)")
            continue
        try:
            toolchains.install(tid, log=lambda line: print(f"  {line}"))
        except Exception as e:  # keep going: the app shows what's missing
            print(f"  ! {tc.name}: {e}")
    print("  IDO (most first-party games) is proprietary: install it from the app if you accept its license.")
    found = [a["name"] for a in agents.detect() if a["installed"] and a["id"] != "api"]
    print("AI agents found: " + (", ".join(found) if found else "none yet (the app helps you add one)"))
    return 0


def stop(args) -> int:
    state_file = home_dir() / ".decomp" / "server.json"
    try:
        prev = json.loads(state_file.read_text())
    except (OSError, ValueError):
        print("decomp.wtf is not running")
        return 0
    if not _alive(prev.get("port", 0)):
        print("decomp.wtf is not running")
        return 0
    req = urllib.request.Request(f"http://127.0.0.1:{prev['port']}/api/quit", method="POST", data=b"{}",
                                 headers={"Cookie": f"decomp_live={prev['secret']}", "X-Decomp": "1",
                                          "Host": f"127.0.0.1:{prev['port']}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except OSError:
        pass
    print("decomp.wtf stopped")
    return 0


def shortcut(args) -> int:
    from .launcher import install_launcher

    print(install_launcher())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="decomp", description="decomp.wtf: your AI agent decompiles your N64 ROMs")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="install the compilers and tools this computer needs")
    sub.add_parser("stop", help="stop the running app")
    sub.add_parser("shortcut", help="add decomp.wtf to your apps menu / desktop")
    args = ap.parse_args(argv)
    return {"setup": setup, "stop": stop, "shortcut": shortcut}.get(args.cmd, run)(args)


if __name__ == "__main__":
    sys.exit(main())
