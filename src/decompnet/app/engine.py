"""The local engine: ROM projects on top of the coordination core.

Network (claims, attempts, best attempts, notes, a regression-tested shared header) already does the
bookkeeping several agents need to work on one codebase. A ROM project adds:

- targets taken straight from the ROM (splat), matched by linking the candidate at the function's
  address (rom.match), so a match is byte-identical code;
- a symbol table, so agents can name things (rename_symbol) without breaking matches;
- tools for what ROMs need: split a function the disassembler merged, read data, see who calls what.

Everything stays on this computer.
"""

from __future__ import annotations

import json
import re
import secrets
import struct
import threading
import time
from pathlib import Path

from .. import seeds, toolchains
from ..network import Agent, Network, NetworkError, _hash_token
from ..rom import detect, match, split
from ..rom.symbols import AUTO_NAME, IDENT, Symbol, SymbolTable

DEFAULT_TOOLCHAIN = {"KMC": "gcc-2.7.2-kmc", "IDO": "ido-7.1", "PSYQ": "gcc-2.7.2-psx"}
COMPILERS = {
    "gcc-2.7.2-kmc": "many third-party N64 games",
    "ido-7.1": "most later first-party N64 games",
    "ido-5.3": "early N64 games (1996-97)",
    "gcc-2.7.2-psx": "PlayStation games (PSY-Q era GCC)",
    "gcc-2.8.1-psx": "later PlayStation games",
}
SHORT_NAMES = {"gcc-2.7.2-kmc": "KMC GCC 2.7.2", "ido-7.1": "IDO 7.1", "ido-5.3": "IDO 5.3",
               "gcc-2.7.2-psx": "GCC 2.7.2 (PS1)", "gcc-2.8.1-psx": "GCC 2.8.1 (PS1)"}
PLATFORM_COMPILERS = {"n64": ["gcc-2.7.2-kmc", "ido-7.1", "ido-5.3"], "psx": ["gcc-2.7.2-psx", "gcc-2.8.1-psx"]}
_AUTO_TOKEN = re.compile(r"\b(?:func|D|B|jtbl|RO|STR|FLT|DBL)_[0-9A-F]{8}\b")
_ADDRESS = re.compile(r"^(?:0x)?([0-9A-Fa-f]{8})$")

BASE_HEADER = """/* context.h for {title}: shared by every function. Agents grow it with edit_context:
 * add a prototype or struct the moment you work one out, so the next function benefits. */

typedef signed char s8;
typedef unsigned char u8;
typedef signed short s16;
typedef unsigned short u16;
typedef signed int s32;
typedef unsigned int u32;
typedef signed long long s64;
typedef unsigned long long u64;
typedef float f32;
typedef double f64;
typedef volatile u8 vu8;
typedef volatile u16 vu16;
typedef volatile u32 vu32;
typedef volatile s32 vs32;
typedef int s32_bool;

#define NULL ((void *)0)
#define TRUE 1
#define FALSE 0
#define ARRAY_COUNT(arr) (s32)(sizeof(arr) / sizeof(arr[0]))
"""


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40] or "rom"


def _preview(data: bytes | None, kind: str) -> str:
    if not data:
        return ""
    text = data.split(b"\0", 1)[0]
    if len(text) >= 3 and all(32 <= c < 127 or c in (9, 10) for c in text):
        return json.dumps(text.decode("ascii"))
    return data[:16].hex(" ")


class RomNetwork(Network):
    def __init__(self, db_path: str | Path, workspace: Path, claim_ttl: int = 30 * 60):
        super().__init__(db_path, claim_ttl=claim_ttl)
        self.workspace = Path(workspace)
        self._syms: dict[int, SymbolTable] = {}
        self._sym_override: dict[int, SymbolTable] = {}
        self._roms: dict[int, tuple[bytes, list[split.Segment]]] = {}
        self._xrefs: dict[int, dict] = {}
        self._seed_lock = threading.Lock()
        self.links: dict = {}  # slug -> collab.Link, for projects joined from a friend's room
        self.rooms: dict = {}  # slug -> collab.Room, for projects this computer shares
        self._remote_ids: dict[str, int] = {}

    # ------------------------------------------------------------------ basics

    def _require_budget(self, agent: Agent) -> None:
        """Your machine, your agent: no compile budget."""

    @staticmethod
    def cfg(proj) -> dict:
        return json.loads(proj["config"])

    def is_rom(self, proj) -> bool:
        return self.cfg(proj).get("kind") == "rom"

    def project_row(self, slug: str):
        return self._project(slug)

    def symbols(self, project_id: int) -> SymbolTable:
        if project_id in self._sym_override:
            return self._sym_override[project_id]
        if project_id not in self._syms:
            rows = self.store.all("SELECT name, address, size, kind FROM symbols WHERE project_id=?", project_id)
            self._syms[project_id] = SymbolTable([Symbol(r["name"], r["address"], r["size"], r["kind"]) for r in rows])
        return self._syms[project_id]

    def _rom(self, proj) -> tuple[bytes, list[split.Segment]]:
        if proj["id"] not in self._roms:
            cfg = self.cfg(proj)
            rom = (Path(proj["root"]) / cfg["rom"]["file"]).read_bytes()
            segs = [split.Segment(**s) for s in cfg["segments"]]
            self._roms[proj["id"]] = (rom, segs)
        return self._roms[proj["id"]]

    def reader(self, proj):
        rom, segs = self._rom(proj)

        def read(addr: int, n: int) -> bytes | None:
            for seg in segs:
                if seg.contains(addr, n):
                    at = seg.rom_start + addr - seg.vram
                    return rom[at:at + n]
            return None
        return read

    def _function(self, name: str, project: str | None):
        """Functions can also be named by address (0x80001234), handy for agents reading assembly."""
        m = _ADDRESS.match(name.strip()) if name else None
        if m:
            proj = self._project(project)
            row = self.store.one("SELECT * FROM functions WHERE project_id=? AND vram=?", proj["id"], int(m.group(1), 16))
            if row is not None:
                return row
        return super()._function(name, project)

    def _rom_target(self, fn) -> match.RomTarget:
        proj = self.store.one("SELECT config FROM projects WHERE id=?", fn["project_id"])
        cfg = json.loads(proj["config"])
        return match.RomTarget(fn["name"], fn["vram"], fn["target"], match.RomTarget.relocs_from_json(json.loads(fn["relocs"])),
                               cfg.get("big_endian", True), cfg.get("gp", 0))

    def _make_target(self, proj, rf: split.RomFunction, syms: SymbolTable) -> match.RomTarget:
        cfg = self.cfg(proj)
        return match.target_from_split(rf, syms.resolve, cfg.get("big_endian", True), cfg.get("gp", 0))

    def _target_obj(self, fn, proj):
        if not self.is_rom(proj):
            return super()._target_obj(fn, proj)
        return match.canonical_target(self._rom_target(fn), self.symbols(proj["id"]))

    def _evaluate(self, fn, proj, obj: bytes):
        if not self.is_rom(proj):
            return super()._evaluate(fn, proj, obj)
        res = match.link(obj, fn["name"], self._rom_target(fn), self.symbols(proj["id"]), self.reader(proj))
        return res.candidate, res.exact if res.candidate is not None else None, res.problems

    def rename_tokens(self, proj, text: str) -> str:
        """Show automatic names under their current names (func_80001234 -> deflate)."""
        syms = self.symbols(proj["id"])
        return _AUTO_TOKEN.sub(lambda m: syms.label(int(m.group(0)[-8:], 16)), text)

    def _asm_text(self, fn, proj) -> str:
        if not self.is_rom(proj):
            return super()._asm_text(fn, proj)
        return self.rename_tokens(proj, fn["asm"])

    # ------------------------------------------------------------------ import

    def import_rom(self, data: bytes, *, toolchain: str | None = None, flags: list[str] | None = None,
                   confirmed: bool = False, log=print) -> str:
        """Import a ROM the user owns: split it, store every function as a target. Returns the slug."""
        if not confirmed:
            raise NetworkError("confirm that this is your own legally obtained copy of the game")
        picked = detect.identify(data)
        rom, info = picked.data, picked.info
        for p in self.store.all("SELECT slug, config FROM projects"):
            if json.loads(p["config"]).get("rom", {}).get("sha1") == info["sha1"]:
                log(f"already imported as {p['slug']}")
                return p["slug"]
        base = _slugify(info["title"])
        slug, n = base, 2
        while self.store.one("SELECT 1 FROM projects WHERE slug=?", slug) or (self.workspace / slug).exists():
            slug, n = f"{base}-{n}", n + 1
        workdir = self.workspace / slug
        workdir.mkdir(parents=True)
        (workdir / picked.filename).write_bytes(rom)
        size = f"{info['size'] // (1024 * 1024)} MiB" if info["size"] >= 1 << 20 else f"{info['size'] // 1024} KiB"
        log(f"{detect.PLATFORMS[picked.platform]}: {info['title']} ({info['game_code'] or 'no game code'}, "
            f"{info['region']}), {size}, looks built with {info['compiler_guess']}")
        res = split.split_rom(workdir, picked.filename, log=log)

        toolchain = toolchain or DEFAULT_TOOLCHAIN.get(res.compiler, DEFAULT_TOOLCHAIN[info["compiler_guess"]])
        tc = toolchains.get(toolchain)
        default_flags = tc.flags
        if picked.platform == "psx":
            default_flags = [f for f in tc.flags if not f.startswith("-G")] + [f"-G{8 if res.gp else 0}"]
        cfg = {
            "kind": "rom", "platform": picked.platform, "big_endian": picked.big_endian, "gp": res.gp,
            "rom": {"file": picked.filename, **info},
            "segments": [vars(s) for s in res.segments],
            "compiler": {"toolchain": toolchain, "flags": flags or default_flags, "timeout": 20},
            "legal": {"basis": "personal-copy", "license": "personal use only",
                      "statement": "imported by the owner of this copy of the game, who confirmed it was legally obtained",
                      "confirmed_at": time.time()},
            "context": {"file": None},
        }
        symbols = SymbolTable()
        for name, addr in res.named.items():
            symbols.add(Symbol(name, addr, 0, "func"), reindex=False)
        for fn in res.functions:
            symbols.add(Symbol(fn.name, fn.vram, fn.size, "func"), reindex=False)
        rom_bytes, segs = rom, res.segments

        def read(addr, n):
            for seg in segs:
                if seg.contains(addr, n):
                    at = seg.rom_start + addr - seg.vram
                    return rom_bytes[at:at + n]
            return None

        data_rows = []
        for d in res.data:
            if d.name in symbols.by_name:
                continue
            symbols.add(Symbol(d.name, d.vram, d.size, d.kind), reindex=False)
            data_rows.append((d.name, d.vram, d.size, d.kind, _preview(read(d.vram, min(d.size, 48)), d.kind) if d.kind != "bss" else ""))
        symbols._reindex()

        now = time.time()
        with self.store.tx() as db:
            pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                             (slug, info["title"], f"{info['game_code']} · {info['region']} · {len(res.functions)} functions",
                              "mips", int(picked.big_endian), str(workdir), json.dumps(cfg))).lastrowid
            for fn in res.functions:
                target = match.target_from_split(fn, symbols.resolve, picked.big_endian, res.gp)
                db.execute(
                    "INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, fn.name, fn.size, len(fn.lines), target.code, json.dumps(target.to_json()),
                     self._asm_source(fn), None, fn.segment, fn.vram, fn.rom))
            for s in symbols.by_name.values():
                preview = next((r[4] for r in data_rows if r[0] == s.name), None)
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview) VALUES(?,?,?,?,?,?)",
                           (pid, s.name, s.address, s.size, s.kind, preview))
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (pid, 1, BASE_HEADER.format(title=info["title"]), None,
                                                  "basic types", now, ""))
            self._event(db, pid, None, "import", f"{len(res.functions)} functions from {info['title']}")
        log(f"ready: {len(res.functions)} functions to decompile")
        return slug

    @staticmethod
    def _asm_source(fn: split.RomFunction) -> str:
        out = [f"glabel {fn.name}"]
        for ln in fn.lines:
            if ln.vram in fn.labels and ln.vram != fn.vram:
                out.append(f".L{ln.vram:08X}:")
            out.append(f"/* {ln.rom:X} {ln.vram:08X} {ln.word:08X} */  {ln.mnemonic:<10} {ln.operands}".rstrip())
        out.append(f"endlabel {fn.name}")
        return "\n".join(out)

    @staticmethod
    def _parse_asm(name: str, text: str, segment: str) -> split.RomFunction:
        lines, labels = [], set()
        for raw in text.splitlines():
            m = split._INSN.match(raw)
            if m:
                rom, vram, word, mnem, ops = m.groups()
                lines.append(split.AsmLine(int(rom, 16), int(vram, 16), int(word, 16), mnem, ops))
                continue
            m = split._LABEL.match(raw)
            if m:
                labels.add(int(m.group(1).lstrip(".L"), 16))
        return split.RomFunction(name, lines[0].vram, lines[0].rom, segment, lines, Path(), labels)

    # ------------------------------------------------------------------ drafts

    def _seed_for(self, fn, proj) -> str | None:
        """m2c draft, made the first time someone opens the function (imports stay fast)."""
        if fn["seed"] is not None or not seeds.available():
            return fn["seed"]
        tc = self.cfg(proj)["compiler"]["toolchain"]
        asm = self._asm_text(fn, proj)
        data_asm = None
        if "jtbl_" in fn["asm"]:
            data_asm = "\n".join(self._jtbl_asm(proj, fn))
        ctx = self._context(proj["id"], fn["unit"])
        draft = seeds.generate_from_asm(asm, fn["name"], tc, ctx["header"] if ctx else "", data_asm)
        text = f"/* machine-generated by m2c: a starting point, not a match */\n{draft}" if draft else ""
        with self.store.tx() as db:
            db.execute("UPDATE functions SET seed=? WHERE id=?", (text, fn["id"]))
        return text

    def _jtbl_asm(self, proj, fn) -> list[str]:
        out = [".section .rodata"]
        read = self.reader(proj)
        syms = self.symbols(proj["id"])
        for m in sorted(set(re.findall(r"jtbl_([0-9A-F]{8})", fn["asm"]))):
            addr = int(m, 16)
            sym = syms.by_addr.get(addr)
            size = sym.size if sym and sym.size else 0
            data = read(addr, size) if size else None
            if not data:
                continue
            out.append(f"dlabel {syms.label(addr)}")
            fmt = ">I" if self.cfg(proj).get("big_endian", True) else "<I"
            for (w,) in struct.iter_unpack(fmt, data[:len(data) // 4 * 4]):
                out.append(f".word .L{w:08X}" if fn["vram"] <= w < fn["vram"] + fn["size"] else f".word 0x{w:08X}")
        return out

    def _brief(self, fn, agent, header):
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if self.is_rom(proj) and fn["status"] != "matched" and not fn["best_source"] and fn["seed"] is None:
            with self._seed_lock:
                self._seed_for(self._function(fn["name"], proj["slug"]), proj)
            fn = self._function(fn["name"], proj["slug"])
        return super()._brief(fn, agent, header)

    # ------------------------------------------------------------------ cross references

    def xrefs(self, proj) -> dict:
        """calls / called_by / uses / used_by, all keyed by address."""
        if proj["id"] in self._xrefs:
            return self._xrefs[proj["id"]]
        funcs = self.store.all("SELECT name, vram, size, relocs FROM functions WHERE project_id=?", proj["id"])
        starts = {f["vram"] for f in funcs}
        calls, called_by, uses, used_by = {}, {}, {}, {}
        for f in funcs:
            for off, rtype, addr in json.loads(f["relocs"]):
                if f["vram"] <= addr < f["vram"] + f["size"]:
                    continue
                if addr in starts:
                    calls.setdefault(f["vram"], set()).add(addr)
                    called_by.setdefault(addr, set()).add(f["vram"])
                elif rtype != match.R_MIPS_26:
                    uses.setdefault(f["vram"], set()).add(addr)
                    used_by.setdefault(addr, set()).add(f["vram"])
        self._xrefs[proj["id"]] = {"calls": calls, "called_by": called_by, "uses": uses, "used_by": used_by}
        return self._xrefs[proj["id"]]

    def _extra_brief(self, fn, proj) -> list[str]:
        if not self.is_rom(proj):
            return []
        syms = self.symbols(proj["id"])
        x = self.xrefs(proj)
        lines = ["", "### Where it lives",
                 f"address 0x{fn['vram']:08X}, ROM offset 0x{fn['rom_offset']:X}, segment {fn['unit']}"]
        calls = sorted(x["calls"].get(fn["vram"], ()))
        callers = sorted(x["called_by"].get(fn["vram"], ()))
        if calls:
            lines.append("calls: " + ", ".join(syms.label(a) for a in calls[:30]))
        if callers:
            lines.append("called by: " + ", ".join(syms.label(a) for a in callers[:30]))
        used = sorted(x["uses"].get(fn["vram"], ()))
        if used:
            lines += ["", "### Data it uses (declare what you need as extern in your source, or in context.h)"]
            prev = {r["name"]: r for r in self.store.all("SELECT name, kind, size, preview FROM symbols WHERE project_id=?", proj["id"])}
            for a in used[:40]:
                name, off = syms.name_at(a)
                r = prev.get(name)
                desc = f"{r['kind']}, {r['size']} bytes" if r else "unknown size"
                if r and r["preview"]:
                    desc += f": {r['preview']}"
                lines.append(f"- {syms.label(a)} (0x{a:08X}; {desc})")
        lines += ["", "Names: use the names shown here (or rename_symbol to give an address a real name). "
                      "Your code is linked at this function's address and must reproduce the ROM bytes exactly."]
        return lines

    # ------------------------------------------------------------------ agent tools for ROMs

    def list_functions(self, project: str | None, status: str = "open", limit: int = 40) -> str:
        proj = self._project(project)
        where = "" if status == "all" else " AND f.status=?"
        args = [proj["id"]] + ([] if status == "all" else [status])
        rows = self.store.all(
            f"SELECT f.name, f.vram, f.n_insns, f.status, f.best_score, a.name AS claimed_by FROM functions f "
            f"LEFT JOIN claims c ON c.function_id=f.id LEFT JOIN agents a ON a.id=c.agent_id "
            f"WHERE f.project_id=?{where} ORDER BY f.vram LIMIT ?", *args, limit)
        if not rows:
            return f"No {status} functions."
        return "\n".join(f"- {r['name']} @0x{r['vram']:08X}: {r['n_insns']} insns, {r['status']}"
                         + (f", best score {r['best_score']}" if r["best_score"] is not None else "")
                         + (f", claimed by {r['claimed_by']}" if r["claimed_by"] else "") for r in rows)

    def read_data(self, project: str | None, what: str, length: int = 64) -> str:
        proj = self._project(project)
        syms = self.symbols(proj["id"])
        m = _ADDRESS.match(what.strip())
        addr = int(m.group(1), 16) if m else syms.resolve(what.strip())
        if addr is None:
            raise NetworkError(f"`{what}` is not an address or a known symbol")
        length = max(4, min(int(length), 512))
        data = self.reader(proj)(addr, length)
        head = f"{syms.label(addr)} at 0x{addr:08X}"
        if data is None:
            sym = syms.by_addr.get(addr)
            if sym and sym.kind == "bss":
                return f"{head}: uninitialized data (.bss), {sym.size} bytes; it starts as zeros at boot."
            return f"{head}: not in a loaded part of the ROM (maybe .bss, or a segment that isn't split yet)."
        out = [head]
        for i in range(0, len(data), 16):
            chunk = data[i:i + 16]
            text = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
            out.append(f"  {addr + i:08X}: {chunk.hex(' '):<47}  {text}")
        e = ">" if self.cfg(proj).get("big_endian", True) else "<"
        words = [w for (w,) in struct.iter_unpack(e + "I", data[:len(data) // 4 * 4])]
        ptrs = [f"+0x{4 * i:X}: {syms.label(w)}" for i, w in enumerate(words) if 0x80000400 <= w < 0x80800000]
        if ptrs:
            out.append("words that look like addresses: " + ", ".join(ptrs[:16]))
        floats = [struct.unpack(e + "f", struct.pack(e + "I", w))[0] for w in words[:8]]
        if any(1e-6 < abs(f) < 1e7 for f in floats):
            out.append("as f32: " + ", ".join(f"{f:g}" for f in floats))
        return "\n".join(out)

    def split_function(self, agent: Agent, project: str | None, function: str, at: str, new_name: str = "") -> str:
        link = self._link(project)
        if link is not None:
            name = self._function(function, project)["name"]
            text = link.call("/collab/split", agent, function=name, at=at, new_name=new_name)
            if text.startswith("✅"):
                link.sync()
            return text
        fn = self._function(function, project)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if not self.is_rom(proj):
            raise NetworkError("split_function is for ROM projects")
        if fn["status"] == "matched":
            raise NetworkError(f"{fn['name']} is matched; it can't be split")
        m = _ADDRESS.match(str(at).strip())
        if not m:
            raise NetworkError("`at` must be an address like 0x80001234")
        addr = int(m.group(1), 16)
        new_name = (new_name or f"func_{addr:08X}").strip()
        syms = self.symbols(proj["id"])
        if not IDENT.match(new_name) or new_name in syms.by_name:
            raise NetworkError(f"`{new_name}` is not a free C identifier")
        rf = self._parse_asm(fn["name"], fn["asm"], fn["unit"])
        try:
            head, tail = split.split_function(rf, addr, new_name)
        except ValueError as e:
            raise NetworkError(str(e)) from None
        syms.add(Symbol(new_name, addr, tail.size, "func"))
        old = syms.by_name.get(fn["name"])
        if old is not None:
            old.size = head.size
        th, tt = self._make_target(proj, head, syms), self._make_target(proj, tail, syms)
        with self.store.tx() as db:
            db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, best_score=NULL "
                       "WHERE id=?", (head.size, len(head.lines), th.code, json.dumps(th.to_json()),
                                      self._asm_source(head), fn["id"]))
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (proj["id"], new_name, tail.size, len(tail.lines), tt.code,
                                                        json.dumps(tt.to_json()), self._asm_source(tail), None,
                                                        fn["unit"], addr, tail.rom))
            db.execute("UPDATE symbols SET size=? WHERE project_id=? AND name=?", (head.size, proj["id"], fn["name"]))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                       (proj["id"], new_name, addr, tail.size, "func"))
            self._event(db, proj["id"], agent.id, "split", f"{fn['name']} / {new_name}")
        self._xrefs.pop(proj["id"], None)
        return (f"✅ split: {fn['name']} is now {len(head.lines)} instructions; {new_name} at 0x{addr:08X} has "
                f"{len(tail.lines)}. Continue with {fn['name']} (get_function shows its new assembly).")

    def rename_symbol(self, agent: Agent, project: str | None, old: str, new: str, message: str = "") -> str:
        proj = self._project(project)
        if not self.is_rom(proj):
            return super().rename_symbol(agent, project, old, new, message)
        link = self._link(project)
        if link is not None:
            text = link.call("/collab/rename", agent, old=old, new=new, message=message)
            if text.startswith("✅"):
                link.sync()
            return text
        old, new = old.strip(), new.strip()
        if not (IDENT.match(old) and IDENT.match(new)):
            raise NetworkError("old and new must be C identifiers")
        syms = self.symbols(proj["id"])
        addr = syms.resolve(old)
        if addr is None:
            raise NetworkError(f"`{old}` is not a symbol of this ROM: use a name from the assembly (func_/D_...) "
                               f"or one given earlier")
        taken = syms.resolve(new)
        if taken is not None and taken != addr:
            raise NetworkError(f"`{new}` already names 0x{taken:08X}")
        word = re.compile(rf"\b{re.escape(old)}\b")
        trial = SymbolTable([Symbol(s.name, s.address, s.size, s.kind) for s in syms.by_name.values()])
        if old in trial.by_name:
            trial.rename(old, new)
        else:
            fnrow = self.store.one("SELECT size FROM functions WHERE project_id=? AND vram=?", proj["id"], addr)
            current, _ = syms.name_at(addr)
            base = syms.by_name.get(current) if current != f"D_{addr:08X}" else None
            trial.add(Symbol(new, addr, fnrow["size"] if fnrow else (base.size if base and base.address == addr else 0),
                             "func" if fnrow else (base.kind if base else "data")))
        units = self._unit_contexts(proj["id"])
        current_ctx = {u: self._context(proj["id"], u) for u in ({""} | units)}
        changed = {u: word.sub(new, c["header"]) for u, c in current_ctx.items() if word.search(c["header"])}
        matched = self.store.all("SELECT * FROM functions WHERE project_id=? AND status='matched'", proj["id"])
        rewritten = {r["id"]: word.sub(new, r["matched_source"]) for r in matched}
        renamed_rows = [dict(r) | {"name": new if r["name"] == old else r["name"]} for r in matched]
        header_for = lambda fn: changed.get(self._effective_unit(fn, units),
                                            current_ctx[self._effective_unit(fn, units)]["header"])
        self._sym_override[proj["id"]] = trial
        try:
            broken = self.regression_test(proj, None, sources=rewritten, only=renamed_rows, header_for=header_for)
        finally:
            self._sym_override.pop(proj["id"], None)
        if broken:
            listing = "\n".join(f"- {n}: {why}" for n, why in broken[:10])
            return f"❌ REJECTED: renaming breaks {len(broken)} matched function(s).\n{listing}"
        msg = message.strip() or f"rename {old} -> {new}"
        with self.store.tx() as db:
            if db.execute("UPDATE symbols SET name=? WHERE project_id=? AND name=?", (new, proj["id"], old)).rowcount == 0:
                s = trial.by_name[new]
                db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                           (proj["id"], new, s.address, s.size, s.kind))
            db.execute("UPDATE functions SET name=? WHERE project_id=? AND (name=? OR vram=?)", (new, proj["id"], old, addr))
            for u, header in changed.items():
                version = self._next_version(db, proj["id"])
                db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                           "VALUES(?,?,?,?,?,?,?)", (proj["id"], version, header, agent.id, msg, time.time(), u))
            for fid, src in rewritten.items():
                db.execute("UPDATE functions SET matched_source=?, best_source=? WHERE id=?", (src, src, fid))
            for r in db.execute("SELECT id, best_source FROM functions WHERE project_id=? AND status='open' "
                                "AND best_source IS NOT NULL", (proj["id"],)).fetchall():
                db.execute("UPDATE functions SET best_source=? WHERE id=?", (word.sub(new, r["best_source"]), r["id"]))
            self._event(db, proj["id"], agent.id, "rename", f"{old} → {new}")
        self._syms.pop(proj["id"], None)
        return f"✅ 0x{addr:08X} is now `{new}` (was `{old}`); {len(rewritten)} matched sources updated, all still match."

    # ------------------------------------------------------------------ rooms (see collab.py)

    def link_project(self, slug: str, info: dict, client=None, start: bool = True):
        from .collab import Link

        proj = self._project(slug)
        cfg = self.cfg(proj)
        cfg["link"] = info
        with self.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        link = Link(self, slug, info["url"], info["room"], info["token"], info["member"], client=client)
        self.links[slug] = link
        if start:
            link.run()
        return link

    def restore_links(self) -> None:
        for p in self.store.all("SELECT slug, config FROM projects"):
            info = json.loads(p["config"]).get("link")
            if info and p["slug"] not in self.links:
                self.link_project(p["slug"], info)

    def unlink(self, slug: str) -> None:
        link = self.links.pop(slug, None)
        if link is not None:
            link.stop()
            try:
                link._req("POST", "/collab/leave", {})
            except NetworkError:
                pass
        proj = self._project(slug)
        cfg = self.cfg(proj)
        cfg.pop("link", None)
        with self.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))

    def _link(self, project: str | None):
        if not self.links:
            return None
        try:
            return self.links.get(self._project(project)["slug"])
        except NetworkError:
            return None

    def claim(self, agent: Agent, project: str | None = None, function: str | None = None, strategy: str = "easiest") -> str:
        link = self._link(project)
        if link is None:
            return super().claim(agent, project, function, strategy)
        if function:
            function = self._function(function, project)["name"]
        name = link.claim(agent, function, strategy)
        if name is None:
            return "Nothing left to claim in this room — every open function is matched or someone is on it. 🎉"
        link.sync()
        fn = self.claim_row(agent, project, name, strategy)
        return self._brief(fn, agent, header="CLAIMED")

    def attempt(self, agent: Agent, function: str, source: str, project: str | None = None) -> dict:
        link = self._link(project)
        before = self._function(function, project)["best_score"] if link else None
        res = super().attempt(agent, function, source, project)
        if link is None or res["score"] is None:
            return res
        name = self._function(function, project)["name"]
        if res["matched"]:
            verdict = link.attempt(agent, name, source)
            if not verdict.get("matched"):
                res["text"] += ("\n⚠️ The room's host could not confirm this match on their copy (score "
                                f"{verdict.get('score')}). Check that both of you use the same compiler settings.")
            else:
                res["text"] += "\nShared with the room: the host confirmed the match."
        elif before is None or res["score"] < before:
            threading.Thread(target=lambda: self._report(link, agent, name, source), daemon=True).start()
        return res

    @staticmethod
    def _report(link, agent, name, source) -> None:
        try:
            link.attempt(agent, name, source)
        except NetworkError:
            pass

    def release(self, agent: Agent, function: str, notes: str, project: str | None = None) -> str:
        link = self._link(project)
        if link is not None:
            link.call("/collab/release", agent, function=self._function(function, project)["name"], notes=notes)
        return super().release(agent, function, notes, project)

    def edit_context(self, agent: Agent, project: str | None, old: str, new: str, message: str,
                     function: str | None = None) -> str:
        link = self._link(project)
        if link is None:
            return super().edit_context(agent, project, old, new, message, function)
        text = link.call("/collab/context", agent, old=old, new=new, message=message, function=function)
        if text.startswith("✅"):
            link.sync()
        return text

    def add_note(self, agent: Agent, project: str | None, text: str, function: str | None = None) -> str:
        link = self._link(project)
        if link is None:
            return super().add_note(agent, project, text, function)
        name = self._function(function, project)["name"] if function else None
        return link.call("/collab/note", agent, text=text, function=name) or "Noted, and shared with the room."

    def _remote_agent(self, name: str) -> int:
        label = "@" + name
        if label in self._remote_ids:
            return self._remote_ids[label]
        row = self.store.one("SELECT id FROM agents WHERE name=?", label)
        if row is None:
            with self.store.tx() as db:
                aid = db.execute("INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin) VALUES(?,?,?,?,1,?)",
                                 (label, "room", "remote:" + secrets.token_hex(16), time.time(), "remote")).lastrowid
        else:
            aid = row["id"]
        self._remote_ids[label] = aid
        return aid

    def remote_event(self, project_id: int, kind: str, detail: str, agent_name: str, created_at: float) -> None:
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            db.execute("INSERT INTO events(project_id,agent_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                       (project_id, aid, kind, detail, created_at))

    def apply_remote_match(self, slug: str, vram: int, source: str, agent_name: str) -> None:
        proj = self._project(slug)
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            row = db.execute("SELECT id, status FROM functions WHERE project_id=? AND vram=?", (proj["id"], vram)).fetchone()
            if row is None or row["status"] == "matched":
                return
            db.execute("UPDATE functions SET status='matched', matched_source=?, matched_agent_id=?, matched_at=?, "
                       "best_score=0, best_source=? WHERE id=?", (source, aid, time.time(), source, row["id"]))
            db.execute("DELETE FROM claims WHERE function_id=?", (row["id"],))

    def apply_remote_best(self, slug: str, vram: int, score: int, source: str) -> None:
        proj = self._project(slug)
        with self.store.tx() as db:
            db.execute("UPDATE functions SET best_score=?, best_source=? WHERE project_id=? AND vram=? AND status='open' "
                       "AND (best_score IS NULL OR best_score > ?)", (score, source, proj["id"], vram, score))

    def apply_remote_note(self, slug: str, host_id: int, vram: int | None, text: str, agent_name: str, created_at: float) -> None:
        proj = self._project(slug)
        fid = None
        if vram is not None:
            row = self.store.one("SELECT id FROM functions WHERE project_id=? AND vram=?", proj["id"], vram)
            fid = row["id"] if row else None
        if self.store.one("SELECT 1 FROM notes WHERE project_id=? AND text=? AND function_id IS ?", proj["id"], text, fid):
            return
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                       (proj["id"], fid, aid, text, created_at))

    def apply_remote_context(self, slug: str, header: str, host_version: int) -> None:
        proj = self._project(slug)
        if self._context(proj["id"])["header"] == header:
            return
        with self.store.tx() as db:
            version = self._next_version(db, proj["id"])
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (proj["id"], version, header, None, f"from the room (host v{host_version})",
                                                  time.time(), ""))

    def apply_remote_layout(self, slug: str, functions: list[dict], symbols: list[dict]) -> None:
        """Mirror the room's splits and names. The host already regression-tested them on the same ROM."""
        proj = self._project(slug)
        pid = proj["id"]
        local = {r["vram"]: r for r in self.store.all("SELECT * FROM functions WHERE project_id=?", pid)}
        for f in sorted(functions, key=lambda f: f["vram"]):
            if f["vram"] in local:
                continue
            owner = next((r for r in local.values() if r["vram"] < f["vram"] < r["vram"] + r["size"]), None)
            if owner is None or owner["status"] == "matched":
                continue
            rf = self._parse_asm(owner["name"], owner["asm"], owner["unit"])
            try:
                head, tail = split.split_function(rf, f["vram"], f["name"])
            except ValueError:
                continue
            syms = self.symbols(pid)
            syms.add(Symbol(f["name"], f["vram"], tail.size, "func"))
            th, tt = self._make_target(proj, head, syms), self._make_target(proj, tail, syms)
            with self.store.tx() as db:
                db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL WHERE id=?",
                           (head.size, len(head.lines), th.code, json.dumps(th.to_json()), self._asm_source(head), owner["id"]))
                db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (pid, f["name"], tail.size, len(tail.lines), tt.code,
                                                            json.dumps(tt.to_json()), self._asm_source(tail), None,
                                                            owner["unit"], f["vram"], tail.rom))
                db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                           (pid, f["name"], f["vram"], tail.size, "func"))
            local = {r["vram"]: r for r in self.store.all("SELECT * FROM functions WHERE project_id=?", pid)}
        self._xrefs.pop(pid, None)
        syms = self.symbols(pid)
        for sym in symbols:
            current, off = syms.name_at(sym["address"])
            if off == 0 and current == sym["name"]:
                continue
            old = current if off == 0 else f"D_{sym['address']:08X}"
            word = re.compile(rf"\b{re.escape(old)}\b")
            with self.store.tx() as db:
                db.execute("DELETE FROM symbols WHERE project_id=? AND (name=? OR name=?)", (pid, old, sym["name"]))
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                           (pid, sym["name"], sym["address"], sym["size"], sym["kind"]))
                db.execute("UPDATE functions SET name=? WHERE project_id=? AND vram=?", (sym["name"], pid, sym["address"]))
                for r in db.execute("SELECT id, matched_source, best_source FROM functions WHERE project_id=? AND "
                                    "(matched_source IS NOT NULL OR best_source IS NOT NULL)", (pid,)).fetchall():
                    db.execute("UPDATE functions SET matched_source=?, best_source=? WHERE id=?",
                               (word.sub(sym["name"], r["matched_source"]) if r["matched_source"] else None,
                                word.sub(sym["name"], r["best_source"]) if r["best_source"] else None, r["id"]))
            self._syms.pop(pid, None)
            syms = self.symbols(pid)

    # ------------------------------------------------------------------ export

    def export_rom(self, slug: str) -> Path:
        """Write the decompiled sources into the project folder, for the user to keep."""
        proj = self._project(slug)
        out = Path(proj["root"]) / "decompiled"
        src = out / "src"
        src.mkdir(parents=True, exist_ok=True)
        (out / "include").mkdir(exist_ok=True)
        (out / "include" / "context.h").write_text(self._context(proj["id"])["header"])
        rows = self.store.all("SELECT f.name, f.vram, f.matched_source, f.status, f.best_source, f.best_score, a.name AS agent "
                              "FROM functions f LEFT JOIN agents a ON a.id=f.matched_agent_id WHERE f.project_id=? "
                              "ORDER BY f.vram", proj["id"])
        body = ['/* Decompiled with decomp.wtf. For your personal use with your own copy of the game. */',
                '#include "context.h"', ""]
        for r in rows:
            if r["status"] == "matched":
                body += [f"/* 0x{r['vram']:08X}: matched by {r['agent']} */", r["matched_source"].strip(), ""]
            else:
                body += [f"/* 0x{r['vram']:08X} {self.rename_tokens(proj, r['name'])}: not matched yet"
                         + (f" (best score {r['best_score']})" if r["best_score"] is not None else "") + " */", ""]
        (src / "game.c").write_text("\n".join(body))
        return out

    # ------------------------------------------------------------------ for the GUI

    def overview(self, slug: str) -> dict:
        proj = self._project(slug)
        cfg = self.cfg(proj)
        agg = self.store.one(
            "SELECT COUNT(*) AS total, SUM(status='matched') AS matched, SUM(size) AS bytes, "
            "SUM(CASE WHEN status='matched' THEN size ELSE 0 END) AS matched_bytes, SUM(attempts) AS attempts "
            "FROM functions WHERE project_id=?", proj["id"])
        tc = toolchains.get(cfg["compiler"]["toolchain"])
        return {
            "slug": proj["slug"], "title": proj["name"], "description": proj["description"],
            "platform": cfg.get("platform", "n64"), "platform_name": detect.PLATFORMS.get(cfg.get("platform", "n64"), ""),
            "compilers": PLATFORM_COMPILERS.get(cfg.get("platform", "n64"), []),
            "rom": {k: cfg["rom"].get(k) for k in ("title", "game_code", "region", "size", "sha1", "byte_order", "compiler_guess")},
            "compiler": {"toolchain": tc.id, "name": SHORT_NAMES.get(tc.id, tc.name), "flags": cfg["compiler"]["flags"],
                         "installed": toolchains.installed(tc.id) is not None, "proprietary": tc.proprietary,
                         "supported": tc.supported},
            "functions": agg["total"] or 0, "matched": agg["matched"] or 0, "bytes": agg["bytes"] or 0,
            "matched_bytes": agg["matched_bytes"] or 0, "attempts": agg["attempts"] or 0,
            "context_version": self._context(proj["id"])["version"],
            "folder": proj["root"],
            "room": self._room_info(proj["slug"]),
        }

    def _room_info(self, slug: str) -> dict | None:
        if slug in self.links:
            link = self.links[slug]
            return {"role": "guest", "host": link.url, "member": link.member, "error": link.error,
                    "members": link.members, "connected": time.time() - link.last_ok < 15}
        if slug in self.rooms:
            return {"role": "host", "members": self.rooms[slug].members()}
        return None

    def function_map(self, slug: str) -> list[dict]:
        proj = self._project(slug)
        rows = self.store.all(
            "SELECT f.name, f.vram, f.size, f.n_insns, f.status, f.best_score, f.attempts, a.name AS agent "
            "FROM functions f LEFT JOIN claims c ON c.function_id=f.id LEFT JOIN agents a ON a.id=c.agent_id "
            "WHERE f.project_id=? ORDER BY f.vram", proj["id"])
        remote = {c["vram"]: "@" + c["agent"] for c in (self.links[slug].claims if slug in self.links else [])}
        return [{"name": r["name"], "vram": r["vram"], "size": r["size"], "insns": r["n_insns"],
                 "status": "matched" if r["status"] == "matched" else "working" if (r["agent"] or r["vram"] in remote) else "open",
                 "best": r["best_score"], "attempts": r["attempts"], "agent": r["agent"] or remote.get(r["vram"])} for r in rows]

    def function_detail(self, slug: str, name: str) -> dict:
        fn = self._function(name, slug)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        attempts = self.store.all(
            "SELECT t.score, t.created_at, a.name AS agent FROM attempts t LEFT JOIN agents a ON a.id=t.agent_id "
            "WHERE t.function_id=? ORDER BY t.id", fn["id"])
        latest = self.store.one("SELECT source, score FROM attempts WHERE function_id=? AND source IS NOT NULL "
                                "ORDER BY id DESC LIMIT 1", fn["id"])
        notes = self.store.all("SELECT n.text, n.created_at, a.name AS agent FROM notes n LEFT JOIN agents a "
                               "ON a.id=n.agent_id WHERE n.function_id=? ORDER BY n.id DESC LIMIT 10", fn["id"])
        x = self.xrefs(proj)
        syms = self.symbols(proj["id"])
        if not (fn["matched_source"] or fn["best_source"] or latest) and fn["seed"] is None:
            with self._seed_lock:
                self._seed_for(fn, proj)
            fn = self._function(fn["name"], slug)
        return {
            "name": fn["name"], "vram": fn["vram"], "rom_offset": fn["rom_offset"], "size": fn["size"],
            "insns": fn["n_insns"], "status": fn["status"], "best": fn["best_score"],
            "asm": self._asm_text(fn, proj),
            "source": fn["matched_source"] or fn["best_source"] or (latest["source"] if latest else None) or fn["seed"],
            "source_kind": "matched" if fn["matched_source"] else "best" if fn["best_source"] else
                           "latest" if latest else "draft" if fn["seed"] else None,
            "scores": [{"score": a["score"], "t": a["created_at"], "agent": a["agent"]} for a in attempts],
            "notes": [dict(n) for n in notes],
            "calls": [syms.label(a) for a in sorted(x["calls"].get(fn["vram"], ()))],
            "called_by": [syms.label(a) for a in sorted(x["called_by"].get(fn["vram"], ()))],
        }

    def live(self, slug: str, since_event: int = 0, since_attempt: int = 0) -> dict:
        proj = self._project(slug)
        with self.store.tx() as db:
            self._expire_claims(db)
        events = self.store.all(
            "SELECT e.id, e.kind, e.detail, e.created_at, a.name AS agent FROM events e LEFT JOIN agents a "
            "ON a.id=e.agent_id WHERE e.project_id=? AND e.id>? ORDER BY e.id DESC LIMIT 60", proj["id"], since_event)
        attempts = self.store.all(
            "SELECT t.id, t.score, t.compiled, t.created_at, f.name AS function, f.n_insns, a.name AS agent "
            "FROM attempts t JOIN functions f ON f.id=t.function_id LEFT JOIN agents a ON a.id=t.agent_id "
            "WHERE f.project_id=? AND t.id>? ORDER BY t.id DESC LIMIT 80", proj["id"], since_attempt)
        claims = self.store.all(
            "SELECT f.name, f.n_insns, f.best_score, f.attempts, a.name AS agent, c.claimed_at FROM claims c "
            "JOIN functions f ON f.id=c.function_id JOIN agents a ON a.id=c.agent_id WHERE f.project_id=?", proj["id"])
        claims = [dict(c) for c in claims]
        if slug in self.links:
            for c in self.links[slug].claims:
                row = self.store.one("SELECT name, n_insns, best_score, attempts FROM functions WHERE project_id=? AND vram=?",
                                     proj["id"], c["vram"])
                if row:
                    claims.append(dict(row) | {"agent": "@" + c["agent"], "claimed_at": None})
        return {"events": [dict(e) for e in reversed(events)], "attempts": [dict(t) for t in reversed(attempts)],
                "claims": claims, "overview": self.overview(slug)}

    def set_compiler(self, slug: str, toolchain: str, flags: list[str] | None = None) -> None:
        proj = self._project(slug)
        tc = toolchains.get(toolchain)
        cfg = self.cfg(proj)
        cfg["compiler"] = {"toolchain": tc.id, "flags": flags or tc.flags, "timeout": 20}
        with self.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
            db.execute("UPDATE functions SET seed=NULL WHERE project_id=? AND status='open'", (proj["id"],))
            self._event(db, proj["id"], None, "compiler", f"{tc.name} {' '.join(cfg['compiler']['flags'])}")

    def local_agent(self, name: str) -> tuple[Agent, str]:
        """An agent identity for one runner on this machine (created on first use)."""
        row = self.store.one("SELECT id FROM agents WHERE name=?", name)
        if row is not None:
            token = "dn_" + secrets.token_urlsafe(24)
            with self.store.tx() as db:
                db.execute("UPDATE agents SET token_hash=? WHERE id=?", (_hash_token(token), row["id"]))
        else:
            token = self.register_agent(name, "you", trusted=True, origin="local")
        return self.authenticate(token), token
