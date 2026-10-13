"""Matching C against code taken straight from a ROM.

The target is final, linked machine code: every address is already filled in. A candidate is a
freshly compiled object whose references are relocations against names. To compare them fairly we
*link* the candidate at the target's address:

- names (func_80001234, D_80012340, or anything renamed in the symbol table) resolve to addresses;
- jumps inside the function resolve against the function's own address;
- data the candidate defines itself (string literals, float constants, jump tables, statics) is
  placed where the ROM keeps the data this instruction refers to, and its contents are checked
  against the ROM bytes there (relocated words, like jump table entries, included).

A match means the linked candidate is byte-for-byte the code in the ROM. For the diff the agent
reads, both sides are shown with the same names (the symbol table's names for those addresses).
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from io import BytesIO
from typing import Callable

from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection

from .. import scoring
from ..elfobj import _MASKS, FunctionObj, Reloc, _canonical, _section_target, _symbol_at
from .split import RomFunction
from .symbols import SymbolTable

R_MIPS_32, R_MIPS_26, R_MIPS_HI16, R_MIPS_LO16, R_MIPS_GPREL16 = 2, 4, 5, 6, 7
_REF = re.compile(r"%(hi|lo|gp_rel)\(\s*([A-Za-z_.$][\w.$]*)\s*(?:([+-])\s*(0x[0-9A-Fa-f]+|\d+))?\s*\)")
_KIND = {"hi": 5, "lo": 6, "gp_rel": 7}
_JUMPS = {"jal", "j"}

ReadFn = Callable[[int, int], "bytes | None"]


@dataclass
class RomTarget:
    name: str
    vram: int
    code: bytes
    relocs: list[tuple[int, int, int]]  # (offset in function, reloc type, absolute address)
    big_endian: bool = True
    gp: int = 0  # $gp, for $gp-relative references (PlayStation small data)

    @property
    def order(self) -> str:
        return "big" if self.big_endian else "little"

    def to_json(self) -> list:
        return [list(r) for r in self.relocs]

    @staticmethod
    def relocs_from_json(v: list) -> list[tuple[int, int, int]]:
        return [tuple(r) for r in v]


@dataclass
class LinkResult:
    candidate: FunctionObj | None  # canonical form, for the diff
    exact: bool
    problems: list[str] = field(default_factory=list)


def encode(rtype: int, residual: int) -> int:
    """The relocation field an assembler writes for `symbol + residual` (REL, MIPS)."""
    if rtype == R_MIPS_26:
        return (residual >> 2) & 0x03FFFFFF
    if rtype == R_MIPS_HI16:
        return ((residual + 0x8000) >> 16) & 0xFFFF
    if rtype == R_MIPS_LO16:
        return residual & 0xFFFF
    return residual & 0xFFFFFFFF


def _patch(word: int, rtype: int, address: int, gp: int = 0) -> int:
    if rtype == R_MIPS_GPREL16:
        return (word & ~0xFFFF) | ((address - gp) & 0xFFFF)
    if rtype == R_MIPS_26:
        return (word & ~0x03FFFFFF) | ((address >> 2) & 0x03FFFFFF)
    if rtype == R_MIPS_HI16:
        return (word & ~0xFFFF) | (((address + 0x8000) >> 16) & 0xFFFF)
    if rtype == R_MIPS_LO16:
        return (word & ~0xFFFF) | (address & 0xFFFF)
    if rtype == R_MIPS_32:
        return address & 0xFFFFFFFF
    raise ValueError(rtype)


# ----------------------------------------------------------------------------------- target

def target_from_split(fn: RomFunction, resolve: Callable[[str], int | None], big_endian: bool = True,
                      gp: int = 0) -> RomTarget:
    """The function's bytes from the ROM plus the address behind every annotated reference."""
    code = b"".join(ln.word.to_bytes(4, "big" if big_endian else "little") for ln in fn.lines)
    relocs: list[tuple[int, int, int]] = []
    for ln in fn.lines:
        off = ln.vram - fn.vram
        m = _REF.search(ln.operands)
        if m:
            kind, sym, sign, num = m.groups()
            base = resolve(sym)
            if base is not None:
                addend = (int(num, 0) if num else 0) * (-1 if sign == "-" else 1)
                relocs.append((off, _KIND[kind], (base + addend) & 0xFFFFFFFF))
            continue
        if ln.mnemonic in _JUMPS:
            op = ln.operands.strip()
            if op.startswith(".L") or op.startswith("L"):
                addr = int(op.lstrip(".L"), 16)
            else:
                addr = resolve(op)
                if addr is None:
                    addr = ((ln.vram + 4) & 0xF0000000) | ((ln.word & 0x03FFFFFF) << 2)
            relocs.append((off, R_MIPS_26, addr))
    return RomTarget(fn.name, fn.vram, code, relocs, big_endian, gp)


def canonical_target(t: RomTarget, symbols: SymbolTable) -> FunctionObj:
    relocs = []
    for off, rtype, addr in t.relocs:
        name, residual = symbols.name_at(addr)
        if t.vram <= addr < t.vram + len(t.code):
            name, residual = t.name, addr - t.vram
        relocs.append(Reloc(off, rtype, name, encode(rtype, residual)))
    return FunctionObj(t.name, "mips", t.big_endian, t.code, relocs)


# -------------------------------------------------------------------------------- candidate

def _raw_relocs(elf: ELFFile, shndx: int, start: int, size: int, code: bytes) -> list[dict]:
    order = "little" if elf.little_endian else "big"
    raw = []
    for rsec in elf.iter_sections():
        if not isinstance(rsec, RelocationSection) or rsec["sh_info"] != shndx:
            continue
        symtab = elf.get_section(rsec["sh_link"])
        for rel in rsec.iter_relocations():
            off = rel["r_offset"]
            if not (start <= off < start + size):
                continue
            rtype = rel["r_info_type"]
            if rel.is_RELA():
                value = rel["r_addend"]
            else:
                delta, length, mask = _MASKS.get(("mips", rtype), (0, 4, 0xFFFFFFFF))
                at = off - start + delta
                value = int.from_bytes(code[at:at + length], order) & mask
            raw.append({"off": off - start, "type": rtype, "symidx": rel["r_info_sym"], "rela": rel.is_RELA(),
                        "value": value, "symtab": symtab})
    raw.sort(key=lambda r: r["off"])
    return raw


class _Linker:
    def __init__(self, obj: bytes, name: str, target: RomTarget, symbols: SymbolTable, read: ReadFn):
        self.elf = ELFFile(BytesIO(obj))
        self.name, self.target, self.symbols, self.read = name, target, symbols, read
        self.symtab = self.elf.get_section_by_name(".symtab")
        self.problems: list[str] = []
        self.local_map: dict[tuple[int, int], int] = {}  # (section index, offset) -> ROM address
        self.target_at = {off: (rtype, addr) for off, rtype, addr in target.relocs}
        self.checked: set[tuple[int, int]] = set()
        self.fn_shndx = None

    def problem(self, text: str) -> None:
        if text not in self.problems:
            self.problems.append(text)

    def function(self) -> tuple[int, int, int] | None:
        """(section index, offset, size) of the submitted function, also for period assemblers' size-0 symbols."""
        from ..elfobj import _function_symbols

        return _function_symbols(self.elf).get(self.name)

    def resolve_name(self, name: str) -> int | None:
        addr = self.symbols.resolve(name)
        if addr is None:
            self.problem(f"`{name}` is not a known symbol. Use the name the assembly shows (like D_80012340 or "
                         f"func_80001234), or give that address this name first with rename_symbol.")
        return addr

    def address(self, raw: list[dict], k: int, fn_shndx: int, fn_start: int) -> int | None:
        r = raw[k]
        if r["type"] == R_MIPS_GPREL16:
            residual = r["value"] - 0x10000 if r["value"] & 0x8000 else r["value"]
        else:
            residual = _section_target("mips", raw, k)
        if residual is None:
            self.problem(f"relocation type {r['type']} is not supported here")
            return None
        if r["type"] == R_MIPS_GPREL16 and not self.target.gp:
            self.problem("your code uses $gp-relative data, but this game doesn't: compile with -G0")
            return None
        sym = r["symtab"].get_symbol(r["symidx"])
        shndx = sym["st_shndx"]
        if shndx in ("SHN_UNDEF", "SHN_COMMON"):
            base = self.resolve_name(_canonical(sym.name, sym["st_info"]["bind"]))
            return None if base is None else (base + residual) & 0xFFFFFFFF
        off = residual if sym["st_info"]["type"] == "STT_SECTION" else sym["st_value"] + residual
        named = _symbol_at(self.symtab, shndx, off)
        if shndx == fn_shndx:
            if named and named[0] == self.name:
                return self.target.vram + named[1]
            if named is None:
                self.problem(f"a jump into .text+0x{off:X} that isn't part of {self.name}")
                return None
            base = self.resolve_name(named[0])
            return None if base is None else base + named[1]
        # data this submission defines itself: place it where the ROM keeps the data this instruction uses
        base = self.symbols.resolve(named[0]) if named else None
        if base is not None:
            addr = base + named[1]
        else:
            want = self.target_at.get(r["off"])
            if want is None or want[0] != r["type"]:
                label = f"{named[0]}+0x{named[1]:X}" if named else f"{self.elf.get_section(shndx).name}+0x{off:X}"
                self.problem(f"your code refers to {label} where the ROM's code has no address")
                return None
            addr = want[1]
            prev = self.local_map.setdefault((shndx, off), addr)
            if prev != addr:
                self.problem(f"{self.elf.get_section(shndx).name}+0x{off:X} is used for two different ROM addresses")
                return None
        self.verify_data(shndx, off, addr, named)
        return addr

    def verify_data(self, shndx: int, off: int, addr: int, named) -> None:
        """Data the candidate defines must be byte-identical to the ROM data it stands for."""
        sec = self.elf.get_section(shndx)
        if sec["sh_type"] == "SHT_NOBITS" or (shndx, off) in self.checked:
            return
        self.checked.add((shndx, off))
        data = sec.data()
        sym = next((s for s in self.symtab.iter_symbols() if s["st_shndx"] == shndx and s["st_value"] == off
                    and s["st_size"]), None) if named and named[1] == 0 else None
        if sym is not None:
            length = sym["st_size"]
        else:  # up to the next symbol (period assemblers don't record sizes)
            bounds = sorted({s["st_value"] for s in self.symtab.iter_symbols() if s["st_shndx"] == shndx}
                            | {o for (i, o) in self.local_map if i == shndx} | {len(data)})
            length = next((b for b in bounds if b > off), len(data)) - off
        length = max(0, min(length, len(data) - off))
        if not length:
            return
        mine = bytearray(data[off:off + length])
        for rsec in self.elf.iter_sections():  # relocated words inside the data, e.g. jump tables
            if not isinstance(rsec, RelocationSection) or rsec["sh_info"] != shndx:
                continue
            rsym = self.elf.get_section(rsec["sh_link"])
            for rel in rsec.iter_relocations():
                at = rel["r_offset"] - off
                if not (0 <= at <= length - 4) or rel["r_info_type"] != R_MIPS_32:
                    continue
                s = rsym.get_symbol(rel["r_info_sym"])
                value = int.from_bytes(mine[at:at + 4], self.target.order)
                if s["st_shndx"] in ("SHN_UNDEF", "SHN_COMMON"):
                    base = self.resolve_name(_canonical(s.name, s["st_info"]["bind"]))
                    dest = None if base is None else base + value
                else:
                    in_sec = value if s["st_info"]["type"] == "STT_SECTION" else s["st_value"] + value
                    hit = _symbol_at(self.symtab, s["st_shndx"], in_sec)
                    base = self.symbols.resolve(hit[0]) if hit and hit[0] != self.name else None
                    if hit and hit[0] == self.name:
                        dest = self.target.vram + hit[1]
                    elif base is not None:
                        dest = base + hit[1]
                    elif s["st_shndx"] != self.fn_shndx and (rom_word := self.read(addr + at, 4)) is not None:
                        # a pointer to more of the submission's own data (e.g. a table of string literals):
                        # it goes where the ROM's pointer points, and that data must match too
                        dest = int.from_bytes(rom_word, self.target.order)
                        if self.local_map.setdefault((s["st_shndx"], in_sec), dest) != dest:
                            dest = None
                        else:
                            self.verify_data(s["st_shndx"], in_sec, dest, hit)
                    else:
                        dest = None
                if dest is None:
                    self.problem(f"can't place a pointer inside your data at {sec.name}+0x{off + at:X}")
                    return
                mine[at:at + 4] = (dest & 0xFFFFFFFF).to_bytes(4, self.target.order)
        rom = self.read(addr, length)
        if rom is None:
            return  # the ROM has no bytes there (e.g. .bss in the ROM): nothing to compare
        if bytes(mine) != rom:
            where = self.symbols.label(addr)
            self.problem(f"your data for {where} differs from the ROM: yours {bytes(mine[:24]).hex(' ')}"
                         f"{'…' if length > 24 else ''}, ROM {rom[:24].hex(' ')}{'…' if length > 24 else ''}")

    def run(self) -> LinkResult:
        fsym = self.function()
        if fsym is None:
            return LinkResult(None, False, [f"the object has no function named `{self.name}`"])
        shndx, start, size = fsym
        self.fn_shndx = shndx
        code = self.elf.get_section(shndx).data()[start:start + size]
        raw = _raw_relocs(self.elf, shndx, start, size, code)
        if self.elf.little_endian == self.target.big_endian:
            return LinkResult(None, False, ["the compiler produced code in the wrong byte order for this game"])
        fmt = (">" if self.target.big_endian else "<") + f"{len(code) // 4}I"
        words = list(struct.unpack(fmt, code[:len(code) // 4 * 4]))
        relocs: list[Reloc] = []
        for k, r in enumerate(raw):
            addr = self.address(raw, k, shndx, start)
            if addr is None:
                sym = r["symtab"].get_symbol(r["symidx"])
                relocs.append(Reloc(r["off"], r["type"], f"?{sym.name or 'local'}", 0))
                continue
            i = r["off"] // 4
            if i < len(words):
                words[i] = _patch(words[i], r["type"], addr, self.target.gp)
            name, residual = self.symbols.name_at(addr)
            if self.target.vram <= addr < self.target.vram + len(self.target.code):
                name, residual = self.name, addr - self.target.vram
            relocs.append(Reloc(r["off"], r["type"], name, encode(r["type"], residual)))
        linked = struct.pack(fmt[0] + f"{len(words)}I", *words)
        exact = not self.problems and linked == self.target.code
        return LinkResult(FunctionObj(self.name, "mips", self.target.big_endian, code, relocs), exact, self.problems)


def link(obj: bytes, name: str, target: RomTarget, symbols: SymbolTable, read: ReadFn) -> LinkResult:
    """Link a compiled candidate at the target's address and compare it with the ROM."""
    return _Linker(obj, name, target, symbols, read).run()


def diff(target: FunctionObj, result: LinkResult) -> scoring.DiffResult:
    """Score with the usual instruction diff, but decide the match by the linked bytes."""
    d = scoring.diff(target, result.candidate)
    if result.exact:
        return scoring.DiffResult(0, d.max_score, d.rows, True)
    return scoring.DiffResult(max(1, d.score), d.max_score, d.rows, False)
