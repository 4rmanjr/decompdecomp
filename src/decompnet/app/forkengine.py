"""Fork projects: continue a public decomp with your agents, inside the project's own source tree.

Progress comes from objdiff reports. Agents claim functions (so parallel agents never collide), write them into
the project's real source files, build just that unit with the project's own build system and let objdiff judge.
A 100% match is committed to a local git branch, ready for the user to push or open a pull request with.
Guards from people who ran agents on decomps before: agents can only write C/C++ sources and headers (never
targets, assembly, configs or build scripts), inline assembly is refused, and a header edit is undone if it costs a
single match.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path

from .. import similarity
from ..forks import catalog, csource, drafts, ghidra, scratch_dsd, scratch_dtk
from ..forks.objdiff import ObjdiffError
from ..forks.project import STATE, ForkError, ForkProject
from ..network import Agent, Network, NetworkError

_REPO = re.compile(r"^(https://(github\.com|gitlab\.com|codeberg\.org|git\.[\w.-]+)/[\w.-]+/[\w.-]+?)(\.git)?/?$")
# A match has to come from C/C++: no inline assembly, asm functions, asm includes or embedded binaries.
_CHEAT = re.compile(r"\b(__asm__|__asm|asm|_asm)\b|INCLUDE_ASM|GLOBAL_ASM|INCLUDE_RODATA|\.incbin|#\s*include\s*[\"<][^\">]*\.(s|S|asm|bin|inc\.c)[\">]")


# `register int x asm("r4");` binds a variable to a register: an accepted matching technique (pret's GBA
# projects use it), not assembly code, so it doesn't count as inline asm.
_REGVAR = re.compile(r"\b(?:asm|__asm__|__asm)\s*\(\s*\"\$?[a-z]{1,2}\d{0,2}\"\s*\)", re.I)


def _strip_comments(code: str) -> str:
    return _REGVAR.sub(" ", re.sub(r"//[^\n]*|/\*.*?\*/", " ", code, flags=re.S))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "fork"


def objdiff_diff(fp: ForkProject, unit, symbol: str, target_only: bool = False):
    from ..forks import objdiff

    if not target_only:
        return fp.diff(unit, symbol)
    return objdiff.diff(unit.target, None, symbol, cwd=fp.root, options=fp.objdiff_config().get("options"))


def scaled(fuzzy: float | None, n_insns: int) -> int:
    """objdiff's match percentage as a score on the engine's scale (0 = matched)."""
    if fuzzy is None:
        return n_insns * 100
    return 0 if fuzzy >= 100 else max(1, round((1 - fuzzy / 100) * n_insns * 100))


class ForkEngine:
    def __init__(self, net):
        self.net = net
        self._locks: dict[int, threading.RLock] = defaultdict(threading.RLock)
        self._index: dict[int, dict] = {}  # project id -> {unit: {"mtime", "order", "ops"}}
        self._indexing: set[int] = set()
        self._ilock = threading.Lock()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def is_fork(proj) -> bool:
        return json.loads(proj["config"]).get("kind") == "fork"

    def get(self, slug: str):
        proj = self.net._project(slug)
        if not self.is_fork(proj):
            raise NetworkError(f"{slug} isn't a fork")
        return proj, ForkProject(Path(proj["root"])), json.loads(proj["config"])

    def _save_cfg(self, proj, cfg: dict) -> None:
        with self.net.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))

    @staticmethod
    def _meta(fn) -> dict:
        return json.loads(fn["meta"]) if fn["meta"] else {}

    def catalog(self) -> list[dict]:
        return catalog.catalog(self.net.workspace / ".decomp" / "catalog.json")

    # ------------------------------------------------------------------ creating a fork

    def clone(self, repo: str, *, name: str = "", platform: str = "", version: str | None = None, log=print) -> str:
        repo = repo.strip()
        local = Path(repo).expanduser()
        if not (_REPO.match(repo) or (local.is_dir() and (local / ".git").exists())):
            raise NetworkError("give a GitHub/GitLab/Codeberg repository URL (https://github.com/owner/repo)")
        if not shutil.which("git"):
            raise NetworkError("forking needs git: install it from https://git-scm.com")
        url = _REPO.match(repo).group(1) if _REPO.match(repo) else str(local.resolve())
        for p in self.net.store.all("SELECT slug, config FROM projects"):
            if json.loads(p["config"]).get("repo_url") == url:
                log(f"already forked as {p['slug']}")
                return p["slug"]
        base = _slug(name or url.rstrip("/").split("/")[-1])
        slug = self._free_slug(base)
        dest = self.net.workspace / slug
        log(f"cloning {url}")
        res = subprocess.run(["git", "clone", "--depth", "1", "--recurse-submodules", "--shallow-submodules", url, str(dest)],
                             capture_output=True, text=True)
        if res.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise NetworkError(f"git clone failed: {res.stderr.strip()[-400:]}")
        fp = ForkProject(dest)
        fp.git("checkout", "-q", "-b", "decomp-wtf")
        kind = fp.kind()
        versions = fp.versions()
        version = version if version in versions else (versions[0] if versions else version)
        cfg = {"kind": "fork", "repo_url": url, "platform": platform, "version": version, "fork_kind": kind,
               "ready": False, "legal": {"basis": "personal-copy", "license": "the project's own license"}}
        self._register(slug, name or url.rstrip("/").split("/")[-1], f"fork of {url}", dest, cfg, f"cloned {url}")
        log(f"cloned into {dest} ({kind} project)")
        return slug

    def _free_slug(self, base: str) -> str:
        slug, n = base, 2
        while self.net.store.one("SELECT 1 FROM projects WHERE slug=?", slug) or (self.net.workspace / slug).exists():
            slug, n = f"{base}-{n}", n + 1
        return slug

    def _register(self, slug: str, title: str, description: str, root: Path, cfg: dict, event: str) -> None:
        with self.net.store.tx() as db:
            pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                             (slug, title, description, "", 0, str(root), json.dumps(cfg))).lastrowid
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (pid, 1, "", None, "projects keep their headers in the tree", time.time(), ""))
            self.net._event(db, pid, None, "fork", event)

    def create_scratch(self, game: Path, *, compiler: str | None = None, log=print) -> str:
        """A GameCube, Wii or DS game, from scratch: generate the community's project layout for it (dtk-template,
        ds-decomp) and open it like a fork."""
        with open(game, "rb") as f:
            ds = scratch_dsd.is_nds(f.read(0x200))
        maker = scratch_dsd if ds else scratch_dtk
        info = maker.game_info(game)
        sha1 = info["sha1"] if ds else hashlib.sha1(info["dol"]).hexdigest()
        for p in self.net.store.all("SELECT slug, config FROM projects"):
            if json.loads(p["config"]).get("program_sha1") == sha1:
                log(f"already set up as {p['slug']}")
                return p["slug"]
        slug = self._free_slug(_slug(f"{info['title']} {info['id']}" if info.get("kind") != "dol" else info["title"]))
        dest = self.net.workspace / slug
        try:
            facts = maker.create(dest, game, compiler=compiler, log=log)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        cfg = {"kind": "fork", "origin": "scratch", "repo_url": None, "platform": facts["platform"],
               "version": facts["id"], "fork_kind": "manifest", "ready": False, "compiler": facts["compiler"],
               "program_sha1": sha1,
               "legal": {"basis": "personal-copy", "license": "personal use only",
                         "statement": "imported by the owner of this copy of the game, who confirmed it was legally obtained",
                         "confirmed_at": time.time()}}
        self._register(slug, facts["title"], f"{catalog.PLATFORMS[facts['platform']]} game {facts['id']}, from scratch",
                       dest, cfg, f"started {facts['id']} from scratch")
        self.check(slug, log=log)
        return slug

    def needs(self, slug: str) -> dict:
        _, fp, cfg = self.get(slug)
        return fp.needs(cfg.get("version")) | {"kind": fp.kind(), "versions": fp.versions(),
                                                "version": cfg.get("version"), "readme": fp.readme()}

    def setup(self, slug: str, game: bytes | Path | None, filename: str = "", log=print, move: bool = False) -> dict:
        """Add the user's game (bytes, or a file on disk) and run the project's setup, then measure it."""
        proj, fp, cfg = self.get(slug)
        if game is not None:
            if isinstance(game, (bytes, bytearray)):
                tmp = fp.root / STATE / "upload.tmp"
                tmp.write_bytes(game)
                game, move = tmp, True
            self._match_version(proj, fp, cfg, Path(game), log)
            dest = fp.place_game(Path(game), filename or Path(game).name, cfg.get("version"), move=move)
            log(f"your copy of the game is at {dest.relative_to(fp.root)}")
        if not fp.needs(cfg.get("version"))["auto"]:
            raise NetworkError("this project has its own setup: follow its README, then click Check again")
        fp.setup(cfg.get("version"), log=log)
        return self.check(slug, log=log)

    def _match_version(self, proj, fp: ForkProject, cfg: dict, game: Path, log) -> None:
        """A GameCube/Wii disc names its version (GP6E01, GP6P01...): use the project's matching version, or say
        which ones the project has, instead of failing deep in the build."""
        if fp.kind() != "dtk":
            return
        versions = fp.versions()
        gid = scratch_dtk.disc_id(game)
        if not gid or not versions or gid == cfg.get("version"):
            return
        if gid in versions:
            log(f"your disc is {gid}: using the project's {gid} version")
            cfg["version"] = gid
            self._save_cfg(proj, cfg)
        elif all(re.fullmatch(r"[A-Z0-9]{6}", v) for v in versions):
            raise NetworkError(f"this disc is {gid}, but the project decompiles {', '.join(versions)}: "
                               "pick your copy of one of those versions")

    def check(self, slug: str, log=print) -> dict:
        """After a manual setup (or to refresh): pick the project up once it has an objdiff.json."""
        proj, fp, cfg = self.get(slug)
        if not (fp.root / "objdiff.json").exists():
            raise NetworkError("no objdiff.json yet. The project's build creates it (often `python configure.py` "
                               "or `make`); see its README")
        cfg["ready"] = True
        self._save_cfg(proj, cfg)
        return self.sync(slug, log=log)

    def sync(self, slug: str, log=print) -> dict:
        """Measure the whole project with objdiff and update every function's state."""
        proj, fp, cfg = self.get(slug)
        log("measuring progress with objdiff")
        try:
            rep = fp.report()
        except (ObjdiffError, ForkError) as e:
            raise NetworkError(f"objdiff couldn't measure the project: {e}") from None
        units = {u.name: u for u in fp.units()}
        rows = [(u["name"], f) for u in rep.get("units", []) for f in u.get("functions", [])]
        counts: dict[str, int] = defaultdict(int)
        for _, f in rows:
            counts[f["name"]] += 1
        existing = {(r["unit"], self._meta(r).get("symbol")): r
                    for r in self.net.store.all("SELECT * FROM functions WHERE project_id=?", proj["id"])}
        with self.net.store.tx() as db:
            for uname, f in rows:
                sym = f["name"]
                fuzzy = f.get("fuzzy_match_percent")
                size = int(f.get("size", 0))
                n = max(1, size // 4)
                u = units.get(uname)
                md = f.get("metadata") or {}
                meta = {"symbol": sym, "demangled": md.get("demangled_name"), "source": u.source if u else None}
                status = "matched" if fuzzy is not None and fuzzy >= 100 else "open" if u and u.source else "nosource"
                score = None if fuzzy is None else scaled(fuzzy, n)
                vram = md.get("virtual_address")
                old = existing.get((uname, sym))
                if old is None:
                    name = sym if counts[sym] == 1 else f"{sym}@{uname}"
                    db.execute("INSERT INTO functions(project_id,name,size,n_insns,status,best_score,unit,vram,meta,asm) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?)", (proj["id"], name, size, n, status, score, uname,
                                                              int(vram) if vram else None, json.dumps(meta), ""))
                else:
                    if status != "matched" and old["best_source"] and old["best_score"] is not None:
                        score = old["best_score"] if score is None else min(score, old["best_score"])
                    db.execute("UPDATE functions SET size=?, n_insns=?, status=?, best_score=?, meta=? WHERE id=?",
                               (size, n, status, score, json.dumps(meta), old["id"]))
            m = rep.get("measures", {})
            self.net._event(db, proj["id"], None, "sync", f"{float(m.get('matched_code_percent', 0)):.2f}% of code matches")
        log(f"{m.get('matched_functions', 0)}/{m.get('total_functions', 0)} functions match, "
            f"{float(m.get('matched_code_percent', 0)):.2f}% of code")
        cfg["measures"] = m
        self._save_cfg(proj, cfg)
        if cfg.get("ready"):
            self.start_indexing(slug)
        return m

    # ------------------------------------------------------------------ look-alike index

    def _index_of(self, proj, fp: ForkProject) -> dict:
        if proj["id"] not in self._index:
            p = fp.root / STATE / "index.json"
            try:
                self._index[proj["id"]] = json.loads(p.read_text()) if p.exists() else {}
            except ValueError:
                self._index[proj["id"]] = {}
        return self._index[proj["id"]]

    def _save_index(self, proj, fp: ForkProject) -> None:
        with self._ilock:
            snapshot = dict(self._index.get(proj["id"], {}))
        try:
            (fp.root / STATE / "index.json").write_text(json.dumps(snapshot))
        except OSError:
            pass

    def _remember(self, proj, fp: ForkProject, unit, d) -> None:
        if unit.target is None or not unit.target.exists():
            return
        entry = {"mtime": unit.target.stat().st_mtime, "order": d.order, "ops": d.ops}
        idx = self._index_of(proj, fp)
        with self._ilock:
            idx[unit.name] = entry

    def _unit_index(self, proj, fp: ForkProject, unit, save: bool = True) -> dict | None:
        """Target order and mnemonics of a unit's functions, from its target object only (never races a build)."""
        idx = self._index_of(proj, fp)
        if unit.target is None or not unit.target.exists():
            return None
        hit = idx.get(unit.name)
        if hit and hit.get("mtime") == unit.target.stat().st_mtime:
            return hit
        row = self.net.store.one("SELECT meta FROM functions WHERE project_id=? AND unit=? LIMIT 1", proj["id"], unit.name)
        if row is None:
            return None
        try:
            d = objdiff_diff(fp, unit, self._meta(row).get("symbol"), target_only=True)
        except (ObjdiffError, ForkError):
            return None
        self._remember(proj, fp, unit, d)
        if save:
            self._save_index(proj, fp)
        return idx.get(unit.name)

    def start_indexing(self, slug: str) -> None:
        """Index every unit in the background so find_similar covers the whole project."""
        proj, fp, cfg = self.get(slug)
        if proj["id"] in self._indexing or not cfg.get("ready"):
            return
        self._indexing.add(proj["id"])

        def run():
            try:
                for n, unit in enumerate(fp.units()):
                    if self.net.store.one("SELECT 1 FROM projects WHERE id=?", proj["id"]) is None:
                        return
                    self._unit_index(proj, fp, unit, save=False)
                    if n % 50 == 49:
                        self._save_index(proj, fp)
                self._save_index(proj, fp)
            finally:
                self._indexing.discard(proj["id"])
        threading.Thread(target=run, daemon=True, name=f"index-{slug}").start()

    def similar(self, proj, fp: ForkProject, fn, top: int = 5) -> list[tuple[dict, float]]:
        """Functions that look like `fn`, best first: [(row, similarity)]."""
        meta = self._meta(fn)
        unit = fp.unit(fn["unit"])
        own = self._unit_index(proj, fp, unit)
        if not own or meta.get("symbol") not in own["ops"]:
            return []
        target = own["ops"][meta["symbol"]]
        cands = {}
        with self._ilock:
            entries = list(self._index_of(proj, fp).items())
        for uname, entry in entries:
            for sym, ops in entry["ops"].items():
                if (uname, sym) != (fn["unit"], meta.get("symbol")):
                    cands[f"{uname}\0{sym}"] = ops
        out = []
        rows = {(r["unit"], self._meta(r).get("symbol")): r for r in self.net.store.all(
            "SELECT id, name, unit, status, best_score, meta, n_insns FROM functions WHERE project_id=?", proj["id"])}
        for key, sim in similarity.rank(target, cands, top=top * 2):
            uname, sym = key.split("\0", 1)
            if (uname, sym) in rows:
                out.append((rows[(uname, sym)], sim))
        out.sort(key=lambda x: (x[0]["status"] != "matched", -x[1]))
        return out[:top]

    def find_similar(self, slug: str, function: str, limit: int = 5) -> str:
        proj, fp, cfg = self.get(slug)
        fn = self._fn(slug, function)
        hits = self.similar(proj, fp, fn, top=max(1, min(int(limit), 15)))
        covered = len(self._index_of(proj, fp))
        total = len(fp.units())
        note = f"(index covers {covered} of {total} source files{'; still indexing' if proj['id'] in self._indexing else ''})"
        if not hits:
            return f"No look-alikes found for {fn['name']} {note}."
        lines = [f"Functions that look like {fn['name']} {note}:"]
        for r, sim in hits:
            state = "matched" if r["status"] == "matched" else f"best {r['best_score']}" if r["best_score"] is not None else "not started"
            lines.append(f"- {r['name']}  {sim * 100:.0f}% similar  {state}  ({r['unit']}, {r['n_insns']} insns)")
        best = next((r for r, _ in hits if r["status"] == "matched"), None)
        if best is not None:
            src = self._current_source(fp, best)
            if src:
                lines += ["", f"Source of {best['name']} (matched):", "```c", src.strip()[:4000], "```"]
        return "\n".join(lines)

    def _current_source(self, fp: ForkProject, fn) -> str | None:
        unit = fp.unit(fn["unit"])
        if not unit.source or not (fp.root / unit.source).exists():
            return None
        text = (fp.root / unit.source).read_text(errors="replace")
        _, span = self._find(text, self._names(self._meta(fn), fn))
        return text[span[0]:span[1]] if span else None

    # ------------------------------------------------------------------ for agents

    def info(self, slug: str) -> str:
        proj, fp, cfg = self.get(slug)
        m = cfg.get("measures") or {}
        units = fp.units() if cfg.get("ready") else []
        scratch = next((u.scratch for u in units if u.scratch), None) or {}
        platform = catalog.PLATFORMS.get(cfg.get("platform"), cfg.get("platform") or "unknown platform")
        return (f"{proj['name']}: a fork of {cfg['repo_url']} ({platform}{', ' + cfg['version'] if cfg.get('version') else ''})\n"
                f"progress: {m.get('matched_functions', '?')}/{m.get('total_functions', '?')} functions, "
                f"{float(m.get('matched_code_percent', 0) or 0):.2f}% of code, in {len(units)} source files\n"
                f"compiler: {scratch.get('compiler', 'set by the project build')} {scratch.get('c_flags', '')}\n"
                "You work in the project's real source tree. claim_function, then compile_and_diff writes your function "
                "into its source file, builds that file with the project's build system and diffs it with objdiff. "
                "read_file and search_code show how the codebase is written; edit_file changes headers and file-level "
                "declarations (undone if it breaks any match). Guides: guide()."
                + "".join(f"\nThe project has notes for contributors: read_file(\"{n}\") before you start."
                          for n in ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md") if (fp.root / n).exists())[:400])

    def _fn(self, slug: str, name: str):
        return self.net._function(name, slug)

    @staticmethod
    def _names(meta: dict, fn) -> list[str]:
        """Ways the function may be spelled in source: full C++ name, Class::method, then the plain symbol."""
        out = []
        dem = (meta.get("demangled") or "").split("(", 1)[0].strip()
        if dem:
            dem = dem.split(" ")[-1] if " " in dem and "operator" not in dem else dem
            out.append(dem)
            parts = dem.split("::")
            if len(parts) > 2:
                out.append("::".join(parts[-2:]))
            if len(parts) > 1:
                out.append(parts[-1])
        out.append(meta.get("symbol") or fn["name"].split("@")[0])
        return list(dict.fromkeys(out))

    def _find(self, text: str, names: list[str]):
        for n in names:
            span = csource.find(text, n)
            if span:
                return n, span
        return None, None

    def _apply_scores(self, proj, unit: str, scores: dict[str, float | None], skip: int | None = None) -> None:
        """Bring every function of a unit up to date with a fresh diff (cheaper than a full report)."""
        rows = self.net.store.all("SELECT id, status, n_insns, best_score, best_source, meta FROM functions "
                                  "WHERE project_id=? AND unit=?", proj["id"], unit)
        with self.net.store.tx() as db:
            for r in rows:
                sym = self._meta(r).get("symbol")
                if r["id"] == skip or sym not in scores or r["status"] == "nosource":
                    continue
                pct = scores[sym]
                if pct is not None and pct >= 100:
                    db.execute("UPDATE functions SET status='matched', best_score=0 WHERE id=?", (r["id"],))
                elif r["status"] == "matched":
                    db.execute("UPDATE functions SET status='open' WHERE id=?", (r["id"],))
                elif pct is not None and not r["best_source"]:
                    db.execute("UPDATE functions SET best_score=? WHERE id=?", (scaled(pct, r["n_insns"]), r["id"]))

    def brief(self, fn, agent: Agent | None, header: str) -> str:
        proj = self.net.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        _, fp, cfg = self.get(proj["slug"])
        meta = self._meta(fn)
        unit = fp.unit(fn["unit"])
        lines = [f"## {header}: {fn['name']}  ({proj['name']})", f"unit {unit.name}, {fn['size']} bytes"]
        if meta.get("demangled"):
            lines.append(f"C++ name: {meta['demangled']}")
        if unit.scratch:
            lines.append(f"compiler: {unit.scratch.get('compiler', '?')} {unit.scratch.get('c_flags', '')}")
        src_text = None
        if unit.source:
            lines.append(f"source file: {unit.source}")
            p = fp.root / unit.source
            src_text = p.read_text(errors="replace") if p.exists() else None
        else:
            lines.append("this unit has no source file yet, so it can't be decompiled from here")
        try:
            d = fp.diff(unit, meta.get("symbol") or fn["name"])
            pct = "not in your build yet" if d.match_percent is None else f"{d.match_percent:.1f}% match"
            asm = d.target_asm().splitlines()
            if len(asm) > 500:
                asm = asm[:500] + [f"... ({len(asm) - 500} more instructions)"]
            lines += [f"current state: {pct}", "", "### Target assembly", "```asm", *asm, "```"]
            if d.match_percent is not None and not d.matched:
                lines += ["", "### Diff against the current build", d.render()]
        except (ObjdiffError, ForkError) as e:
            lines.append(f"(objdiff couldn't diff this function: {e})")
        _, span = self._find(src_text, self._names(meta, fn)) if src_text else (None, None)
        if span:
            lines += ["", "### Current source", "```c", src_text[span[0]:span[1]].strip(), "```"]
        else:
            lines += ["", f"### Not in {unit.source or 'the source'} yet: compile_and_diff adds it at the end."]
        if fn["best_source"] and fn["status"] != "matched":
            lines += ["", f"### Best attempt so far (score {fn['best_score']})", "```c", fn["best_source"].strip(), "```"]
        elif fn["status"] != "matched" and (span is None or csource.is_stub(src_text[span[0]:span[1]])):
            seed = self._draft(proj, fp, cfg, fn, unit)
            if seed:
                lines += ["", "### Machine draft: a starting point, often wrong in types and details", "```c",
                          seed.strip(), "```"]
        try:
            alike = [(r, sim) for r, sim in self.similar(proj, fp, fn, top=3) if r["status"] == "matched"]
        except (ObjdiffError, ForkError):
            alike = []
        if alike:
            r, sim = alike[0]
            src = self._current_source(fp, r)
            lines += ["", f"### Looks like {r['name']} ({sim * 100:.0f}% similar, already matched)"]
            if src and len(src) < 3000:
                lines += ["```c", src.strip(), "```"]
            if len(alike) > 1:
                lines.append("also similar and matched: " + ", ".join(f"{x['name']} ({s * 100:.0f}%)" for x, s in alike[1:]))
        done = self.net.store.all("SELECT name FROM functions WHERE project_id=? AND unit=? AND status='matched' "
                                  "AND id != ? ORDER BY n_insns DESC LIMIT 12", fn["project_id"], fn["unit"], fn["id"])
        if done:
            lines += ["", "Already matched in this file (read them for the codebase's style): "
                      + ", ".join(r["name"] for r in done)]
        notes = self.net.store.all("SELECT n.text, a.name FROM notes n LEFT JOIN agents a ON a.id=n.agent_id "
                                   "WHERE n.function_id=? ORDER BY n.id DESC LIMIT 6", fn["id"])
        if notes:
            lines += ["", "### Notes"] + [f"- ({n['name']}) {n['text']}" for n in notes]
        lines += ["", "Send compile_and_diff exactly one complete function definition (same name and signature as the "
                      "original). Structs, prototypes and file-level data go in with edit_file."]
        return "\n".join(lines)

    def _draft(self, proj, fp: ForkProject, cfg: dict, fn, unit) -> str | None:
        """m2c draft from the project's assembly for this function, cached in the database."""
        if fn["seed"] is not None:
            return fn["seed"] or None
        hint = (unit.scratch or {}).get("compiler", "") or json.dumps(fp.needs(cfg.get("version")))
        meta = self._meta(fn)
        try:
            text = drafts.draft(fp.root, cfg.get("platform") or "", unit.name, meta.get("symbol") or fn["name"],
                                unit.source, compiler_hint=hint)
            text = text and "/* m2c */\n" + text
            if not text and unit.target is not None and unit.target.exists() and ghidra.available():
                found = ghidra.decompile_object(unit.target, fp.root / STATE / "ghidra")
                names = [meta.get("symbol") or fn["name"], *self._names(meta, fn)]
                body = next((found[n] for n in names if n in found), None)
                text = body and "/* Ghidra */\n" + body.strip()
        except OSError:
            text = None
        with self.net.store.tx() as db:
            db.execute("UPDATE functions SET seed=? WHERE id=?", (text or "", fn["id"]))
        return text

    def claim(self, agent: Agent, slug: str, function: str | None = None, strategy: str = "easiest") -> str:
        fn = self.net.claim_row(agent, slug, function, strategy)
        if fn is None:
            return "Nothing left to claim: every function with a source file matches or someone is on it. 🎉"
        return self.brief(fn, agent, "CLAIMED")

    def get_function(self, agent: Agent | None, slug: str, function: str) -> str:
        return self.brief(self._fn(slug, function), agent, "VIEW")

    def list_functions(self, slug: str, status: str = "open", limit: int = 40) -> str:
        proj, _, _ = self.get(slug)
        where = {"open": "status='open'", "matched": "status='matched'", "all": "1"}.get(status)
        if where is None:
            raise NetworkError("status must be open, matched or all")
        rows = self.net.store.all(f"SELECT name, unit, n_insns, status, best_score FROM functions WHERE project_id=? "
                                  f"AND {where} ORDER BY n_insns LIMIT ?", proj["id"], max(1, min(int(limit), 200)))
        out = [f"{r['name']:<40} {r['unit']:<36} {r['n_insns']:>5} insns  "
               f"{'matched' if r['status'] == 'matched' else 'best ' + str(r['best_score']) if r['best_score'] is not None else 'new'}"
               for r in rows]
        return "\n".join(out) or "none"

    def attempt(self, agent: Agent, slug: str, function: str, source: str) -> dict:
        proj, fp, cfg = self.get(slug)
        fn = self._fn(slug, function)
        meta = self._meta(fn)
        if len(source) > 64 * 1024:
            raise NetworkError("source too large")
        if _CHEAT.search(_strip_comments(source)):
            raise NetworkError("inline assembly, asm functions and asm includes aren't allowed: a match has to come from C/C++")
        names = self._names(meta, fn)
        _, span = self._find(source, names)
        if span is None:
            raise NetworkError(f"send the complete definition of {names[0]} (with its body)")
        if csource._blank(source[:span[0]] + source[span[1]:]).strip():
            raise NetworkError("send only the one function definition. Put structs, prototypes, globals and helper "
                               "functions in with edit_file first")
        code = source[span[0]:span[1]]
        unit = fp.unit(fn["unit"])
        if not unit.source:
            raise NetworkError(f"unit {unit.name} has no source file, so there's nowhere to write it")
        with self.net.store.tx() as db:  # claim bookkeeping, as for ROM projects
            self.net._expire_claims(db)
            claim = db.execute("SELECT agent_id FROM claims WHERE function_id=?", (fn["id"],)).fetchone()
            if claim is not None and claim["agent_id"] != agent.id:
                raise NetworkError(f"{fn['name']} is claimed by {self.net._agent_name(claim['agent_id'])}")
            now = time.time()
            if claim is None and fn["status"] != "matched":
                db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + self.net.claim_ttl))
            elif claim is not None:
                db.execute("UPDATE claims SET expires_at=? WHERE function_id=?", (now + self.net.claim_ttl, fn["id"]))
        path = fp.safe_path(unit.source, write=True)
        symbol = meta.get("symbol") or fn["name"]
        protected = {self._meta(r).get("symbol") for r in self.net.store.all(
            "SELECT meta FROM functions WHERE project_id=? AND unit=? AND status='matched' AND id != ?",
            proj["id"], fn["unit"], fn["id"])}
        with self._locks[proj["id"]]:
            before = path.read_text(errors="replace") if path.exists() else ""
            hit, old_span = self._find(before, names)
            if old_span:
                after = before[:old_span[0]] + code.rstrip("\n") + "\n" + before[old_span[1]:]
            else:
                after = self._insert_in_order(proj, fp, unit, fn, before, code)
            path.write_text(after)

            def undo() -> None:
                path.write_text(before)
                fp.build_unit(unit)

            ok, log = fp.build_unit(unit)
            if not ok:
                undo()
                self.net._record(fn, agent, None, code, 0)
                return {"text": f"❌ BUILD ERROR (your change was undone)\n{log[-3000:]}", "score": None, "matched": False}
            try:
                d = fp.diff(unit, symbol)
            except (ObjdiffError, ForkError) as e:
                undo()
                return {"text": f"❌ objdiff couldn't compare (your change was undone): {e}", "score": None, "matched": False}
            broken = sorted(n for n in protected if n and (d.unit_scores.get(n) or 0) < 100)
            if broken:
                undo()
                self.net._record(fn, agent, None, code, 0, compiled=True)
                return {"text": f"❌ REJECTED and undone: your change breaks matched function(s) {', '.join(broken[:8])}. "
                                "Check you replaced the right definition (overloads?) and didn't change shared code.",
                        "score": None, "matched": False}
            pct = d.match_percent if d.match_percent is not None else 0.0
            score = scaled(pct, fn["n_insns"])
            matched = d.matched
            if fn["status"] == "matched":  # a rewrite of a matched function must keep matching
                if not matched:
                    undo()
                    return {"text": f"❌ {fn['name']} already matches; your version is {pct:.1f}%, so the matching one "
                                    "was kept. Call claim_function for a function that still needs work.",
                            "score": score, "matched": False}
                fp.git("add", unit.source)
                fp.git("commit", "-q", "-m", f"Tidy {meta.get('demangled') or symbol} (still matches)\n\n"
                                              f"Edited by {agent.name} with decomp.wtf.")
                return {"text": f"✅ {fn['name']} still matches; your tidier version is committed.", "score": 0,
                        "matched": True}
            self.net._record(fn, agent, score, code, 0, matched=matched)
            self._apply_scores(proj, fn["unit"], d.unit_scores, skip=fn["id"])
            self._remember(proj, fp, unit, d)
            if matched:
                fp.git("add", unit.source)
                fp.git("commit", "-q", "-m", f"Match {meta.get('demangled') or symbol}\n\n"
                                              f"Decompiled by {agent.name} with decomp.wtf.")
                return {"text": f"✅ MATCH! {fn['name']} is 100% identical to the original. Committed to git in the "
                                "project. Call claim_function for the next one.", "score": 0, "matched": True}
        return {"text": f"{pct:.1f}% match (score {score}; 0 = identical).\n{d.render()}", "score": score, "matched": False}

    def _insert_in_order(self, proj, fp: ForkProject, unit, fn, text: str, code: str) -> str:
        """Place a new function where it was in the original: after the closest earlier function present in the
        file, else before the closest later one, else at the end."""
        code = code.strip("\n") + "\n"
        entry = self._unit_index(proj, fp, unit)
        sym = self._meta(fn).get("symbol")
        order = entry["order"] if entry else []
        if sym in order:
            rows = {self._meta(r).get("symbol"): r for r in self.net.store.all(
                "SELECT name, meta FROM functions WHERE project_id=? AND unit=?", proj["id"], unit.name)}
            k = order.index(sym)
            for other in reversed(order[:k]):
                if other in rows:
                    _, span = self._find(text, self._names(self._meta(rows[other]), rows[other]))
                    if span:
                        return text[:span[1]].rstrip("\n") + "\n\n" + code + "\n" + text[span[1]:].lstrip("\n")
            for other in order[k + 1:]:
                if other in rows:
                    _, span = self._find(text, self._names(self._meta(rows[other]), rows[other]))
                    if span:
                        return text[:span[0]] + code + "\n" + text[span[0]:]
        return text.rstrip("\n") + "\n\n" + code

    def release(self, agent: Agent, slug: str, function: str, notes: str) -> str:
        fn = self._fn(slug, function)
        latest = self.net.store.one("SELECT score FROM attempts WHERE function_id=? AND agent_id=? ORDER BY id DESC LIMIT 1",
                                    fn["id"], agent.id)
        if (fn["best_source"] and fn["status"] != "matched" and latest and latest["score"] is not None
                and fn["best_score"] is not None and latest["score"] > fn["best_score"]):
            try:  # leave the file at its best state for the next agent
                self.attempt(agent, slug, function, fn["best_source"])
            except NetworkError:
                pass
        return Network.release(self.net, agent, function, notes, slug)

    def read_file(self, slug: str, path: str, start: int = 1, count: int = 200) -> str:
        _, fp, _ = self.get(slug)
        p = fp.safe_path(path)
        if not p.is_file():
            raise NetworkError(f"{path} isn't a file")
        with open(p, "rb") as f:
            head = f.read(8192)
        if b"\0" in head or p.stat().st_size > 4 * 1024 * 1024:
            raise NetworkError(f"{path} isn't a source or text file")  # never the user's game data
        lines = p.read_text(errors="replace").splitlines()
        start = max(1, int(start))
        chunk = lines[start - 1:start - 1 + max(1, min(int(count), 600))]
        out = [f"{path} (lines {start}-{start + len(chunk) - 1} of {len(lines)})"]
        out += [f"{start + i:5d}  {line}" for i, line in enumerate(chunk)]
        return "\n".join(out)

    def search(self, slug: str, pattern: str, glob: str = "") -> str:
        _, fp, _ = self.get(slug)
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise NetworkError(f"bad pattern: {e}") from None
        root = fp.root.resolve()
        if glob:
            files = sorted(fp.root.glob(glob))
        else:
            files = sorted(p for ext in ("c", "cc", "cpp", "h", "hpp", "inc") for p in fp.root.rglob(f"*.{ext}"))
        hits = []
        for p in files:
            rp = p.resolve()
            if root not in rp.parents or not rp.is_file():
                continue
            rel = rp.relative_to(root)
            if {".git", "build", STATE} & set(rel.parts):
                continue
            for i, line in enumerate(rp.read_text(errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel.as_posix()}:{i}: {line.strip()[:160]}")
                    if len(hits) >= 80:
                        return "\n".join(hits + ["... (more matches; narrow the pattern)"])
        return "\n".join(hits) or "no matches"

    def edit_file(self, agent: Agent, slug: str, path: str, old: str, new: str, message: str) -> str:
        proj, fp, cfg = self.get(slug)
        p = fp.safe_path(path, write=True)
        if not message.strip():
            raise NetworkError("give a short message explaining the change")
        if _CHEAT.search(_strip_comments(new)):
            raise NetworkError("inline assembly, asm functions and asm includes aren't allowed")
        with self._locks[proj["id"]]:
            existed = p.exists()
            text = p.read_text(errors="replace") if existed else ""
            if old:
                if text.count(old) != 1:
                    raise NetworkError(f"`old` must appear exactly once in {path} (found {text.count(old)})")
                updated = text.replace(old, new)
            else:
                updated = text.rstrip("\n") + ("\n" if text else "") + new.strip("\n") + "\n"
            before = {(r["unit"], self._meta(r).get("symbol")) for r in self.net.store.all(
                "SELECT unit, meta FROM functions WHERE project_id=? AND status='matched'", proj["id"])}
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(updated)

            def undo() -> None:
                if existed:
                    p.write_text(text)
                else:
                    p.unlink(missing_ok=True)
                fp.build_all()

            fp.build_all()
            try:
                rep = fp.report()
            except (ObjdiffError, ForkError) as e:
                undo()
                return f"❌ the project couldn't be measured after your edit, so it was undone: {e}"
            still = {(u["name"], f["name"]) for u in rep.get("units", []) for f in u.get("functions", [])
                     if (f.get("fuzzy_match_percent") or 0) >= 100}
            lost = sorted(sym for unit, sym in before - still)
            if lost:
                undo()
                return f"❌ REJECTED and undone: the edit breaks {len(lost)} matched function(s): {', '.join(lost[:10])}"
            fp.git("add", path)
            fp.git("commit", "-q", "-m", f"{message.strip()}\n\nEdited by {agent.name} with decomp.wtf.")
            with self.net.store.tx() as db:
                self.net._event(db, proj["id"], agent.id, "context", f"{path}: {message.strip()[:120]}")
        self.sync(slug, log=lambda *_: None)
        return f"✅ {path} updated; every matched function still matches."

    # ------------------------------------------------------------------ for the GUI

    def overview(self, slug: str) -> dict:
        proj, fp, cfg = self.get(slug)
        agg = self.net.store.one(
            "SELECT COUNT(*) AS total, SUM(status='matched') AS matched, SUM(size) AS bytes, "
            "SUM(CASE WHEN status='matched' THEN size ELSE 0 END) AS matched_bytes, SUM(attempts) AS attempts "
            "FROM functions WHERE project_id=?", proj["id"])
        m = cfg.get("measures") or {}
        need = fp.needs(cfg.get("version")) if not cfg.get("ready") else None
        return {
            "slug": slug, "title": proj["name"], "description": proj["description"], "kind": "fork",
            "platform": cfg.get("platform"), "platform_name": catalog.PLATFORMS.get(cfg.get("platform"), cfg.get("platform") or ""),
            "repo_url": cfg.get("repo_url"), "fork_kind": cfg.get("fork_kind"), "version": cfg.get("version"),
            "origin": cfg.get("origin"), "compiler_name": cfg.get("compiler"),
            "versions": fp.versions(), "ready": bool(cfg.get("ready")), "needs": need,
            "rom": {"game_code": cfg.get("version") or "", "region": catalog.PLATFORMS.get(cfg.get("platform"), ""),
                    "title": proj["name"]},
            "compiler": {"toolchain": "project", "name": "Project's own build", "flags": [], "installed": True,
                         "proprietary": False, "supported": True},
            "compilers": [],
            "functions": agg["total"] or 0, "matched": agg["matched"] or 0, "bytes": agg["bytes"] or 0,
            "matched_bytes": agg["matched_bytes"] or 0, "attempts": agg["attempts"] or 0,
            "context_version": 0, "folder": proj["root"], "room": None,
            "measures": m,
        }

    def function_diff(self, slug: str, name: str) -> dict:
        """objdiff's diff of the function as it is in the source tree now."""
        proj, fp, cfg = self.get(slug)
        fn = self._fn(slug, name)
        meta = self._meta(fn)
        try:
            d = fp.diff(fp.unit(fn["unit"]), meta.get("symbol") or fn["name"])
        except (ObjdiffError, ForkError) as e:
            return {"kind": None, "rows": None, "error": f"objdiff: {e}"}
        if d.match_percent is None:
            return {"kind": None, "rows": None}
        return {"kind": "matched" if fn["status"] == "matched" else "latest", "rows": [list(r) for r in d.rows],
                "score": None, "percent": round(d.match_percent, 1), "problems": []}

    def function_detail(self, slug: str, name: str) -> dict:
        proj, fp, cfg = self.get(slug)
        fn = self._fn(slug, name)
        meta = self._meta(fn)
        unit = fp.unit(fn["unit"])
        asm, current, pct = "", None, None
        try:
            d = fp.diff(unit, meta.get("symbol") or fn["name"])
            asm, pct = d.target_asm(), d.match_percent
        except (ObjdiffError, ForkError) as e:
            asm = f"(objdiff: {e})"
        if unit.source and (fp.root / unit.source).exists():
            text = (fp.root / unit.source).read_text(errors="replace")
            _, span = self._find(text, self._names(meta, fn))
            current = text[span[0]:span[1]] if span else None
        attempts = self.net.store.all("SELECT t.score, t.created_at, a.name AS agent FROM attempts t LEFT JOIN agents a "
                                      "ON a.id=t.agent_id WHERE t.function_id=? ORDER BY t.id", fn["id"])
        notes = self.net.store.all("SELECT n.text, n.created_at, a.name AS agent FROM notes n LEFT JOIN agents a "
                                   "ON a.id=n.agent_id WHERE n.function_id=? ORDER BY n.id DESC LIMIT 10", fn["id"])
        return {"name": fn["name"], "vram": fn["vram"] or 0, "rom_offset": 0, "size": fn["size"], "insns": fn["n_insns"],
                "status": fn["status"], "best": fn["best_score"], "asm": asm, "unit": fn["unit"],
                "source": current or fn["best_source"], "source_kind": "matched" if fn["status"] == "matched" else
                ("latest" if current else "best" if fn["best_source"] else None),
                "match_percent": pct, "scores": [{"score": a["score"], "t": a["created_at"], "agent": a["agent"]} for a in attempts],
                "notes": [dict(n) for n in notes], "calls": [], "called_by": []}
