"""ELF object parsing: pull a single function's bytes + relocations out of a .o file.

Matching is done on *normalized* bytes: every byte range touched by a relocation is
masked out, and the relocations themselves are compared symbolically. That way a
call to `foo` matches whether `foo` lives at 0x80001234 in the ROM or is an
undefined extern in the candidate object.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from io import BytesIO

from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection
from elftools.elf.sections import SymbolTableSection

# e_machine -> arch key used throughout decompnet
MACHINES = {"EM_X86_64": "x86_64", "EM_386": "x86", "EM_MIPS": "mips", "EM_PPC": "ppc", "EM_ARM": "arm"}

# (arch, reloc type) -> (byte offset from r_offset, length, big-endian bitmask to clear)
# Anything not listed masks the 4 bytes at r_offset, which is right for most absolute/PC32 relocs.
_MASKS: dict[tuple[str, int], tuple[int, int, int]] = {
    ("x86_64", 1): (0, 8, 0xFFFFFFFFFFFFFFFF),  # R_X86_64_64
    ("mips", 4): (0, 4, 0x03FFFFFF),  # R_MIPS_26
    ("mips", 5): (0, 4, 0x0000FFFF),  # R_MIPS_HI16
    ("mips", 6): (0, 4, 0x0000FFFF),  # R_MIPS_LO16
    ("mips", 7): (0, 4, 0x0000FFFF),  # R_MIPS_GPREL16
    ("mips", 21): (0, 4, 0x0000FFFF),  # R_MIPS_GOT16 (IDO -KPIC)
    ("ppc", 4): (0, 2, 0xFFFF),  # R_PPC_ADDR16_LO
    ("ppc", 5): (0, 2, 0xFFFF),  # R_PPC_ADDR16_HI
    ("ppc", 6): (0, 2, 0xFFFF),  # R_PPC_ADDR16_HA
    ("ppc", 10): (0, 4, 0x03FFFFFC),  # R_PPC_REL24
    ("ppc", 109): (0, 4, 0x001FFFFF),  # R_PPC_EMB_SDA21
    ("arm", 28): (0, 4, 0x00FFFFFF),  # R_ARM_CALL
    ("arm", 29): (0, 4, 0x00FFFFFF),  # R_ARM_JUMP24
    ("arm", 43): (0, 4, 0x000F0FFF),  # R_ARM_MOVW_ABS_NC
    ("arm", 44): (0, 4, 0x000F0FFF),  # R_ARM_MOVT_ABS
}


@dataclass(frozen=True)
class Reloc:
    offset: int  # relative to function start
    type: int
    symbol: str
    addend: int  # only meaningful (and only compared) for non-section symbols

    def key(self) -> tuple:
        return (self.offset, self.type, self.symbol, self.addend)

    def to_json(self) -> list:
        return [self.offset, self.type, self.symbol, self.addend]

    @staticmethod
    def from_json(v: list) -> Reloc:
        return Reloc(*v)


@dataclass
class FunctionObj:
    name: str
    arch: str
    big_endian: bool
    code: bytes
    relocs: list[Reloc] = field(default_factory=list)

    def normalized(self) -> bytes:
        return mask_relocs(self.code, self.relocs, self.arch, self.big_endian)

    def fingerprint(self) -> str:
        """Hash of masked bytes + symbolic relocs. Equal fingerprints == matching function."""
        h = hashlib.sha256(self.normalized())
        for r in sorted(self.relocs, key=Reloc.key):
            h.update(repr(r.key()).encode())
        return h.hexdigest()


def mask_relocs(code: bytes, relocs: list[Reloc], arch: str, big_endian: bool) -> bytes:
    buf = bytearray(code)
    for r in relocs:
        delta, length, mask = _MASKS.get((arch, r.type), (0, 4, 0xFFFFFFFF))
        start = r.offset + delta
        if start < 0 or start + length > len(buf):
            continue
        order = "big" if big_endian else "little"
        val = int.from_bytes(buf[start : start + length], order)
        buf[start : start + length] = (val & ~mask & ((1 << (8 * length)) - 1)).to_bytes(length, order)
    return bytes(buf)


def _arch_of(elf: ELFFile) -> tuple[str, bool]:
    machine = elf["e_machine"]
    if machine not in MACHINES:
        raise ValueError(f"unsupported ELF machine {machine}")
    return MACHINES[machine], not elf.little_endian


_LOCAL_STATIC = re.compile(r"^(.+)\.\d+$")


def _canonical(name: str, bind: str) -> str:
    """GCC names function-local statics `name.N`, where N counts statics earlier in the file, so the same
    variable gets a different N when its function is compiled on its own. Compare them by base name."""
    m = _LOCAL_STATIC.match(name) if bind == "STB_LOCAL" else None
    return m.group(1) if m else name


def _symbol_name(elf: ELFFile, symtab: SymbolTableSection, idx: int) -> tuple[str, bool]:
    """Return (name, is_section_symbol). Section symbols get the section's name, with
    `.text.foo` (from -ffunction-sections) collapsed to `foo` so static callees line up."""
    sym = symtab.get_symbol(idx)
    if sym["st_info"]["type"] == "STT_SECTION":
        sec = elf.get_section(sym["st_shndx"]).name
        if sec.startswith(".text."):
            return sec[len(".text.") :], True
        # .rodata.str1.1 / .rodata.cst4 etc. are all "rodata" as far as matching cares
        return ("." + sec.split(".")[1]) if sec.count(".") > 1 else sec, True
    return _canonical(sym.name, sym["st_info"]["bind"]), False


def _sext16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def _section_target(arch: str, raw: list[dict], k: int) -> int | None:
    """Offset inside the referenced section that a section-relative relocation points at."""
    r = raw[k]
    if r["rela"]:
        # absolute relocations only; PC-relative addends are biased by the instruction layout
        return r["value"] if (arch, r["type"]) in {("x86_64", 1), ("x86_64", 10), ("x86_64", 11), ("mips", 2),
                                                    ("mips", 4), ("mips", 5), ("mips", 6)} else None
    if arch != "mips":
        return r["value"] if r["type"] == 2 else None
    t, v = r["type"], r["value"]
    if t == 4:  # R_MIPS_26: word index
        return v << 2
    if t == 2:  # R_MIPS_32
        return v
    same = [x for x in raw if x["symidx"] == r["symidx"] and x["symtab"] is r["symtab"]]
    if t == 5:  # HI16: combine with the next LO16 of the same symbol
        lo = next((x for x in same if x["off"] > r["off"] and x["type"] == 6), None)
        return (v << 16) + _sext16(lo["value"]) if lo else v << 16
    if t == 6:  # LO16: combine with the previous HI16 of the same symbol
        hi = next((x for x in reversed(same) if x["off"] < r["off"] and x["type"] == 5), None)
        return ((hi["value"] << 16) if hi else 0) + _sext16(v)
    return None


def _symbol_at(symtab: SymbolTableSection, shndx: int, offset: int) -> tuple[str, int] | None:
    """(name, offset into it) of the named symbol covering `offset` in a section, preferring exact starts,
    global symbols, and functions/objects over plain labels."""
    best = None
    for s in symtab.iter_symbols():
        if s["st_shndx"] != shndx or not s.name or s.name[0] in ".$" or s["st_info"]["type"] in ("STT_SECTION", "STT_FILE"):
            continue
        if s.name.endswith(".") or s.name.startswith("__gnu_compiled"):  # compiler markers, e.g. gcc2_compiled.
            continue
        lo, size = s["st_value"], s["st_size"]
        if not (lo == offset or lo <= offset < lo + size):
            continue
        kind = {"STT_FUNC": 2, "STT_OBJECT": 1}.get(s["st_info"]["type"], 0)
        rank = (lo == offset, kind, s["st_info"]["bind"] == "STB_GLOBAL")
        if best is None or rank > best[0]:
            best = (rank, _canonical(s.name, s["st_info"]["bind"]), offset - lo)
    return (best[1], best[2]) if best else None


def _encode(arch: str, r: dict, residual: int) -> int:
    """The relocation field value an assembler writes for a reference to `symbol + residual`."""
    if r["rela"] or arch != "mips":
        return residual
    t = r["type"]
    if t == 4:
        return (residual >> 2) & 0x03FFFFFF
    if t == 5:
        return ((residual + 0x8000) >> 16) & 0xFFFF
    if t == 6:
        return residual & 0xFFFF
    return residual & 0xFFFFFFFF


def list_functions(data: bytes) -> list[str]:
    elf = ELFFile(BytesIO(data))
    symtab = elf.get_section_by_name(".symtab")
    if symtab is None:
        return []
    return [
        s.name
        for s in symtab.iter_symbols()
        if s["st_info"]["type"] == "STT_FUNC" and s["st_shndx"] != "SHN_UNDEF" and s["st_size"] > 0
    ]


def extract_function(data: bytes, name: str) -> FunctionObj | None:
    elf = ELFFile(BytesIO(data))
    arch, big_endian = _arch_of(elf)
    symtab = elf.get_section_by_name(".symtab")
    if symtab is None:
        return None
    syms = [
        s
        for s in symtab.iter_symbols()
        if s.name == name and s["st_info"]["type"] == "STT_FUNC" and s["st_shndx"] != "SHN_UNDEF"
    ]
    if not syms:
        return None
    sym = syms[0]
    shndx = sym["st_shndx"]
    section = elf.get_section(shndx)
    start = sym["st_value"]
    size = sym["st_size"]
    code = section.data()[start : start + size]

    raw: list[dict] = []
    for rsec in elf.iter_sections():
        if not isinstance(rsec, RelocationSection) or rsec["sh_info"] != shndx:
            continue
        rsymtab = elf.get_section(rsec["sh_link"])
        for rel in rsec.iter_relocations():
            off = rel["r_offset"]
            if not (start <= off < start + size):
                continue
            rtype = rel["r_info_type"]
            if rel.is_RELA():
                value = rel["r_addend"]
            else:
                # REL (MIPS): the addend lives in the masked instruction field itself
                delta, length, mask = _MASKS.get((arch, rtype), (0, 4, 0xFFFFFFFF))
                field_at = off - start + delta
                value = int.from_bytes(code[field_at : field_at + length], "big" if big_endian else "little") & mask
            raw.append({"off": off - start, "type": rtype, "symidx": rel["r_info_sym"], "rela": rel.is_RELA(),
                        "value": value, "symtab": rsymtab})
    raw.sort(key=lambda r: r["off"])

    relocs: list[Reloc] = []
    for k, r in enumerate(raw):
        sym_name, is_section = _symbol_name(elf, r["symtab"], r["symidx"])
        addend = r["value"]
        if is_section:
            # The compiler referenced a same-file function or static by section+offset. Resolve it to the
            # named symbol at that offset, encoded the way a reference to that symbol by name would be,
            # so a function compiled on its own (which references neighbours by name) can still match.
            addend = 0
            sec_idx = r["symtab"].get_symbol(r["symidx"])["st_shndx"]
            target = _section_target(arch, raw, k)
            named = _symbol_at(symtab, sec_idx, target) if target is not None else None
            if named:
                sym_name, residual = named
                addend = _encode(arch, r, residual)
        relocs.append(Reloc(r["off"], r["type"], sym_name, addend))
    relocs.sort(key=lambda r: r.offset)
    return FunctionObj(name=name, arch=arch, big_endian=big_endian, code=code, relocs=relocs)
