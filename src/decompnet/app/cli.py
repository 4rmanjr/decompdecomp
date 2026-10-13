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
import urllib.error
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


def connect_agents(args) -> int:
    """Give the AI agents you use decomp.wtf's tools: then ask them to decompile your game."""
    from . import agents, connect

    unknown = [a for a in args.agents if a not in connect.LABEL]
    if unknown:
        print(f"unknown agent {unknown[0]!r}: use claude, codex, gemini or opencode")
        return 2
    found = [a for a in agents.detect() if a["id"] in connect.LABEL and a["installed"]]
    wanted = [a for a in found if not args.agents or a["id"] in args.agents]
    if not found:
        print("No agent CLI found (Claude Code, Codex, Gemini CLI or opencode). Install one, then run `decomp connect`.")
        return 1
    for a in wanted:
        if args.list:
            print(f"{a['name']}: {'connected' if connect.status(a['id']) else 'not connected'}")
            continue
        try:
            connect.connect(a["id"])
            print(f"{a['name']}: connected")
        except (ValueError, OSError) as e:
            print(f"{a['name']}: {e}")
    if not args.list:
        print('Now ask your agent, for example: "decompile my game at ~/roms/game.z64"')
    return 0


def _api(path: str, body: dict | None = None) -> dict:
    """Call the running app's API (for commands that act on it, like `decomp share`)."""
    state = home_dir() / ".decomp" / "server.json"
    try:
        info = json.loads(state.read_text())
    except (OSError, ValueError):
        raise SystemExit("decomp.wtf isn't running: start it with `decomp --no-browser`") from None
    if not _alive(info.get("port", 0)):
        raise SystemExit("decomp.wtf isn't running: start it with `decomp --no-browser`")
    req = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}", method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Cookie": f"decomp_live={info['secret']}", "X-Decomp": "1",
                                          "Host": f"127.0.0.1:{info['port']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(json.loads(e.read() or b"{}").get("error") or str(e)) from None


def share(args) -> int:
    """Share a project with a team from this computer or a server (decomp --no-browser keeps running)."""
    state = _api("/api/state")
    slugs = [p["slug"] for p in state["projects"]]
    if args.project not in slugs:
        raise SystemExit(f"no project {args.project!r}; projects here: {', '.join(slugs) or 'none'}")
    body = {"slug": args.project, "visibility": "public" if args.public else "private", "approve": args.approve,
            "mode": "address" if args.address else "tunnel", "address": args.address or "", "port": args.port}
    job = _api("/api/collab/start", body)["job"]
    for _ in range(240):
        st = _api(f"/api/job?id={job}")
        if st["state"] != "running":
            break
        time.sleep(0.5)
    if st["state"] != "done":
        raise SystemExit(st.get("error") or "sharing failed")
    print(f"Sharing {args.project} ({body['visibility']}{', you approve each member' if args.approve else ''}).")
    print(f"Invite link: {st['result']['link']}")
    print("Teammates open decomp.wtf, choose Join a friend's room, paste the link and pick their own copy of the game.")
    return 0


def shortcut(args) -> int:
    from .launcher import install_launcher

    print(install_launcher())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="decomp", description="decomp.wtf: your AI agents decompile your games, matching byte for byte")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="install the compilers and tools this computer needs")
    sub.add_parser("stop", help="stop the running app")
    sub.add_parser("shortcut", help="add decomp.wtf to your apps menu / desktop")
    c = sub.add_parser("connect", help="give your AI agents (Claude Code, Codex, Gemini CLI, opencode) decomp.wtf's tools")
    c.add_argument("agents", nargs="*", metavar="agent", help="claude, codex, gemini or opencode (default: every one installed)")
    c.add_argument("--list", action="store_true", help="only show which are connected")
    sh = sub.add_parser("share", help="share a project with your team (the app must be running)")
    sh.add_argument("project", help="the project's slug (see the app, or `decomp share x` lists them)")
    sh.add_argument("--address", help="the address teammates use, e.g. http://my-server:6481 (default: a quick tunnel)")
    sh.add_argument("--port", type=int, default=6481, help="port for --address (default 6481)")
    sh.add_argument("--public", action="store_true", help="anyone with the address can join (default: invite link)")
    sh.add_argument("--approve", action="store_true", help="you let each new member in")
    m = sub.add_parser("mcp", help="serve decomp.wtf to the AI agent you talk to (stdio MCP; starts the app if needed)")
    m.add_argument("--name", default=os.environ.get("DECOMP_AGENT_NAME") or "chat",
                   help="how this agent shows up in the app (e.g. claude-code; or set DECOMP_AGENT_NAME)")
    args = ap.parse_args(argv)
    if args.cmd == "mcp":
        from .bridge import main as bridge
        return bridge(args.name)
    return {"setup": setup, "stop": stop, "shortcut": shortcut, "connect": connect_agents, "share": share}.get(args.cmd, run)(args)


if __name__ == "__main__":
    sys.exit(main())
