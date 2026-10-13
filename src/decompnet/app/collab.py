"""Private rooms: friends who each own the same game decompile it together.

The host's decomp.wtf is the hub. It serves a small, separate HTTP app (only /collab routes; never the GUI or the
agents' MCP endpoint) that a quick tunnel makes reachable. A friend joins with the invite link *and their own copy of
the game*: the ROM's SHA-1 must equal the host's, checked on both sides. No ROM bytes or assembly ever cross the
wire. Each side reads the code from its own copy; what travels is the work: claims (so nobody does the same function
twice), C attempts, matches, names, struct/prototype edits, notes. The host re-compiles every attempt it accepts
against its own ROM, so a match always means the same thing.

Host side: `host_app()`. Guest side: `Link`, which RomNetwork consults for every collaborative action.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections import defaultdict, deque

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .. import toolchains
from ..network import Agent, NetworkError, _hash_token
from ..rom.symbols import AUTO_NAME

NAME = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
MAX_BODY = 256 * 1024
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# ---------------------------------------------------------------------------------------------- host

class Room:
    """Host-side state for one shared project."""

    def __init__(self, net, slug: str):
        self.net, self.slug = net, slug
        self.proj = net.project_row(slug)
        self._hits: dict[str, deque] = defaultdict(lambda: deque(maxlen=900))

    @property
    def cfg(self) -> dict:
        return json.loads(self.net.project_row(self.slug)["config"])

    def open_invite(self) -> str:
        code = secrets.token_urlsafe(16)
        cfg = self.cfg
        cfg["collab"] = {"invite": _hash(code), "open": True, "since": time.time()}
        with self.net.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(cfg), self.slug))
        return code

    def close(self) -> None:
        cfg = self.cfg
        if "collab" in cfg:
            cfg["collab"]["open"] = False
            with self.net.store.tx() as db:
                db.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(cfg), self.slug))

    def invite_ok(self, code: str) -> bool:
        c = self.cfg.get("collab") or {}
        return bool(c.get("open")) and secrets.compare_digest(c.get("invite", ""), _hash(code or ""))

    @property
    def origin(self) -> str:
        return f"collab:{self.slug}"

    def members(self) -> list[dict]:
        rows = self.net.store.all("SELECT id, name, created_at, last_seen FROM agents WHERE origin=? AND owner IS NULL",
                                  self.origin)
        out = []
        for r in rows:
            agents = self.net.store.all("SELECT id, name FROM agents WHERE origin=? AND owner=?", self.origin, r["name"])
            ids = [a["id"] for a in agents] or [-1]
            matched = self.net.store.one(
                f"SELECT COUNT(*) AS n FROM functions WHERE matched_agent_id IN ({','.join('?' * len(ids))})", *ids)["n"]
            out.append({"name": r["name"], "joined": r["created_at"], "last_seen": r["last_seen"],
                        "agents": [a["name"].split(".", 1)[1] for a in agents], "matched": matched})
        return out

    def join(self, name: str, sha1: str) -> tuple[str, str]:
        """New member: (token, the name they got; a taken name gets a number)."""
        if not NAME.match(name or ""):
            raise NetworkError("pick a name of up to 24 letters, digits, - or _")
        if sha1 != self.cfg["rom"]["sha1"]:
            raise NetworkError("that isn't the same ROM: everyone in a room needs their own copy of exactly this "
                               f"game ({self.cfg['rom']['title']}, {self.cfg['rom']['game_code']}, same version)")
        base, n = name, 2
        while self.net.store.one("SELECT 1 FROM agents WHERE lower(name)=lower(?)", name):
            name, n = f"{base}{n}", n + 1
        token = "dn_" + secrets.token_urlsafe(24)
        with self.net.store.tx() as db:
            db.execute("INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin) VALUES(?,?,?,?,1,?)",
                       (name, None, _hash_token(token), time.time(), self.origin))
            self.net._event(db, self.proj["id"], None, "collab", f"{name} joined")
        return token, name

    def member(self, request: Request) -> Agent:
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        agent = self.net.authenticate(token or None)
        row = self.net.store.one("SELECT origin, owner FROM agents WHERE id=?", agent.id)
        if row["origin"] != self.origin or row["owner"] is not None:
            raise NetworkError("not a member of this room")
        hits = self._hits[agent.name]
        now = time.time()
        hits.append(now)
        if len(hits) == hits.maxlen and now - hits[0] < 60:
            raise NetworkError("slow down")
        return agent

    def agent_for(self, member: Agent, label: str) -> Agent:
        """The host-side identity of one of a member's agents, e.g. alex.claude-1."""
        label = re.sub(r"[^A-Za-z0-9_-]", "", str(label or "agent"))[:24] or "agent"
        name = f"{member.name}.{label}"
        row = self.net.store.one("SELECT id FROM agents WHERE name=?", name)
        if row is None:
            with self.net.store.tx() as db:
                aid = db.execute("INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin) VALUES(?,?,?,?,1,?)",
                                 (name, member.name, "remote:" + secrets.token_hex(16), time.time(), self.origin)).lastrowid
            return Agent(aid, name, member.name, True)
        return Agent(row["id"], name, member.name, True)

    def state(self, e: int, a: int, ctx_version: int, member: Agent) -> dict:
        """What changed since the guest's cursors: work product only, never ROM-derived data."""
        pid = self.proj["id"]
        net = self.net
        with net.store.tx() as db:
            net._expire_claims(db)
        events = net.store.all("SELECT e.id, e.kind, e.detail, e.created_at, a.name AS agent FROM events e LEFT JOIN agents a "
                               "ON a.id=e.agent_id WHERE e.project_id=? AND e.id>? ORDER BY e.id LIMIT 500", pid, e)
        max_e = events[-1]["id"] if events else e
        kinds = {ev["kind"] for ev in events}
        names = {ev["detail"] for ev in events if ev["kind"] in ("match",)}
        out: dict = {"e": max_e, "events": [dict(ev) for ev in events]}
        first = e == 0
        if first or names:
            rows = net.store.all("SELECT f.name, f.vram, f.matched_source, a.name AS agent FROM functions f LEFT JOIN agents a "
                                 "ON a.id=f.matched_agent_id WHERE f.project_id=? AND f.status='matched'", pid)
            out["matched"] = [dict(r) for r in rows if first or r["name"] in names]
        best = net.store.all("SELECT f.name, f.vram, f.best_score, f.best_source, MAX(t.id) AS last FROM attempts t "
                             "JOIN functions f ON f.id=t.function_id WHERE f.project_id=? AND t.id>? AND f.status='open' "
                             "AND f.best_source IS NOT NULL GROUP BY f.id", pid, a)
        out["best"] = [{"name": r["name"], "vram": r["vram"], "score": r["best_score"], "source": r["best_source"]} for r in best]
        last = net.store.one("SELECT MAX(t.id) AS m FROM attempts t JOIN functions f ON f.id=t.function_id WHERE f.project_id=?", pid)
        out["a"] = max(a, last["m"] or 0)
        if first or kinds & {"rename", "split"}:
            out["symbols"] = [dict(r) for r in net.store.all(
                "SELECT name, address, size, kind FROM symbols WHERE project_id=?", pid) if not AUTO_NAME.match(r["name"])]
            out["functions"] = [dict(r) for r in net.store.all(
                "SELECT name, vram, size FROM functions WHERE project_id=? ORDER BY vram", pid)]
        ctx = net._context(pid)
        if ctx["version"] != ctx_version:
            out["context"] = {"version": ctx["version"], "header": ctx["header"]}
        if first or kinds & {"note"}:
            out["notes"] = [dict(r) for r in net.store.all(
                "SELECT n.id, n.text, n.created_at, f.vram, a.name AS agent FROM notes n LEFT JOIN functions f ON f.id=n.function_id "
                "LEFT JOIN agents a ON a.id=n.agent_id WHERE n.project_id=? ORDER BY n.id", pid)]
        out["claims"] = [dict(r) for r in net.store.all(
            "SELECT f.name, f.vram, a.name AS agent FROM claims c JOIN functions f ON f.id=c.function_id JOIN agents a "
            "ON a.id=c.agent_id WHERE f.project_id=?", pid)]
        out["members"] = self.members()
        return out


def host_app(net, rooms: dict[str, Room]) -> Starlette:
    """The public side of a shared project, served through the tunnel."""

    def room_for(request: Request) -> Room:
        slug = request.query_params.get("room") or request.headers.get("x-room", "")
        room = rooms.get(slug)
        if room is None:
            raise NetworkError("this room is closed")
        return room

    async def body(request: Request) -> dict:
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            raise NetworkError("request too large")
        try:
            data = await request.json()
        except ValueError:
            raise NetworkError("invalid request") from None
        if not isinstance(data, dict):
            raise NetworkError("invalid request")
        return data

    def endpoint(fn, auth: bool = True):
        async def handler(request: Request):
            import anyio

            try:
                room = room_for(request)
                data = await body(request) if request.method == "POST" else dict(request.query_params)
                member = await anyio.to_thread.run_sync(room.member, request) if auth else None
                result = await anyio.to_thread.run_sync(lambda: fn(room, member, data))
                return JSONResponse(result)
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        return handler

    def hello(room, member, q):
        if not room.invite_ok(q.get("invite", "")):
            raise NetworkError("this invite link is no longer valid")
        cfg = room.cfg
        ov = net.overview(room.slug)
        return {"room": room.slug, "title": cfg["rom"]["title"], "game_code": cfg["rom"]["game_code"],
                "region": cfg["rom"]["region"], "sha1": cfg["rom"]["sha1"], "compiler": cfg["compiler"],
                "functions": ov["functions"], "matched": ov["matched"], "members": len(room.members())}

    def join(room, member, d):
        if not room.invite_ok(str(d.get("invite", ""))):
            raise NetworkError("this invite link is no longer valid")
        token, name = room.join(str(d.get("name", "")), str(d.get("sha1", "")))
        return {"token": token, "room": room.slug, "name": name}

    def whoami(room, member, q):
        return {"name": member.name, "room": room.slug}

    def state(room, member, q):
        return room.state(int(q.get("e") or 0), int(q.get("a") or 0), int(q.get("v") or 0), member)

    def claim(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        fn = net.claim_row(agent, room.slug, d.get("function") or None, str(d.get("strategy") or "easiest"))
        return {"function": fn["name"] if fn else None, "vram": fn["vram"] if fn else None}

    def attempt(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        res = net.attempt(agent, str(d.get("function", "")), str(d.get("source", "")), room.slug)
        return {"score": res["score"], "matched": res["matched"]}

    def release(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.release(agent, str(d.get("function", "")), str(d.get("notes", "")), room.slug)}

    def context(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.edit_context(agent, room.slug, str(d.get("old", "")), str(d.get("new", "")),
                                         str(d.get("message", "")), d.get("function") or None)}

    def rename(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.rename_symbol(agent, room.slug, str(d.get("old", "")), str(d.get("new", "")),
                                          str(d.get("message", "")))}

    def split(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.split_function(agent, room.slug, str(d.get("function", "")), str(d.get("at", "")),
                                           str(d.get("new_name", "")))}

    def note(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.add_note(agent, room.slug, str(d.get("text", "")), d.get("function") or None)}

    def leave(room, member, d):
        with net.store.tx() as db:
            db.execute("DELETE FROM claims WHERE agent_id IN (SELECT id FROM agents WHERE owner=? AND origin=?)",
                       (member.name, room.origin))
        return {"ok": True}

    async def landing(request: Request):
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>decomp.wtf room</title>"
                            "<body style='font:16px system-ui;max-width:36em;margin:4rem auto;padding:0 1rem'>"
                            "<h1>decomp.wtf room</h1><p>This is an invite to decompile a game together. Open "
                            "<b>decomp.wtf</b>, choose <b>Join a friend</b>, and paste this page's address. "
                            "You'll need your own copy of the game.</p><p>Don't have the app? "
                            "<a href='https://decomp.wtf'>decomp.wtf</a></p>")

    routes = [
        Route("/", landing), Route("/join/{code}", landing),
        Route("/collab/hello", endpoint(hello, auth=False)),
        Route("/collab/join", endpoint(join, auth=False), methods=["POST"]),
        Route("/collab/state", endpoint(state)),
        Route("/collab/whoami", endpoint(whoami)),
        Route("/collab/claim", endpoint(claim), methods=["POST"]),
        Route("/collab/attempt", endpoint(attempt), methods=["POST"]),
        Route("/collab/release", endpoint(release), methods=["POST"]),
        Route("/collab/context", endpoint(context), methods=["POST"]),
        Route("/collab/rename", endpoint(rename), methods=["POST"]),
        Route("/collab/split", endpoint(split), methods=["POST"]),
        Route("/collab/note", endpoint(note), methods=["POST"]),
        Route("/collab/leave", endpoint(leave), methods=["POST"]),
    ]
    return Starlette(routes=routes)


class Tunnel:
    """A Cloudflare quick tunnel (free, no account) to the collab server."""

    def __init__(self, port: int):
        self.port, self.proc, self.url = port, None, None

    def start(self, log=print, timeout: float = 45) -> str:
        if toolchains.installed("cloudflared") is None:
            toolchains.install("cloudflared", log=log)
        exe = toolchains.path("cloudflared") / ("cloudflared.exe" if (toolchains.path("cloudflared") / "cloudflared.exe").exists() else "cloudflared")
        self.proc = subprocess.Popen([str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
        found = threading.Event()

        def read():
            for line in self.proc.stderr:
                m = TUNNEL_URL.search(line)
                if m and not self.url:
                    self.url = m.group(0)
                    found.set()
        threading.Thread(target=read, daemon=True).start()
        if not found.wait(timeout):
            self.stop()
            raise RuntimeError("couldn't open a tunnel (is the internet reachable?)")
        log(f"tunnel open: {self.url}")
        return self.url

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc, self.url = None, None


# --------------------------------------------------------------------------------------------- guest

def parse_invite(link: str) -> tuple[str, str]:
    """(base url, invite code) from a link like https://abc.trycloudflare.com/join/CODE#room=slug"""
    link = link.strip()
    m = re.match(r"^(https?://[^/\s]+)/join/([A-Za-z0-9_-]{8,})(?:#room=([\w-]+))?$", link)
    if not m:
        raise NetworkError("that doesn't look like a decomp.wtf invite link")
    return m.group(1), m.group(2) + (f"#{m.group(3)}" if m.group(3) else "")


class Link:
    """The guest's connection to a host's room."""

    def __init__(self, net, slug: str, url: str, room: str, token: str, member: str, client: httpx.Client | None = None):
        self.net, self.slug, self.url, self.room, self.token, self.member = net, slug, url.rstrip("/"), room, token, member
        self.client = client or httpx.Client(timeout=httpx.Timeout(60, connect=15))
        self.cursor = {"e": 0, "a": 0, "v": 0}
        self.claims: list[dict] = []
        self.members: list[dict] = []
        self.error = ""
        self.last_ok = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- transport

    def _req(self, method: str, path: str, data: dict | None = None) -> dict:
        headers = {"authorization": f"Bearer {self.token}", "x-room": self.room}
        try:
            if method == "GET":
                r = self.client.get(f"{self.url}{path}", params={**(data or {}), "room": self.room}, headers=headers)
            else:
                r = self.client.post(f"{self.url}{path}?room={self.room}", json=data or {}, headers=headers)
        except httpx.HTTPError as e:
            self.error = f"can't reach the host ({type(e).__name__})"
            raise NetworkError(f"the room's host isn't reachable right now: {self.error}") from None
        out = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            raise NetworkError(out.get("error") or f"host answered {r.status_code}")
        self.error, self.last_ok = "", time.time()
        return out

    # -- actions, called by RomNetwork for a linked project

    def claim(self, agent: Agent, function: str | None, strategy: str) -> str | None:
        return self._req("POST", "/collab/claim", {"agent": agent.name, "function": function, "strategy": strategy})["function"]

    def attempt(self, agent: Agent, function: str, source: str) -> dict:
        return self._req("POST", "/collab/attempt", {"agent": agent.name, "function": function, "source": source})

    def call(self, path: str, agent: Agent, **data) -> str:
        return self._req("POST", path, {"agent": agent.name, **data}).get("text", "")

    # -- mirroring the host's work into the local project

    def sync(self) -> None:
        with self._lock:
            st = self._req("GET", "/collab/state", self.cursor)
            self.apply(st)

    def apply(self, st: dict) -> None:
        net, slug = self.net, self.slug
        proj = net.project_row(slug)
        pid = proj["id"]
        mine = f"{self.member}."
        if st.get("symbols") is not None or st.get("functions") is not None:
            net.apply_remote_layout(slug, st.get("functions") or [], st.get("symbols") or [])
        if st.get("context"):
            net.apply_remote_context(slug, st["context"]["header"], st["context"]["version"])
            self.cursor["v"] = st["context"]["version"]
        for m in st.get("matched") or []:
            net.apply_remote_match(slug, m["vram"], m["matched_source"], m["agent"] or "host")
        for b in st.get("best") or []:
            net.apply_remote_best(slug, b["vram"], b["score"], b["source"])
        for n in st.get("notes") or []:
            net.apply_remote_note(slug, n["id"], n["vram"], n["text"], n["agent"] or "host", n["created_at"])
        for ev in st.get("events") or []:
            if ev["kind"] in ("match", "claim", "release", "context", "rename", "split", "collab") and not (ev["agent"] or "").startswith(mine):
                net.remote_event(pid, ev["kind"], ev["detail"], ev["agent"] or "host", ev["created_at"])
        self.claims = [c for c in st.get("claims") or [] if not (c["agent"] or "").startswith(mine)]
        self.members = st.get("members") or []
        self.cursor["e"], self.cursor["a"] = st.get("e", self.cursor["e"]), st.get("a", self.cursor["a"])

    def run(self, every: float = 2.0) -> None:
        def loop():
            while not self._stop.is_set():
                try:
                    self.sync()
                except NetworkError:
                    pass
                except Exception as e:  # keep syncing; show the problem in the GUI
                    self.error = f"{type(e).__name__}: {e}"
                self._stop.wait(every)
        threading.Thread(target=loop, daemon=True, name=f"link-{self.slug}").start()

    def stop(self) -> None:
        self._stop.set()


def _hello(http: httpx.Client, link: str) -> tuple[str, str, dict]:
    url, code = parse_invite(link)
    code = code.split("#", 1)[0]
    room_hint = link.split("#room=", 1)[1] if "#room=" in link else ""
    try:
        r = http.get(f"{url}/collab/hello", params={"invite": code, "room": room_hint})
    except httpx.HTTPError:
        raise NetworkError("can't reach that room: is the host's decomp.wtf open and sharing?") from None
    hello = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200:
        raise NetworkError(hello.get("error", "the invite didn't work"))
    return url, code, hello


def _linked_project(net, sha1: str, room: str):
    for p in net.store.all("SELECT slug, config FROM projects"):
        cfg = json.loads(p["config"])
        if cfg.get("rom", {}).get("sha1") == sha1 and (cfg.get("link") or {}).get("room") == room:
            return p["slug"], cfg["link"]
    return None, None


def reconnect(net, slug: str, link: str, log=print, client: httpx.Client | None = None, start: bool = True) -> str:
    """Guest: the host restarted and shared a new link. Reconnect this project as the same member if the host still
    knows us, or join again with the invite."""
    http = client or httpx.Client(timeout=httpx.Timeout(60, connect=15))
    url, code, hello = _hello(http, link)
    proj = net.project_row(slug)
    cfg = json.loads(proj["config"])
    if cfg.get("rom", {}).get("sha1") != hello["sha1"]:
        raise NetworkError(f"that link is for {hello['title']}, a different game than this project")
    info = dict(cfg.get("link") or {})
    if info.get("token"):
        r = http.get(f"{url}/collab/whoami", params={"room": hello["room"]},
                     headers={"authorization": f"Bearer {info['token']}", "x-room": hello["room"]})
        if r.status_code == 200:
            old = net.links.pop(slug, None)
            if old is not None:
                old.stop()
            net.link_project(slug, info | {"url": url, "room": hello["room"], "member": r.json()["name"]},
                             client=client, start=start)
            log(f"reconnected to {hello['title']} as {r.json()['name']}")
            return slug
    name = info.get("member") or "friend"
    r = http.post(f"{url}/collab/join?room={hello['room']}", json={"invite": code, "name": name, "sha1": hello["sha1"]})
    joined = r.json()
    if r.status_code != 200:
        raise NetworkError(joined.get("error", "couldn't join"))
    old = net.links.pop(slug, None)
    if old is not None:
        old.stop()
    net.link_project(slug, {"url": url, "room": hello["room"], "token": joined["token"], "member": joined["name"],
                            "host_title": hello["title"]}, client=client, start=start)
    log(f"joined {hello['title']} again as {joined['name']}")
    return slug


def join_room(net, link: str, name: str, rom: bytes, log=print, client: httpx.Client | None = None,
              start: bool = True) -> str:
    """Guest: check the invite, check the ROM is the same game, join, import it locally, link. Returns the slug.
    If this game is already linked to that room here, reconnect instead (no ROM needed)."""
    from ..rom import detect

    http = client or httpx.Client(timeout=httpx.Timeout(60, connect=15))
    url, code, hello = _hello(http, link)
    slug, _info = _linked_project(net, hello["sha1"], hello["room"])
    if slug:
        return reconnect(net, slug, link, log=log, client=client, start=start)
    if not rom:
        raise NetworkError("pick your own copy of the game to join")
    picked = detect.identify(rom)
    if picked.info["sha1"] != hello["sha1"]:
        raise NetworkError(f"this ROM isn't the same as the host's: the room is for {hello['title']} "
                           f"({hello['game_code']}, {hello['region']}), and every member needs their own copy of exactly that version")
    log(f"joining {hello['title']} ({hello['functions']} functions, {hello['matched']} matched)")
    r = http.post(f"{url}/collab/join?room={hello['room']}", json={"invite": code, "name": name, "sha1": hello["sha1"]})
    joined = r.json()
    if r.status_code != 200:
        raise NetworkError(joined.get("error", "couldn't join"))
    slug = net.import_rom(rom, toolchain=hello["compiler"]["toolchain"], flags=hello["compiler"]["flags"],
                          confirmed=True, log=log)
    net.link_project(slug, {"url": url, "room": hello["room"], "token": joined["token"], "member": joined["name"],
                            "host_title": hello["title"]}, client=client, start=start)
    log(f"linked as {joined['name']}: your agents now work with the room")
    return slug


def cloudflared_available() -> bool:
    return toolchains.get("cloudflared").supported or bool(shutil.which("cloudflared"))
