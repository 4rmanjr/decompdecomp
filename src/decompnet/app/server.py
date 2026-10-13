"""The local decomp.wtf server: the GUI, its API, and the MCP endpoint agents connect to.

It listens on 127.0.0.1 only. The GUI needs a per-launch secret (handed to the browser in the URL the
app opens, then kept in a same-site cookie); agents need their own bearer token. Host headers are
checked on every request, so web pages can't reach it through DNS rebinding.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from importlib import resources
from pathlib import Path

import anyio
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import __version__, toolchains
from ..network import NetworkError
from ..rom.n64 import RomError
from . import agents, collab, tools
from .engine import COMPILERS, SHORT_NAMES, RomNetwork

COOKIE = "decomp_live"
MAX_ROM = 64 * 1024 * 1024


class Jobs:
    """Background work the GUI can follow: imports and installs."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}

    def start(self, kind: str, fn) -> str:
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "kind": kind, "state": "running", "log": [], "result": None, "error": None, "t": time.time()}
        self.jobs[jid] = job

        def log(line: str) -> None:
            job["log"].append(str(line))

        def run():
            try:
                job["result"] = fn(log)
                job["state"] = "done"
            except (NetworkError, RomError, toolchains.ToolchainError, ValueError, RuntimeError, OSError) as e:
                job["error"], job["state"] = str(e), "error"
            except subprocess.CalledProcessError as e:
                job["error"], job["state"] = f"command failed ({e.returncode})", "error"
        threading.Thread(target=run, daemon=True, name=f"job-{kind}").start()
        return jid


class Settings:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def public(self) -> dict:
        api = dict(self.data.get("api", {}))
        if api.get("api_key"):
            api["api_key"] = "•" * 8 + api["api_key"][-4:]
            api["has_key"] = True
        return {"api": api, "per_session": self.data.get("per_session", 4), "last_project": self.data.get("last_project")}


def _static(name: str) -> bytes | None:
    if ".." in name or name.startswith(("/", "\\")):
        return None
    try:
        return resources.files("decompnet.app").joinpath("static", name).read_bytes()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None


def open_folder(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 (opening the user's own folder)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build(net: RomNetwork, home: Path, port: int, secret: str):
    mcp_url = f"http://127.0.0.1:{port}/mcp"
    manager = agents.Manager(net, home, mcp_url)
    jobs = Jobs()
    settings = Settings(home / "settings.json")
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def agent_of(ctx: Context):
        headers = ctx.headers or {}
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        agent = net.authenticate(auth.removeprefix("Bearer ").strip() or None)
        slug = manager.project_of(agent.id) or settings.data.get("last_project")
        if not slug:
            raise NetworkError("no project is open in decomp.wtf; open a ROM in the app first")
        return agent, slug

    mcp = tools.build_mcp(net, agent_of)

    def guard(request: Request, write: bool = False) -> Response | None:
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"error": "bad host"}, status_code=403)
        if not secrets.compare_digest(request.cookies.get(COOKIE, ""), secret):
            return JSONResponse({"error": "open decomp.wtf from the app or the `decomp` command"}, status_code=401)
        if write and request.headers.get("x-decomp") != "1":
            return JSONResponse({"error": "missing header"}, status_code=403)
        return None

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    async def call(fn, *args):
        return await anyio.to_thread.run_sync(fn, *args)

    def fail(e: Exception, code: int = 400) -> JSONResponse:
        return JSONResponse({"error": str(e)}, status_code=code)

    # ------------------------------------------------------------------ pages

    @mcp.custom_route("/", methods=["GET"])
    async def index(request: Request) -> Response:
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"error": "bad host"}, status_code=403)
        key = request.query_params.get("k")
        if key:
            if not secrets.compare_digest(key, secret):
                return HTMLResponse("This link is from an earlier session. Open decomp.wtf again.", status_code=401)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, secret, httponly=True, samesite="strict", max_age=30 * 86400)
            return resp
        if not secrets.compare_digest(request.cookies.get(COOKIE, ""), secret):
            return HTMLResponse("<!doctype html><meta charset=utf-8><title>decomp.wtf</title>"
                                "<body style='font:16px system-ui;background:#0b0c0e;color:#e8e6e1;padding:3rem'>"
                                "Open decomp.wtf from its app icon or by running <code>decomp</code>.", status_code=401)
        return HTMLResponse(_static("index.html").decode(), headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/assets/{name:path}", methods=["GET"])
    async def asset(request: Request) -> Response:
        if request.headers.get("host") not in allowed_hosts:
            return Response(status_code=403)
        name = request.path_params["name"]
        data = _static(name)
        if data is None:
            return Response(status_code=404)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return Response(data, media_type=ctype, headers={"Cache-Control": "no-cache"})

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "app": "decomp-wtf", "version": __version__})

    # ------------------------------------------------------------------ state

    def state() -> dict:
        projects = []
        for p in net.store.all("SELECT slug FROM projects ORDER BY id DESC"):
            try:
                projects.append(net.overview(p["slug"]))
            except (NetworkError, OSError, KeyError):
                continue
        tcs = [t for t in toolchains.status() if t["id"] in COMPILERS or t["id"] == "m2c"]
        for t in tcs:
            t["use"] = COMPILERS.get(t["id"], "machine-made first drafts")
            t["name"] = SHORT_NAMES.get(t["id"], t["name"])
        return {"version": __version__, "platform": toolchains.current_platform(), "home": str(home.parent),
                "projects": projects, "toolchains": tcs, "agents": agents.detect(),
                "api_presets": agents.API_PRESETS, "settings": settings.public(), "mcp_url": mcp_url}

    @mcp.custom_route("/api/state", methods=["GET"])
    async def api_state(request: Request) -> Response:
        return guard(request) or JSONResponse(await call(state))

    @mcp.custom_route("/api/import", methods=["POST"])
    async def api_import(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        q = request.query_params
        demo = q.get("demo") == "1"
        if demo:
            data = _static("demo/demo-quest-64.z64")
            if data is None:
                return fail(ValueError("the demo ROM is missing from this install"))
        else:
            if q.get("confirmed") != "1":
                return fail(ValueError("confirm that this is your own legally obtained copy of the game"))
            data = await request.body()
            if len(data) > MAX_ROM:
                return fail(ValueError("file is larger than any N64 cartridge"))
        toolchain = q.get("toolchain") or None

        def work(log):
            slug = net.import_rom(data, toolchain=toolchain, confirmed=True, log=log)
            settings.data["last_project"] = slug
            settings.save()
            return {"slug": slug}
        return JSONResponse({"job": jobs.start("import", work)})

    @mcp.custom_route("/api/job", methods=["GET"])
    async def api_job(request: Request) -> Response:
        if (bad := guard(request)):
            return bad
        job = jobs.jobs.get(request.query_params.get("id", ""))
        return JSONResponse(job) if job else fail(ValueError("no such job"), 404)

    # ------------------------------------------------------------------ projects

    def with_project(fn):
        async def handler(request: Request) -> Response:
            if (bad := guard(request, write=request.method == "POST")):
                return bad
            data = await body(request) if request.method == "POST" else {}
            slug = data.get("slug") or request.query_params.get("slug", "")
            try:
                return JSONResponse(await call(lambda: fn(slug, request, data)))
            except (NetworkError, ValueError, toolchains.ToolchainError, OSError) as e:
                return fail(e)
        return handler

    def p_overview(slug, request, data):
        settings.data["last_project"] = slug
        settings.save()
        return net.overview(slug)

    def p_live(slug, request, data):
        q = request.query_params
        out = net.live(slug, int(q.get("e", 0) or 0), int(q.get("a", 0) or 0))
        try:
            since = json.loads(q.get("s") or "{}")
        except ValueError:
            since = {}
        out["sessions"] = [s for s in manager.snapshot(since) if s["slug"] == slug]
        return out

    def p_compiler(slug, request, data):
        net.set_compiler(slug, str(data.get("toolchain", "")), data.get("flags") or None)
        return net.overview(slug)

    def p_export(slug, request, data):
        out = net.export_rom(slug)
        if data.get("open"):
            open_folder(out)
        return {"path": str(out)}

    def p_open(slug, request, data):
        open_folder(Path(net.project_row(slug)["root"]))
        return {"ok": True}

    def p_delete(slug, request, data):
        manager.stop()
        proj = net.project_row(slug)
        net.remove_project(slug)
        if data.get("files"):
            shutil.rmtree(proj["root"], ignore_errors=True)
        if settings.data.get("last_project") == slug:
            settings.data["last_project"] = None
            settings.save()
        return {"ok": True}

    def p_manual(slug, request, data):
        name = f"manual-{secrets.token_hex(2)}"
        agent, token = net.local_agent(name)
        manager.assignments[agent.id] = slug
        return {"agent": name, "url": mcp_url, "token": token,
                "claude": f'claude mcp add --transport http decomp {mcp_url} --header "Authorization: Bearer {token}"',
                "json": json.dumps({"mcpServers": {"decomp": {"type": "http", "url": mcp_url,
                                                              "headers": {"Authorization": f"Bearer {token}"}}}}, indent=2)}

    routes = {
        "/api/project": (p_overview, ["GET"]), "/api/map": (lambda s, r, d: net.function_map(s), ["GET"]),
        "/api/live": (p_live, ["GET"]),
        "/api/function": (lambda s, r, d: net.function_detail(s, r.query_params.get("name", "")), ["GET"]),
        "/api/compiler": (p_compiler, ["POST"]), "/api/export": (p_export, ["POST"]), "/api/open": (p_open, ["POST"]),
        "/api/delete": (p_delete, ["POST"]), "/api/agents/manual": (p_manual, ["POST"]),
    }
    for path, (fn, methods) in routes.items():
        mcp.custom_route(path, methods=methods)(with_project(fn))

    # ------------------------------------------------------------------ installs, agents, settings

    @mcp.custom_route("/api/toolchain", methods=["POST"])
    async def api_toolchain(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        data = await body(request)
        tid, accept = str(data.get("id", "")), bool(data.get("accept_license"))
        return JSONResponse({"job": jobs.start("toolchain", lambda log: (toolchains.install(tid, accept_license=accept, log=log)
                                                                         and {"id": tid}))})

    @mcp.custom_route("/api/agents/install", methods=["POST"])
    async def api_agent_install(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        kind = agents.KINDS.get(str((await body(request)).get("kind", "")))
        if kind is None or not kind.install:
            return fail(ValueError("nothing to install"))
        cmd = kind.install["windows" if sys.platform == "win32" else "unix"]

        def work(log):
            log(f"$ {cmd}")
            argv = ["powershell", "-NoProfile", "-Command", cmd] if sys.platform == "win32" else ["bash", "-lc", cmd]
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
            for line in proc.stdout:
                log(line.rstrip())
            if proc.wait() != 0:
                raise RuntimeError(f"the installer exited with code {proc.returncode}")
            agents._versions.pop(kind.id, None)
            return {"kind": kind.id}
        return JSONResponse({"job": jobs.start("agent-install", work)})

    @mcp.custom_route("/api/agents/start", methods=["POST"])
    async def api_agents_start(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        data = await body(request)
        kind, slug = str(data.get("kind", "")), str(data.get("slug", ""))
        options = {k: v for k, v in (data.get("options") or {}).items() if k in ("model", "per_session")}
        if kind == "api":
            api = dict(settings.data.get("api", {}))
            api.update({k: v for k, v in (data.get("options") or {}).items()
                        if k in ("provider", "base_url", "model") and v})
            if not api.get("base_url") or not api.get("model"):
                return fail(ValueError("choose a provider and model first"))
            options.update(api)
            options["provider_name"] = agents.API_PRESETS.get(api.get("provider", ""), {}).get("name", "API")
        options.setdefault("per_session", settings.data.get("per_session", 4))
        try:
            net.project_row(slug)
            ids = manager.start(kind, slug, int(data.get("count", 1)), options)
        except (NetworkError, ValueError) as e:
            return fail(e)
        return JSONResponse({"started": ids})

    @mcp.custom_route("/api/agents/stop", methods=["POST"])
    async def api_agents_stop(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        data = await body(request)
        if data.get("remove"):
            manager.remove(str(data.get("id")))
        else:
            manager.stop(data.get("id") or None)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/api/settings", methods=["POST"])
    async def api_settings(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        data = await body(request)
        if isinstance(data.get("api"), dict):
            api = settings.data.setdefault("api", {})
            for k in ("provider", "base_url", "model"):
                if k in data["api"]:
                    api[k] = str(data["api"][k]).strip()
            if data["api"].get("api_key") and "•" not in data["api"]["api_key"]:
                api["api_key"] = str(data["api"]["api_key"]).strip()
            if data["api"].get("clear_key"):
                api.pop("api_key", None)
        if "per_session" in data:
            settings.data["per_session"] = max(1, min(int(data["per_session"]), 20))
        settings.save()
        return JSONResponse(settings.public())

    @mcp.custom_route("/api/quit", methods=["POST"])
    async def api_quit(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad

        def bye():
            manager.stop()
            manager.on_stop()
            time.sleep(0.5)
            os._exit(0)
        threading.Thread(target=bye, daemon=True).start()
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------ rooms (collab.py)
    # The tunnel only ever reaches this separate little server, never the GUI or the MCP endpoint above.

    share = {"server": None, "port": None, "tunnel": None, "links": {}}
    for p in net.store.all("SELECT slug FROM projects"):
        collab.Room(net, p["slug"]).close()  # invites don't survive a restart: the tunnel address changes
    net.restore_links()

    def collab_server() -> int:
        if share["server"] is not None:
            return share["port"]
        import socket

        import uvicorn

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(collab.host_app(net, net.rooms), host="127.0.0.1", port=port,
                                               log_level="warning"))
        threading.Thread(target=server.run, daemon=True, name="collab-server").start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        share.update(server=server, port=port)
        return port

    @mcp.custom_route("/api/collab", methods=["GET"])
    async def api_collab(request: Request) -> Response:
        if (bad := guard(request)):
            return bad
        slug = request.query_params.get("slug", "")
        try:
            info = await call(net._room_info, slug)
        except NetworkError as e:
            return fail(e)
        return JSONResponse({"sharing": slug in net.rooms, "link": share["links"].get(slug), "room": info,
                             "tunnel_supported": collab.cloudflared_available()})

    @mcp.custom_route("/api/collab/start", methods=["POST"])
    async def api_collab_start(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        slug = str((await body(request)).get("slug", ""))
        if slug in net.links:
            return fail(ValueError("this project belongs to a friend's room; only its host can invite people"))

        def work(log):
            port = collab_server()
            room = net.rooms.get(slug) or collab.Room(net, slug)
            code = room.open_invite()
            net.rooms[slug] = room
            if share["tunnel"] is None or share["tunnel"].proc is None or share["tunnel"].proc.poll() is not None:
                log("opening a private tunnel to this computer (Cloudflare quick tunnel, no account needed)")
                share["tunnel"] = collab.Tunnel(port)
                share["tunnel"].start(log)
            link = f"{share['tunnel'].url}/join/{code}#room={slug}"
            share["links"][slug] = link
            return {"link": link}
        return JSONResponse({"job": jobs.start("share", work)})

    @mcp.custom_route("/api/collab/stop", methods=["POST"])
    async def api_collab_stop(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        slug = str((await body(request)).get("slug", ""))
        room = net.rooms.pop(slug, None)
        if room is not None:
            room.close()
        share["links"].pop(slug, None)
        if not net.rooms and share["tunnel"] is not None:
            share["tunnel"].stop()
            share["tunnel"] = None
        return JSONResponse({"ok": True})

    @mcp.custom_route("/api/collab/join", methods=["POST"])
    async def api_collab_join(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        q = request.query_params
        if q.get("confirmed") != "1":
            return fail(ValueError("confirm that this is your own legally obtained copy of the game"))
        data = await request.body()
        link, name = q.get("link", ""), q.get("name", "")

        def work(log):
            slug = collab.join_room(net, link, name, data, log=log)
            settings.data["last_project"] = slug
            settings.save()
            return {"slug": slug}
        return JSONResponse({"job": jobs.start("join", work)})

    @mcp.custom_route("/api/collab/reconnect", methods=["POST"])
    async def api_collab_reconnect(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        data = await body(request)
        slug, link = str(data.get("slug", "")), str(data.get("link", ""))
        return JSONResponse({"job": jobs.start("reconnect", lambda log: {"slug": collab.reconnect(net, slug, link, log=log)})})

    @mcp.custom_route("/api/collab/leave", methods=["POST"])
    async def api_collab_leave(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        try:
            await call(net.unlink, str((await body(request)).get("slug", "")))
        except NetworkError as e:
            return fail(e)
        return JSONResponse({"ok": True})

    def shutdown_rooms() -> None:
        if share["tunnel"] is not None:
            share["tunnel"].stop()
        for link in list(net.links.values()):
            link.stop()

    manager.on_stop = shutdown_rooms

    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=sorted(allowed_hosts),
                                         allowed_origins=[f"http://{h}" for h in sorted(allowed_hosts)])
    return mcp, manager, security
